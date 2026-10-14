"""Durable orchestration checkpoints. Operational writes still use ToolExecutor."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.errors import TurnError
from app.agent.schemas import AgentTurnRequest, AgentTurnResult, DemoSession, TurnCheckpoint
from app.db.agent_models import AgentTurn
from app.db.models import ConversationSession, Resident
from app.db.repository import SQLAlchemyPropertyRepository
from app.db.session import Database
from app.db.tool_store import SQLAlchemyToolStore
from app.db.workflows import SQLAlchemyConversationRepository
from app.property.context import PropertyContextResolver
from app.property.schemas import PropertyContext
from app.tools.errors import StorageFailure
from app.tools.schemas import ToolResult
from app.tools.validation import request_hash
from app.workflows.schemas import ActionRequest, ConversationState


class SQLAlchemyTurnStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def last_response(self, session_id: UUID) -> str | None:
        """Only saved user-facing text is eligible to seed speech recognition context."""
        async with self.database.session() as session:
            await self._state(session, session_id)
            row = await session.scalar(
                select(AgentTurn)
                .where(AgentTurn.session_id == session_id, AgentTurn.finished.is_(True))
                .order_by(AgentTurn.turn_number.desc())
                .limit(1)
            )
            if row is None:
                return None
            checkpoint = TurnCheckpoint.model_validate(row.checkpoint)
            return checkpoint.result.response_text if checkpoint.result else None

    @asynccontextmanager
    async def guard(self, session_id: UUID) -> AsyncIterator[None]:
        # A transaction-scoped PostgreSQL guard spans separate tool commits. It never locks
        # the session row, which the Phase 5 transaction must acquire independently.
        key = int.from_bytes(session_id.bytes[:8], "big", signed=True)
        async with self.database.engine.connect() as connection, connection.begin():
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
            )
            if not acquired:
                raise TurnError("SESSION_BUSY")
            yield

    async def _state(self, session: AsyncSession, session_id: UUID) -> ConversationState:
        row = await session.get(ConversationSession, session_id)
        if row is None:
            raise TurnError("SESSION_NOT_FOUND", 404)
        state = await SQLAlchemyConversationRepository(session).get(session_id, row.property_id)
        assert state
        if not state.active:
            raise TurnError("SESSION_INACTIVE")
        return state

    async def start(self, request: AgentTurnRequest) -> TurnCheckpoint:
        async with self.database.session() as session:
            state = await self._state(session, request.session_id)
            existing = await session.get(AgentTurn, request.turn_id)
            if existing:
                saved = TurnCheckpoint.model_validate(existing.checkpoint)
                if saved.request != request:
                    raise TurnError("TURN_IDENTITY_CONFLICT")
                return saved
            unfinished = await session.scalar(
                select(AgentTurn.id).where(
                    AgentTurn.session_id == request.session_id, AgentTurn.finished.is_(False)
                )
            )
            if unfinished:
                raise TurnError("PREVIOUS_TURN_INCOMPLETE")
            if (
                state.revision != request.expected_revision
                or state.turn_number + 1 != request.turn_number
            ):
                raise TurnError("STALE_TURN")
            checkpoint = TurnCheckpoint(request=request, occurred_at=datetime.now(UTC))
            session.add(
                AgentTurn(
                    id=request.turn_id,
                    session_id=request.session_id,
                    property_id=state.property_id,
                    turn_number=request.turn_number,
                    checkpoint=checkpoint.model_dump(mode="json"),
                )
            )
            return checkpoint

    async def load(self, session_id: UUID) -> tuple[ConversationState, PropertyContext]:
        async with self.database.session() as session:
            state = await self._state(session, session_id)
            resolver = PropertyContextResolver(SQLAlchemyPropertyRepository(session))
            if state.resident_id:
                context = await resolver.resolve_by_resident_id(state.resident_id)
            else:
                raise TurnError("DEMO_RESIDENT_REQUIRED", 422)
            if (
                context.property.id,
                context.resident.id if context.resident else None,
                context.unit.id if context.unit else None,
            ) != (state.property_id, state.resident_id, state.unit_id):
                raise TurnError("SESSION_CONTEXT_CHANGED")
            return state, context

    async def checkpoint(
        self, value: TurnCheckpoint, state: ConversationState | None = None
    ) -> ConversationState | None:
        async with self.database.session() as session:
            if state is not None:
                state = await SQLAlchemyConversationRepository(session).save(state)
            row = await session.get(AgentTurn, value.request.turn_id)
            if row is None or row.session_id != value.request.session_id:
                raise TurnError("TURN_NOT_FOUND", 404)
            row.checkpoint = value.model_dump(mode="json")
            return state

    async def finish(
        self, value: TurnCheckpoint, state: ConversationState, result: AgentTurnResult
    ) -> AgentTurnResult:
        async with self.database.session() as session:
            state = await SQLAlchemyConversationRepository(session).save(state)
            result = result.model_copy(update={"revision": state.revision})
            # A single final copy contains decisions/results. The working checkpoint is compacted.
            value.result = result
            value.route = None
            value.decision = None
            value.tool_result = None
            row = await session.get(AgentTurn, value.request.turn_id)
            assert row and row.session_id == state.session_id
            row.checkpoint = value.model_dump(mode="json")
            row.finished = True
        return result

    async def verified(self, session_id: UUID, request: ActionRequest) -> ToolResult | None:
        try:
            return await SQLAlchemyToolStore(self.database).verify(
                session_id, request, request_hash(session_id, request)
            )
        except StorageFailure:
            return None

    async def create_demo(self, resident_name: str) -> DemoSession:
        if resident_name not in {"Ikram", "Alex"}:
            raise TurnError("UNKNOWN_DEMO_RESIDENT", 422)
        async with self.database.session() as session:
            resident = (
                await session.scalars(select(Resident).where(Resident.first_name == resident_name))
            ).one_or_none()
            if resident is None:
                raise TurnError("DEMO_SEED_REQUIRED", 404)
            context = await PropertyContextResolver(
                SQLAlchemyPropertyRepository(session)
            ).resolve_by_resident_id(resident.id)
            state = await SQLAlchemyConversationRepository(session).save(
                ConversationState.start(uuid4(), context)
            )
            return DemoSession(
                session_id=state.session_id,
                revision=state.revision,
                property_name=context.property.name,
                resident_name=f"{resident.first_name} {resident.last_name}",
                unit_number=context.unit.number if context.unit else None,
            )
