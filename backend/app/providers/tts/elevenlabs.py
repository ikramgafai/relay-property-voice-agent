"""ElevenLabs HTTP streaming TTS. Raw httpx on an injected client; no vendor SDK.

Relay always has the complete trusted text before synthesis, so the HTTP stream endpoint
is used rather than the incremental-text WebSocket API.
"""

from collections.abc import AsyncGenerator
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app.providers.tts.schemas import SpeechRequest, TTSFailure
from app.realtime.media.protocol import AudioFormat

BASE_URL = "https://api.elevenlabs.io"
OUTPUT_FORMAT = "pcm_24000"  # signed 16-bit little-endian, 24 kHz, mono
# At most 100 ms of 24 kHz mono PCM16 per yield, bounding how much audio one chunk pushes
# into SpeechOutput's frame buffer. Bytes are yielded as soon as they arrive; larger network
# reads are only split, never held back to fill a chunk.
READ_CHUNK_BYTES = 4800


def language_code(tag: str | None) -> str | None:
    """ElevenLabs accepts ISO 639-1. Map a BCP-47-style tag's primary subtag when it is a
    two-letter code; otherwise omit the field rather than send an invalid value."""
    if tag is None:
        return None
    primary = tag.split("-", 1)[0]
    return primary.lower() if len(primary) == 2 and primary.isalpha() else None


class ElevenLabsStreamingTTS:
    provider_name = "elevenlabs"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        voice_id: str,
        model: Literal["eleven_flash_v2_5"] = "eleven_flash_v2_5",
        *,
        base_url: str = BASE_URL,
        timeout_seconds: float = 5,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self.voice_id, self.model = voice_id, model
        self._url = httpx.URL(base_url).copy_with(
            path=f"/v1/text-to-speech/{quote(voice_id, safe='')}/stream"
        )
        # Connect, write, pool and every read (first audio and each later chunk) are bounded.
        self._timeout = httpx.Timeout(timeout_seconds)
        self.format = AudioFormat(sample_rate=24000)

    def __repr__(self) -> str:
        return f"ElevenLabsStreamingTTS(model={self.model!r}, voice_id={self.voice_id!r})"

    async def synthesize(self, request: SpeechRequest) -> AsyncGenerator[bytes, None]:
        body: dict[str, str] = {"text": request.text, "model_id": self.model}
        code = language_code(request.language)
        if code is not None:
            body["language_code"] = code
        received = 0
        try:
            async with self._client.stream(
                "POST",
                self._url,
                params={"output_format": OUTPUT_FORMAT},
                headers={"xi-api-key": self._api_key.get_secret_value()},
                json=body,
                timeout=self._timeout,
            ) as response:
                if not response.is_success:
                    raise TTSFailure("UNAVAILABLE")  # body is never read or exposed
                async for data in response.aiter_bytes():
                    for start in range(0, len(data), READ_CHUNK_BYTES):
                        chunk = data[start : start + READ_CHUNK_BYTES]
                        received += len(chunk)
                        yield chunk
        except TTSFailure:
            raise
        except httpx.TimeoutException:
            raise TTSFailure("TIMEOUT") from None
        except httpx.HTTPError:
            raise TTSFailure("UNAVAILABLE") from None
        # Cancellation and generator close propagate unchanged; the context manager above
        # closes the response without consuming the rest of the stream.
        if received == 0:
            raise TTSFailure("INVALID_AUDIO")
