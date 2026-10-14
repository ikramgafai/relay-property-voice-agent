"""HTTP stack composition with observation outside framework error handling."""

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from app.core.config import Settings
from app.core.middleware import RequestContextMiddleware


class FoundationFastAPI(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        settings: Settings = self.state.settings
        stack = CORSMiddleware(
            super().build_middleware_stack(),
            allow_origins=settings.cors_origins,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["GET", "POST"],
            allow_headers=["X-Request-ID", "Content-Type"],
            expose_headers=["X-Request-ID"],
        )
        # Outside ServerErrorMiddleware so even unexpected 500s carry a request ID.
        return RequestContextMiddleware(stack)
