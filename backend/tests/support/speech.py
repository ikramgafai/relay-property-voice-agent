import asyncio
from collections.abc import AsyncGenerator
from uuid import uuid4

from app.providers.stt.schemas import SpeechContext, STTEvent
from app.realtime.media.protocol import AudioFrame


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
