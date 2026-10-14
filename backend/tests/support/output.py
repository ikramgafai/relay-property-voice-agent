import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

from app.providers.tts.schemas import SpeechRequest
from app.realtime.media.protocol import AudioFormat, AudioFrame, MediaSinkClosed


class StaleOutputError(AssertionError):
    """A frame arrived for an output after a newer output had started."""


class FakeSink:
    """Deterministic MediaAudioSink: records accepted frames, gates, failures and closes."""

    def __init__(
        self,
        sample_rate: int = 24000,
        *,
        gate: asyncio.Event | None = None,
        delay: float = 0,
        fail_after: int | None = None,
        media_end_after: int | None = None,
        fail_connect: bool = False,
    ) -> None:
        self.format = AudioFormat(sample_rate=sample_rate)
        self.gate, self.delay = gate, delay
        self.fail_after, self.media_end_after = fail_after, media_end_after
        self.fail_connect = fail_connect
        self.accepted: list[tuple[UUID, AudioFrame]] = []
        self.connects = self.clears = self.closes = 0
        self.closed = False
        self.waiting = asyncio.Event()  # set when a capture is blocked at the gate
        self._current: UUID | None = None
        self._finished: set[UUID] = set()

    @property
    def frames(self) -> list[AudioFrame]:
        return [frame for _, frame in self.accepted]

    async def connect(self) -> None:
        self.connects += 1
        if self.fail_connect:
            raise ConnectionError("connect refused")

    async def capture(self, frame: AudioFrame, speech_output_id: UUID) -> None:
        if self.closed:
            raise MediaSinkClosed()
        if frame.format != self.format:
            raise ValueError("Frame format does not match the sink")
        if speech_output_id in self._finished:
            raise StaleOutputError(str(speech_output_id))
        if speech_output_id != self._current:
            if self._current is not None:
                self._finished.add(self._current)
            self._current = speech_output_id
        if self.fail_after is not None and len(self.accepted) >= self.fail_after:
            raise ConnectionError("transport failed")
        if self.media_end_after is not None and len(self.accepted) >= self.media_end_after:
            raise MediaSinkClosed()
        if self.gate is not None and not self.gate.is_set():
            self.waiting.set()
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.accepted.append((speech_output_id, frame))

    async def clear(self) -> None:
        self.clears += 1

    async def close(self) -> None:
        self.closes += 1
        self.closed = True


class SteppedTTS:
    """StreamingTTSProvider fed chunk by chunk from the test; ``None`` ends the stream."""

    def __init__(self, sample_rate: int = 24000) -> None:
        self.format = AudioFormat(sample_rate=sample_rate)
        self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        self.requests: list[SpeechRequest] = []

    async def synthesize(self, request: SpeechRequest) -> AsyncGenerator[bytes, None]:
        self.requests.append(request)
        while True:
            chunk = await self.chunks.get()
            if chunk is None:
                return
            yield chunk
