import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from app.agent.schemas import AgentTurnRequest, AgentTurnResult, Intent
from app.db.agent_models import AgentTurn
from app.db.models import ConversationSession, WorkflowRun
from app.db.operation_models import EmergencyIncident, Handoff, ToolCall
from app.main import create_app
from app.realtime.manager import RealtimeSessionManager
from app.realtime.schemas import (
    AgentResponse,
    Binding,
    Cancel,
    Code,
    Error,
    Ping,
    ServerPayload,
    TurnCancelled,
    TurnCompleted,
)
from app.realtime.session import RealtimeSession
from app.routing.schemas import RoutingDecision, RoutingRequest
from app.tools.schemas import ToolResult
from app.workflows.schemas import ActionRequest
from fastapi import FastAPI
from sqlalchemy import delete, select

from tests.integration.test_agent import TextHarness
from tests.integration.test_agent import text_harness as text_harness  # noqa: F401
from tests.integration.test_tools import harness as harness  # noqa: F401
from tests.support.realtime import Socket, live_relay_tasks

pytestmark = pytest.mark.integration


@pytest.fixture
async def realtime_app(text_harness: TextHarness) -> AsyncIterator[FastAPI]:
    t = text_harness
    app = create_app()

    async def lookup(session_id: UUID) -> Binding:
        state, _ = await t.store.load(session_id)
        return Binding(revision=state.revision, turn_number=state.turn_number)

    manager = RealtimeSessionManager(lookup, t.agent)
    app.state.realtime_manager = manager
    try:
        yield app
    finally:
        await manager.shutdown()
        assert manager.active_count == 0
        assert not live_relay_tasks()


async def test_flagship_and_durable_replay(
    realtime_app: FastAPI, text_harness: TextHarness
) -> None:
    t = text_harness
    socket = Socket(realtime_app, t.source.session_id)
    await socket.start()

    async def say(text: str, key: str | None = None) -> tuple[AgentTurnRequest, AgentResponse]:
        if key:
            t.route(key)
        request = await t.request(text)
        events = await socket.turn(request)
        assert [e.event_type for e in events] == [
            "SESSION_STATE_CHANGED",
            "TURN_STARTED",
            "AGENT_RESPONSE",
            "TURN_COMPLETED",
            "SESSION_STATE_CHANGED",
        ]
        assert all(e.turn_id == request.turn_id for e in events[1:4])
        response = events[2].payload
        assert isinstance(response, AgentResponse)
        return request, response

    try:
        _, first = await say("My AC isn't cooling.", "non_emergency_maintenance")
        assert first.response_intent == Intent.ASK
        _, proposal = await say("Answer: It blows warm air.")
        assert proposal.response_intent == Intent.CONFIRM
        await say("Actually water is pouring out of it.", "emergency_maintenance")
        await say("Answer: Living room AC")
        await say("Answer: No known electrical contact")
        action, created = await say("Answer: Continuous leak")
        assert created.response_intent == Intent.SUCCESS
        t.route("emergency_maintenance", "electrical_hazard")
        _, updated = await say("Wait, the water is getting close to an electrical outlet.")
        assert updated.response_intent == Intent.SUCCESS
        await say("I want to speak with someone.", "human_handoff")
        _, handed = await say("Yes, please.")
        assert (
            handed.response_intent == Intent.SUCCESS and "No live transfer" in handed.response_text
        )
        assert await t.source.count(EmergencyIncident) == await t.source.count(Handoff) == 1
        async with t.source.database.session() as session:
            incident = (
                await session.scalars(
                    select(EmergencyIncident).where(
                        EmergencyIncident.session_id == t.source.session_id
                    )
                )
            ).one()
            handoff = (
                await session.scalars(
                    select(Handoff).where(Handoff.session_id == t.source.session_id)
                )
            ).one()
            assert incident.revision == 2 and "electrical outlet" in incident.hazard_detail
            assert str(incident.id) in created.response_text and handoff.incident_id == incident.id
        replay = (await socket.turn(action))[2].payload
        assert isinstance(replay, AgentResponse) and replay.replayed
        assert replay.response_text == created.response_text
        conflict = await socket.turn(action.model_copy(update={"utterance": "changed"}))
        assert [e.event_type for e in conflict] == [
            "SESSION_STATE_CHANGED",
            "TURN_STARTED",
            "ERROR",
            "SESSION_STATE_CHANGED",
        ]
        assert (
            isinstance(conflict[2].payload, Error)
            and conflict[2].payload.code == Code.TURN_CONFLICT
        )
    finally:
        await socket.close()
    fresh = Socket(realtime_app, t.source.session_id)
    await fresh.start()
    try:
        replay = (await fresh.turn(action))[2].payload
        assert isinstance(replay, AgentResponse) and replay.replayed
        assert replay.response_text == created.response_text
        assert fresh.events[0].sequence == 1
        assert await t.source.count(EmergencyIncident) == await t.source.count(Handoff) == 1
    finally:
        await fresh.close()


async def test_bind_duplicate_reconnect_and_missing(
    realtime_app: FastAPI, text_harness: TextHarness
) -> None:
    first = Socket(realtime_app, text_harness.source.session_id)
    await first.start()
    second = Socket(realtime_app, first.session_id)
    try:
        await second.connect()
        error = (await second.receive()).payload
        assert isinstance(error, Error) and error.code == Code.DUPLICATE_CONNECTION
        await second.join()
        await first.send(Ping())
        assert (await first.receive()).event_type == "PONG"
        assert realtime_app.state.realtime_manager.active_count == 1
    finally:
        await first.close()
    assert realtime_app.state.realtime_manager.active_count == 0
    fresh = Socket(realtime_app, first.session_id)
    await fresh.start()
    await fresh.close()
    missing = Socket(realtime_app, uuid4())
    await missing.connect()
    error = (await missing.receive()).payload
    assert isinstance(error, Error) and error.code == Code.SESSION_NOT_FOUND
    await missing.join()


class Pause:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.resume = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.resume.wait()


@pytest.mark.parametrize("stage", ["before", "routing", "committed"])
@pytest.mark.parametrize("disconnect", [False, True])
async def test_cancel_disconnect_and_recovery(
    realtime_app: FastAPI,
    text_harness: TextHarness,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    disconnect: bool,
) -> None:
    t, pause = text_harness, Pause()
    if stage == "committed":
        request = await t.emergency_ready()
        execute = t.source.executor.execute

        async def held_execute(session_id: UUID, action: ActionRequest) -> ToolResult:
            result = await execute(session_id, action)
            await pause.wait()
            return result

        monkeypatch.setattr(t.source.executor, "execute", held_execute)
    else:
        t.route("non_emergency_maintenance")
        request = await t.request("My AC isn't cooling.")
        if stage == "before":
            process = t.agent.process

            async def held_process(value: AgentTurnRequest) -> AgentTurnResult:
                await pause.wait()
                return await process(value)

            monkeypatch.setattr(t.agent, "process", held_process)
        else:
            # Pauses inside the non-side-effect routing stage, after durable turn start.
            route = t.agent.router.route

            async def held_route(value: RoutingRequest) -> RoutingDecision:
                await pause.wait()
                return await route(value)

            monkeypatch.setattr(t.agent.router, "route", held_route)
    socket = Socket(realtime_app, t.source.session_id)
    await socket.start()
    await socket.submit(request)
    async with asyncio.timeout(5):
        await pause.entered.wait()
    assert (await socket.receive()).event_type == "SESSION_STATE_CHANGED"
    assert (await socket.receive()).event_type == "TURN_STARTED"
    if stage == "committed":
        assert await t.source.count(EmergencyIncident) == 1
    if disconnect:
        await socket.close()
    else:
        await socket.submit(request)
        error = (await socket.receive()).payload
        assert isinstance(error, Error) and error.code == Code.SESSION_BUSY
        await socket.send(Cancel(turn_id=uuid4()))
        error = (await socket.receive()).payload
        assert isinstance(error, Error) and error.code == Code.TURN_NOT_ACTIVE
        await socket.send(Cancel(turn_id=request.turn_id))
        cancelled = await socket.until_idle()
        assert isinstance(cancelled[0].payload, TurnCancelled)
        assert cancelled[0].payload.reconciliation_required
        await socket.close()
    assert realtime_app.state.realtime_manager.active_count == 0
    assert not live_relay_tasks()
    assert await t.source.count(EmergencyIncident) == (1 if stage == "committed" else 0)
    monkeypatch.undo()
    fresh = Socket(realtime_app, t.source.session_id)
    await fresh.start()
    try:
        recovered = (await fresh.turn(request))[2].payload
        assert isinstance(recovered, AgentResponse)
        assert recovered.response_intent == (Intent.SUCCESS if stage == "committed" else Intent.ASK)
        replay = (await fresh.turn(request))[2].payload
        assert isinstance(replay, AgentResponse) and replay.replayed
        assert replay.response_text == recovered.response_text
        assert await t.source.count(EmergencyIncident) == (1 if stage == "committed" else 0)
    finally:
        await fresh.close()


async def test_different_durable_sessions_run_independently(
    realtime_app: FastAPI,
    text_harness: TextHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t, pause = text_harness, Pause()
    other = await t.store.create_demo("Alex")
    original = t.agent.process

    async def held(request: AgentTurnRequest) -> AgentTurnResult:
        if request.session_id == t.source.session_id:
            await pause.wait()
        return await original(request)

    monkeypatch.setattr(t.agent, "process", held)
    first, second = (
        Socket(realtime_app, t.source.session_id),
        Socket(realtime_app, other.session_id),
    )
    await first.start()
    await second.start()
    try:
        await first.submit(await t.request("First uncertain question"))
        async with asyncio.timeout(5):
            await pause.entered.wait()
        state, _ = await t.store.load(other.session_id)
        result = await second.turn(
            AgentTurnRequest(
                session_id=other.session_id,
                turn_id=uuid4(),
                turn_number=1,
                expected_revision=state.revision,
                utterance="Other uncertain question",
            )
        )
        assert isinstance(result[2].payload, AgentResponse)
        assert not pause.resume.is_set()
        pause.resume.set()
        await first.until_idle()
    finally:
        await first.close()
        await second.close()
        async with t.source.database.session() as session:
            for model in (AgentTurn, ToolCall, WorkflowRun):
                await session.execute(delete(model).where(model.session_id == other.session_id))
            await session.execute(
                delete(ConversationSession).where(ConversationSession.id == other.session_id)
            )


async def test_cancel_after_result_is_controlled(
    realtime_app: FastAPI,
    text_harness: TextHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t, pause = text_harness, Pause()
    request = await t.emergency_ready()
    original = RealtimeSession.emit

    async def hold_completion(
        self: RealtimeSession, payload: ServerPayload, turn_id: UUID | None = None
    ) -> None:
        if isinstance(payload, TurnCompleted):
            await pause.wait()
        await original(self, payload, turn_id)

    monkeypatch.setattr(RealtimeSession, "emit", hold_completion)
    socket = Socket(realtime_app, t.source.session_id)
    await socket.start()
    try:
        await socket.submit(request)
        async with asyncio.timeout(5):
            await pause.entered.wait()
        events = [await socket.receive() for _ in range(3)]
        assert isinstance(events[-1].payload, AgentResponse)
        await socket.send(Cancel(turn_id=request.turn_id))
        error = (await socket.receive()).payload
        assert isinstance(error, Error) and error.code == Code.TURN_ALREADY_COMPLETED
        pause.resume.set()
        await socket.until_idle()
        assert await t.source.count(EmergencyIncident) == 1
    finally:
        await socket.close()
