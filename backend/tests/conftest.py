from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from app.core.config import Settings
from app.db.session import Database
from app.main import create_app
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, AsyncHTTPTransport, Request, Response


@pytest.fixture(autouse=True)
def forbid_external_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def blocked(self: AsyncHTTPTransport, request: Request) -> Response:
        raise AssertionError("Tests must not use a live HTTP transport")

    monkeypatch.setattr(AsyncHTTPTransport, "handle_async_request", blocked)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No developer .env or process settings may change a test's outcome.
    monkeypatch.chdir(tmp_path)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)
    monkeypatch.setenv("APP_ENV", "test")


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    # Infrastructure unit tests have a healthy DB seam; integration tests use real PostgreSQL.
    async def available(self: Database) -> bool:
        return True

    monkeypatch.setattr(Database, "is_available", available)
    yield create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # HTTPX's ASGI transport does not run the application lifespan itself.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http_client,
    ):
        yield http_client
