import asyncio
from collections.abc import AsyncGenerator
from uuid import UUID

from app.providers.tts.schemas import SpeechRequest
from app.realtime.media.protocol import AudioFormat, AudioFrame, MediaSinkClosed


class StaleOutputError(AssertionError):
    """A frame arrived for an output after a newer output had started."""


class FakeSink:
    """Deterministic MediaAudioSink: records accepted frames, gates, failures and closes.

    ``events`` is the ordered history of captures and clears: ("capture", id) when a capture
    begins, then ("captured", id) or ("cancelled", id); ("clear", None) for every clear.
    ``release_on_clear`` mimics LiveKit: clearing the native queue releases a blocked capture.
    """

    def __init__(
        self,
        sample_rate: int = 24000,
        *,
        gate: asyncio.Event | None = None,
        delay: float = 0,
        fail_after: int | None = None,
        media_end_after: int | None = None,
        fail_connect: bool = False,
        release_on_clear: bool = False,
        fail_clear: bool = False,
    ) -> None:
        self.format = AudioFormat(sample_rate=sample_rate)
        self.gate, self.delay = gate, delay
        self.fail_after, self.media_end_after = fail_after, media_end_after
        self.fail_connect = fail_connect
        self.release_on_clear, self.fail_clear = release_on_clear, fail_clear
        self.accepted: list[tuple[UUID, AudioFrame]] = []
        self.events: list[tuple[str, UUID | None]] = []
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
        self.events.append(("capture", speech_output_id))
        try:
            if self.gate is not None and not self.gate.is_set():
                self.waiting.set()
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.events.append(("cancelled", speech_output_id))
            raise
        self.accepted.append((speech_output_id, frame))
        self.events.append(("captured", speech_output_id))

    def cancelled(self, speech_output_id: UUID) -> bool:
        return ("cancelled", speech_output_id) in self.events

    async def clear(self) -> None:
        self.clears += 1
        self.events.append(("clear", None))
        if self.fail_clear:
            raise ConnectionError("clear failed")
        if self.release_on_clear and self.gate is not None:
            self.gate.set()

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


class StubbornTTS:
    """A provider slow to honour cancellation, on its first request only.

    The first stream yields ``first``, then waits for audio that never comes. It swallows the
    cancellation, waits for ``release`` and then tries to keep producing ``late``. Later
    requests stream ``chunks`` normally."""

    def __init__(self, first: bytes, late: list[bytes], chunks: list[bytes]) -> None:
        self.format = AudioFormat(sample_rate=24000)
        self.first, self.late, self.chunks = first, late, chunks
        self.requests: list[SpeechRequest] = []
        self.release = asyncio.Event()
        self.swallowed = self.late_yields = 0
        self.closed_after_release = False

    async def synthesize(self, request: SpeechRequest) -> AsyncGenerator[bytes, None]:
        self.requests.append(request)
        if len(self.requests) > 1:
            for chunk in self.chunks:
                yield chunk
            return
        yield self.first
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.swallowed += 1
            await self.release.wait()
        try:
            for chunk in self.late:
                self.late_yields += 1
                yield chunk
        except GeneratorExit:
            self.closed_after_release = True
            raise
