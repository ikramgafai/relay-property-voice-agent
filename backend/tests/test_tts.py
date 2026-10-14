import ast
import random
from contextlib import aclosing
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.providers.tts.protocol import StreamingTTSProvider
from app.providers.tts.schemas import MAX_SPEECH_TEXT, SpeechRequest, TTSFailure
from app.realtime.media.pcm import PcmAlignmentError, PcmFramer
from app.realtime.media.protocol import AudioFormat
from pydantic import SecretStr, ValidationError

from tests.support.speech import FakeTTS

PCM_24K = AudioFormat(sample_rate=24000)
FRAME = 960  # 20 ms of 24 kHz mono PCM16


def request(text: str = "Your request was created.") -> SpeechRequest:
    return SpeechRequest(speech_output_id=uuid4(), text=text)


def pcm(samples: int, start: int = 0) -> bytes:
    """Distinct little-endian samples so any loss, duplication or reorder is visible."""
    return b"".join(((start + i) % 65536).to_bytes(2, "little") for i in range(samples))


async def collect(provider: StreamingTTSProvider, value: SpeechRequest) -> list[bytes]:
    async with aclosing(provider.synthesize(value)) as stream:
        return [chunk async for chunk in stream]


# --- FakeTTS (A-D) -----------------------------------------------------------------------


async def test_fake_tts_emits_chunks_in_order() -> None:
    chunks = [b"\x01\x00", b"\x02\x00\x03", b"\x00"]
    provider: StreamingTTSProvider = FakeTTS(chunks)
    value = request()
    assert await collect(provider, value) == chunks
    assert isinstance(provider, FakeTTS)
    assert provider.requests == [value] and provider.outcomes == ["COMPLETED"]


async def test_fake_tts_fails_before_first_chunk() -> None:
    provider = FakeTTS([b"\x01\x00"], fail_after=0)
    received: list[bytes] = []
    with pytest.raises(TTSFailure) as failure:
        async with aclosing(provider.synthesize(request())) as stream:
            async for chunk in stream:
                received.append(chunk)
    assert failure.value.reason == "UNAVAILABLE"
    assert received == [] and provider.yielded == 0 and provider.outcomes == ["FAILED"]


async def test_fake_tts_fails_after_partial_output() -> None:
    provider = FakeTTS([b"\x01\x00", b"\x02\x00", b"\x03\x00"], fail_after=2)
    received: list[bytes] = []
    with pytest.raises(TTSFailure):
        async with aclosing(provider.synthesize(request())) as stream:
            async for chunk in stream:
                received.append(chunk)
    assert received == [b"\x01\x00", b"\x02\x00"] and provider.outcomes == ["FAILED"]


async def test_fake_tts_stream_closes_cleanly() -> None:
    provider = FakeTTS([b"\x01\x00", b"\x02\x00", b"\x03\x00"])
    async with aclosing(provider.synthesize(request())) as stream:
        assert await anext(stream) == b"\x01\x00"
    assert provider.yielded == 1 and provider.outcomes == ["CLOSED"]


# --- Contracts ---------------------------------------------------------------------------


def test_speech_request_carries_only_presentation_text() -> None:
    value = SpeechRequest(speech_output_id=uuid4(), text="  Hello.  ", language="en")
    assert value.text == "Hello." and value.language == "en"
    for invalid in (
        {"text": "   "},
        {"text": "x" * (MAX_SPEECH_TEXT + 1)},
        {"text": "Hello.", "language": "en_US"},
        {"text": "Hello.", "tool_results": []},
        {"text": "Hello.", "turn_id": str(uuid4())},
    ):
        with pytest.raises(ValidationError):
            SpeechRequest.model_validate({"speech_output_id": str(uuid4()), **invalid})
    assert SpeechRequest(speech_output_id=uuid4(), text="x" * MAX_SPEECH_TEXT)


@pytest.mark.parametrize(
    "tag", ["en", "fr", "ar", "fil", "en-US", "en-GB", "ar-MA", "fr-FR", "es-419", "zh-Hant-TW"]
)
def test_language_tag_accepted_and_preserved(tag: str) -> None:
    assert SpeechRequest(speech_output_id=uuid4(), text="Hello.", language=tag).language == tag


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "e",
        "english",
        "en_US",
        "en-",
        "-US",
        "en--US",
        "en-USA",
        "en-1234",
        "en-US-",
        "en-US-x-private",
        "en US",
        "en-US;drop",
        "ar-\nMA",
        "zh-Hant-TW-extra",
    ],
)
def test_malformed_language_tag_rejected(tag: str) -> None:
    with pytest.raises(ValidationError):
        SpeechRequest(speech_output_id=uuid4(), text="Hello.", language=tag)


def test_tts_failure_exposes_only_a_safe_reason() -> None:
    failure = TTSFailure("TIMEOUT")
    assert failure.reason == "TIMEOUT" and str(failure) == "TIMEOUT"


# --- PCM framing (E-L) -------------------------------------------------------------------


def test_pcm_24k_mono_format_and_frame_size() -> None:
    assert (PCM_24K.sample_rate, PCM_24K.channels, PCM_24K.encoding) == (24000, 1, "pcm_s16le")
    framer = PcmFramer(PCM_24K)
    assert framer.frame_bytes == FRAME == 480 * 2
    with pytest.raises(ValueError):
        PcmFramer(PCM_24K, frame_ms=5)
    with pytest.raises(ValueError):
        PcmFramer(AudioFormat(sample_rate=22050), frame_ms=10)  # 220.5 samples


def test_exact_frame_produces_one_frame() -> None:
    framer = PcmFramer(PCM_24K)
    data = pcm(480)
    frames = framer.push(data)
    assert [f.data for f in frames] == [data] and framer.pending_bytes == 0
    assert frames[0].format == PCM_24K and frames[0].sequence == 1
    assert framer.finish() == []


def test_multiple_frames_in_one_chunk() -> None:
    framer = PcmFramer(PCM_24K)
    data = pcm(480 * 3 + 10)
    frames = framer.push(data)
    assert [len(f.data) for f in frames] == [FRAME] * 3 and [f.sequence for f in frames] == [
        1,
        2,
        3,
    ]
    assert framer.pending_bytes == 20
    tail = framer.finish()
    assert [len(f.data) for f in tail] == [20] and tail[0].sequence == 4
    assert b"".join(f.data for f in frames + tail) == data


def test_one_frame_reassembled_from_many_chunks() -> None:
    framer = PcmFramer(PCM_24K)
    data = pcm(480)
    frames = []
    for offset in range(0, FRAME, 7):  # odd-sized slices split samples
        frames += framer.push(data[offset : offset + 7])
    assert [f.data for f in frames] == [data]


def test_odd_byte_boundaries_carry_safely() -> None:
    framer = PcmFramer(PCM_24K)
    data = pcm(480 + 3)
    first = framer.push(data[:1])
    assert first == [] and framer.pending_bytes == 1
    frames = framer.push(data[1 : FRAME + 1]) + framer.push(data[FRAME + 1 :]) + framer.finish()
    assert b"".join(f.data for f in frames) == data
    assert [len(f.data) for f in frames] == [FRAME, 6]


def test_short_final_remainder_is_not_padded() -> None:
    framer = PcmFramer(PCM_24K)
    data = pcm(100)
    assert framer.push(data) == []
    tail = framer.finish()
    assert len(tail) == 1 and tail[0].data == data and len(tail[0].data) == 200
    with pytest.raises(RuntimeError):
        framer.push(b"\x00\x00")


def test_odd_final_byte_fails_without_inventing_audio() -> None:
    framer = PcmFramer(PCM_24K)
    frames = framer.push(pcm(480) + b"\x7f")
    assert len(frames) == 1
    with pytest.raises(PcmAlignmentError):
        framer.finish()
    assert framer.pending_bytes == 0 and framer.finished


@pytest.mark.parametrize("seed", range(25))
def test_arbitrary_chunk_boundaries_preserve_exact_samples(seed: int) -> None:
    rng = random.Random(seed)
    data = pcm(rng.randrange(1, 480 * 12), start=seed * 1000)
    framer = PcmFramer(PCM_24K)
    frames, offset = [], 0
    while offset < len(data):
        size = rng.choice([1, 2, 3, 7, 959, 960, 961, 1500, 4800, rng.randrange(1, 6000)])
        frames += framer.push(data[offset : offset + size])
        offset += size
    frames += framer.finish()
    assert b"".join(f.data for f in frames) == data
    assert all(len(f.data) == FRAME for f in frames[:-1]) and 0 < len(frames[-1].data) <= FRAME
    assert [f.sequence for f in frames] == list(range(1, len(frames) + 1))


async def test_fake_provider_stream_frames_end_to_end() -> None:
    data = pcm(480 * 2 + 50)
    provider = FakeTTS([data[:5], data[5:1001], data[1001:]])
    framer = PcmFramer(provider.format)
    frames = []
    async with aclosing(provider.synthesize(request())) as stream:
        async for chunk in stream:
            frames += framer.push(chunk)
    frames += framer.finish()
    assert b"".join(f.data for f in frames) == data


# --- Configuration (M-N) -----------------------------------------------------------------


def test_tts_disabled_requires_no_provider_configuration() -> None:
    settings = Settings()
    assert not settings.tts_enabled and settings.elevenlabs_api_key is None
    assert settings.tts_voice_id is None and settings.tts_model == "eleven_flash_v2_5"
    assert not Settings(voice_enabled=False, tts_enabled=False).tts_enabled


def voice() -> dict[str, object]:
    return {
        "voice_enabled": True,
        "livekit_url": "ws://livekit:7880",
        "livekit_api_key": SecretStr("devkey"),
        "livekit_api_secret": SecretStr("secret"),
        "assemblyai_api_key": SecretStr("stt-test-key"),
    }


@pytest.mark.parametrize(
    "values",
    [
        {"tts_enabled": True},
        {"tts_enabled": True, "elevenlabs_api_key": "k", "tts_voice_id": "Voice1"},
        {**voice(), "tts_enabled": True, "tts_voice_id": "Voice1"},
        {**voice(), "tts_enabled": True, "elevenlabs_api_key": "k"},
        {**voice(), "tts_enabled": True, "elevenlabs_api_key": "  ", "tts_voice_id": "Voice1"},
        {"tts_voice_id": "../voices/x"},
        {"tts_voice_id": "voice id"},
        {"tts_model": "eleven_v4_turbo"},
    ],
)
def test_tts_configuration_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(**values)  # type: ignore[arg-type]
    assert "stt-test-key" not in str(error.value)


def test_tts_enabled_configuration_accepted_and_secret_hidden() -> None:
    settings = Settings(
        **voice(),  # type: ignore[arg-type]
        tts_enabled=True,
        elevenlabs_api_key=SecretStr("tts-secret-value"),
        tts_voice_id="Voice123",
    )
    assert settings.tts_enabled and settings.tts_voice_id == "Voice123"
    assert "tts-secret-value" not in repr(settings) and "tts-secret-value" not in str(settings)


# --- Boundaries (O) ----------------------------------------------------------------------


def imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_tts_dependency_boundaries_and_no_runtime_wiring() -> None:
    import app

    root = Path(app.__file__).parent
    allowed = {"app.property.schemas", "app.realtime.media.protocol"}
    for file in (root / "providers" / "tts").glob("*.py"):
        app_imports = {m for m in imports(file) if m.startswith("app.")}
        assert app_imports <= allowed | {"app.providers.tts.schemas"}, file.name
        # The neutral contract is transport-free; only the concrete adapter may use httpx.
        transport = (
            ("livekit", "elevenlabs") if file.name == "elevenlabs.py" else ("livekit", "httpx")
        )
        assert not any(m.startswith(transport) for m in imports(file)), file.name
    assert {m for m in imports(root / "realtime/media/pcm.py") if m.startswith("app.")} == {
        "app.realtime.media.protocol"
    }
    assert not any(
        m.startswith(("livekit", "asyncio")) for m in imports(root / "realtime/media/pcm.py")
    )
    # Step 7A: only the composition root builds the TTS adapter; sessions, the input
    # pipeline and voice admission never import providers or PCM framing directly.
    for runtime in ("realtime/session.py", "realtime/speech.py", "api/voice.py"):
        assert not any(
            m.startswith(("app.providers.tts", "app.realtime.media.pcm"))
            for m in imports(root / runtime)
        ), runtime
    lifecycle = {
        m for m in imports(root / "core/lifecycle.py") if m.startswith("app.providers.tts")
    }
    assert lifecycle == {"app.providers.tts.elevenlabs"}
