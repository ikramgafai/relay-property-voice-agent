from uuid import UUID

import pytest
from app.main import create_app
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def test_application_factory() -> None:
    first, second = create_app(), create_app()
    assert isinstance(first, FastAPI)
    assert first is not second
    assert first.title == "Relay"
    schemas = first.openapi()["paths"]
    assert schemas["/health"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/HealthResponse"}
    assert schemas["/ready"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ReadyResponse"}


@pytest.mark.parametrize(
    ("path", "payload"),
    [("/health", {"status": "ok"}), ("/ready", {"status": "ready", "database": "ok"})],
)
async def test_probes(client: AsyncClient, path: str, payload: dict[str, str]) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert response.json() == payload
    assert UUID(response.headers["X-Request-ID"]).version == 4


async def test_readiness_follows_lifespan(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "database": "unavailable"}
        async with app.router.lifespan_context(app):
            assert (await client.get("/ready")).status_code == 200
        assert (await client.get("/ready")).status_code == 503


async def test_valid_request_id_is_preserved(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "client-request_123.abc"})
    assert response.headers["X-Request-ID"] == "client-request_123.abc"


@pytest.mark.parametrize("value", ["", "spaces are invalid", "x" * 129, "bad\r\nheader", "/unsafe"])
async def test_invalid_request_ids_are_replaced(client: AsyncClient, value: str) -> None:
    response = await client.get("/health", headers={"X-Request-ID": value})
    assert UUID(response.headers["X-Request-ID"]).version == 4


async def test_duplicate_request_ids_are_replaced(client: AsyncClient) -> None:
    response = await client.get(
        "/health", headers=[("X-Request-ID", "first"), ("X-Request-ID", "second")]
    )
    assert UUID(response.headers["X-Request-ID"]).version == 4


async def test_cors_preflight_and_exposed_id(client: AsyncClient) -> None:
    response = await client.options(
        "/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Request-ID",
        },
    )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert "X-Request-ID" in response.headers
    response = await client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert "X-Request-ID" in response.headers["Access-Control-Expose-Headers"]
    denied = await client.get("/health", headers={"Origin": "https://untrusted.example"})
    assert "Access-Control-Allow-Origin" not in denied.headers


async def test_framework_errors_have_request_ids(client: AsyncClient) -> None:
    for method, path, status in [("GET", "/missing", 404), ("POST", "/health", 405)]:
        response = await client.request(method, path)
        assert response.status_code == status
        assert UUID(response.headers["X-Request-ID"]).version == 4
