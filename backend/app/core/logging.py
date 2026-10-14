"""One JSON logging pipeline for structlog, the application and Uvicorn."""

import logging.config

import structlog


def configure_logging(level: str) -> None:
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": lambda: formatter}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "json",
                }
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                "uvicorn": {"handlers": [], "propagate": True, "level": level},
                "uvicorn.error": {"handlers": [], "propagate": True, "level": level},
                # The app factory runs after Uvicorn config; do not re-enable access logs.
                "uvicorn.access": {"handlers": [], "propagate": False, "level": "WARNING"},
                # HTTPX's INFO request logs include full URLs, including query strings.
                "httpx": {"handlers": [], "propagate": True, "level": "WARNING"},
                # Provider handshakes may contain credentials/context; suppress wire debug.
                "websockets": {"handlers": [], "propagate": True, "level": "WARNING"},
                "livekit": {"handlers": [], "propagate": True, "level": "WARNING"},
            },
        }
    )
