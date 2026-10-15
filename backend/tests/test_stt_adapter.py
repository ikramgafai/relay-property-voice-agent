import asyncio
import json
from collections.abc import Callable
from typing import cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.providers.stt import assemblyai
from app.providers.stt.assemblyai import AssemblyAIStreaming
from app.providers.stt.schemas import SpeechContext, STTFailure
from app.realtime.media.protocol import AudioFormat, AudioFrame
from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection


class Wire:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.closed = False

    async def recv(self) -> str:
        return await self.messages.get()

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data)["type"] == "Terminate":
            await self.messages.put('{"type":"Termination"}')

    async def close(self) -> None:
        self.closed = True


async def open_adapter(
    monkeypatch: pytest.MonkeyPatch, log: Callable[..., None] | None = None
) -> tuple[AssemblyAIStreaming, Wire, str]:
    wire = Wire()
    await wire.messages.put(json.dumps({"type": "Begin", "id": str(uuid4())}))
    uri = ""

    async def connect(url: str, **kwargs: object) -> ClientConnection:
        nonlocal uri
        uri = url
        assert kwargs["additional_headers"] == {"Authorization": "test-only"}
        assert kwargs["max_queue"] == 16
        return cast(ClientConnection, wire)

    monkeypatch.setattr(assemblyai, "connect", connect)
    adapter = AssemblyAIStreaming(
        Settings(assemblyai_api_key=SecretStr("test-only"), stt_language_codes=["en", "fr", "ar"]),
        log=log,
    )
    await adapter.open(SpeechContext(keyterms=["Palm Heights"], agent_response="Which service?"))
    return adapter, wire, uri


async def test_real_adapter_wire_semantics_context_and_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, wire, uri = await open_adapter(monkeypatch)
    params = parse_qs(urlsplit(uri).query)
    assert params["speech_model"] == ["universal-3-6-pro"]
    assert json.loads(params["language_codes"][0]) == ["en", "fr", "ar"]
    assert json.loads(params["keyterms_prompt"][0]) == ["Palm Heights"]
    assert params["agent_context"] == ["Which service?"]
    assert "test-only" not in uri
    await adapter.send(AudioFrame(data=b"\x00\x00" * 400, sequence=0))
    assert not wire.sent
    await adapter.send(AudioFrame(data=b"\x00\x00" * 400, sequence=1))
    assert len(wire.sent[0]) == 1600
    await wire.messages.put('{"type":"SpeechStarted","timestamp":123}')
    partial = {"type": "Turn", "turn_order": 7, "transcript": "I need", "end_of_turn": False}
    await wire.messages.put(json.dumps(partial))
    started = await adapter.receive()
    assert started.kind == "SPEECH_STARTED" and started.turn_order == 7
    assert started.audio_timestamp_ms == 123
    assert (await adapter.receive()).kind == "PARTIAL"
    final = dict(partial, transcript="I need maintenance", end_of_turn=True)
    await wire.messages.put(json.dumps(final))
    await wire.messages.put(json.dumps(final))
    one, two = await adapter.receive(), await adapter.receive()
    assert one == two and one.kind == "FINAL"
    await adapter.update_context("Trusted exact response")
    assert json.loads(cast(str, wire.sent[-1])) == {
        "type": "UpdateConfiguration",
        "agent_context": "Trusted exact response",
    }
    await adapter.update_context("x" * 2000)
    assert json.loads(cast(str, wire.sent[-1]))["agent_context"] == "x" * 1750
    await adapter.close()
    assert wire.closed and json.loads(cast(str, wire.sent[-1]))["type"] == "Terminate"


@pytest.mark.parametrize(
    "message",
    [
        '{"type":"Error","error":"SECRET"}',
        '{"type":"Turn","end_of_turn":true,"transcript":"unsafe"}',
        "{broken",
    ],
)
async def test_malformed_or_failed_provider_never_emits_final(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    adapter, wire, _ = await open_adapter(monkeypatch)
    try:
        await wire.messages.put(message)
        with pytest.raises(STTFailure) as error:
            await adapter.receive()
        assert "SECRET" not in str(error.value)
    finally:
        await adapter.close()


async def test_wrong_audio_format_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, _, _ = await open_adapter(monkeypatch)
    try:
        with pytest.raises(STTFailure):
            await adapter.send(
                AudioFrame(
                    data=b"\x00\x00" * 800, sequence=1, format=AudioFormat(sample_rate=48000)
                )
            )
    finally:
        await adapter.close()


async def connection_params(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> dict[str, list[str]]:
    wire = Wire()
    await wire.messages.put(json.dumps({"type": "Begin", "id": str(uuid4())}))
    uri = ""

    async def connect(url: str, **kwargs: object) -> ClientConnection:
        nonlocal uri
        uri = url
        return cast(ClientConnection, wire)

    monkeypatch.setattr(assemblyai, "connect", connect)
    adapter = AssemblyAIStreaming(settings)
    await adapter.open(SpeechContext())
    await adapter.close()
    return parse_qs(urlsplit(uri).query)


async def test_turn_detection_defaults_tolerate_mid_sentence_pauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = await connection_params(monkeypatch, Settings(assemblyai_api_key=SecretStr("k")))
    assert params["min_turn_silence"] == ["800"]
    assert params["max_turn_silence"] == ["2000"]
    # Unrelated STT configuration is unchanged.
    assert params["speech_model"] == ["universal-3-6-pro"]
    assert params["sample_rate"] == ["16000"] and params["encoding"] == ["pcm_s16le"]
    assert params["language_detection"] == ["true"]
    assert json.loads(params["language_codes"][0]) == ["en"]
    assert json.loads(params["keyterms_prompt"][0]) == []
    # Only parameters documented for universal-3-6-pro turn detection are sent.
    assert "end_of_turn_confidence_threshold" not in params and "mode" not in params


async def test_turn_detection_remains_explicitly_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        assemblyai_api_key=SecretStr("k"), stt_min_turn_silence=640, stt_max_turn_silence=1500
    )
    params = await connection_params(monkeypatch, settings)
    assert params["min_turn_silence"] == ["640"] and params["max_turn_silence"] == ["1500"]


async def test_only_provider_end_of_turn_becomes_final(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, wire, _ = await open_adapter(monkeypatch)
    for order, end_of_turn in ((1, False), (1, False), (1, True)):
        turn = {
            "type": "Turn",
            "turn_order": order,
            "transcript": "Wait",
            "end_of_turn": end_of_turn,
        }
        await wire.messages.put(json.dumps(turn))
    kinds = [(await adapter.receive()).kind for _ in range(3)]
    assert kinds == ["PARTIAL", "PARTIAL", "FINAL"]
    await adapter.close()


# --- Phase 10 diagnostics: speech-start hold and counts-only turn logs ----------------------


Logged = list[tuple[str, dict[str, object]]]


def recorder() -> tuple[Logged, Callable[..., None]]:
    logs: Logged = []
    return logs, lambda event, **fields: logs.append((event, fields))


def named(logs: Logged, name: str) -> list[dict[str, object]]:
    return [fields for event, fields in logs if event == name]


async def logged(logs: Logged, name: str, count: int = 1) -> dict[str, object]:
    for _ in range(1000):
        if len(named(logs, name)) >= count:
            return named(logs, name)[-1]
        await asyncio.sleep(0)
    raise AssertionError(f"{name} not logged {count} time(s)")


def fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [100.0]
    monkeypatch.setattr(assemblyai, "perf_counter", lambda: clock[0])
    return clock


async def test_raw_speech_start_is_logged_on_arrival_and_its_hold_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = fake_clock(monkeypatch)
    logs, log = recorder()
    adapter, wire, _ = await open_adapter(monkeypatch, log)
    for sequence in range(6):  # 4800 bytes = three 50 ms provider chunks
        await adapter.send(AudioFrame(data=b"\x00\x00" * 400, sequence=sequence))
    receive = asyncio.create_task(adapter.receive())
    clock[0] = 101.0
    await wire.messages.put('{"type":"SpeechStarted","timestamp":1234}')
    raw = await logged(logs, "ASSEMBLYAI_RAW_SPEECH_STARTED")
    # A. Logged when the raw message arrives, while nothing is exposed yet (current hold).
    assert not receive.done()
    assert raw == {
        "stream_id": str(adapter.stream_id),
        "since_stream_open_ms": pytest.approx(1000.0),
        "audio_sent_ms": pytest.approx(150.0),
        "audio_timestamp_ms": 1234,
        "replaced_waiting_speech_start": False,
    }
    assert adapter.speech_tick == 101.0  # B. monotonic arrival retained
    clock[0] = 101.75
    secret = "Actually tell me"
    turn = {
        "type": "Turn",
        "turn_order": 4,
        "transcript": secret,
        "end_of_turn": False,
        "language_code": "en",
        "words": [
            {"start": 1300, "end": 1500, "text": "Actually"},
            {"start": 1550, "end": 1700, "text": "tell"},
            {"start": 1720, "end": 1840, "text": "me"},
        ],
    }
    await wire.messages.put(json.dumps(turn))
    # C. Public behavior unchanged: SPEECH_STARTED first, then the pending PARTIAL.
    started = await receive
    assert started.kind == "SPEECH_STARTED" and started.turn_order == 4
    assert started.audio_timestamp_ms == 1234 and started.transcript == ""
    partial = await adapter.receive()
    assert partial.kind == "PARTIAL" and partial.transcript == secret
    assert partial.turn_order == 4 and partial.language == "en"
    # D. The hold is measured from raw arrival to release at the first Turn.
    received = named(logs, "ASSEMBLYAI_TURN_RECEIVED")[0]
    assert received == {
        "stream_id": str(adapter.stream_id),
        "since_stream_open_ms": pytest.approx(1750.0),
        "audio_sent_ms": pytest.approx(150.0),
        "turn_order": 4,
        "end_of_turn": False,
        "transcript_char_count": len(secret),
        "transcript_word_count": 3,
        "language": "en",
        "first_word_start_ms": 1300,
        "last_word_end_ms": 1840,
        "speech_start_waiting": True,
        "held_speech_start_ms": pytest.approx(750.0),
    }
    released = named(logs, "ASSEMBLYAI_SPEECH_START_RELEASED")[0]
    assert released["turn_order"] == 4 and released["audio_timestamp_ms"] == 1234
    assert released["held_speech_start_ms"] == pytest.approx(750.0)
    assert adapter.speech_tick is None and adapter.speech_timestamp is None
    assert [event for event, _ in logs] == [
        "ASSEMBLYAI_RAW_SPEECH_STARTED",
        "ASSEMBLYAI_TURN_RECEIVED",
        "ASSEMBLYAI_SPEECH_START_RELEASED",
    ]
    # E. No transcript text in any diagnostic log.
    assert "Actually" not in str(logs) and "tell" not in str(logs)
    await adapter.close()


async def test_partials_and_final_log_only_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    logs, log = recorder()
    adapter, wire, _ = await open_adapter(monkeypatch, log)
    texts = ["Lease", "Leasing info", "Leasing information please"]
    for text in texts:
        await wire.messages.put(
            json.dumps({"type": "Turn", "turn_order": 2, "transcript": text, "end_of_turn": False})
        )
    await wire.messages.put(
        json.dumps(
            {
                "type": "Turn",
                "turn_order": 2,
                "transcript": "Leasing information, please.",
                "end_of_turn": True,
            }
        )
    )
    events = [await adapter.receive() for _ in range(4)]
    # Public events are exactly as before: no speech start without a raw SpeechStarted.
    assert [e.kind for e in events] == ["PARTIAL", "PARTIAL", "PARTIAL", "FINAL"]
    assert [e.transcript for e in events[:3]] == texts
    turns = named(logs, "ASSEMBLYAI_TURN_RECEIVED")
    assert [t["transcript_word_count"] for t in turns] == [1, 2, 3, 3]
    assert [t["transcript_char_count"] for t in turns] == [5, 12, 26, 28]
    assert [t["end_of_turn"] for t in turns] == [False, False, False, True]
    assert all(t["speech_start_waiting"] is False for t in turns)
    assert all(t["held_speech_start_ms"] is None for t in turns)
    assert all(t["first_word_start_ms"] is None for t in turns)  # no word timings sent
    assert not named(logs, "ASSEMBLYAI_SPEECH_START_RELEASED")
    assert "Leas" not in str(logs) and "please" not in str(logs)
    await adapter.close()


async def test_repeated_speech_start_keeps_current_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = fake_clock(monkeypatch)
    logs, log = recorder()
    adapter, wire, _ = await open_adapter(monkeypatch, log)
    receive = asyncio.create_task(adapter.receive())
    await wire.messages.put('{"type":"SpeechStarted","timestamp":500}')
    await logged(logs, "ASSEMBLYAI_RAW_SPEECH_STARTED")
    clock[0] = 100.4
    await wire.messages.put('{"type":"SpeechStarted","timestamp":900}')
    await logged(logs, "ASSEMBLYAI_RAW_SPEECH_STARTED", count=2)
    clock[0] = 100.5
    await wire.messages.put(
        json.dumps({"type": "Turn", "turn_order": 1, "transcript": "x", "end_of_turn": False})
    )
    started = await receive
    assert started.kind == "SPEECH_STARTED" and started.audio_timestamp_ms == 900  # as before
    assert (await adapter.receive()).kind == "PARTIAL"
    raws = named(logs, "ASSEMBLYAI_RAW_SPEECH_STARTED")
    assert [r["replaced_waiting_speech_start"] for r in raws] == [False, True]
    released = named(logs, "ASSEMBLYAI_SPEECH_START_RELEASED")[0]
    assert released["held_speech_start_ms"] == pytest.approx(100.0)  # from the latest
    await adapter.close()


async def test_unexpected_word_timings_never_change_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs, log = recorder()
    adapter, wire, _ = await open_adapter(monkeypatch, log)
    await wire.messages.put(
        json.dumps(
            {
                "type": "Turn",
                "turn_order": 3,
                "transcript": "Hello",
                "end_of_turn": False,
                "words": "unexpected",
            }
        )
    )
    event = await adapter.receive()
    assert event.kind == "PARTIAL" and event.transcript == "Hello"
    turn = named(logs, "ASSEMBLYAI_TURN_RECEIVED")[0]
    assert turn["first_word_start_ms"] is None and turn["last_word_end_ms"] is None
    await adapter.close()
