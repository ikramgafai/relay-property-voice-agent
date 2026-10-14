import asyncio
import json
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


async def open_adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[AssemblyAIStreaming, Wire, str]:
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
        Settings(assemblyai_api_key=SecretStr("test-only"), stt_language_codes=["en", "fr", "ar"])
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
