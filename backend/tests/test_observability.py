import asyncio
import json
import logging
from collections.abc import Iterator
from io import StringIO
from typing import Any

import pytest
import structlog
from app.core.errors import RelayError
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def log_output(app: FastAPI) -> Iterator[StringIO]:
    # Capture the real JSON formatter without relying on pytest's per-phase stdout swaps.
    output = StringIO()
    handler = next(item for item in logging.getLogger().handlers if item.name == "console")
    assert isinstance(handler, logging.StreamHandler)
    previous = handler.setStream(output)
    try:
        yield output
    finally:
        handler.setStream(previous)


def read_events(output: StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


async def test_lifecycle_and_request_logs(app: FastAPI, log_output: StringIO) -> None:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.get("/health?do_not_log=private", headers={"X-Request-ID": "observed-123"})
        logging.getLogger("uvicorn.access").info("GET /health?do_not_log=private")
    events = read_events(log_output)
    assert any(event["event"] == "application_started" for event in events)
    assert any(event["event"] == "application_stopped" for event in events)
    completion = next(event for event in events if event["event"] == "http_request_completed")
    assert completion["request_id"] == "observed-123"
    assert completion["method"] == "GET"
    assert completion["path"] == "/health"
    assert completion["status_code"] == 200
    assert completion["duration_ms"] >= 0
    assert completion["response_completed"] is True
    assert "private" not in json.dumps(events)
    assert "request_id" not in structlog.contextvars.get_contextvars()


async def test_concurrent_request_context_isolation(app: FastAPI, log_output: StringIO) -> None:
    both_entered = asyncio.Event()
    entered = 0

    @app.get("/test-overlap")
    async def overlap() -> dict[str, str]:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        structlog.get_logger().info("inside_request")
        return {"status": "ok"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(
            client.get("/test-overlap", headers={"X-Request-ID": "first"}),
            client.get("/test-overlap", headers={"X-Request-ID": "second"}),
        )
    assert [response.headers["X-Request-ID"] for response in responses] == ["first", "second"]
    events = read_events(log_output)
    assert {event["request_id"] for event in events if event["event"] == "inside_request"} == {
        "first",
        "second",
    }
    assert "request_id" not in structlog.contextvars.get_contextvars()


async def test_centralized_errors_and_unexpected_exception_propagation(
    app: FastAPI, log_output: StringIO
) -> None:
    @app.get("/test-relay-error")
    async def relay_error() -> None:
        raise RelayError("internal application details")

    @app.get("/test-unexpected-error")
    async def unexpected_error() -> None:
        raise RuntimeError("unexpected failure")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        for path, detail in [
            ("/test-relay-error", "Application error"),
            ("/test-unexpected-error", "Internal server error"),
        ]:
            response = await client.get(
                path, headers={"X-Request-ID": "error-test", "Origin": "http://localhost:5173"}
            )
            assert response.status_code == 500
            assert response.json() == {"detail": detail}
            assert response.headers["X-Request-ID"] == "error-test"
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    events = read_events(log_output)
    unexpected = next(event for event in events if event["event"] == "unexpected_error")
    assert "RuntimeError" in unexpected["exception"]
    completions = [event for event in events if event["event"] == "http_request_completed"]
    assert len(completions) == 2
    assert all(event["status_code"] == 500 for event in completions)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="unexpected failure"):
            await client.get("/test-unexpected-error")
