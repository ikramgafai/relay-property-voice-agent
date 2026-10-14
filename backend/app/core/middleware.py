"""Pure ASGI request context around the entire HTTP/error response stack."""

import re
from time import perf_counter
from uuid import uuid4

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = [value for key, value in scope["headers"] if key.lower() == b"x-request-id"]
        candidate = incoming[0].decode("latin-1") if len(incoming) == 1 else ""
        request_id = candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        started = perf_counter()
        status_code = 500
        response_completed = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_completed
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_completed = True

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            try:
                structlog.get_logger().info(
                    "http_request_completed",
                    method=scope["method"],
                    path=scope["path"],
                    status_code=status_code,
                    duration_ms=round((perf_counter() - started) * 1000, 3),
                    response_completed=response_completed,
                )
            finally:
                structlog.contextvars.reset_contextvars(**tokens)
