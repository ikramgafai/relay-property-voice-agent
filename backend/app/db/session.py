"""Engine ownership, bounded connectivity checks, and transaction-scoped sessions."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import structlog
from asyncpg.exceptions import PostgresError  # type: ignore[import-untyped]
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_timeout=2,
            hide_parameters=True,
            isolation_level="REPEATABLE READ",
            connect_args={
                "timeout": 2,
                "command_timeout": 2,
                "server_settings": {"timezone": "UTC"},
            },
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            yield session

    async def is_available(self) -> bool:
        try:
            async with asyncio.timeout(2), self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except (SQLAlchemyError, PostgresError, OSError, TimeoutError) as exc:
            # Deliberately omit exception text, traceback, parameters, and credentials.
            structlog.get_logger().warning("database_unavailable", error_type=type(exc).__name__)
            return False

    async def close(self) -> None:
        await self.engine.dispose()


def get_database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_database(request).session() as session:
        yield session
