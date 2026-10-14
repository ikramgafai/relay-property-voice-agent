"""Ephemeral task/event ownership. All user-turn work stays in TurnOrchestrator."""

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

import structlog

from app.agent.errors import TurnError
from app.agent.schemas import AgentTurnRequest, AgentTurnResult
from app.realtime.errors import Backpressure, application_code
from app.realtime.presentation import PresentationEvent, SpeechPresentation
from app.realtime.protocol import SessionLookup, SpeechAttachment, TurnProcessor
from app.realtime.queue import OutboundQueue
from app.realtime.schemas import (
    AgentAudioOutputEnded,
    AgentAudioOutputStarted,
    AgentResponse,
    Binding,
    Cancel,
    ClientEvent,
    Code,
    End,
    Ended,
    Error,
    Ping,
    Pong,
    ServerEvent,
    ServerPayload,
    SpeechFinal,
    Start,
    Started,
    State,
    StateChanged,
    TextTurn,
    TurnCancelled,
    TurnCompleted,
    TurnStarted,
)
from app.realtime.spoken import spoken_text

PRESENTATION_EVENTS = 16  # at most two per presentation attempt; bounded, never silent


class RealtimeSession:
    def __init__(
        self,
        session_id: UUID,
        binding: Binding,
        processor: TurnProcessor,
        *,
        queue_capacity: int = 32,
        queue_timeout: float = 1,
        lookup: SessionLookup | None = None,
    ) -> None:
        self.conversation_session_id = session_id
        self.realtime_session_id, self.connection_id = uuid4(), uuid4()
        self.binding, self.processor = binding, processor
        self.state = State.CONNECTED
        self.connected_at = self.last_activity_at = datetime.now(UTC)
        self.started_tick = perf_counter()
        self.sequence = 0
        self.active_task: asyncio.Task[None] | None = None
        self.active_turn_id: UUID | None = None
        self.completed = False
        self.outbound = OutboundQueue(queue_capacity, queue_timeout)
        self.stopped = asyncio.Event()
        self.reason: Ended = Ended(reason="DISCONNECTED")
        self.closed = False
        self._emit_lock = asyncio.Lock()
        self._end_lock = asyncio.Lock()
        self.cancelled_turns = 0
        self.error_count = 0
        self.busy_count = 0
        self.lookup = lookup
        self.speech: SpeechAttachment | None = None
        self.output: SpeechPresentation | None = None
        self.last_response: str | None = None
        self._input_lock = asyncio.Lock()
        self._presentation_events: asyncio.Queue[PresentationEvent] = asyncio.Queue(
            maxsize=PRESENTATION_EVENTS
        )
        self._presentation_task: asyncio.Task[None] | None = None

    def log(self, event: str, **fields: object) -> None:
        structlog.get_logger().info(
            event,
            realtime_session_id=str(self.realtime_session_id),
            conversation_session_id=str(self.conversation_session_id),
            connection_id=str(self.connection_id),
            state=self.state.value,
            queue_depth=self.outbound.items.qsize(),
            **fields,
        )

    async def emit(self, payload: ServerPayload, turn_id: UUID | None = None) -> None:
        if payload.kind == "STT_PARTIAL" and self._emit_lock.locked():
            self.outbound.partial_drops += 1
            return
        async with self._emit_lock:
            self.sequence += 1
            event = ServerEvent(
                session_id=self.conversation_session_id,
                sequence=self.sequence,
                event_type=payload.kind,
                turn_id=turn_id,
                payload=payload,
            )
            try:
                queued = await self.outbound.put(event)
            except Backpressure:
                self.request_stop("BACKPRESSURE")
                self.log(
                    "REALTIME_BACKPRESSURE",
                    sequence=event.sequence,
                    event_id=str(event.event_id),
                    event_type=event.event_type,
                    replay_required=True,
                )
                raise
            if not queued:
                return
            self.log(
                "REALTIME_EVENT_QUEUED",
                event_id=str(event.event_id),
                event_type=event.event_type,
                sequence=event.sequence,
                turn_id=str(turn_id) if turn_id else None,
            )

    def request_stop(self, reason: str) -> None:
        # Call sites supply constants; revalidation prevents arbitrary text reaching close/logs.
        self.reason = Ended.model_validate({"reason": reason})
        self.stopped.set()

    async def error(self, code: Code, turn_id: UUID | None = None) -> None:
        self.error_count += 1
        if code == Code.SESSION_BUSY:
            self.busy_count += 1
        await self.emit(Error(code=code), turn_id)

    async def change(self, state: State) -> None:
        self.state = state
        await self.emit(StateChanged(state=state))

    async def handle(self, event: ClientEvent) -> None:
        async with self._input_lock:
            await self._handle(event)

    async def submit_speech(self, final: SpeechFinal) -> bool:
        # Same gate as text; lookup happens inside the gate so text cannot race speech
        # between revision selection and task ownership. Persistence remains authoritative.
        async with self._input_lock:
            if self.closed or self.stopped.is_set():
                return False
            if self.active_task is not None and not self.active_task.done():
                await self.emit(final, final.turn_id)
                await self.error(Code.SESSION_BUSY, final.turn_id)
                return False
            binding = (
                await self.lookup(self.conversation_session_id) if self.lookup else self.binding
            )
            final = final.model_copy(
                update={
                    "turn_number": binding.turn_number + 1,
                    "expected_revision": binding.revision,
                }
            )
            await self.emit(final, final.turn_id)
            await self._handle(
                ClientEvent(
                    session_id=self.conversation_session_id,
                    sequence=0,
                    event_type="CLIENT_TEXT_TURN",
                    turn_id=final.turn_id,
                    payload=TextTurn(
                        turn_id=final.turn_id,
                        turn_number=binding.turn_number + 1,
                        expected_revision=binding.revision,
                        utterance=final.transcript,
                    ),
                )
            )
            return self.active_turn_id == final.turn_id

    async def _handle(self, event: ClientEvent) -> None:
        self.last_activity_at = datetime.now(UTC)
        if event.session_id != self.conversation_session_id:
            await self.error(Code.INVALID_EVENT)
            return
        if self.closed or self.stopped.is_set():
            return
        payload = event.payload
        if isinstance(payload, Start):
            if self.state != State.CONNECTED:
                await self.error(Code.SESSION_ALREADY_STARTED)
                return
            await self.emit(
                Started(
                    realtime_session_id=self.realtime_session_id,
                    connection_id=self.connection_id,
                    revision=self.binding.revision,
                    next_turn_number=self.binding.turn_number + 1,
                )
            )
            await self.change(State.IDLE)
        elif isinstance(payload, Ping):
            await self.emit(Pong(ping_event_id=event.event_id))
        elif isinstance(payload, End):
            self.request_stop("CLIENT_END")
        elif self.state == State.CONNECTED:
            await self.error(Code.SESSION_NOT_STARTED, event.turn_id)
        elif isinstance(payload, Cancel):
            await self.cancel(payload.turn_id)
        elif isinstance(payload, TextTurn):
            if self.active_task is not None and not self.active_task.done():
                await self.error(Code.SESSION_BUSY, payload.turn_id)
                return
            await self.change(State.PROCESSING)
            await self.emit(TurnStarted(turn_id=payload.turn_id), payload.turn_id)
            self.active_turn_id = payload.turn_id
            self.completed = False
            request = AgentTurnRequest(
                session_id=self.conversation_session_id, **payload.model_dump(exclude={"kind"})
            )
            self.active_task = asyncio.create_task(
                self._run(request), name=f"relay-turn-{payload.turn_id}"
            )

    async def _run(self, request: AgentTurnRequest) -> None:
        try:
            await self._execute(request)
            if not self.stopped.is_set():
                await self.change(State.IDLE)
        except Backpressure:
            pass  # emit has requested an explicit replay-required close.
        except asyncio.CancelledError:
            raise
        except Exception:
            self.request_stop("INTERNAL_ERROR")
        finally:
            self.active_task = None
            self.active_turn_id = None

    async def _execute(self, request: AgentTurnRequest) -> None:
        tick = perf_counter()
        cancelled = False
        try:
            result = await self.processor.process(request)
            self.completed = True
            await self.emit(
                AgentResponse(
                    turn_id=result.turn_id,
                    response_text=result.response_text,
                    response_intent=result.response_plan.intent,
                    status=result.status,
                    revision=result.revision,
                    replayed=result.replayed,
                ),
                result.turn_id,
            )
            await self.emit(
                TurnCompleted(
                    turn_id=result.turn_id,
                    turn_number=result.turn_number,
                    revision=result.revision,
                    status=result.status,
                    replayed=result.replayed,
                ),
                result.turn_id,
            )
            self.binding = Binding(revision=result.revision, turn_number=result.turn_number)
            self.last_response = result.response_text
            if self.speech is not None:
                self.speech.on_response(result.response_text)
            # Text and completion are already queued; speech never delays or changes them.
            self._present(result)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except TurnError as exc:
            code = application_code(exc)
            await self.error(code, request.turn_id)
            if code in {Code.SESSION_NOT_FOUND, Code.SESSION_UNAVAILABLE}:
                self.request_stop("SESSION_UNAVAILABLE")
        except Backpressure:
            pass  # emit already signalled explicit connection termination.
        except Exception:
            await self.error(Code.INTERNAL_ERROR, request.turn_id)
        finally:
            self.log(
                "REALTIME_TURN_FINISHED",
                turn_id=str(request.turn_id),
                duration_ms=(perf_counter() - tick) * 1000,
                cancelled=cancelled,
            )

    def attach_output(self, output: SpeechPresentation) -> None:
        """Own an optional speech presentation and the task mapping its lifecycle to events."""
        if self.output is not None or self.closed:
            raise RuntimeError("Speech output already attached or session closed")
        self.output = output
        if self._presentation_task is None:
            self._presentation_task = asyncio.create_task(
                self._presentation_pump(), name=f"relay-presentation-{self.realtime_session_id}"
            )
        output.start()

    def _present(self, result: AgentTurnResult) -> None:
        """Speak only first deliveries; exact business replays are never re-spoken."""
        output = self.output
        if output is None or result.replayed or not output.available or self.stopped.is_set():
            return
        try:
            output.present(result.turn_id, spoken_text(result.response_text))
        except Exception:
            self.log("SPEECH_PRESENTATION_FAILED", turn_id=str(result.turn_id))

    def presentation_event(self, event: PresentationEvent) -> None:
        try:
            self._presentation_events.put_nowait(event)
        except asyncio.QueueFull:
            self.log(
                "SPEECH_OUTPUT_EVENT_DROPPED",
                speech_output_id=str(event.speech_output_id),
                kind=event.kind,
                status=event.status,
            )

    async def _presentation_pump(self) -> None:
        while True:
            event = await self._presentation_events.get()
            payload: AgentAudioOutputStarted | AgentAudioOutputEnded
            if event.kind == "STARTED":
                payload = AgentAudioOutputStarted(
                    speech_output_id=event.speech_output_id,
                    turn_id=event.turn_id,
                    provider=event.provider,
                    model=event.model,
                )
            else:
                payload = AgentAudioOutputEnded.model_validate(
                    {
                        "speech_output_id": event.speech_output_id,
                        "turn_id": event.turn_id,
                        "status": event.status,
                        "code": event.code,
                        "accepted_audio_ms": event.accepted_audio_ms,
                    }
                )
            try:
                await self.emit(payload, event.turn_id)
            except Backpressure:
                return  # emit already requested an explicit replay-required close

    async def cancel(self, turn_id: UUID) -> None:
        task = self.active_task
        if task is None or task.done() or turn_id != self.active_turn_id:
            await self.error(Code.TURN_NOT_ACTIVE, turn_id)
            return
        if self.completed:
            await self.error(Code.TURN_ALREADY_COMPLETED, turn_id)
            return
        task.cancel()
        self.cancelled_turns += 1
        # A disconnect may cancel the reader while cleanup is in progress. The session
        # still owns this task; end() awaits it without injecting a second cancellation.
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        self.active_task = None
        self.active_turn_id = None
        await self.emit(TurnCancelled(turn_id=turn_id), turn_id)
        await self.change(State.IDLE)

    async def end(self, *, notify: bool = False) -> None:
        async with self._end_lock:
            if self.closed:
                return
            self.stopped.set()
            self.state = State.ENDING
            if self.speech is not None:
                await self.speech.close()
                self.speech = None
            if self.output is not None:
                await self.output.close()
                self.output = None
            if self._presentation_task is not None:
                self._presentation_task.cancel()
                await asyncio.gather(self._presentation_task, return_exceptions=True)
                self._presentation_task = None
            task = self.active_task
            if task is not None:
                if not task.done() and not task.cancelling():
                    task.cancel()
                    self.cancelled_turns += 1
                await asyncio.gather(task, return_exceptions=True)
            self.active_task = None
            self.active_turn_id = None
            if notify:
                try:
                    await self.emit(StateChanged(state=State.ENDING))
                    await self.emit(StateChanged(state=State.ENDED))
                    await self.emit(self.reason)
                except Backpressure:
                    pass
            self.state = State.ENDED
            self.closed = True
            self.log(
                "REALTIME_SESSION_ENDED",
                duration_ms=(perf_counter() - self.started_tick) * 1000,
                cancelled_turns=self.cancelled_turns,
                event_errors=self.error_count,
                session_busy=self.busy_count,
                queue_pressure=self.outbound.pressure_count,
                partial_drops=self.outbound.partial_drops,
                disconnect_reason=self.reason.reason,
            )
