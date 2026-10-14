from typing import Literal
from uuid import UUID

from pydantic import Field

from app.property.schemas import DomainModel


class SpeechContext(DomainModel):
    keyterms: list[str] = Field(default_factory=list, max_length=50)
    agent_response: str | None = None


class STTEvent(DomainModel):
    kind: Literal["SPEECH_STARTED", "PARTIAL", "FINAL", "ERROR"]
    stream_id: UUID
    turn_order: int = Field(default=0, ge=0)
    transcript: str = Field(default="", max_length=8000)
    language: str | None = None
    provider: str = "assemblyai"
    model: str
    audio_timestamp_ms: int | None = None


class STTFailure(Exception):
    """Safe provider failure. Raw provider messages never cross this boundary."""
