import ast
import asyncio
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from app.realtime.media.protocol import AudioFormat, AudioFrame
from app.realtime.output import (
    BUFFER_FRAMES,
    STARTED_TURNS,
    OutputFailure,
    OutputStatus,
    SpeechOutput,
    SpeechOutputRecord,
)

from tests.support.output import FakeSink, StaleOutputError, SteppedTTS
from tests.support.speech import FakeTTS

FRAME = 960  # 20 ms at 24 kHz mono PCM16


def pcm(samples: int, start: int = 0) -> bytes:
    return b"".join(((start + i) % 65536).to_bytes(2, "little") for i in range(samples))


def frame_chunks(count: int, start: int = 0) -> list[bytes]:
    return [pcm(480, start + i * 480) for i in range(count)]


def relay_tasks() -> set[asyncio.Task[object]]:
    return {
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith(("relay-output", "relay-tts")) and not task.done()
    }


async def until(predicate: Callable[[], bool], rounds: int = 10_000) -> None:
    """Yield to the event loop a bounded number of times until the condition holds."""
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def ended(record: SpeechOutputRecord | None) -> SpeechOutputRecord:
    assert record is not None
    async with asyncio.timeout(2):
        await record.ended.wait()
    return record


def started(provider: object, sink: FakeSink, **options: object) -> SpeechOutput:
    output = SpeechOutput(provider, sink, provider_name="fake", model="deterministic", **options)  # type: ignore[arg-type]
    output.start()
    return output


# --- A-C: happy path and identity -----------------------------------------------------


async def test_happy_path_frames_reach_sink_in_order() -> None:
    data = pcm(480 * 3 + 100)
    provider, sink = FakeTTS([data[:5], data[5:2001], data[2001:]]), FakeSink()
    logs: list[dict[str, object]] = []
    output = started(provider, sink, log=lambda event, **fields: logs.append(fields))
    record = await ended(output.present(uuid4(), "Hello there."))
    assert record.status == OutputStatus.COMPLETED and record.code is None
    assert b"".join(frame.data for frame in sink.frames) == data
    assert [len(frame.data) for frame in sink.frames] == [FRAME, FRAME, FRAME, 200]
    assert [frame.sequence for frame in sink.frames] == [1, 2, 3, 4]
    assert provider.requests[0].text == "Hello there."
    assert provider.requests[0].speech_output_id == record.speech_output_id
    assert {output_id for output_id, _ in sink.accepted} == {record.speech_output_id}
    assert record.frames_accepted == 4 and record.accepted_audio_ms == pytest.approx(1540 / 24)
    assert record.provider == "fake" and record.model == "deterministic"
    assert record.tts_requested is not None and record.tts_first_audio_received is not None
    assert record.first_frame_accepted_by_sink is not None and record.tts_completed is not None
    assert record.output_drained is not None and record.ended_at is not None
    assert record.tts_requested <= record.tts_first_audio_received
    assert record.tts_first_audio_received <= record.first_frame_accepted_by_sink
    assert record.tts_completed <= record.output_drained <= record.ended_at
    assert "Hello there." not in repr(record) and "Hello there." not in str(logs)
    await output.close()
    assert not relay_tasks()


async def test_each_attempt_gets_a_stable_new_identity() -> None:
    provider, sink = FakeTTS(frame_chunks(3)), FakeSink()
    output = started(provider, sink)
    first = await ended(output.present(uuid4(), "One."))
    second = await ended(output.present(uuid4(), "Two."))
    assert first.speech_output_id != second.speech_output_id
    assert [output_id for output_id, _ in sink.accepted] == [first.speech_output_id] * 3 + [
        second.speech_output_id
    ] * 3
    assert isinstance(first.speech_output_id, UUID) and first.speech_output_id.version == 4
    await output.close()


# --- D: dependency boundary --------------------------------------------------------------


def test_output_has_no_business_or_vendor_dependencies() -> None:
    import app

    root = Path(app.__file__).parent
    found: set[str] = set()
    for node in ast.walk(ast.parse((root / "realtime" / "output.py").read_text())):
        if isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    forbidden = (
        "app.agent",
        "app.workflows",
        "app.routing",
        "app.tools",
        "app.db",
        "app.property",
        "app.realtime.session",
        "livekit",
        "httpx",
    )
    assert not any(module.startswith(forbidden) for module in found), found
    # Step 7A: voice admission composes SpeechOutput; the session and input pipeline only
    # know the provider-neutral presentation protocol.
    for runtime in ("realtime/session.py", "realtime/speech.py"):
        assert "app.realtime.output" not in (root / runtime).read_text()


# --- E-I: failures and cleanup ---------------------------------------------------------


async def test_tts_failure_before_first_chunk() -> None:
    provider, sink = FakeTTS(frame_chunks(2), fail_after=0), FakeSink()
    output = started(provider, sink)
    record = await ended(output.present(uuid4(), "Hello."))
    assert record.status == OutputStatus.FAILED and record.code == OutputFailure.TTS_UNAVAILABLE
    assert sink.accepted == [] and record.first_frame_accepted_by_sink is None
    assert output.available  # a provider failure does not disable the sink path
    await output.close()


async def test_tts_failure_after_partial_audio_drains_and_does_not_retry() -> None:
    provider, sink = FakeTTS(frame_chunks(3), fail_after=2), FakeSink()
    output = started(provider, sink)
    record = await ended(output.present(uuid4(), "Hello."))
    assert record.status == OutputStatus.FAILED and record.code == OutputFailure.TTS_INTERRUPTED
    assert b"".join(f.data for f in sink.frames) == b"".join(frame_chunks(2))
    assert len(provider.requests) == 1 and provider.outcomes == ["FAILED"]
    await output.close()


@pytest.mark.parametrize(
    ("sink", "code"),
    [
        (FakeSink(fail_after=2), OutputFailure.PUBLISH_FAILED),
        (FakeSink(media_end_after=2), OutputFailure.MEDIA_ENDED),
    ],
)
async def test_sink_failure_cancels_synthesis_and_closes_stream(
    sink: FakeSink, code: OutputFailure
) -> None:
    provider = FakeTTS(frame_chunks(60))
    output = started(provider, sink)
    record = await ended(output.present(uuid4(), "Hello."))
    assert record.status == OutputStatus.FAILED and record.code == code
    assert len(sink.accepted) == 2 and provider.outcomes == ["CLOSED"]
    assert provider.yielded < 60 and not output.available
    later = await ended(output.present(uuid4(), "Later."))
    assert later.code == OutputFailure.OUTPUT_UNAVAILABLE and len(provider.requests) == 1
    await output.close()
    assert not relay_tasks()


async def test_close_during_synthesis_cancels_and_cleans_up() -> None:
    before = asyncio.all_tasks()
    provider, sink = FakeTTS(frame_chunks(5), gate=asyncio.Event()), FakeSink()
    output = started(provider, sink)
    record = output.present(uuid4(), "Hello.")
    await until(lambda: bool(provider.requests))
    await settle()
    await output.close()
    record = await ended(record)
    assert record.status == OutputStatus.CANCELLED and record.code == OutputFailure.SESSION_ENDED
    assert provider.outcomes == ["CLOSED"] and sink.closes == 1
    assert not relay_tasks() and asyncio.all_tasks() <= before | {asyncio.current_task()}


async def test_close_cancels_pending_output() -> None:
    gate = asyncio.Event()
    provider, sink = FakeTTS(frame_chunks(2)), FakeSink(gate=gate)
    output = started(provider, sink)
    active = output.present(uuid4(), "Active.")
    await until(sink.waiting.is_set)
    pending = output.present(uuid4(), "Pending.")
    await output.close()
    assert (await ended(active)).status == OutputStatus.CANCELLED
    assert (await ended(pending)).status == OutputStatus.CANCELLED
    assert [r.text for r in provider.requests] == ["Active."]


# --- J-N: bounded buffer, backpressure, underruns ---------------------------------------


async def test_slow_sink_fills_exactly_the_bounded_buffer_then_recovers() -> None:
    gate = asyncio.Event()
    provider, sink = FakeTTS(frame_chunks(60)), FakeSink(gate=gate)
    output = started(provider, sink)
    record = output.present(uuid4(), "Hello.")
    # One frame is held by the blocked sink, 25 are buffered, one waits in the producer.
    await until(lambda: sink.waiting.is_set() and provider.yielded == BUFFER_FRAMES + 2)
    await settle()
    assert BUFFER_FRAMES == 25
    assert provider.yielded == 27, "producer must stop reading the provider stream"
    assert output._run_state is not None and output._run_state.queue.qsize() == 25
    assert record is not None and record.buffer_high_water == 25
    gate.set()
    record = await ended(record)
    assert record.status == OutputStatus.COMPLETED and len(sink.accepted) == 60
    assert record.buffer_high_water == 25  # never exceeded
    assert b"".join(f.data for f in sink.frames) == b"".join(frame_chunks(60))
    await output.close()


async def test_underruns_count_only_mid_stream_starvation() -> None:
    provider, sink = SteppedTTS(), FakeSink()
    output = started(provider, sink)
    record = output.present(uuid4(), "Hello.")
    await until(lambda: bool(provider.requests))
    await settle()
    assert record is not None and record.underruns == 0  # waiting for first audio
    provider.chunks.put_nowait(pcm(480))
    await until(lambda: len(sink.accepted) == 1)
    await settle()
    assert record.underruns == 1  # sink path starved while synthesis was still running
    provider.chunks.put_nowait(pcm(480, 480))
    provider.chunks.put_nowait(None)
    record = await ended(record)
    assert record.status == OutputStatus.COMPLETED and record.underruns == 1
    await output.close()


async def test_no_underrun_when_audio_is_ahead_of_the_sink() -> None:
    provider, sink = SteppedTTS(), FakeSink()
    for chunk in frame_chunks(4):
        provider.chunks.put_nowait(chunk)
    provider.chunks.put_nowait(None)
    output = started(provider, sink)
    record = await ended(output.present(uuid4(), "Hello."))
    assert record.underruns == 0 and record.frames_accepted == 4
    await output.close()


# --- O-T: scheduling and duplicate defense ----------------------------------------------


async def test_active_continues_newer_pending_supersedes_older_pending() -> None:
    gate = asyncio.Event()
    provider, sink = FakeTTS(frame_chunks(3)), FakeSink(gate=gate)
    output = started(provider, sink)
    active = output.present(uuid4(), "Active.")
    await until(sink.waiting.is_set)
    second = output.present(uuid4(), "Second.")
    assert second is not None and output.pending is second and output.active is active
    third = output.present(uuid4(), "Third.")
    assert (await ended(second)).status == OutputStatus.SUPERSEDED
    assert output.pending is third and active is not None and not active.terminal
    assert [r.text for r in provider.requests] == ["Active."]
    gate.set()
    assert (await ended(active)).status == OutputStatus.COMPLETED
    assert (await ended(third)).status == OutputStatus.COMPLETED
    assert [r.text for r in provider.requests] == ["Active.", "Third."]
    ids = [output_id for output_id, _ in sink.accepted]
    assert active is not None and third is not None
    assert ids == [active.speech_output_id] * 3 + [third.speech_output_id] * 3
    assert second.speech_output_id not in ids
    await output.close()


async def test_duplicate_of_active_or_pending_turn_is_refused() -> None:
    gate = asyncio.Event()
    provider, sink = FakeTTS(frame_chunks(1)), FakeSink(gate=gate)
    output = started(provider, sink)
    active_turn, pending_turn = uuid4(), uuid4()
    active = output.present(active_turn, "Active.")
    await until(sink.waiting.is_set)
    assert output.present(active_turn, "Active again.") is None  # 1. active duplicate
    pending = output.present(pending_turn, "Pending.")
    assert pending is not None and output.pending is pending
    assert output.present(pending_turn, "Pending again.") is None  # 2. pending duplicate
    assert output.pending is pending and pending.status == OutputStatus.PENDING
    gate.set()
    await ended(active)
    await ended(pending)
    assert [r.text for r in provider.requests] == ["Active.", "Pending."]
    await output.close()


async def test_superseded_turn_was_never_presented_and_may_return() -> None:
    gate = asyncio.Event()
    provider, sink = FakeTTS(frame_chunks(1)), FakeSink(gate=gate)
    output = started(provider, sink)
    a, b, c = uuid4(), uuid4(), uuid4()
    first = output.present(a, "A.")
    await until(sink.waiting.is_set)
    superseded = output.present(b, "B.")
    output.present(c, "C.")
    assert (await ended(superseded)).status == OutputStatus.SUPERSEDED  # 3.
    assert b not in output.started_turns
    gate.set()
    await ended(first)
    await until(lambda: output.active is None and output.pending is None)
    again = output.present(b, "B again.")  # 4. never started, so not a duplicate
    assert again is not None and (await ended(again)).status == OutputStatus.COMPLETED
    assert output.present(b, "B third time.") is None  # 5. started: refused
    assert [r.text for r in provider.requests] == ["A.", "C.", "B again."]
    await output.close()


async def test_started_turn_refused_even_before_completion() -> None:
    provider, sink = FakeTTS(frame_chunks(2), gate=asyncio.Event()), FakeSink()
    output = started(provider, sink)
    turn = uuid4()
    record = output.present(turn, "Hello.")
    await until(lambda: bool(provider.requests))
    assert turn in output.started_turns  # marked when synthesis is requested
    assert output.present(turn, "Hello again.") is None
    await output.close()
    assert record is not None and record.status == OutputStatus.CANCELLED
    assert turn in output.started_turns  # a cancelled started presentation is not retried


async def test_started_history_is_bounded() -> None:
    provider, sink = FakeTTS(frame_chunks(1)), FakeSink()
    output = started(provider, sink)
    turns = [uuid4() for _ in range(STARTED_TURNS + 6)]
    for turn in turns:
        await ended(output.present(turn, "Hi."))
    assert len(output.started_turns) == STARTED_TURNS == 64  # 6.
    assert list(output.started_turns) == turns[-64:]
    assert output.present(turns[0], "Evicted.") is not None
    await output.close()


async def test_response_text_is_not_retained_after_terminal_state() -> None:
    text = "Secret-looking response " + uuid4().hex
    provider, sink = FakeTTS(frame_chunks(2)), FakeSink()
    logs: list[dict[str, object]] = []
    output = started(provider, sink, log=lambda event, **fields: logs.append(fields))
    record = await ended(output.present(uuid4(), text))
    superseded_gate = asyncio.Event()
    blocked = started(FakeTTS(frame_chunks(1)), FakeSink(gate=superseded_gate))
    blocked.present(uuid4(), "Active.")
    lost = blocked.present(uuid4(), text)
    blocked.present(uuid4(), "Newest.")
    await ended(lost)
    for value in (record, lost):
        assert value is not None and "text" not in vars(value)
        assert text not in repr(vars(value))
    assert "text" not in SpeechOutputRecord.__dataclass_fields__
    for holder in (output, blocked):
        assert all(text not in repr(r) for r in holder.finished)
        assert all(isinstance(turn, UUID) for turn in holder.started_turns)
    assert output.active is None and output.pending is None and output._active is None
    assert text not in str(logs)
    await asyncio.gather(output.close(), blocked.close())
    assert not relay_tasks()


# --- U-W: identity, format and close ----------------------------------------------------


async def test_stale_output_frames_never_reach_the_sink() -> None:
    gate = asyncio.Event()
    provider, sink = SteppedTTS(), FakeSink(gate=gate)
    output = started(provider, sink)
    record = output.present(uuid4(), "Hello.")
    provider.chunks.put_nowait(pcm(480))
    await until(sink.waiting.is_set)
    assert output._run_state is not None
    stale = AudioFrame(data=pcm(480, 9000), format=provider.format, sequence=1)
    output._run_state.queue.put_nowait((uuid4(), stale))
    provider.chunks.put_nowait(pcm(480, 480))
    provider.chunks.put_nowait(None)
    gate.set()
    record = await ended(record)
    assert record.status == OutputStatus.COMPLETED and record.stale_frames == 1
    assert {output_id for output_id, _ in sink.accepted} == {record.speech_output_id}
    assert stale not in sink.frames and len(sink.accepted) == 2
    await output.close()


async def test_fake_sink_rejects_an_earlier_output_after_a_newer_one() -> None:
    sink = FakeSink()
    frame = AudioFrame(data=pcm(480), format=sink.format, sequence=1)
    old, new = uuid4(), uuid4()
    await sink.capture(frame, old)
    await sink.capture(frame, new)
    with pytest.raises(StaleOutputError):
        await sink.capture(frame, old)
    with pytest.raises(ValueError):
        await sink.capture(AudioFrame(data=pcm(480), format=AudioFormat(), sequence=2), new)


async def test_format_mismatch_fails_without_calling_tts() -> None:
    provider, sink = FakeTTS(frame_chunks(2)), FakeSink(sample_rate=16000)
    output = started(provider, sink)
    record = await ended(output.present(uuid4(), "Hello."))
    assert record.status == OutputStatus.FAILED and record.code == OutputFailure.INVALID_AUDIO
    assert provider.requests == [] and sink.accepted == []
    await output.close()


async def test_misaligned_final_audio_fails_after_valid_frames() -> None:
    provider, sink = FakeTTS([pcm(480) + b"\x01"]), FakeSink()
    output = started(provider, sink)
    record = await ended(output.present(uuid4(), "Hello."))
    assert record.code == OutputFailure.INVALID_AUDIO and record.frames_accepted == 1
    await output.close()


async def test_invalid_text_fails_without_calling_tts() -> None:
    provider, sink = FakeTTS(frame_chunks(1)), FakeSink()
    output = started(provider, sink)
    record = await ended(output.present(uuid4(), "   "))
    assert record.code == OutputFailure.INVALID_REQUEST and provider.requests == []
    await output.close()


async def test_close_is_idempotent_and_refuses_new_output() -> None:
    sink = FakeSink()
    output = started(FakeTTS(frame_chunks(1)), sink)
    await output.close()
    await output.close()
    assert sink.closes == 1 and output.present(uuid4(), "After.") is None
    assert not relay_tasks()


# --- Timeouts and connection ------------------------------------------------------------


async def test_connect_failure_makes_output_unavailable() -> None:
    provider, sink = FakeTTS(frame_chunks(1)), FakeSink(fail_connect=True)
    output = started(provider, sink)
    first = await ended(output.present(uuid4(), "Hello."))
    second = await ended(output.present(uuid4(), "Again."))
    assert first.code == second.code == OutputFailure.OUTPUT_UNAVAILABLE
    assert provider.requests == [] and sink.connects == 1
    await output.close()


async def test_first_audio_timeout_is_tts_unavailable() -> None:
    provider, sink = SteppedTTS(), FakeSink()
    output = started(provider, sink, first_audio_timeout=0.05)
    record = await ended(output.present(uuid4(), "Hello."))
    assert record.code == OutputFailure.TTS_UNAVAILABLE and sink.accepted == []
    await output.close()


async def test_stalled_sink_capture_times_out() -> None:
    provider, sink = FakeTTS(frame_chunks(3)), FakeSink(gate=asyncio.Event())
    output = started(provider, sink, capture_timeout=0.05)
    record = await ended(output.present(uuid4(), "Hello."))
    assert record.code == OutputFailure.PUBLISH_FAILED and provider.outcomes == ["COMPLETED"]
    await output.close()
    assert not relay_tasks()


# --- X-Y: independence and leaks --------------------------------------------------------


async def test_independent_outputs_run_concurrently() -> None:
    first_data, second_data = frame_chunks(30), frame_chunks(30, start=20000)
    a = started(FakeTTS(first_data), sink_a := FakeSink(delay=0.001))
    b = started(FakeTTS(second_data), sink_b := FakeSink(delay=0.001))
    records = await asyncio.gather(ended(a.present(uuid4(), "A.")), ended(b.present(uuid4(), "B.")))
    assert all(r.status == OutputStatus.COMPLETED for r in records)
    assert b"".join(f.data for f in sink_a.frames) == b"".join(first_data)
    assert b"".join(f.data for f in sink_b.frames) == b"".join(second_data)
    await asyncio.gather(a.close(), b.close())
    assert not relay_tasks()
