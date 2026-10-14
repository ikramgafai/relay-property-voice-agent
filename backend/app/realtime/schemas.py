"""Versioned text-only transport protocol. No business authority enters from clients."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from app.agent.schemas import Intent, TurnInput
from app.property.schemas import DomainModel


class State(StrEnum):
    CONNECTED = "CONNECTED"
    IDLE = "IDLE"
    PROCESSING = "PROCESSING_TURN"
    ENDING = "ENDING"
    ENDED = "ENDED"


class Code(StrEnum):
    INVALID_EVENT = "INVALID_EVENT"
    UNSUPPORTED_EVENT = "UNSUPPORTED_EVENT"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_UNAVAILABLE = "SESSION_UNAVAILABLE"
    SESSION_BUSY = "SESSION_BUSY"
    SESSION_NOT_STARTED = "SESSION_NOT_STARTED"
    SESSION_ALREADY_STARTED = "SESSION_ALREADY_STARTED"
    DUPLICATE_CONNECTION = "DUPLICATE_CONNECTION"
    TURN_NOT_ACTIVE = "TURN_NOT_ACTIVE"
    TURN_ALREADY_COMPLETED = "TURN_ALREADY_COMPLETED"
    TURN_CONFLICT = "TURN_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class Payload(DomainModel):
    kind: str


class Start(Payload):
    kind: Literal["SESSION_START"] = "SESSION_START"


class TextTurn(TurnInput, Payload):
    kind: Literal["CLIENT_TEXT_TURN"] = "CLIENT_TEXT_TURN"


class Cancel(Payload):
    kind: Literal["CLIENT_CANCEL_TURN"] = "CLIENT_CANCEL_TURN"
    turn_id: UUID


class Ping(Payload):
    kind: Literal["PING"] = "PING"


class End(Payload):
    kind: Literal["SESSION_END"] = "SESSION_END"


ClientPayload = Annotated[Start | TextTurn | Cancel | Ping | End, Field(discriminator="kind")]


class Binding(DomainModel):
    revision: int
    turn_number: int


class Started(Payload):
    kind: Literal["SESSION_STARTED"] = "SESSION_STARTED"
    realtime_session_id: UUID
    connection_id: UUID
    revision: int
    next_turn_number: int


class StateChanged(Payload):
    kind: Literal["SESSION_STATE_CHANGED"] = "SESSION_STATE_CHANGED"
    state: State


class TurnStarted(Payload):
    kind: Literal["TURN_STARTED"] = "TURN_STARTED"
    turn_id: UUID


class AgentResponse(Payload):
    kind: Literal["AGENT_RESPONSE"] = "AGENT_RESPONSE"
    turn_id: UUID
    response_text: str
    response_intent: Intent
    status: Literal["COMPLETED", "UNRESOLVED"]
    revision: int
    replayed: bool


class TurnCompleted(Payload):
    kind: Literal["TURN_COMPLETED"] = "TURN_COMPLETED"
    turn_id: UUID
    turn_number: int
    revision: int
    status: Literal["COMPLETED", "UNRESOLVED"]
    replayed: bool


class TurnCancelled(Payload):
    kind: Literal["TURN_CANCELLED"] = "TURN_CANCELLED"
    turn_id: UUID
    # Cancellation acknowledges task termination, never business rollback.
    reconciliation_required: Literal[True] = True


class Error(Payload):
    kind: Literal["ERROR"] = "ERROR"
    code: Code
    message: str = "The realtime request could not be completed."


class Pong(Payload):
    kind: Literal["PONG"] = "PONG"
    ping_event_id: UUID


class Ended(Payload):
    kind: Literal["SESSION_ENDED"] = "SESSION_ENDED"
    reason: Literal[
        "CLIENT_END",
        "DISCONNECTED",
        "BACKPRESSURE",
        "SEND_FAILED",
        "SERVER_SHUTDOWN",
        "INTERNAL_ERROR",
        "SESSION_UNAVAILABLE",
    ]


class SpeechPartial(Payload):
    kind: Literal["STT_PARTIAL"] = "STT_PARTIAL"
    speech_turn_id: UUID
    transcript: str
    revision: int
    is_final: Literal[False] = False


class SpeechFinal(Payload):
    kind: Literal["STT_FINAL"] = "STT_FINAL"
    speech_turn_id: UUID
    turn_id: UUID
    turn_number: int | None = None
    expected_revision: int | None = None
    transcript: str
    language: str | None = None
    provider: str
    model: str
    end_of_turn: Literal[True] = True


class SpeechSignal(Payload):
    kind: Literal["USER_SPEECH_STARTED", "USER_SPEECH_ENDED"]
    speech_turn_id: UUID
    audio_timestamp_ms: int | None = None


class SpeechFailed(Payload):
    kind: Literal["STT_FAILED"] = "STT_FAILED"
    code: Literal["STREAM_FAILED", "MEDIA_ENDED"] = "STREAM_FAILED"
    message: str = "Speech input stopped. Reconnect voice and repeat unfinished speech."


ServerPayload = Annotated[
    Started
    | StateChanged
    | TurnStarted
    | AgentResponse
    | TurnCompleted
    | TurnCancelled
    | Error
    | Pong
    | Ended
    | SpeechPartial
    | SpeechFinal
    | SpeechSignal
    | SpeechFailed,
    Field(discriminator="kind"),
]


class Envelope[P: Payload](DomainModel):
    schema_version: Literal[1] = 1
    event_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    sequence: int = Field(ge=0)
    event_type: str
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    turn_id: UUID | None = None
    payload: P

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.event_type != self.payload.kind:
            raise ValueError("Envelope and payload event types must match")
        payload_turn = getattr(self.payload, "turn_id", None)
        if payload_turn is not None and self.turn_id != payload_turn:
            raise ValueError("Turn correlation must match payload")
        return self


ClientEvent = Envelope[ClientPayload]
ServerEvent = Envelope[ServerPayload]
