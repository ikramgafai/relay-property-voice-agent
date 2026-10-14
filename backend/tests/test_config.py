from pathlib import Path

import pytest
from app.core.config import Settings, load_settings
from app.core.errors import ConfigurationError
from app.main import create_app
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("APP_ENV", "staging"),
        ("PORT", "0"),
        ("PORT", "65536"),
        ("PORT", "not-a-number"),
        ("LOG_LEVEL", "VERBOSE"),
        ("DEBUG", "perhaps"),
        ("APP_NAME", " "),
        ("HOST", "http://localhost"),
        ("DATABASE_URL", "sqlite:///relay.db"),
        ("DATABASE_URL", "postgresql+asyncpg://localhost"),
        ("CORS_ORIGINS", '["*"]'),
        ("CORS_ORIGINS", '["https://example.com/path"]'),
        ("CORS_ORIGINS", '["https://user:password@example.com"]'),
        ("CORS_ORIGINS", "not-json"),
    ],
)
def test_invalid_environment_fails_before_startup(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError, match="Invalid application configuration"):
        create_app()


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_supported_environments(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    monkeypatch.setenv("APP_ENV", environment)
    assert load_settings().app_env == environment


def test_production_disallows_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG", "true")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_secrets_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "postgresql+asyncpg://relay:secret-test-value@localhost:5432/relay"
    monkeypatch.setenv("DATABASE_URL", secret)
    assert "secret-test-value" not in repr(load_settings())
    monkeypatch.setenv("DATABASE_URL", "invalid-secret-test-value")
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert "secret-test-value" not in str(caught.value)
    with pytest.raises(ValidationError) as direct:
        Settings()
    assert "secret-test-value" not in str(direct.value)


def test_dotenv_and_environment_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_NAME=From dotenv\nPORT=8100\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "8200")
    settings = load_settings()
    assert settings.app_name == "From dotenv"
    assert settings.port == 8200


def test_explicit_cors_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["https://example.com"]')
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")
    assert load_settings().cors_allow_credentials is True
    monkeypatch.setenv("CORS_ORIGINS", '["*"]')
    with pytest.raises(ConfigurationError):
        load_settings()
