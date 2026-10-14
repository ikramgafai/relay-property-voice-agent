"""Streaming synthesis of trusted text into raw PCM; presentation only, never business state."""

from collections.abc import AsyncGenerator
from typing import Protocol

from app.providers.tts.schemas import SpeechRequest
from app.realtime.media.protocol import AudioFormat


class StreamingTTSProvider(Protocol):
    @property
    def format(self) -> AudioFormat:
        """Declared PCM output; every yielded byte stream uses exactly this format."""
        ...

    def synthesize(self, request: SpeechRequest) -> AsyncGenerator[bytes, None]:
        """Yield raw PCM bytes at arbitrary chunk boundaries; raise TTSFailure on failure.

        Callers close the generator (``aclose``) to abort an in-flight provider stream.
        """
        ...
