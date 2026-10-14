"""Async Alembic environment; also accepts a supplied sync connection in tests."""

import asyncio

from alembic import context
from app.core.config import load_settings
from app.db import agent_models, models, operation_models  # noqa: F401 -- registers metadata
from app.db.base import Base
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    engine = create_async_engine(
        load_settings().database_url.get_secret_value(),
        poolclass=NullPool,
        connect_args={"server_settings": {"timezone": "UTC"}},
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=load_settings().database_url.get_secret_value(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif context.config.attributes.get("connection") is not None:
    migrate(context.config.attributes["connection"])
else:
    asyncio.run(online())
