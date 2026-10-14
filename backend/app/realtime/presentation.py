"""Provider-neutral boundary between realtime sessions and speech presentation.

A session only asks an optional presentation to speak trusted text and receives lifecycle
notices. It never sees providers, audio formats, transports or credentials.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class PresentationEvent:
    """STARTED: the first frame was accepted by the local output sink (not remote receipt).
    ENDED: the presentation reached a terminal status."""

    kind: Literal["STARTED", "ENDED"]
    speech_output_id: UUID
    turn_id: UUID
    provider: str | None
    model: str | None
    status: str | None = None
    code: str | None = None
    accepted_audio_ms: float = 0.0


PresentationObserver = Callable[[PresentationEvent], None]


class SpeechPresentation(Protocol):
    @property
    def available(self) -> bool: ...
    def start(self) -> None: ...
    def present(self, turn_id: UUID, text: str) -> object: ...
    async def close(self) -> None: ...
