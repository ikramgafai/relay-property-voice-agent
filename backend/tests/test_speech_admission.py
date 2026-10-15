"""Relay-side final-admission grace: provider finals stay provisional, then admit or coalesce."""

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4, uuid5

import pytest
from app.agent.schemas import AgentTurnRequest, AgentTurnResult
from app.core.config import Settings
from app.providers.stt.schemas import SpeechContext
from app.realtime import speech
from app.realtime.schemas import Binding, ClientEvent, Code, Error, ServerEvent, Start
from app.realtime.session import RealtimeSession
from app.realtime.speech import SpeechPipeline
from pydantic import ValidationError

from tests.support.speech import FakeMedia, FakeSTT
from tests.test_realtime_presentation import result

GRACE_MS = 250


class Recorder:
    """Stands in for TurnOrchestrator: records every request it is asked to process."""

    def __init__(self, block: asyncio.Event | None = None) -> None:
        self.requests: list[AgentTurnRequest] = []
        self.block = block

    async def process(self, request: AgentTurnRequest) -> AgentTurnResult:
        self.requests.append(request)
        if self.block is not None:
            await self.block.wait()
        return result(request)


async def until(predicate: Callable[[], bool], limit: float = 3.0) -> None:
    for _ in range(int(limit / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


async def attached(
    grace_ms: int = GRACE_MS, recorder: Recorder | None = None
) -> tuple[RealtimeSession, SpeechPipeline, FakeSTT, FakeMedia, Recorder]:
    recorder = recorder or Recorder()
    session_id = uuid4()
    session = RealtimeSession(
        session_id, Binding(revision=1, turn_number=0), recorder, queue_capacity=1024
    )
    await session.handle(
        ClientEvent(session_id=session_id, sequence=1, event_type="SESSION_START", payload=Start())
    )
    media, stt = FakeMedia(), FakeSTT()
    pipeline = SpeechPipeline(session, media, stt, SpeechContext(), admission_grace_ms=grace_ms)
    session.speech = pipeline
    pipeline.start()
    await stt.opened.wait()
    return session, pipeline, stt, media, recorder


async def requests_reach(recorder: Recorder, count: int) -> None:
    await until(lambda: len(recorder.requests) == count)


def events(session: RealtimeSession) -> list[ServerEvent]:
    found = []
    while not session.outbound.items.empty():
        found.append(session.outbound.items.get_nowait())
        session.outbound.items.task_done()
    return found


def relay_input_tasks() -> set[str]:
    return {
        t.get_name()
        for t in asyncio.all_tasks()
        if t.get_name().startswith(("relay-speech", "relay-stt", "relay-audio")) and not t.done()
    }


def speech_id(stt: FakeSTT, order: int) -> UUID:
    return uuid5(stt.stream_id, str(order))


async def processed(session: RealtimeSession) -> None:
    await until(lambda: session.active_task is None)


# --- 1-2: single final ---------------------------------------------------------------------


async def test_single_final_admitted_once_after_grace_and_never_before() -> None:
    session, pipeline, stt, _, recorder = await attached()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await stt.emit("FINAL", "My AC isn't cooling properly.")
    await until(lambda: pipeline.provisional is not None)
    # Provisional: RealtimeSession, routing, workflows and tools have not run.
    assert recorder.requests == [] and session.active_task is None
    assert "STT_FINAL" not in [e.event_type for e in events(session)]
    await until(lambda: len(recorder.requests) == 1)
    assert loop.time() - started >= GRACE_MS / 1000 * 0.9
    [request] = recorder.requests
    assert request.utterance == "My AC isn't cooling properly."  # unmodified
    assert request.turn_id == uuid5(session.conversation_session_id, str(speech_id(stt, 0)))
    await processed(session)
    await asyncio.sleep(GRACE_MS / 1000 * 1.5)
    assert len(recorder.requests) == 1 and pipeline.provisional is None
    await session.end()


# --- 3-5, 7: continuation and coalescing -----------------------------------------------------


@pytest.mark.parametrize("signal", ["PARTIAL", "SPEECH_STARTED"])
async def test_continuation_inside_grace_coalesces_into_one_turn(signal: str) -> None:
    session, pipeline, stt, _, recorder = await attached()
    await stt.emit("FINAL", "Wait, the water is getting close.", order=0)
    await until(lambda: pipeline.provisional is not None)
    await stt.emit(signal, "through" if signal == "PARTIAL" else "", order=1)
    await until(lambda: pipeline.continuations == 1)
    await asyncio.sleep(GRACE_MS / 1000 * 1.6)  # well past the original grace
    assert recorder.requests == [], "A must never be admitted on its own"
    await stt.emit("FINAL", "through an electrical outlet.", order=1)
    await until(lambda: len(recorder.requests) == 1)
    [request] = recorder.requests
    assert request.utterance == "Wait, the water is getting close. through an electrical outlet."
    # The first provider segment's identity is the admitted identity.
    assert request.turn_id == uuid5(session.conversation_session_id, str(speech_id(stt, 0)))
    finals = [e for e in events(session) if e.event_type == "STT_FINAL"]
    assert len(finals) == 1 and finals[0].payload.model_dump()["speech_turn_id"] == speech_id(
        stt, 0
    )
    assert pipeline.coalesced == 1
    await processed(session)
    await session.end()


async def test_chain_of_continuations_admits_one_turn_after_final_grace() -> None:
    session, pipeline, stt, _, recorder = await attached()
    await stt.emit("FINAL", "A.", order=0)
    await stt.emit("PARTIAL", "B", order=1)
    await stt.emit("FINAL", "B.", order=1)
    await stt.emit("SPEECH_STARTED", order=2)
    await stt.emit("FINAL", "C.", order=2)
    await until(lambda: len(recorder.requests) == 1)
    assert recorder.requests[0].utterance == "A. B. C."
    await asyncio.sleep(GRACE_MS / 1000 * 1.5)
    assert len(recorder.requests) == 1 and pipeline.coalesced == 2
    await processed(session)
    await session.end()


# --- 6: continuation after the grace ----------------------------------------------------------


async def test_speech_after_grace_is_a_new_turn() -> None:
    session, _, stt, _, recorder = await attached()
    await stt.emit("FINAL", "My AC isn't cooling properly.", order=0)
    await until(lambda: len(recorder.requests) == 1)
    await processed(session)
    await stt.emit("PARTIAL", "It is", order=1)
    await stt.emit("FINAL", "It's only blowing warm air.", order=1)
    await until(lambda: len(recorder.requests) == 2)
    assert [r.utterance for r in recorder.requests] == [
        "My AC isn't cooling properly.",
        "It's only blowing warm air.",
    ]
    assert recorder.requests[1].turn_id == uuid5(
        session.conversation_session_id, str(speech_id(stt, 1))
    )
    await processed(session)
    await session.end()


# --- 8-10: duplicate, stale and partial protection --------------------------------------------


async def test_duplicate_and_stale_finals_remain_protected() -> None:
    session, pipeline, stt, _, recorder = await attached()
    await stt.emit("FINAL", "First.", order=1)
    await stt.emit("FINAL", "First.", order=1)  # duplicate while provisional
    await stt.emit("FINAL", "Stale.", order=0)  # stale provider turn
    await stt.emit("PARTIAL", "stale partial", order=0)  # stale: not continuation
    await until(lambda: len(recorder.requests) == 1)
    assert recorder.requests[0].utterance == "First."
    assert pipeline.duplicates == 2 and pipeline.continuations == 0
    await processed(session)
    await stt.emit("FINAL", "First.", order=1)  # duplicate after admission
    await asyncio.sleep(GRACE_MS / 1000 * 1.5)
    assert len(recorder.requests) == 1 and pipeline.duplicates == 3
    await session.end()


async def assert_clean_utterance_still_admitted(
    session: RealtimeSession, stt: FakeSTT, recorder: Recorder, order: int
) -> None:
    before = len(recorder.requests)
    await stt.emit("FINAL", "A clean new request.", order=order)
    await requests_reach(recorder, before + 1)
    assert recorder.requests[-1].utterance == "A clean new request."
    await processed(session)


async def test_continuation_timeout_fails_closed_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(speech, "CONTINUATION_WATCHDOG_SECONDS", 0.3)
    session, pipeline, stt, _, recorder = await attached()
    await stt.emit("FINAL", "Wait, the water is getting close.", order=0)
    await stt.emit("PARTIAL", "through", order=1)  # speaker resumed; never finalizes
    await until(lambda: pipeline.aborted.get("CONTINUATION_TIMEOUT") == 1)
    assert recorder.requests == [] and pipeline.provisional is None  # no prefix turn
    assert "STT_FINAL" not in [e.event_type for e in events(session)]
    # The resumed provider turn is consumed: its late final never becomes a fragment turn.
    await stt.emit("FINAL", "through an electrical outlet.", order=1)
    await asyncio.sleep(GRACE_MS / 1000 * 1.5)
    assert recorder.requests == [] and pipeline.duplicates == 1
    await assert_clean_utterance_still_admitted(session, stt, recorder, order=2)
    await session.end()
    assert not relay_input_tasks()


async def test_watchdog_rearms_on_continuation_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(speech, "CONTINUATION_WATCHDOG_SECONDS", 0.3)
    session, pipeline, stt, _, recorder = await attached()
    await stt.emit("FINAL", "First part.", order=0)
    for word in ("still", "still talking", "still talking here"):
        await stt.emit("PARTIAL", word, order=1)
        await asyncio.sleep(0.2)  # each gap is shorter than the watchdog
    await stt.emit("FINAL", "still talking here.", order=1)
    await requests_reach(recorder, 1)
    assert recorder.requests[0].utterance == "First part. still talking here."
    assert pipeline.aborted == {}
    await processed(session)
    await session.end()


@pytest.mark.parametrize("limit", ["segments", "characters"])
async def test_admission_limit_fails_closed_then_recovers(
    limit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if limit == "segments":
        monkeypatch.setattr(speech, "MAX_COALESCED_SEGMENTS", 2)
    else:
        monkeypatch.setattr(speech, "MAX_UTTERANCE_CHARS", 12)
    session, pipeline, stt, _, recorder = await attached()
    await stt.emit("FINAL", "One.", order=0)
    await stt.emit("FINAL", "Two.", order=1)
    await stt.emit("FINAL", "Three more.", order=2)  # exceeds the bound
    await until(lambda: pipeline.aborted.get("ADMISSION_LIMIT_EXCEEDED") == 1)
    await asyncio.sleep(GRACE_MS / 1000 * 1.5)
    assert recorder.requests == [] and pipeline.provisional is None  # no prefix, no tail
    assert "STT_FINAL" not in [e.event_type for e in events(session)]
    await assert_clean_utterance_still_admitted(session, stt, recorder, order=3)
    await session.end()
    assert not relay_input_tasks()


async def test_provider_failure_never_flushes_provisional() -> None:
    session, pipeline, stt, _, recorder = await attached(grace_ms=5000)
    await stt.emit("FINAL", "Create an emergency incident.", order=0)
    await until(lambda: pipeline.provisional is not None)
    await stt.emit("ERROR")
    await until(lambda: pipeline.task is not None and pipeline.task.done())
    assert recorder.requests == [] and pipeline.aborted == {"PROVIDER_FAILURE": 1}
    kinds = [e.event_type for e in events(session)]
    assert "STT_FINAL" not in kinds and "STT_FAILED" in kinds  # normal failure path
    assert session.active_task is None
    await session.end()
    assert not relay_input_tasks()


async def test_input_ending_during_continuation_fails_closed() -> None:
    session, pipeline, stt, media, recorder = await attached(grace_ms=5000)
    await stt.emit("FINAL", "Wait, the water is getting close.", order=0)
    await stt.emit("PARTIAL", "through", order=1)
    await until(lambda: pipeline.continuations == 1)
    await media.frames.put(None)  # clean media end, but the speaker had resumed
    await until(lambda: pipeline.task is not None and pipeline.task.done())
    assert recorder.requests == []
    assert pipeline.aborted == {"INPUT_ENDED_DURING_CONTINUATION": 1}
    await session.end()
    assert not relay_input_tasks()


# --- 11-14: tasks, bounded state, shutdown and disconnect --------------------------------------


async def test_many_turns_leave_bounded_state_and_no_tasks() -> None:
    session, pipeline, stt, _, recorder = await attached(grace_ms=20)
    for order in range(30):
        await stt.emit("FINAL", f"Turn {order}.", order=order)
        await requests_reach(recorder, order + 1)
        await processed(session)
        if order % 3 == 0:
            await stt.emit("PARTIAL", "x", order=order + 1)  # cancels nothing once admitted
        events(session)
    assert pipeline.provisional is None and len(recorder.requests) == 30
    assert sum(name == "relay-stt-receive" for name in relay_input_tasks()) == 1
    await session.end()
    assert not relay_input_tasks()


async def test_clean_shutdown_while_idle() -> None:
    session, pipeline, _, media, _ = await attached()
    await session.end()
    assert media.closed and pipeline.closed and not relay_input_tasks()


async def test_input_ending_flushes_provisional_through_normal_admission() -> None:
    session, pipeline, stt, media, recorder = await attached(grace_ms=5000)
    await stt.emit("FINAL", "Water is leaking.", order=0)
    await until(lambda: pipeline.provisional is not None)
    await media.frames.put(None)  # media ends: no continuation can arrive
    await until(lambda: len(recorder.requests) == 1)
    assert recorder.requests[0].utterance == "Water is leaking." and pipeline.flushed == 1
    await until(lambda: pipeline.closed and pipeline.task is not None and pipeline.task.done())
    kinds = [e.event_type for e in events(session)]
    assert kinds.index("STT_FINAL") < kinds.index("STT_FAILED")  # admitted before teardown
    await processed(session)
    await session.end()
    assert not relay_input_tasks()


async def test_session_ending_discards_provisional_explicitly() -> None:
    session, pipeline, stt, _, recorder = await attached(grace_ms=5000)
    await stt.emit("FINAL", "Create an incident.", order=0)
    await until(lambda: pipeline.provisional is not None)
    await session.end()
    assert recorder.requests == [] and pipeline.provisional is None
    assert pipeline.aborted == {"SESSION_END": 1}
    assert not relay_input_tasks()


# --- 15: SESSION_BUSY unchanged after admission ------------------------------------------------


async def test_admitted_final_during_active_turn_is_session_busy() -> None:
    gate = asyncio.Event()
    session, _, stt, _, recorder = await attached(recorder=Recorder(block=gate))
    await stt.emit("FINAL", "First request.", order=0)
    await until(lambda: len(recorder.requests) == 1)
    await stt.emit("FINAL", "Second request.", order=1)  # grace expires while turn 1 runs
    await until(lambda: session.busy_count == 1)
    busy = [e for e in events(session) if e.event_type == "ERROR"]
    assert isinstance(busy[-1].payload, Error) and busy[-1].payload.code == Code.SESSION_BUSY
    assert len(recorder.requests) == 1
    gate.set()
    await processed(session)
    await session.end()


# --- configuration ------------------------------------------------------------------------------


def test_admission_grace_setting() -> None:
    assert Settings().stt_final_admission_grace_ms == 400
    assert Settings(stt_final_admission_grace_ms=0).stt_final_admission_grace_ms == 0
    for invalid in (-1, 5001):
        with pytest.raises(ValidationError):
            Settings(stt_final_admission_grace_ms=invalid)
