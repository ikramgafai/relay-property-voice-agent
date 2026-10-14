from collections.abc import AsyncGenerator
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from app.property.schemas import DomainModel


class AudioFormat(DomainModel):
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: Literal[1] = 1
    encoding: Literal["pcm_s16le"] = "pcm_s16le"


class AudioFrame(DomainModel):
    data: bytes = Field(min_length=2, max_length=96000)
    format: AudioFormat = Field(default_factory=AudioFormat)
    sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def aligned(self) -> Self:
        if len(self.data) % 2:
            raise ValueError("PCM16 samples must be aligned")
        return self


class MediaAudioSource(Protocol):
    def __aiter__(self) -> AsyncGenerator[AudioFrame, None]: ...
    async def close(self) -> None: ...
