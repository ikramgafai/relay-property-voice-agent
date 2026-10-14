"""Minimal application errors and centralized HTTP error responses."""

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class RelayError(Exception):
    """Base application failure; internal details are never returned to clients."""


class ConfigurationError(RelayError):
    """Invalid process configuration; startup must fail."""


async def handle_relay_error(request: Request, exc: Exception) -> JSONResponse:
    structlog.get_logger().error("application_error", error_type=type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Application error"})


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    structlog.get_logger().error("unexpected_error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RelayError, handle_relay_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
