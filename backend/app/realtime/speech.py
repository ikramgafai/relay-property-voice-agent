"""Audio and recognition execution; final text stops at RealtimeSession."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from time import perf_counter
from uuid import UUID, uuid4, uuid5

from app.providers.stt.protocol import StreamingSTTProvider
from app.providers.stt.schemas import SpeechContext, STTFailure
from app.realtime.media.protocol import AudioFrame, MediaAudioSource
from app.realtime.schemas import SpeechFailed, SpeechFinal, SpeechPartial, SpeechSignal
from app.realtime.session import RealtimeSession


class SpeechPipeline:
    def __init__(
        self,
        session: RealtimeSession,
        source: MediaAudioSource,
        provider: StreamingSTTProvider,
        context: SpeechContext,
    ) -> None:
        self.session, self.source, self.provider, self.context = session, source, provider, context
        self.media_session_id = uuid4()
        self.task: asyncio.Task[None] | None = None
        self.contexts: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
        self.failed = asyncio.Event()
        self.closed = False
        self.last_final = -1
        self.stream_id: UUID | None = None
        self.partials = self.finals = self.duplicates = self.failures = 0
        self.first_audio: float | None = None
        self.last_audio: float | None = None
        self.started = perf_counter()

    def start(self) -> None:
        if self.task is not None:
            raise RuntimeError("Speech already started")
        self.task = asyncio.create_task(self.run(), name=f"relay-speech-{self.media_session_id}")

    def on_response(self, response_text: str) -> None:
        if self.closed:
            return
        try:
            self.contexts.put_nowait(response_text)
        except asyncio.QueueFull:
            self.failed.set()

    def log(self, event: str, **fields: object) -> None:
        self.session.log(event, media_session_id=str(self.media_session_id), **fields)

    async def run(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        failure = False
        try:
            async with asyncio.timeout(15):
                await self.provider.open(self.context)
            self.log("STT_STREAM_OPENED")
            tasks = [
                asyncio.create_task(self.audio(), name="relay-audio-input"),
                asyncio.create_task(self.events(), name="relay-stt-events"),
                asyncio.create_task(self.updates(), name="relay-stt-context"),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = True
            self.failures += 1
        finally:
            self.closed = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            while not self.contexts.empty():
                self.contexts.get_nowait()
                self.contexts.task_done()
            for resource in (self.provider, self.source):
                try:
                    async with asyncio.timeout(5):
                        await resource.close()
                except Exception:
                    self.failures += 1
            if not self.session.stopped.is_set():
                try:
                    await self.session.emit(
                        SpeechFailed(code="STREAM_FAILED" if failure else "MEDIA_ENDED")
                    )
                except Exception:
                    pass  # Critical queue failure already requests connection termination.
            self.log(
                "STT_STREAM_CLOSED",
                duration_ms=(perf_counter() - self.started) * 1000,
                partials=self.partials,
                finals=self.finals,
                duplicates=self.duplicates,
                failures=self.failures,
                partial_drops=self.session.outbound.partial_drops,
            )

    async def audio(self) -> None:
        async with aclosing(self.source.__aiter__()) as frames:
            await self.send_frames(frames)

    async def send_frames(self, frames: AsyncIterator[AudioFrame]) -> None:
        async for frame in frames:
            now = perf_counter()
            if self.first_audio is None:
                self.first_audio = now
                self.log("FIRST_AUDIO_RECEIVED")
            self.last_audio = now
            async with asyncio.timeout(5):
                await self.provider.send(frame)

    async def updates(self) -> None:
        while True:
            if self.failed.is_set():
                raise STTFailure()
            response = await self.contexts.get()
            try:
                async with asyncio.timeout(5):
                    await self.provider.update_context(response)
            finally:
                self.contexts.task_done()

    async def events(self) -> None:
        while True:
            event = await self.provider.receive()
            if event.kind == "ERROR":
                raise STTFailure()
            if self.stream_id is None:
                self.stream_id = event.stream_id
            if event.stream_id != self.stream_id:
                raise STTFailure()
            speech_id = uuid5(event.stream_id, str(event.turn_order))
            if event.kind == "SPEECH_STARTED":
                await self.session.emit(
                    SpeechSignal(
                        kind="USER_SPEECH_STARTED",
                        speech_turn_id=speech_id,
                        audio_timestamp_ms=event.audio_timestamp_ms,
                    )
                )
                continue
            if event.turn_order <= self.last_final:
                if event.kind == "FINAL":
                    self.duplicates += 1
                continue
            text = " ".join(event.transcript.split())
            if event.kind == "PARTIAL":
                self.partials += 1
                if self.partials == 1 and self.first_audio is not None:
                    self.log(
                        "STT_FIRST_PARTIAL",
                        first_audio_to_first_partial_ms=(perf_counter() - self.first_audio) * 1000,
                    )
                await self.session.emit(
                    SpeechPartial(speech_turn_id=speech_id, transcript=text, revision=self.partials)
                )
                continue
            self.last_final = event.turn_order
            if not text:
                continue
            self.finals += 1
            tick = perf_counter()
            if self.last_audio is not None:
                self.log(
                    "STT_FINAL_RECEIVED", last_audio_to_final_ms=(tick - self.last_audio) * 1000
                )
            await self.session.emit(
                SpeechSignal(kind="USER_SPEECH_ENDED", speech_turn_id=speech_id)
            )
            accepted = await self.session.submit_speech(
                SpeechFinal(
                    speech_turn_id=speech_id,
                    turn_id=uuid5(self.session.conversation_session_id, str(speech_id)),
                    transcript=text,
                    language=event.language,
                    provider=event.provider,
                    model=event.model,
                )
            )
            if accepted:
                self.log(
                    "STT_FINAL_SUBMITTED",
                    final_transcript_to_turn_start_ms=(perf_counter() - tick) * 1000,
                )
            else:
                self.log("STT_FINAL_REJECTED_BUSY")

    async def close(self) -> None:
        if self.task is not None and self.task is not asyncio.current_task():
            if not self.closed and not self.task.done() and not self.task.cancelling():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
