import asyncio
import json
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from app.agent.errors import TurnError
from app.agent.schemas import AgentTurnRequest, AgentTurnResult, Intent, ResponsePlan
from app.core.config import Settings
from app.core.lifecycle import lifespan
from app.main import create_app
from app.providers.tts.elevenlabs import ElevenLabsStreamingTTS
from app.realtime.manager import RealtimeSessionManager
from app.realtime.output import OutputFailure, SpeechOutput
from app.realtime.presentation import PresentationEvent
from app.realtime.schemas import (
    AgentAudioOutputEnded,
    AgentResponse,
    Binding,
    ClientEvent,
    ServerEvent,
    SpeechSignal,
    Start,
    TextTurn,
)
from app.realtime.session import PRESENTATION_EVENTS, RealtimeSession
from app.realtime.spoken import spoken_text
from app.routing.schemas import DecisionType, ReasonCode, RoutingDecision
from pydantic import SecretStr

from tests.support.output import FakePresentation, FakeSink
from tests.support.speech import FakeTTS

REFERENCE = "dd51053d-f8d0-4619-a8dd-c700e3728e59"
SUCCESS = f"Your emergency incident was created. Reference: {REFERENCE}."


def frames(count: int) -> list[bytes]:
    return [bytes([index % 251, 1]) * 480 for index in range(count)]


def result(
    request: AgentTurnRequest, text: str = "How can I help?", replayed: bool = False
) -> AgentTurnResult:
    return AgentTurnResult(
        session_id=request.session_id,
        turn_id=request.turn_id,
        turn_number=request.turn_number,
        routing_decision=RoutingDecision(
            decision=DecisionType.ABSTAINED,
            property_id=uuid4(),
            reason_code=ReasonCode.OUT_OF_SCOPE,
            reason="test",
            catalog_fingerprint="test",
            duration_ms=0,
        ),
        workflow_decision=None,
        response_plan=ResponsePlan(intent=Intent.INFORMATION, property_name="Atlas"),
        response_text=text,
        revision=request.expected_revision + 1,
        replayed=replayed,
    )


class Scripted:
    def __init__(self, respond: Callable[[AgentTurnRequest], AgentTurnResult]) -> None:
        self.respond = respond
        self.requests: list[AgentTurnRequest] = []

    async def process(self, request: AgentTurnRequest) -> AgentTurnResult:
        self.requests.append(request)
        return self.respond(request)


async def started(processor: Scripted) -> RealtimeSession:
    session_id = uuid4()
    session = RealtimeSession(session_id, Binding(revision=1, turn_number=0), processor)
    await session.handle(
        ClientEvent(session_id=session_id, sequence=1, event_type="SESSION_START", payload=Start())
    )
    drain(session)
    return session


def drain(session: RealtimeSession) -> list[ServerEvent]:
    events = []
    while not session.outbound.items.empty():
        events.append(session.outbound.items.get_nowait())
        session.outbound.items.task_done()
    return events


async def turn(session: RealtimeSession, turn_id: UUID | None = None, number: int = 1) -> None:
    payload = TextTurn(
        turn_id=turn_id or uuid4(), turn_number=number, expected_revision=number, utterance="Hi."
    )
    await session.handle(
        ClientEvent(
            session_id=session.conversation_session_id,
            sequence=number + 1,
            event_type="CLIENT_TEXT_TURN",
            turn_id=payload.turn_id,
            payload=payload,
        )
    )
    task = session.active_task
    assert task is not None
    await task


def attach(session: RealtimeSession, tts: FakeTTS, sink: FakeSink) -> SpeechOutput:
    output = SpeechOutput(
        tts, sink, provider_name="fake", model="deterministic", observer=session.presentation_event
    )
    session.attach_output(output)
    return output


async def settle_output(output: SpeechOutput, session: RealtimeSession) -> None:
    """Bounded yields until no presentation is scheduled and the event pump is drained."""
    for _ in range(10_000):
        idle = output.active is None and output.pending is None
        ended = all(record.ended.is_set() for record in output.finished)
        if idle and ended and session._presentation_events.empty():
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("presentation did not settle")
    for _ in range(5):  # let the pump finish emitting the last event it dequeued
        await asyncio.sleep(0)


def kinds(events: list[ServerEvent]) -> list[str]:
    return [event.event_type for event in events]


def relay_output_tasks() -> set[str]:
    return {
        task.get_name()
        for task in asyncio.all_tasks()
        if task.get_name().startswith(("relay-output", "relay-tts", "relay-presentation"))
        and not task.done()
    }


# --- A-D: unchanged text path, ordering ----------------------------------------------------


async def test_without_output_the_event_sequence_is_unchanged() -> None:
    session = await started(Scripted(result))
    await turn(session)
    assert kinds(drain(session)) == [
        "SESSION_STATE_CHANGED",
        "TURN_STARTED",
        "AGENT_RESPONSE",
        "TURN_COMPLETED",
        "SESSION_STATE_CHANGED",
    ]
    await session.end()


async def test_first_delivery_is_spoken_once_after_text_and_completion() -> None:
    session = await started(Scripted(result))
    tts, sink = FakeTTS(frames(3)), FakeSink()
    output = attach(session, tts, sink)
    turn_id = uuid4()
    await turn(session, turn_id)
    record = output.active or output.pending or next(iter(output.finished), None)
    assert record is not None
    await record.ended.wait()
    await settle_output(output, session)
    events = drain(session)
    names = kinds(events)
    assert names.index("AGENT_RESPONSE") < names.index("TURN_COMPLETED")
    assert names.index("TURN_COMPLETED") < names.index("AGENT_AUDIO_OUTPUT_STARTED")
    assert names.count("AGENT_AUDIO_OUTPUT_STARTED") == 1
    assert names.count("AGENT_AUDIO_OUTPUT_ENDED") == 1
    started_event = next(e for e in events if e.event_type == "AGENT_AUDIO_OUTPUT_STARTED")
    ended_event = next(e for e in events if e.event_type == "AGENT_AUDIO_OUTPUT_ENDED")
    assert started_event.turn_id == ended_event.turn_id == turn_id
    payload = ended_event.payload
    assert isinstance(payload, AgentAudioOutputEnded)
    assert payload.status == "COMPLETED" and payload.code is None
    assert payload.accepted_audio_ms == pytest.approx(60)
    assert len(tts.requests) == 1 and len(sink.accepted) == 3
    await session.end()
    assert not relay_output_tasks()


async def test_turn_completes_before_voice_finishes() -> None:
    session = await started(Scripted(result))
    gate = asyncio.Event()
    output = attach(session, FakeTTS(frames(3)), FakeSink(gate=gate))
    await turn(session)
    names = kinds(drain(session))
    assert "TURN_COMPLETED" in names and names[-1] == "SESSION_STATE_CHANGED"
    assert output.active is not None and not output.active.terminal  # voice still running
    gate.set()
    await session.end()


# --- E-G: spoken reference transform ---------------------------------------------------------


async def test_reference_rewritten_only_for_speech() -> None:
    session = await started(Scripted(lambda r: result(r, SUCCESS)))
    tts = FakeTTS(frames(1))
    output = attach(session, tts, FakeSink())
    await turn(session)
    await settle_output(output, session)
    response = next(e.payload for e in drain(session) if e.event_type == "AGENT_RESPONSE")
    assert isinstance(response, AgentResponse)
    assert response.response_text == SUCCESS and REFERENCE in response.response_text
    assert tts.requests[0].text == (
        "Your emergency incident was created. The reference is available in the chat."
    )
    assert REFERENCE not in tts.requests[0].text
    await session.end()


@pytest.mark.parametrize(
    "text",
    [
        f"Please quote {REFERENCE} when calling.",
        f"Reference: {REFERENCE}.",  # no leading clause: not the renderer form
        f"Your request was created. Reference: {REFERENCE.upper()}.",
        f"Your request was created. reference: {REFERENCE}.",
        f"Your request was created. Reference: {REFERENCE}",
        f"Your request was created. Reference: {REFERENCE}. Thanks.",
        f"Reference: {REFERENCE}. Your request was created. Reference: {REFERENCE}x.",
    ],
)
def test_spoken_transform_leaves_other_text_untouched(text: str) -> None:
    assert spoken_text(text) == text


def test_spoken_transform_for_every_renderer_success_clause() -> None:
    for clause in (
        "Your maintenance request was created.",
        "Your emergency incident was created.",
        "Your emergency incident was updated with the reported information.",
        "Your tour request was recorded. A calendar booking is not yet confirmed.",
        "Your human handoff request was created. No live transfer has occurred.",
    ):
        identifier = str(uuid4())
        assert spoken_text(f"{clause} Reference: {identifier}.") == (
            f"{clause} The reference is available in the chat."
        )


# --- H-J: replay, recovery and TTS failure ---------------------------------------------------


async def test_exact_replay_is_not_spoken_again() -> None:
    delivered: dict[UUID, int] = {}

    def respond(request: AgentTurnRequest) -> AgentTurnResult:
        delivered[request.turn_id] = delivered.get(request.turn_id, 0) + 1
        return result(request, replayed=delivered[request.turn_id] > 1)

    session = await started(Scripted(respond))
    tts = FakeTTS(frames(1))
    output = attach(session, tts, FakeSink())
    turn_id = uuid4()
    await turn(session, turn_id)
    await settle_output(output, session)
    first = drain(session)
    await turn(session, turn_id)  # same request: business replay
    await settle_output(output, session)
    second = drain(session)
    replayed = next(e.payload for e in second if e.event_type == "AGENT_RESPONSE")
    assert isinstance(replayed, AgentResponse) and replayed.replayed
    assert kinds(first).count("AGENT_AUDIO_OUTPUT_ENDED") == 1
    assert "AGENT_AUDIO_OUTPUT_STARTED" not in kinds(second)
    assert "AGENT_AUDIO_OUTPUT_ENDED" not in kinds(second)
    assert len(tts.requests) == 1
    await session.end()


async def test_recovered_first_delivery_is_spoken() -> None:
    attempts = {"count": 0}

    def respond(request: AgentTurnRequest) -> AgentTurnResult:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TurnError("PREVIOUS_TURN_INCOMPLETE")
        return result(request, replayed=False)  # recovered: first delivery of the result

    session = await started(Scripted(respond))
    tts = FakeTTS(frames(1))
    output = attach(session, tts, FakeSink())
    turn_id = uuid4()
    await turn(session, turn_id)
    assert "ERROR" in kinds(drain(session)) and tts.requests == []
    await turn(session, turn_id)
    await settle_output(output, session)
    assert kinds(drain(session)).count("AGENT_AUDIO_OUTPUT_ENDED") == 1
    assert len(tts.requests) == 1
    await session.end()


async def test_tts_failure_leaves_text_and_business_result_intact() -> None:
    session = await started(Scripted(lambda r: result(r, SUCCESS)))
    output = attach(session, FakeTTS(frames(2), fail_after=0), FakeSink())
    await turn(session)
    await settle_output(output, session)
    events = drain(session)
    response = next(e.payload for e in events if e.event_type == "AGENT_RESPONSE")
    completed = next(e for e in events if e.event_type == "TURN_COMPLETED")
    ended = next(e.payload for e in events if e.event_type == "AGENT_AUDIO_OUTPUT_ENDED")
    assert isinstance(response, AgentResponse) and response.response_text == SUCCESS
    assert completed.payload.model_dump()["status"] == "COMPLETED"
    assert isinstance(ended, AgentAudioOutputEnded)
    assert ended.status == "FAILED" and ended.code == "TTS_UNAVAILABLE"
    assert "AGENT_AUDIO_OUTPUT_STARTED" not in kinds(events)
    assert session.binding.turn_number == 1  # business progress recorded as usual
    await session.end()


# --- K-O: failure isolation and event content ------------------------------------------------


class SpeechInput:
    def __init__(self) -> None:
        self.responses: list[str] = []
        self.closed = False

    def on_response(self, response_text: str) -> None:
        self.responses.append(response_text)

    async def close(self) -> None:
        self.closed = True


async def test_output_sink_failure_leaves_input_attached() -> None:
    session = await started(Scripted(result))
    speech = SpeechInput()
    session.speech = speech
    output = attach(session, FakeTTS(frames(5)), FakeSink(fail_after=1))
    await turn(session, number=1)
    await settle_output(output, session)
    ended = next(e.payload for e in drain(session) if e.event_type == "AGENT_AUDIO_OUTPUT_ENDED")
    assert isinstance(ended, AgentAudioOutputEnded) and ended.code == "PUBLISH_FAILED"
    assert session.speech is speech and not speech.closed and not output.available
    await turn(session, number=2)  # input and text processing continue
    assert speech.responses == ["How can I help?", "How can I help?"]
    assert "AGENT_AUDIO_OUTPUT_ENDED" not in kinds(drain(session))  # unusable output skipped
    await session.end()
    assert speech.closed


async def test_lifecycle_events_never_carry_text_or_secrets() -> None:
    session = await started(Scripted(lambda r: result(r, SUCCESS)))
    output = attach(session, FakeTTS(frames(2)), FakeSink())
    await turn(session)
    await settle_output(output, session)
    audio = [e for e in drain(session) if e.event_type.startswith("AGENT_AUDIO_OUTPUT")]
    assert [e.event_type for e in audio] == [
        "AGENT_AUDIO_OUTPUT_STARTED",
        "AGENT_AUDIO_OUTPUT_ENDED",
    ]
    for event in audio:
        dumped = event.model_dump_json()
        assert REFERENCE not in dumped and "emergency" not in dumped.lower()
        assert set(json.loads(dumped)["payload"]) <= {
            "kind",
            "speech_output_id",
            "turn_id",
            "provider",
            "model",
            "status",
            "code",
            "accepted_audio_ms",
        }
    await session.end()


def test_public_failure_codes_match_output_failures() -> None:
    code = AgentAudioOutputEnded.model_fields["code"].annotation
    assert set(code.__args__[0].__args__) == {f.value for f in OutputFailure}  # type: ignore[union-attr]


async def test_event_overflow_is_logged_not_silent() -> None:
    session = await started(Scripted(result))
    logged: list[str] = []
    session.log = lambda event, **fields: logged.append(event)  # type: ignore[method-assign]
    for _ in range(PRESENTATION_EVENTS + 2):
        session.presentation_event(
            PresentationEvent("ENDED", uuid4(), uuid4(), None, None, "COMPLETED")
        )
    assert logged.count("SPEECH_OUTPUT_EVENT_DROPPED") == 2
    await session.end()


# --- S-V: shutdown, leaks, isolation, lifecycle wiring ---------------------------------------


async def test_shutdown_closes_output_and_leaves_no_tasks() -> None:
    async def lookup(session_id: UUID) -> Binding:
        return Binding(revision=1, turn_number=0)

    manager = RealtimeSessionManager(lookup, Scripted(result))
    session = await manager.bind(uuid4())
    sink = FakeSink(gate=asyncio.Event())
    output = attach(session, FakeTTS(frames(3)), sink)
    output.present(uuid4(), "Speaking while the server stops.")
    await manager.shutdown()
    assert session.output is None and sink.closes == 1 and output.closed
    assert not relay_output_tasks()


async def test_sessions_keep_presentation_state_isolated() -> None:
    first, second = await started(Scripted(result)), await started(Scripted(result))
    tts_a, tts_b = FakeTTS(frames(2)), FakeTTS(frames(2))
    out_a, out_b = attach(first, tts_a, FakeSink()), attach(second, tts_b, FakeSink())
    shared_turn = uuid4()
    await turn(first, shared_turn)
    await turn(second, shared_turn)  # same turn id in another session is independent
    await settle_output(out_a, first)
    await settle_output(out_b, second)
    assert len(tts_a.requests) == len(tts_b.requests) == 1
    a_ids = {
        e.payload.model_dump()["speech_output_id"] for e in drain(first) if "AUDIO" in e.event_type
    }
    b_ids = {
        e.payload.model_dump()["speech_output_id"] for e in drain(second) if "AUDIO" in e.event_type
    }
    assert a_ids and b_ids and not a_ids & b_ids
    await asyncio.gather(first.end(), second.end())
    assert not relay_output_tasks()


async def test_lifespan_builds_tts_only_when_enabled_on_shared_client() -> None:
    voice = {
        "voice_enabled": True,
        "livekit_url": "ws://livekit:7880",
        "livekit_api_key": SecretStr("devkey"),
        "livekit_api_secret": SecretStr("secret"),
        "assemblyai_api_key": SecretStr("stt-key"),
    }
    disabled = create_app()
    async with lifespan(disabled):
        assert disabled.state.tts_provider is None
    enabled = create_app()
    enabled.state.settings = Settings(
        **voice,  # type: ignore[arg-type]
        tts_enabled=True,
        elevenlabs_api_key=SecretStr("fake-tts-key"),
        tts_voice_id="Voice123",
    )
    async with lifespan(enabled):
        provider = enabled.state.tts_provider
        assert isinstance(provider, ElevenLabsStreamingTTS)
        assert provider._client is enabled.state.http_client
        assert provider.voice_id == "Voice123" and "fake-tts-key" not in repr(provider)
    assert enabled.state.http_client.is_closed


# --- Phase 10: barge-in policy ------------------------------------------------------------------


def speech_start(audio_timestamp_ms: int | None = None) -> SpeechSignal:
    return SpeechSignal(
        kind="USER_SPEECH_STARTED", speech_turn_id=uuid4(), audio_timestamp_ms=audio_timestamp_ms
    )


class Blocking:
    """A processor whose business turn stays running until released."""

    def __init__(self) -> None:
        self.gate, self.entered = asyncio.Event(), asyncio.Event()

    async def process(self, request: AgentTurnRequest) -> AgentTurnResult:
        self.entered.set()
        await self.gate.wait()
        return result(request)


def captured_logs(session: RealtimeSession) -> list[tuple[str, dict[str, object]]]:
    logs: list[tuple[str, dict[str, object]]] = []
    session.log = lambda event, **fields: logs.append((event, fields))  # type: ignore[method-assign]
    return logs


async def test_speech_start_requests_interruption_and_emits_signal() -> None:  # A
    session = await started(Scripted(result))
    presentation = FakePresentation()
    session.attach_output(presentation)
    logs = captured_logs(session)
    signal = speech_start(audio_timestamp_ms=1200)
    assert await session.user_speech_started(signal) is True
    assert presentation.interrupts == 1
    [event] = drain(session)
    assert event.event_type == "USER_SPEECH_STARTED" and event.payload == signal
    assert session.cancelled_turns == 0 and session.state.value == "IDLE"
    barge = [fields for name, fields in logs if name == "REALTIME_BARGE_IN_REQUESTED"]
    assert barge == [{"speech_turn_id": str(signal.speech_turn_id), "presentation_targeted": True}]
    await session.end()


async def test_presentation_interrupt_failure_is_isolated() -> None:  # B
    processor = Scripted(result)
    session = await started(processor)
    presentation = FakePresentation(fail=True)
    session.attach_output(presentation)
    logs = captured_logs(session)
    assert await session.user_speech_started(speech_start()) is False
    assert presentation.interrupts == 1
    assert kinds(drain(session)) == ["USER_SPEECH_STARTED"]
    assert "SPEECH_PRESENTATION_INTERRUPT_FAILED" in [name for name, _ in logs]
    await turn(session)  # the session keeps working, and the next response is presented
    assert len(processor.requests) == 1 and len(presentation.presented) == 1
    assert "AGENT_RESPONSE" in kinds(drain(session)) and session.cancelled_turns == 0
    await session.end()


async def test_speech_start_never_cancels_the_active_business_turn() -> None:  # G
    processor = Blocking()
    session_id = uuid4()
    session = RealtimeSession(session_id, Binding(revision=1, turn_number=0), processor)
    await session.handle(
        ClientEvent(session_id=session_id, sequence=1, event_type="SESSION_START", payload=Start())
    )
    presentation = FakePresentation()
    session.attach_output(presentation)

    async def forbidden(turn_id: UUID) -> None:
        raise AssertionError("barge-in must never cancel a business turn")

    session.cancel = forbidden  # type: ignore[method-assign]
    turn_id = uuid4()
    payload = TextTurn(turn_id=turn_id, turn_number=1, expected_revision=1, utterance="Hi.")
    await session.handle(
        ClientEvent(
            session_id=session_id,
            sequence=2,
            event_type="CLIENT_TEXT_TURN",
            turn_id=turn_id,
            payload=payload,
        )
    )
    await processor.entered.wait()
    task = session.active_task
    assert task is not None
    await session.user_speech_started(speech_start())
    await asyncio.sleep(0)
    assert presentation.interrupts == 1
    assert session.active_task is task and not task.done() and task.cancelling() == 0
    assert session.active_turn_id == turn_id and session.binding.turn_number == 0
    processor.gate.set()
    await task
    assert session.binding.turn_number == 1 and session.cancelled_turns == 0
    assert "AGENT_RESPONSE" in kinds(drain(session))
    assert presentation.presented == [(turn_id, "How can I help?")]
    await session.end()


async def test_speech_start_without_presentation_still_emits() -> None:  # J
    session = await started(Scripted(result))
    logs = captured_logs(session)
    assert await session.user_speech_started(speech_start()) is False
    assert kinds(drain(session)) == ["USER_SPEECH_STARTED"]
    assert "REALTIME_BARGE_IN_REQUESTED" not in [name for name, _ in logs]
    await turn(session)
    await session.end()


async def test_idle_presentation_is_still_asked_to_interrupt() -> None:  # K
    session = await started(Scripted(result))
    presentation = FakePresentation(targeted=False)
    session.attach_output(presentation)
    assert await session.user_speech_started(speech_start()) is False
    assert presentation.interrupts == 1  # it may still owe a native clear
    await session.end()


async def test_idle_real_output_clears_audio_already_accepted() -> None:  # K
    session = await started(Scripted(result))
    sink = FakeSink()
    output = attach(session, FakeTTS(frames(3)), sink)
    await turn(session)
    await settle_output(output, session)
    assert output.finished[-1].status.value == "COMPLETED" and sink.clears == 0
    assert await session.user_speech_started(speech_start()) is False
    for _ in range(100):
        if sink.clears:
            break
        await asyncio.sleep(0)
    assert sink.clears == 1 and output.finished[-1].status.value == "COMPLETED"
    await session.end()


async def test_interrupt_is_requested_before_the_outbound_event_is_queued() -> None:
    session_id = uuid4()
    session = RealtimeSession(
        session_id,
        Binding(revision=1, turn_number=0),
        Scripted(result),
        queue_capacity=1,
        queue_timeout=5,
    )
    seen: list[list[str]] = []
    presentation = FakePresentation(
        on_interrupt=lambda: seen.append(
            [event.event_type for event in list(session.outbound.items._queue)]  # type: ignore[attr-defined]
        )
    )
    session.attach_output(presentation)
    # A client that is not reading: the single outbound slot is already taken.
    await session.emit(SpeechSignal(kind="USER_SPEECH_ENDED", speech_turn_id=uuid4()))
    pending = asyncio.create_task(session.user_speech_started(speech_start()))
    for _ in range(20):
        await asyncio.sleep(0)
    # The local interrupt already happened; the outbound event is still waiting for room.
    assert presentation.interrupts == 1 and seen == [["USER_SPEECH_ENDED"]]
    assert not pending.done()
    assert kinds(drain(session)) == ["USER_SPEECH_ENDED"]
    assert await asyncio.wait_for(pending, 1) is True
    assert kinds(drain(session)) == ["USER_SPEECH_STARTED"]
    await session.end()


async def test_barge_in_on_real_output_reports_user_interrupted() -> None:
    session = await started(Scripted(result))
    gate = asyncio.Event()
    sink = FakeSink(gate=gate, release_on_clear=True)
    output = attach(session, FakeTTS(frames(10)), sink)
    turn_id = uuid4()
    await turn(session, turn_id)
    for _ in range(10_000):
        if sink.waiting.is_set():
            break
        await asyncio.sleep(0)
    assert await session.user_speech_started(speech_start()) is True
    await settle_output(output, session)
    ended_events = [e for e in drain(session) if e.event_type == "AGENT_AUDIO_OUTPUT_ENDED"]
    [ended] = ended_events
    payload = ended.payload
    assert isinstance(payload, AgentAudioOutputEnded) and ended.turn_id == turn_id
    assert payload.status == "CANCELLED" and payload.code == "USER_INTERRUPTED"
    assert output.available and session.cancelled_turns == 0
    await session.end()
    assert not relay_output_tasks()
