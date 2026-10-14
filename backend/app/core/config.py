"""Validated process and optional routing-provider configuration."""

from typing import Literal, Self

from pydantic import (
    Field,
    HttpUrl,
    PostgresDsn,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from app.core.errors import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="forbid", hide_input_in_errors=True
    )

    app_name: str = Field(default="Relay", min_length=1, max_length=100)
    app_env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://relay:relay_dev_only@localhost:5432/relay"
    )
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    cors_allow_credentials: bool = False
    routing_model_base_url: str | None = None
    routing_model_api_key: SecretStr | None = None
    routing_model_name: str | None = None
    routing_model_timeout_seconds: float = Field(default=10, gt=0, le=60)
    routing_model_max_completion_tokens: int = Field(default=1200, ge=256, le=8192)
    response_model_enabled: bool = False
    voice_enabled: bool = False
    livekit_url: str | None = None
    livekit_api_key: SecretStr | None = None
    livekit_api_secret: SecretStr | None = None
    assemblyai_api_key: SecretStr | None = None
    stt_model: Literal["universal-3-6-pro"] = "universal-3-6-pro"
    stt_language_codes: list[str] = Field(default_factory=lambda: ["en"])
    stt_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    # AssemblyAI turn detection (ms). min: silence before the model runs an end-of-turn
    # check; max: silence that always ends the turn. 128 ms ended turns on natural
    # mid-sentence pauses in live validation, committing fragments as separate turns.
    stt_min_turn_silence: int = Field(default=800, ge=50, le=10000)
    stt_max_turn_silence: int = Field(default=2000, ge=50, le=10000)
    # Speech output (Phase 9). Disabled by default; nothing below is read while disabled.
    tts_enabled: bool = False
    elevenlabs_api_key: SecretStr | None = None
    tts_voice_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]{1,64}$")
    tts_model: Literal["eleven_flash_v2_5"] = "eleven_flash_v2_5"

    @field_validator("livekit_url")
    @classmethod
    def media_url(cls, value: str | None) -> str | None:
        if value is not None:
            from urllib.parse import urlsplit

            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"ws", "wss"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("LiveKit requires a WebSocket URL without credentials")
        return value

    @field_validator("stt_language_codes")
    @classmethod
    def languages(cls, values: list[str]) -> list[str]:
        if len(values) > 10 or any(
            not v.isalpha() or len(v) not in {2, 3} or v != v.lower() for v in values
        ):
            raise ValueError("Use lowercase provider language codes, or [] for auto detection")
        return values

    @field_validator("routing_model_base_url")
    @classmethod
    def validate_model_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = TypeAdapter(HttpUrl).validate_python(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Model URL must not contain credentials, query or fragment")
        if parsed.scheme != "https" and parsed.host not in ("localhost", "127.0.0.1", "[::1]"):
            raise ValueError("Remote model endpoints require HTTPS")
        return value.rstrip("/")

    @field_validator("routing_model_name")
    @classmethod
    def validate_model_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("Model name must be nonblank without surrounding whitespace")
        return value

    @field_validator(
        "routing_model_api_key",
        "assemblyai_api_key",
        "livekit_api_key",
        "livekit_api_secret",
        "elevenlabs_api_key",
    )
    @classmethod
    def validate_model_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("Provider credential must not be blank")
        return value

    @field_validator("app_name", "host")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("must be nonblank with no surrounding whitespace")
        return value

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if any(character.isspace() for character in value) or "/" in value:
            raise ValueError("HOST must be a hostname or IP address, not a URL")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            parsed = TypeAdapter(PostgresDsn).validate_python(value.get_secret_value())
            if parsed.scheme != "postgresql+asyncpg" or not (parsed.path or "").strip("/"):
                raise ValueError
        except ValueError:
            raise ValueError(
                "DATABASE_URL must be a postgresql+asyncpg URL with a database"
            ) from None
        return value

    @field_validator("cors_origins")
    @classmethod
    def validate_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            if value == "*":
                raise ValueError("CORS requires explicit origins; wildcards are not supported")
            parsed = TypeAdapter(HttpUrl).validate_python(value)
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.query is not None
                or parsed.fragment is not None
                or parsed.path not in (None, "/")
                or value.endswith("/")
            ):
                raise ValueError(
                    "CORS entries must be origins without credentials, paths or suffixes"
                )
        return values

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        if self.stt_min_turn_silence > self.stt_max_turn_silence:
            raise ValueError("Minimum speech silence exceeds maximum")
        if self.voice_enabled and not all(
            (
                self.livekit_url,
                self.livekit_api_key,
                self.livekit_api_secret,
                self.assemblyai_api_key,
            )
        ):
            raise ValueError("Voice requires explicit LiveKit and AssemblyAI configuration")
        if self.tts_enabled and not (
            self.voice_enabled and self.elevenlabs_api_key and self.tts_voice_id
        ):
            raise ValueError("Speech output requires voice, an ElevenLabs key and a voice ID")
        if self.response_model_enabled and self.routing_model_base_url is None:
            raise ValueError("Response phrasing requires explicit model configuration")
        if bool(self.routing_model_base_url) != bool(self.routing_model_name):
            raise ValueError("Configure both routing model base URL and name")
        if self.routing_model_api_key is not None and self.routing_model_base_url is None:
            raise ValueError("Routing API key requires explicit endpoint and model")
        if self.app_env == "production" and self.debug:
            raise ValueError("DEBUG must be false in production")
        return self


def load_settings() -> Settings:
    """Fail before startup without exposing configuration values in the exception."""
    try:
        return Settings()
    except (ValidationError, SettingsError):
        raise ConfigurationError(
            "Invalid application configuration; check environment settings"
        ) from None
