"""Audio and recognition execution; final text stops at RealtimeSession.

The provider proposes turn boundaries (its FINAL events). Relay admits a boundary as an
application turn only after a short continuation grace: a final stays provisional while
the caller may still be speaking, and finals separated by a mid-sentence pause are
coalesced into one admitted utterance. A provisional final never reaches RealtimeSession,
so it can never route, advance a workflow or execute a tool.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from time import perf_counter
from uuid import UUID, uuid4, uuid5

from app.providers.stt.protocol import StreamingSTTProvider
from app.providers.stt.schemas import SpeechContext, STTEvent, STTFailure
from app.realtime.media.protocol import AudioFrame, MediaAudioSource
from app.realtime.schemas import SpeechFailed, SpeechFinal, SpeechPartial, SpeechSignal
from app.realtime.session import RealtimeSession

# Watchdog while the speaker has resumed: if continuation speech shows no provider activity
# for this long, the whole logical utterance is aborted (never admitted as a prefix).
CONTINUATION_WATCHDOG_SECONDS = 10.0
# Bounds on one coalesced logical utterance; exceeding them aborts it (fail closed).
MAX_COALESCED_SEGMENTS = 8
MAX_UTTERANCE_CHARS = 8000  # application turn limit (TurnInput.utterance)


@dataclass(eq=False)
class _Provisional:
    """Provider final(s) awaiting admission. Identity is the first segment's."""

    speech_id: UUID
    segments: list[str]
    language: str | None
    provider: str
    model: str
    deadline: float
    first_final: float = field(default_factory=perf_counter)
    continuing: bool = False
    continuation_turn: int | None = None


class SpeechPipeline:
    def __init__(
        self,
        session: RealtimeSession,
        source: MediaAudioSource,
        provider: StreamingSTTProvider,
        context: SpeechContext,
        *,
        admission_grace_ms: int = 0,
    ) -> None:
        self.session, self.source, self.provider, self.context = session, source, provider, context
        # 0 admits provider finals immediately (the original Phase 8 behavior).
        self.admission_grace = admission_grace_ms / 1000
        self.provisional: _Provisional | None = None
        self.coalesced = self.continuations = self.flushed = 0
        self.aborted: dict[str, int] = {}
        self.media_session_id = uuid4()
        self.task: asyncio.Task[None] | None = None
        self.contexts: asyncio.Queue[str] = asyncio.Queue(maxsize=4)
        self.failed = asyncio.Event()
        self.closed = False
        self.last_final = -1
        self.last_speech_start = -1  # newest provider turn already signalled as speech start
        self._partial_turn: int | None = None  # diagnostics: partial ordinal within one turn
        self._partial_ordinal = 0
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
        cancelled = False
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
            cancelled = True
            raise
        except Exception:
            failure = True
            self.failures += 1
        finally:
            self.closed = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if cancelled:
                await self._end_provisional("SESSION_END")
            elif failure:
                await self._end_provisional("PROVIDER_FAILURE")
            else:
                await self._end_provisional("INPUT_ENDED")
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
        receive: asyncio.Task[STTEvent] | None = None
        loop = asyncio.get_running_loop()
        try:
            while True:
                if receive is None:
                    receive = asyncio.create_task(self.provider.receive(), name="relay-stt-receive")
                timeout = (
                    None
                    if self.provisional is None
                    else max(0.0, self.provisional.deadline - loop.time())
                )
                done, _ = await asyncio.wait({receive}, timeout=timeout)
                if not done:
                    if self._continuing():
                        # The speaker resumed but the continuation stalled: fail closed.
                        self._abort("CONTINUATION_TIMEOUT")
                    else:
                        await self._admit("GRACE")
                    continue
                event, receive = receive.result(), None
                await self._handle_event(event)
        finally:
            if receive is not None:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)

    def _continuing(self) -> bool:
        return self.provisional is not None and self.provisional.continuing

    def _log_provider_event(self, event: STTEvent) -> None:
        """Diagnostics only, before any handling: counts and state, never transcript text."""
        ordinal = None
        if event.kind == "PARTIAL":
            if event.turn_order != self._partial_turn:
                self._partial_turn, self._partial_ordinal = event.turn_order, 0
            self._partial_ordinal += 1
            ordinal = self._partial_ordinal
        pending = self.provisional
        self.log(
            "STT_PROVIDER_EVENT_RECEIVED",
            kind=event.kind,
            stream_id=str(event.stream_id),
            turn_order=event.turn_order,
            transcript_char_count=len(event.transcript),
            transcript_word_count=len(event.transcript.split()),
            audio_timestamp_ms=event.audio_timestamp_ms,
            partial_ordinal=ordinal,
            speech_start_signalled=event.turn_order <= self.last_speech_start,
            last_speech_start=self.last_speech_start,
            last_final=self.last_final,
            provisional_exists=pending is not None,
            continuing=pending is not None and pending.continuing,
        )

    async def _handle_event(self, event: STTEvent) -> None:
        loop = asyncio.get_running_loop()
        self._log_provider_event(event)
        if event.kind == "ERROR":
            raise STTFailure()
        if self.stream_id is None:
            self.stream_id = event.stream_id
        if event.stream_id != self.stream_id:
            raise STTFailure()
        speech_id = uuid5(event.stream_id, str(event.turn_order))
        if event.kind == "SPEECH_STARTED":
            self._continuation(event.turn_order, loop.time())
            await self._speech_started(event, speech_id)
            return
        if event.turn_order <= self.last_final:
            if event.kind == "FINAL":
                self.duplicates += 1
            return
        text = " ".join(event.transcript.split())
        if event.kind == "PARTIAL":
            self.partials += 1
            self._continuation(event.turn_order, loop.time())
            await self._speech_started(event, speech_id)  # fallback when none was signalled
            if self.partials == 1 and self.first_audio is not None:
                self.log(
                    "STT_FIRST_PARTIAL",
                    first_audio_to_first_partial_ms=(perf_counter() - self.first_audio) * 1000,
                )
            await self.session.emit(
                SpeechPartial(speech_turn_id=speech_id, transcript=text, revision=self.partials)
            )
            return
        self.last_final = event.turn_order
        if not text:
            if self.provisional is not None:
                # A blank provider final ends the continuation without adding words.
                self.provisional.continuing = False
                self.provisional.deadline = loop.time() + self.admission_grace
            return
        self.finals += 1
        if self.last_audio is not None:
            self.log(
                "STT_FINAL_RECEIVED",
                last_audio_to_final_ms=(perf_counter() - self.last_audio) * 1000,
            )
        await self.session.emit(SpeechSignal(kind="USER_SPEECH_ENDED", speech_turn_id=speech_id))
        if self.admission_grace <= 0:
            await self._submit(speech_id, [text], event.language, event.provider, event.model)
            return
        pending = self.provisional
        if pending is not None and (
            len(pending.segments) >= MAX_COALESCED_SEGMENTS
            or len(" ".join([*pending.segments, text])) > MAX_UTTERANCE_CHARS
        ):
            # The logical utterance is larger than an application turn may be. Neither the
            # prefix nor this segment becomes a turn.
            self._abort("ADMISSION_LIMIT_EXCEEDED")
            return
        if pending is None:
            self.provisional = _Provisional(
                speech_id=speech_id,
                segments=[text],
                language=event.language,
                provider=event.provider,
                model=event.model,
                deadline=loop.time() + self.admission_grace,
            )
        else:
            pending.segments.append(text)
            pending.continuing = False
            pending.deadline = loop.time() + self.admission_grace
            self.coalesced += 1
        self.log(
            "STT_FINAL_PROVISIONAL",
            segments=len(self.provisional.segments) if self.provisional else 0,
            grace_ms=self.admission_grace * 1000,
        )

    async def _speech_started(self, event: STTEvent, speech_id: UUID) -> None:
        """One logical caller-speech start per provider turn: SPEECH_STARTED, or else the
        first PARTIAL of that turn. A repeat, late or stale signal for a turn that was
        already signalled or finalized is ignored. This is presentation policy only; the
        admission continuation is handled separately by _continuation()."""
        if event.turn_order <= max(self.last_speech_start, self.last_final):
            return
        self.last_speech_start = event.turn_order
        await self.session.user_speech_started(
            SpeechSignal(
                kind="USER_SPEECH_STARTED",
                speech_turn_id=speech_id,
                audio_timestamp_ms=event.audio_timestamp_ms,
            )
        )

    def _continuation(self, turn_order: int, now: float) -> None:
        """Speech activity for a newer provider turn while a final awaits admission."""
        pending = self.provisional
        if pending is None or turn_order <= self.last_final:
            return
        if not pending.continuing:
            pending.continuing = True
            self.continuations += 1
            self.log("STT_CONTINUATION_DETECTED", segments=len(pending.segments))
        # Each sign of continuation activity re-arms the watchdog.
        pending.deadline = now + CONTINUATION_WATCHDOG_SECONDS
        pending.continuation_turn = max(turn_order, pending.continuation_turn or turn_order)

    async def _admit(self, reason: str) -> None:
        pending, self.provisional = self.provisional, None
        if pending is None:
            return
        self.log(
            "STT_FINAL_ADMITTED",
            reason=reason,
            segments=len(pending.segments),
            admission_delay_ms=(perf_counter() - pending.first_final) * 1000,
        )
        await self._submit(
            pending.speech_id, pending.segments, pending.language, pending.provider, pending.model
        )

    async def _submit(
        self,
        speech_id: UUID,
        segments: list[str],
        language: str | None,
        provider: str,
        model: str,
    ) -> None:
        # Provider-final segments are authoritative recognition output: joined with one
        # space, never rewritten. The first segment's identity is the admitted identity.
        tick = perf_counter()
        accepted = await self.session.submit_speech(
            SpeechFinal(
                speech_turn_id=speech_id,
                turn_id=uuid5(self.session.conversation_session_id, str(speech_id)),
                transcript=" ".join(segments),
                language=language,
                provider=provider,
                model=model,
            )
        )
        if accepted:
            self.log(
                "STT_FINAL_SUBMITTED",
                segments=len(segments),
                final_transcript_to_turn_start_ms=(perf_counter() - tick) * 1000,
            )
        else:
            self.log("STT_FINAL_REJECTED_BUSY", segments=len(segments))

    def _abort(self, reason: str) -> None:
        """Fail closed: the provisional logical utterance never reaches RealtimeSession."""
        pending, self.provisional = self.provisional, None
        if pending is None:
            return
        if pending.continuation_turn is not None:
            # Consume the resumed provider turn so its tail can never become its own turn.
            self.last_final = max(self.last_final, pending.continuation_turn)
        self.aborted[reason] = self.aborted.get(reason, 0) + 1
        self.log("STT_PROVISIONAL_ABORTED", reason=reason, segments=len(pending.segments))

    async def _end_provisional(self, reason: str) -> None:
        """Input stopped. Only a clean input end with no sign of continuation admits the
        provisional final; provider failure, session end, or a speaker who had resumed
        abort it."""
        pending = self.provisional
        if pending is None:
            return
        if reason == "INPUT_ENDED" and self.session.stopped.is_set():
            reason = "SESSION_END"
        elif reason == "INPUT_ENDED" and pending.continuing:
            reason = "INPUT_ENDED_DURING_CONTINUATION"
        if reason != "INPUT_ENDED":
            self._abort(reason)
            return
        self.flushed += 1
        try:
            await self._admit("INPUT_ENDED")
        except Exception:
            self.failures += 1  # critical delivery failure already ends the session

    async def close(self) -> None:
        if self.task is not None and self.task is not asyncio.current_task():
            if not self.closed and not self.task.done() and not self.task.cancelling():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None
