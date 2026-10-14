from typing import Literal
from uuid import UUID

from pydantic import Field

from app.property.schemas import DomainModel, NonBlank

MAX_SPEECH_TEXT = 4000
# Conservative BCP-47 subset: language, optional script, optional region (alpha-2 or UN M.49).
# Tags are case-insensitive and preserved as given; extensions/private-use are not accepted.
LANGUAGE_TAG = r"^[A-Za-z]{2,3}(-[A-Za-z]{4})?(-(?:[A-Za-z]{2}|[0-9]{3}))?$"


class SpeechRequest(DomainModel):
    """Trusted presentation text only. No business objects cross into synthesis."""

    speech_output_id: UUID
    text: NonBlank = Field(max_length=MAX_SPEECH_TEXT)
    language: str | None = Field(default=None, max_length=12, pattern=LANGUAGE_TAG)


class TTSFailure(Exception):
    """Safe provider failure. Raw provider messages, bodies and headers never cross."""

    def __init__(self, reason: Literal["UNAVAILABLE", "TIMEOUT", "INVALID_AUDIO"]) -> None:
        self.reason = reason
        super().__init__(reason)
