import ast
import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.agent.errors import TurnError
from app.agent.schemas import AgentTurnRequest, AgentTurnResult
from app.main import create_app
from app.realtime import websocket
from app.realtime.errors import Backpressure, RealtimeError
from app.realtime.manager import RealtimeSessionManager
from app.realtime.schemas import Binding, Cancel, Code, End, Error, Ping, Start, TextTurn
from app.realtime.session import RealtimeSession
from fastapi import FastAPI

from tests.support.realtime import Socket, live_relay_tasks


class Processor:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.released = asyncio.Event()
        self.error: Exception = TurnError("STALE_TURN")

    async def process(self, request: AgentTurnRequest) -> AgentTurnResult:
        self.entered.set()
        await self.released.wait()
        raise self.error


async def lookup(session_id: UUID) -> Binding:
    return Binding(revision=1, turn_number=0)


def configured() -> tuple[FastAPI, RealtimeSessionManager, Processor]:
    app, processor = create_app(), Processor()
    manager = RealtimeSessionManager(lookup, processor)
    app.state.realtime_manager = manager
    return app, manager, processor


def text_turn() -> TextTurn:
    return TextTurn(turn_id=uuid4(), turn_number=1, expected_revision=1, utterance="Something")


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "invalid_json",
        "blank",
        "missing_turn",
        "property_id",
        "resident_id",
        "unit_id",
        "routing_decision",
        "workflow_decision",
        "action_request",
        "tool_result",
        "wrong_correlation",
        "wrong_session",
        "binary",
        "oversize",
    ],
)
async def test_protocol_rejects_invalid_without_losing_heartbeat(mutation: str) -> None:
    app, manager, _ = configured()
    socket = Socket(app, uuid4())
    await socket.start()
    payload = text_turn()
    data = {
        "session_id": str(socket.session_id),
        "sequence": 1,
        "turn_id": str(payload.turn_id),
        "event_type": payload.kind,
        "payload": payload.model_dump(mode="json"),
    }
    if mutation == "unknown":
        data["event_type"] = "CLIENT_AUDIO"
    elif mutation == "blank":
        data["payload"]["utterance"] = " "  # type: ignore[index]
    elif mutation == "missing_turn":
        del data["payload"]["turn_id"]  # type: ignore[attr-defined]
    elif mutation == "wrong_correlation":
        data["turn_id"] = str(uuid4())
    elif mutation == "wrong_session":
        data["session_id"] = str(uuid4())
    elif mutation not in {"invalid_json", "binary", "oversize"}:
        data["payload"][mutation] = str(uuid4())  # type: ignore[index]
    try:
        if mutation == "binary":
            await socket.incoming.put({"type": "websocket.receive", "bytes": b"audio"})
        else:
            await socket.raw(
                "{"
                if mutation == "invalid_json"
                else ("x" * 32769 if mutation == "oversize" else json.dumps(data))
            )
        error = (await socket.receive()).payload
        assert isinstance(error, Error)
        assert error.code == (
            Code.UNSUPPORTED_EVENT if mutation == "unknown" else Code.INVALID_EVENT
        )
        assert error.message == "The realtime request could not be completed."
        await socket.send(Ping())
        assert (await socket.receive()).event_type == "PONG"
    finally:
        await socket.close()
    assert manager.active_count == 0 and not live_relay_tasks()


async def test_start_end_and_cancel_inactive() -> None:
    app, manager, _ = configured()
    socket = Socket(app, uuid4())
    await socket.connect()
    await socket.send(text_turn())
    error = (await socket.receive()).payload
    assert isinstance(error, Error) and error.code == Code.SESSION_NOT_STARTED
    await socket.send(Start())
    await socket.receive()
    await socket.receive()
    await socket.send(Start())
    error = (await socket.receive()).payload
    assert isinstance(error, Error) and error.code == Code.SESSION_ALREADY_STARTED
    await socket.send(Cancel(turn_id=uuid4()))
    error = (await socket.receive()).payload
    assert isinstance(error, Error) and error.code == Code.TURN_NOT_ACTIVE
    await socket.send(End())
    assert [(await socket.receive()).event_type for _ in range(3)] == [
        "SESSION_STATE_CHANGED",
        "SESSION_STATE_CHANGED",
        "SESSION_ENDED",
    ]
    assert (await socket.message())["code"] == 1000
    await socket.join()
    assert manager.active_count == 0 and not live_relay_tasks()


@pytest.mark.parametrize("unexpected", [False, True])
async def test_orchestrator_errors_safe_and_session_recovers(unexpected: bool) -> None:
    app, manager, processor = configured()
    if unexpected:
        processor.error = RuntimeError("SQL password=SECRET C:/private/file.py")
    processor.released.set()
    socket = Socket(app, uuid4())
    await socket.start()
    try:
        await socket.send(text_turn())
        events = await socket.until_idle()
        assert [e.event_type for e in events] == [
            "SESSION_STATE_CHANGED",
            "TURN_STARTED",
            "ERROR",
            "SESSION_STATE_CHANGED",
        ]
        assert isinstance(events[2].payload, Error)
        assert events[2].payload.code == (Code.INTERNAL_ERROR if unexpected else Code.TURN_CONFLICT)
        assert "SECRET" not in "".join(e.model_dump_json() for e in events)
        await socket.send(Ping())
        assert (await socket.receive()).event_type == "PONG"
    finally:
        await socket.close()
    assert manager.active_count == 0


async def test_production_disabled() -> None:
    app, manager, _ = configured()
    app.state.settings.app_env = "production"
    socket = Socket(app, uuid4())
    assert (await socket.connect())["code"] == 1008
    await socket.join()
    assert manager.active_count == 0


async def test_queue_saturation_is_explicit_and_preserves_queued_event() -> None:
    session = RealtimeSession(
        uuid4(), await lookup(uuid4()), Processor(), queue_capacity=1, queue_timeout=0.01
    )
    await session.error(Code.INVALID_EVENT)
    with pytest.raises(Backpressure):
        await session.error(Code.INTERNAL_ERROR)
    assert session.stopped.is_set() and session.reason.reason == "BACKPRESSURE"
    assert session.outbound.items.qsize() == 1 and session.outbound.pressure_count == 1
    event = session.outbound.items.get_nowait()
    assert isinstance(event.payload, Error) and event.payload.code == Code.INVALID_EVENT
    session.outbound.items.task_done()
    await session.end()


async def test_stalled_transport_closes_and_cleans(monkeypatch: pytest.MonkeyPatch) -> None:
    app, manager, _ = configured()
    monkeypatch.setattr(websocket, "SEND_TIMEOUT", 0.02)
    socket = Socket(app, uuid4())
    socket.block_send = asyncio.Event()
    await socket.connect()
    await socket.send(Start())
    await socket.send_entered.wait()
    message = await socket.message()
    assert message["type"] == "websocket.close" and message["code"] == 1013
    assert message["reason"] == "BACKPRESSURE_REPLAY_REQUIRED"
    await socket.join()
    assert manager.active_count == 0 and not live_relay_tasks()


async def test_shutdown_cancels_and_awaits_owned_tasks() -> None:
    app, manager, processor = configured()
    socket = Socket(app, uuid4())
    await socket.start()
    await socket.send(text_turn())
    async with asyncio.timeout(5):
        await processor.entered.wait()
    await manager.shutdown()
    await socket.join()
    assert manager.active_count == 0 and not live_relay_tasks()
    with pytest.raises(RealtimeError) as error:
        await manager.bind(uuid4())
    assert error.value.code == Code.SESSION_UNAVAILABLE


async def test_unrecoverable_session_error_ends_connection() -> None:
    app, manager, processor = configured()
    processor.error = TurnError("SESSION_INACTIVE")
    processor.released.set()
    socket = Socket(app, uuid4())
    await socket.start()
    await socket.send(text_turn())
    events = [await socket.receive() for _ in range(6)]
    assert isinstance(events[2].payload, Error)
    assert events[2].payload.code == Code.SESSION_UNAVAILABLE
    assert events[-1].event_type == "SESSION_ENDED"
    await socket.join()
    assert manager.active_count == 0 and not live_relay_tasks()


async def test_queue_overflow_while_reporting_turn_error_is_owned() -> None:
    app, manager, processor = configured()
    manager.queue_capacity = 1
    manager.queue_timeout = 0.01
    processor.released.set()
    session = await manager.bind(uuid4())
    await session.error(Code.INVALID_EVENT)
    request = AgentTurnRequest(
        session_id=session.conversation_session_id, **text_turn().model_dump(exclude={"kind"})
    )
    session.active_task = asyncio.create_task(session._run(request), name="relay-test-turn")
    await session.active_task
    assert session.active_task is None and session.stopped.is_set()
    assert session.reason.reason == "BACKPRESSURE"
    session.outbound.discard_on_close()
    await manager.release(session)
    assert manager.active_count == 0 and not live_relay_tasks()


async def test_manager_terminate_connection() -> None:
    app, manager, _ = configured()
    socket = Socket(app, uuid4())
    await socket.start()
    assert await manager.get(socket.session_id) is not None
    await manager.terminate(socket.session_id)
    await socket.join()
    assert await manager.get(socket.session_id) is None
    assert not live_relay_tasks()


async def test_handler_cancellation_awaits_turn_cleanup_without_second_cancel() -> None:
    app, manager, processor = configured()
    cleaning, allow_cleanup = asyncio.Event(), asyncio.Event()
    clean = False

    class CleaningProcessor:
        async def process(self, request: AgentTurnRequest) -> AgentTurnResult:
            nonlocal clean
            try:
                return await processor.process(request)
            finally:
                cleaning.set()
                await allow_cleanup.wait()
                clean = True

    manager.processor = CleaningProcessor()
    socket = Socket(app, uuid4())
    await socket.start()
    value = text_turn()
    await socket.send(value)
    async with asyncio.timeout(5):
        await processor.entered.wait()
        await socket.send(Cancel(turn_id=value.turn_id))
        await cleaning.wait()
    assert socket.task
    socket.task.cancel()
    allow_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await socket.join()
    assert clean and manager.active_count == 0 and not live_relay_tasks()


async def test_send_failure_releases_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    app, manager, _ = configured()
    socket = Socket(app, uuid4())
    original = socket._send

    async def failing(message: dict[str, object]) -> None:
        if message["type"] == "websocket.send":
            raise OSError("Secret transport details")
        await original(message)

    monkeypatch.setattr(socket, "_send", failing)
    await socket.connect()
    await socket.send(Start())
    await socket.join()
    assert manager.active_count == 0 and not live_relay_tasks()


async def test_duplicate_during_binding_and_failed_binding_releases_reservation() -> None:
    pause = Processor()

    async def slow_lookup(session_id: UUID) -> Binding:
        pause.entered.set()
        await pause.released.wait()
        raise TurnError("SESSION_NOT_FOUND")

    manager = RealtimeSessionManager(slow_lookup, pause)
    session_id = uuid4()
    task = asyncio.create_task(manager.bind(session_id))
    try:
        await pause.entered.wait()
        with pytest.raises(RealtimeError) as error:
            await manager.bind(session_id)
        assert error.value.code == Code.DUPLICATE_CONNECTION
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    manager.lookup = lookup
    session = await manager.bind(session_id)
    await manager.release(session)
    assert manager.active_count == 0


def test_realtime_boundary_has_no_domain_execution_imports() -> None:
    root = Path(websocket.__file__).parent
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    ("app.db", "app.routing", "app.tools", "app.workflows")
                )
    from app.realtime.schemas import ServerEvent

    schema = json.dumps(ServerEvent.model_json_schema())
    assert "ActionRequest" not in schema and "ToolResult" not in schema
    assert "WorkflowDecision" not in schema and "RoutingDecision" not in schema
