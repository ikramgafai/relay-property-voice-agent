"""Own the database engine for one application lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import structlog
from fastapi import FastAPI

from app.agent.orchestrator import TurnOrchestrator
from app.agent.protocol import ResponseRenderer
from app.agent.renderer import ConstrainedModelRenderer, DeterministicRenderer
from app.db.agent import SQLAlchemyTurnStore
from app.db.session import Database
from app.db.tool_store import SQLAlchemyToolStore
from app.providers.llm.openai_compatible import OpenAICompatibleRoutingProvider
from app.providers.llm.phrasing import CompatiblePhrasingProvider
from app.providers.tts.elevenlabs import ElevenLabsStreamingTTS
from app.realtime.manager import RealtimeSessionManager
from app.realtime.schemas import Binding
from app.routing.router import ServiceRouter
from app.tools.executor import ToolExecutor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger = structlog.get_logger()
    database = Database(app.state.settings.database_url.get_secret_value())
    app.state.database = database
    client = httpx.AsyncClient()
    app.state.http_client = client
    settings = app.state.settings
    # Speech output is optional presentation; built only when explicitly enabled, on the
    # shared HTTP client. Voice admission attaches it per session.
    app.state.tts_provider = None
    if settings.tts_enabled:
        assert settings.elevenlabs_api_key and settings.tts_voice_id  # validated settings
        app.state.tts_provider = ElevenLabsStreamingTTS(
            client, settings.elevenlabs_api_key, settings.tts_voice_id, settings.tts_model
        )
    provider = (
        OpenAICompatibleRoutingProvider(settings, client)
        if settings.routing_model_base_url
        else None
    )
    renderer: ResponseRenderer = DeterministicRenderer()
    if settings.response_model_enabled:
        renderer = ConstrainedModelRenderer(CompatiblePhrasingProvider(settings, client))
    store = SQLAlchemyTurnStore(database)
    app.state.orchestrator = TurnOrchestrator(
        store,
        ServiceRouter(provider, timeout_seconds=settings.routing_model_timeout_seconds),
        ToolExecutor(SQLAlchemyToolStore(database)),
        renderer,
        # Same configured provider; pending-reply interpretation reuses its single call.
        interpretation=provider,
    )

    async def lookup(session_id: UUID) -> Binding:
        state, _ = await store.load(session_id)
        return Binding(revision=state.revision, turn_number=state.turn_number)

    app.state.realtime_manager = RealtimeSessionManager(lookup, app.state.orchestrator)
    app.state.ready = True
    logger.info("application_started", app_name=app.title, app_env=app.state.settings.app_env)
    try:
        yield
    finally:
        app.state.ready = False
        await app.state.realtime_manager.shutdown()
        await client.aclose()
        await database.close()
        logger.info("application_stopped", app_name=app.title)
