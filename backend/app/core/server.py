"""Portable settings-aware Uvicorn entry point: uv run relay [--reload]."""

import argparse

import uvicorn

from app.core.config import load_settings
from app.core.errors import ConfigurationError
from app.core.logging import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Relay backend")
    parser.add_argument("--reload", action="store_true", help="Development-only automatic reload")
    args = parser.parse_args()
    settings = load_settings()
    if args.reload and settings.app_env != "development":
        raise ConfigurationError("Automatic reload is only supported in development")
    configure_logging(settings.log_level)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=args.reload,
        log_config=None,
        access_log=False,
    )
