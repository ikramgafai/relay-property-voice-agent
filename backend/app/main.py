"""Application factory and composition root."""

from fastapi import FastAPI

from app.api.router import router
from app.core.application import FoundationFastAPI
from app.core.config import load_settings
from app.core.errors import register_error_handlers
from app.core.lifecycle import lifespan
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = load_settings()
    configure_logging(settings.log_level)
    app = FoundationFastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
    app.state.settings = settings
    app.state.ready = False
    register_error_handlers(app)
    app.include_router(router)
    return app
