import pytest
from app.db.models import Property
from app.db.session import Database
from app.main import create_app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration


async def test_real_database_readiness_and_unavailable_response(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for url, expected_status, expected_payload in (
        (postgres_url, 200, {"status": "ready", "database": "ok"}),
        (
            make_url(postgres_url)
            .set(password="wrong-test-password")
            .render_as_string(hide_password=False),
            503,
            {"status": "not_ready", "database": "unavailable"},
        ),
        (
            "postgresql+asyncpg://relay:do-not-leak@127.0.0.1:1/relay_test",
            503,
            {"status": "not_ready", "database": "unavailable"},
        ),
    ):
        monkeypatch.setenv("DATABASE_URL", url)
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            health = await client.get("/health")
            assert health.status_code == 200 and health.json() == {"status": "ok"}
            ready = await client.get("/ready")
            assert ready.status_code == expected_status
            assert ready.json() == expected_payload
            assert "X-Request-ID" in ready.headers
            assert "do-not-leak" not in ready.text


async def test_transaction_rollback_and_engine_cleanup(postgres_url: str) -> None:
    database = Database(postgres_url)
    try:
        with pytest.raises(RuntimeError, match="rollback"):
            async with database.session() as session:
                session.add(Property(slug="must-rollback", name="Rollback", timezone="UTC"))
                await session.flush()
                raise RuntimeError("rollback")
        async with database.session() as session:
            assert (
                await session.scalar(select(Property).where(Property.slug == "must-rollback"))
                is None
            )
        assert await database.is_available()
    finally:
        await database.close()
