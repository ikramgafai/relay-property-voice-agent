"""Ephemeral speech presentation: trusted text → TTS → bounded frames → media sink.

Presentation only. This module never sees business results, workflow state or tools; a
presentation failure is never a business failure. Nothing here is persisted.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from uuid import UUID, uuid4

import structlog
from pydantic import ValidationError

from app.providers.tts.protocol import StreamingTTSProvider
from app.providers.tts.schemas import SpeechRequest, TTSFailure
from app.realtime.media.pcm import PcmAlignmentError, PcmFramer
from app.realtime.media.protocol import AudioFrame, MediaAudioSink, MediaSinkClosed

FRAME_MS = 20
BUFFER_FRAMES = 25  # 500 ms of Python-side audio ahead of the sink
STARTED_TURNS = 64  # bounded history of turns whose presentation actually started
FINISHED_RECORDS = 64


class OutputStatus(StrEnum):
    PENDING = "PENDING"
    SYNTHESIZING = "SYNTHESIZING"
    OUTPUTTING = "OUTPUTTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class OutputFailure(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    TTS_UNAVAILABLE = "TTS_UNAVAILABLE"
    TTS_INTERRUPTED = "TTS_INTERRUPTED"
    INVALID_AUDIO = "INVALID_AUDIO"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    MEDIA_ENDED = "MEDIA_ENDED"
    OUTPUT_UNAVAILABLE = "OUTPUT_UNAVAILABLE"
    SESSION_ENDED = "SESSION_ENDED"


TERMINAL = {
    OutputStatus.COMPLETED,
    OutputStatus.FAILED,
    OutputStatus.CANCELLED,
    OutputStatus.SUPERSEDED,
}


@dataclass(eq=False)
class SpeechOutputRecord:
    """Diagnostics for one presentation attempt. Times are perf_counter seconds.

    Holds identity, timing and counters only; the spoken text never lives here."""

    turn_id: UUID
    provider: str | None = None
    model: str | None = None
    speech_output_id: UUID = field(default_factory=uuid4)
    status: OutputStatus = OutputStatus.PENDING
    code: OutputFailure | None = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    created: float = field(default_factory=perf_counter)
    tts_requested: float | None = None
    tts_first_audio_received: float | None = None
    tts_completed: float | None = None
    first_frame_accepted_by_sink: float | None = None
    output_drained: float | None = None
    ended_at: float | None = None
    frames_accepted: int = 0
    accepted_audio_ms: float = 0.0
    buffer_high_water: int = 0
    underruns: int = 0
    stale_frames: int = 0
    ended: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


@dataclass(eq=False)
class _Job:
    """Scheduled work. The only holder of trusted text, dropped once the job ends."""

    record: SpeechOutputRecord
    text: str = field(repr=False)


@dataclass(eq=False)
class _Run:
    queue: asyncio.Queue[tuple[UUID, AudioFrame] | None]
    synthesis_done: bool = False
    code: OutputFailure | None = None


class SpeechOutput:
    """Owns presentation tasks for one voice attachment: one active output, one pending."""

    def __init__(
        self,
        provider: StreamingTTSProvider,
        sink: MediaAudioSink,
        *,
        provider_name: str | None = None,
        model: str | None = None,
        first_audio_timeout: float = 5,
        chunk_timeout: float = 5,
        capture_timeout: float = 5,
        connect_timeout: float = 10,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.provider, self.sink = provider, sink
        self.provider_name, self.model = provider_name, model
        self.first_audio_timeout, self.chunk_timeout = first_audio_timeout, chunk_timeout
        self.capture_timeout, self.connect_timeout = capture_timeout, connect_timeout
        self._log = log or structlog.get_logger().info
        self.attachment_id = uuid4()
        self.available = True
        self.closed = False
        self.finished: deque[SpeechOutputRecord] = deque(maxlen=FINISHED_RECORDS)
        self.started_turns: deque[UUID] = deque(maxlen=STARTED_TURNS)
        self._active: _Job | None = None
        self._pending: _Job | None = None
        self._run_state: _Run | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._close_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Speech output already started")
        self._task = asyncio.create_task(self._run(), name=f"relay-output-{self.attachment_id}")

    @property
    def active(self) -> SpeechOutputRecord | None:
        return None if self._active is None else self._active.record

    @property
    def pending(self) -> SpeechOutputRecord | None:
        return None if self._pending is None else self._pending.record

    def present(self, turn_id: UUID, text: str) -> SpeechOutputRecord | None:
        """Non-blocking. Refuses a turn that is scheduled now or whose presentation
        already started; a turn superseded before it started was never presented."""
        if self.closed:
            return None
        scheduled = {job.record.turn_id for job in (self._active, self._pending) if job}
        if turn_id in scheduled or turn_id in self.started_turns:
            self._log("SPEECH_OUTPUT_DUPLICATE_REFUSED", turn_id=str(turn_id))
            return None
        record = SpeechOutputRecord(turn_id, self.provider_name, self.model)
        if not self.available:
            self._finish(record, OutputStatus.FAILED, OutputFailure.OUTPUT_UNAVAILABLE)
            return record
        if self._pending is not None:
            self._finish(self._pending.record, OutputStatus.SUPERSEDED)
        self._pending = _Job(record, text)
        self._wake.set()
        return record

    async def close(self) -> None:
        async with self._close_lock:
            if self.closed:
                return
            self.closed = True
            if self._pending is not None:
                record = self._pending.record
                self._pending = None
                self._finish(record, OutputStatus.CANCELLED, OutputFailure.SESSION_ENDED)
            task = self._task
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            try:
                async with asyncio.timeout(5):
                    await self.sink.close()
            except Exception:
                pass

    async def _run(self) -> None:
        try:
            async with asyncio.timeout(self.connect_timeout):
                await self.sink.connect()
        except Exception:
            self._unavailable()
            return
        while True:
            await self._wake.wait()
            self._wake.clear()
            self._active, self._pending = self._pending, None
            if self._active is None:
                continue
            try:
                if self.available:
                    await self._present(self._active)
                else:
                    self._finish(
                        self._active.record,
                        OutputStatus.FAILED,
                        OutputFailure.OUTPUT_UNAVAILABLE,
                    )
            finally:
                self._active = None  # releases the text

    def _unavailable(self) -> None:
        self.available = False
        if self._pending is not None:
            record = self._pending.record
            self._pending = None
            self._finish(record, OutputStatus.FAILED, OutputFailure.OUTPUT_UNAVAILABLE)

    async def _present(self, job: _Job) -> None:
        record = job.record
        try:
            request = SpeechRequest(speech_output_id=record.speech_output_id, text=job.text)
        except ValidationError:
            self._finish(record, OutputStatus.FAILED, OutputFailure.INVALID_REQUEST)
            return
        if self.provider.format != self.sink.format:
            self._finish(record, OutputStatus.FAILED, OutputFailure.INVALID_AUDIO)
            return
        run = _Run(asyncio.Queue(maxsize=BUFFER_FRAMES))
        self._run_state = run
        name = record.speech_output_id
        producer = asyncio.create_task(
            self._synthesize(record, request, run), name=f"relay-tts-{name}"
        )
        consumer = asyncio.create_task(
            self._output(record, run), name=f"relay-output-frames-{name}"
        )
        try:
            await consumer
        except asyncio.CancelledError:
            for task in (producer, consumer):
                task.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
            self._finish(record, OutputStatus.CANCELLED, OutputFailure.SESSION_ENDED)
            raise
        except Exception as exc:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            self._unavailable()
            code = (
                OutputFailure.MEDIA_ENDED
                if isinstance(exc, MediaSinkClosed)
                else OutputFailure.PUBLISH_FAILED
            )
            self._finish(record, OutputStatus.FAILED, code)
            return
        finally:
            self._run_state = None
        await asyncio.gather(producer, return_exceptions=True)
        if run.code is not None:
            self._finish(record, OutputStatus.FAILED, run.code)
        else:
            self._finish(record, OutputStatus.COMPLETED)

    async def _synthesize(
        self, record: SpeechOutputRecord, request: SpeechRequest, run: _Run
    ) -> None:
        """Producer. Converts failures into a safe code, then always ends the frame stream."""
        try:
            await self._stream(record, request, run)
        except asyncio.CancelledError:
            raise
        except PcmAlignmentError:
            run.code = OutputFailure.INVALID_AUDIO
        except TTSFailure as failure:
            run.code = (
                OutputFailure.INVALID_AUDIO
                if failure.reason == "INVALID_AUDIO"
                else self._interrupted(record)
            )
        except Exception:
            run.code = self._interrupted(record)
        run.synthesis_done = True
        await run.queue.put(None)

    @staticmethod
    def _interrupted(record: SpeechOutputRecord) -> OutputFailure:
        if record.tts_first_audio_received is None:
            return OutputFailure.TTS_UNAVAILABLE
        return OutputFailure.TTS_INTERRUPTED

    async def _stream(self, record: SpeechOutputRecord, request: SpeechRequest, run: _Run) -> None:
        framer = PcmFramer(self.provider.format, FRAME_MS)
        record.status = OutputStatus.SYNTHESIZING
        record.tts_requested = perf_counter()
        # From here the presentation has started: never presented automatically again.
        self.started_turns.append(record.turn_id)
        async with aclosing(self.provider.synthesize(request)) as stream:
            while True:
                limit = (
                    self.first_audio_timeout
                    if record.tts_first_audio_received is None
                    else self.chunk_timeout
                )
                try:
                    async with asyncio.timeout(limit):
                        chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    raise TTSFailure("TIMEOUT") from None
                if chunk and record.tts_first_audio_received is None:
                    record.tts_first_audio_received = perf_counter()
                for frame in framer.push(chunk):
                    await self._enqueue(record, run, frame)
        record.tts_completed = perf_counter()
        run.synthesis_done = True
        for frame in framer.finish():
            await self._enqueue(record, run, frame)

    @staticmethod
    async def _enqueue(record: SpeechOutputRecord, run: _Run, frame: AudioFrame) -> None:
        # Waits when 25 frames are buffered: the provider stream is not read meanwhile.
        await run.queue.put((record.speech_output_id, frame))
        record.buffer_high_water = max(record.buffer_high_water, run.queue.qsize())

    async def _output(self, record: SpeechOutputRecord, run: _Run) -> None:
        """Consumer. Only frames tagged with this record's identity reach the sink."""
        while True:
            # Underrun: the sink path needs a frame, none is buffered and synthesis is still
            # running. The wait before the first frame is not counted.
            if run.queue.empty() and record.frames_accepted and not run.synthesis_done:
                record.underruns += 1
            item = await run.queue.get()
            if item is None:
                break
            output_id, frame = item
            if output_id != record.speech_output_id:
                record.stale_frames += 1
                continue
            async with asyncio.timeout(self.capture_timeout):
                await self.sink.capture(frame, output_id)
            if record.first_frame_accepted_by_sink is None:
                record.first_frame_accepted_by_sink = perf_counter()
                record.status = OutputStatus.OUTPUTTING
            record.frames_accepted += 1
            record.accepted_audio_ms += len(frame.data) / 2 / frame.format.sample_rate * 1000
        record.output_drained = perf_counter()

    def _finish(
        self, record: SpeechOutputRecord, status: OutputStatus, code: OutputFailure | None = None
    ) -> None:
        if record.terminal:
            return
        record.status, record.code = status, code
        record.ended_at = perf_counter()
        record.ended.set()
        self.finished.append(record)

        def since(tick: float | None) -> float | None:
            return None if tick is None else (tick - record.created) * 1000

        self._log(
            "SPEECH_OUTPUT_ENDED",
            speech_output_id=str(record.speech_output_id),
            turn_id=str(record.turn_id),
            provider=record.provider,
            model=record.model,
            status=status.value,
            code=code.value if code else None,
            tts_requested_ms=since(record.tts_requested),
            tts_first_audio_received_ms=since(record.tts_first_audio_received),
            first_frame_accepted_by_sink_ms=since(record.first_frame_accepted_by_sink),
            tts_completed_ms=since(record.tts_completed),
            output_drained_ms=since(record.output_drained),
            ended_ms=since(record.ended_at),
            frames_accepted=record.frames_accepted,
            accepted_audio_ms=record.accepted_audio_ms,
            buffer_high_water=record.buffer_high_water,
            underruns=record.underruns,
            stale_frames=record.stale_frames,
        )
