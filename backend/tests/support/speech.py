import asyncio
from collections.abc import AsyncGenerator
from typing import Literal
from uuid import uuid4

from app.providers.stt.schemas import SpeechContext, STTEvent
from app.providers.tts.schemas import SpeechRequest, TTSFailure
from app.realtime.media.protocol import AudioFormat, AudioFrame


class FakeMedia:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[AudioFrame | None] = asyncio.Queue(maxsize=8)
        self.closed = False

    async def __aiter__(self) -> AsyncGenerator[AudioFrame, None]:
        while not self.closed:
            frame = await self.frames.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        self.closed = True


class FakeSTT:
    def __init__(self) -> None:
        self.events: asyncio.Queue[STTEvent] = asyncio.Queue(maxsize=32)
        self.audio: asyncio.Queue[AudioFrame] = asyncio.Queue(maxsize=32)
        self.context: SpeechContext | None = None
        self.updates: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self.opened = asyncio.Event()
        self.closed = False
        self.stream_id = uuid4()

    async def open(self, context: SpeechContext) -> None:
        self.context = context
        self.opened.set()

    async def send(self, frame: AudioFrame) -> None:
        await self.audio.put(frame)

    async def receive(self) -> STTEvent:
        return await self.events.get()

    async def update_context(self, response_text: str) -> None:
        await self.updates.put(response_text)

    async def close(self) -> None:
        self.closed = True

    async def emit(self, kind: str, text: str = "", order: int = 0) -> None:
        await self.events.put(
            STTEvent.model_validate(
                dict(
                    kind=kind,
                    transcript=text,
                    turn_order=order,
                    stream_id=self.stream_id,
                    provider="fake",
                    model="deterministic",
                )
            )
        )


class FakeTTS:
    """Deterministic StreamingTTSProvider: scripted chunks, failures and gates."""

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        fail_after: int | None = None,
        sample_rate: int = 24000,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.format = AudioFormat(sample_rate=sample_rate)
        self.chunks = list(chunks or [])
        self.fail_after = fail_after  # 0 fails before the first chunk.
        self.gate = gate  # Awaited before every chunk when set.
        self.requests: list[SpeechRequest] = []
        self.yielded = 0
        self.outcomes: list[Literal["COMPLETED", "FAILED", "CLOSED"]] = []

    async def synthesize(self, request: SpeechRequest) -> AsyncGenerator[bytes, None]:
        self.requests.append(request)
        try:
            for index, chunk in enumerate(self.chunks):
                if self.fail_after == index:
                    raise TTSFailure("UNAVAILABLE")
                if self.gate is not None:
                    await self.gate.wait()
                self.yielded += 1
                yield chunk
            if self.fail_after is not None and self.fail_after >= len(self.chunks):
                raise TTSFailure("UNAVAILABLE")
        except TTSFailure:
            self.outcomes.append("FAILED")
            raise
        except (GeneratorExit, asyncio.CancelledError):
            self.outcomes.append("CLOSED")
            raise
        self.outcomes.append("COMPLETED")
