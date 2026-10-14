import ast
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.providers.stt.schemas import SpeechContext
from app.realtime.errors import Backpressure
from app.realtime.media.protocol import AudioFrame
from app.realtime.schemas import Code, Error, SpeechFinal, SpeechPartial
from app.realtime.speech import SpeechPipeline
from pydantic import ValidationError

from tests.support.realtime import Socket, live_relay_tasks
from tests.support.speech import FakeMedia, FakeSTT
from tests.test_realtime import configured


async def test_audio_partial_final_duplicate_and_busy() -> None:
    app, manager, processor = configured()
    socket = Socket(app, uuid4())
    await socket.start()
    session = await manager.get(socket.session_id)
    assert session
    media, stt = FakeMedia(), FakeSTT()
    pipeline = SpeechPipeline(session, media, stt, SpeechContext())
    session.speech = pipeline
    pipeline.start()
    try:
        await stt.opened.wait()
        frame = AudioFrame(data=b"\x01\x00" * 800, sequence=1)
        await media.frames.put(frame)
        async with asyncio.timeout(2):
            assert await stt.audio.get() == frame
        await stt.emit("SPEECH_STARTED")
        assert (await socket.receive()).event_type == "USER_SPEECH_STARTED"
        await stt.emit("PARTIAL", "not finished")
        assert (await socket.receive()).event_type == "STT_PARTIAL"
        assert not processor.entered.is_set()
        await stt.emit("FINAL", "The actual request")
        async with asyncio.timeout(2):
            await processor.entered.wait()
        assert [(await socket.receive()).event_type for _ in range(4)] == [
            "USER_SPEECH_ENDED",
            "STT_FINAL",
            "SESSION_STATE_CHANGED",
            "TURN_STARTED",
        ]
        await stt.emit("FINAL", "The actual request")
        await stt.emit("FINAL", "Second request", order=1)
        await socket.receive()
        await socket.receive()
        error = (await socket.receive()).payload
        assert isinstance(error, Error) and error.code == Code.SESSION_BUSY
        assert pipeline.duplicates == 1
    finally:
        await socket.close()
    assert media.closed and stt.closed and not live_relay_tasks()


@pytest.mark.parametrize("reason", ["blank", "provider", "media", "control"])
async def test_speech_failures_never_promote_partials(reason: str) -> None:
    app, manager, processor = configured()
    socket = Socket(app, uuid4())
    await socket.start()
    session = await manager.get(socket.session_id)
    assert session
    media, stt = FakeMedia(), FakeSTT()
    pipeline = SpeechPipeline(session, media, stt, SpeechContext())
    session.speech = pipeline
    pipeline.start()
    await stt.opened.wait()
    await stt.emit("PARTIAL", "Please create an emergency incident")
    await socket.receive()
    if reason == "blank":
        await stt.emit("FINAL", "  ")
        await stt.emit("ERROR")
    elif reason == "provider":
        await stt.emit("ERROR")
    elif reason == "media":
        await media.frames.put(None)
    if reason != "control":
        assert (await socket.receive()).event_type == "STT_FAILED"
    await socket.close()
    assert not processor.entered.is_set()
    assert media.closed and stt.closed and not live_relay_tasks()


async def test_transient_flood_reserves_critical_space() -> None:
    app, manager, _ = configured()
    manager.queue_capacity = 3
    session = await manager.bind(uuid4())
    for revision in range(1000):
        await session.emit(
            SpeechPartial(speech_turn_id=uuid4(), transcript="partial", revision=revision)
        )
    assert session.outbound.items.qsize() == 1
    assert session.outbound.partial_drops == 999
    await session.error(Code.INTERNAL_ERROR)
    assert session.outbound.items.qsize() == 2 and not session.stopped.is_set()
    session.outbound.discard_on_close()
    await manager.release(session)


async def test_final_remains_critical_under_queue_pressure() -> None:
    _, manager, _ = configured()
    manager.queue_capacity, manager.queue_timeout = 1, 0.01
    session = await manager.bind(uuid4())
    await session.error(Code.INVALID_EVENT)
    final = SpeechFinal(
        speech_turn_id=uuid4(), turn_id=uuid4(), transcript="Final", provider="fake", model="test"
    )
    with pytest.raises(Backpressure):
        await session.emit(final, final.turn_id)
    assert session.reason.reason == "BACKPRESSURE"
    session.outbound.discard_on_close()
    await manager.release(session)


@pytest.mark.parametrize(
    "values",
    [
        {"voice_enabled": True},
        {"stt_language_codes": ["invalid"]},
        {"stt_min_turn_silence": 2000, "stt_max_turn_silence": 1000},
        {"livekit_url": "ws://secret@host"},
    ],
)
def test_voice_configuration_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]


def test_turn_silence_defaults_and_validation() -> None:
    settings = Settings()
    assert (settings.stt_min_turn_silence, settings.stt_max_turn_silence) == (800, 2000)
    with pytest.raises(ValidationError):
        Settings(stt_min_turn_silence=2500)  # above the 2000 ms maximum default


def test_multilingual_and_audio_contract() -> None:
    assert Settings(stt_language_codes=["en", "fr", "ar"]).stt_language_codes == ["en", "fr", "ar"]
    with pytest.raises(ValidationError):
        AudioFrame(data=b"x", sequence=1)


def test_business_contracts_have_no_vendor_imports() -> None:
    import app

    root = Path(app.__file__).parent
    for directory in ("agent", "routing", "workflows", "tools", "property"):
        for file in (root / directory).glob("*.py"):
            for node in ast.walk(ast.parse(file.read_text())):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith(
                        ("livekit", "assemblyai", "app.providers.stt.assemblyai")
                    )
