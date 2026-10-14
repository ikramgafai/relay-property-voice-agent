"""Real PostgreSQL migration setup and rollback-isolated tests. No production URL fallback."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from pydantic_settings import BaseSettings
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[3]
EXPECTED_TABLES = {
    "properties",
    "services",
    "property_services",
    "service_scenarios",
    "units",
    "residents",
    "sessions",
    "workflow_runs",
    "tool_calls",
    "maintenance_requests",
    "emergency_incidents",
    "leasing_tours",
    "handoffs",
    "agent_turns",
}


class IntegrationSettings(BaseSettings):
    test_database_url: SecretStr | None = None


async def table_names(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            return set(names) - {"alembic_version"}
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    value = IntegrationSettings().test_database_url
    if value is None:
        pytest.skip(
            "Use compose.test.yml for the full suite, or provide an EMPTY relay_test database"
        )
    url = value.get_secret_value()
    if make_url(url).database != "relay_test":
        pytest.fail("Integration tests require the dedicated relay_test database")
    if asyncio.run(table_names(url)):
        pytest.fail("Migration tests require an empty database; refusing to alter existing tables")
    config = Config(str(ROOT / "alembic.ini"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", url)
        command.upgrade(config, "head")
        assert asyncio.run(table_names(url)) == EXPECTED_TABLES
        command.downgrade(config, "-1")
        assert asyncio.run(table_names(url)) == EXPECTED_TABLES - {"agent_turns"}
        command.upgrade(config, "head")
        command.downgrade(config, "0002_workflow_state")
        assert asyncio.run(table_names(url)) == EXPECTED_TABLES - {
            "agent_turns",
            "tool_calls",
            "maintenance_requests",
            "emergency_incidents",
            "leasing_tours",
            "handoffs",
        }
        command.upgrade(config, "head")
        assert asyncio.run(table_names(url)) == EXPECTED_TABLES
        command.downgrade(config, "base")
        assert asyncio.run(table_names(url)) == set()
        command.upgrade(config, "head")
        assert asyncio.run(table_names(url)) == EXPECTED_TABLES
        command.check(config)
    yield url
    # This fixture created these tables in a previously empty, explicitly named test DB.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", url)
        command.downgrade(config, "base")


@pytest.fixture
async def db_session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
