"""Atomic optimistic persistence. Caller owns the transaction, as in Phase 2."""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConversationSession, PropertyService, Resident, WorkflowRun
from app.workflows.errors import StaleRevision, WorkflowError
from app.workflows.schemas import ConversationState, WorkflowState


class SQLAlchemyConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, session_id: UUID, property_id: UUID) -> ConversationState | None:
        row = (
            (
                await self.session.execute(
                    select(ConversationSession.__table__).where(
                        ConversationSession.id == session_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["property_id"] != property_id:
            raise WorkflowError("SESSION_CONTEXT_MISMATCH")
        runs = (
            (
                await self.session.execute(
                    select(WorkflowRun.state)
                    .where(WorkflowRun.session_id == session_id)
                    .order_by(WorkflowRun.created_at, WorkflowRun.id)
                )
            )
            .scalars()
            .all()
        )
        ordered = sorted(
            (WorkflowState.model_validate(run) for run in runs), key=lambda run: run.started_turn
        )
        return ConversationState.model_validate(
            {
                **row["state"],
                "revision": row["revision"],
                "workflows": ordered,
            }
        )

    async def save(self, state: ConversationState) -> ConversationState:
        state = ConversationState.model_validate(state.model_dump())
        try:
            # Savepoint makes a failed compare-and-swap leave no partial workflow rows.
            async with self.session.begin_nested():
                return await self._save(state)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "40001":
                raise StaleRevision("STALE_SESSION_REVISION_RELOAD_TRANSACTION") from None
            raise

    async def _save(self, state: ConversationState) -> ConversationState:
        if (
            state.resident_id is not None
            and await self.session.scalar(
                select(Resident.id).where(
                    Resident.id == state.resident_id, Resident.property_id == state.property_id
                )
            )
            is None
        ):
            raise WorkflowError("RESIDENT_PROPERTY_MISMATCH")
        service_ids = {run.service.property_service_id for run in state.workflows}
        if service_ids:
            owned = set(
                await self.session.scalars(
                    select(PropertyService.id).where(
                        PropertyService.property_id == state.property_id,
                        PropertyService.id.in_(service_ids),
                    )
                )
            )
            if owned != service_ids:
                raise WorkflowError("WORKFLOW_PROPERTY_MISMATCH")
        revision = state.revision + 1
        snapshot = state.model_dump(mode="json", exclude={"workflows", "revision"})
        if state.revision == 0:
            accepted = await self.session.scalar(
                insert(ConversationSession)
                .values(
                    id=state.session_id,
                    property_id=state.property_id,
                    resident_id=state.resident_id,
                    unit_id=state.unit_id,
                    revision=revision,
                    state=snapshot,
                )
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(ConversationSession.id)
            )
        else:
            accepted = await self.session.scalar(
                update(ConversationSession)
                .where(
                    ConversationSession.id == state.session_id,
                    ConversationSession.property_id == state.property_id,
                    ConversationSession.resident_id.is_not_distinct_from(state.resident_id),
                    ConversationSession.unit_id.is_not_distinct_from(state.unit_id),
                    ConversationSession.revision == state.revision,
                )
                .values(revision=revision, state=snapshot)
                .returning(ConversationSession.id)
            )
        if accepted is None:
            raise StaleRevision("STALE_SESSION_REVISION")
        for run in state.workflows:
            await self._save_run(state, run)
        return state.model_copy(update={"revision": revision}, deep=True)

    async def _save_run(self, state: ConversationState, run: WorkflowState) -> None:
        statement = insert(WorkflowRun).values(
            id=run.workflow_id,
            session_id=state.session_id,
            property_id=state.property_id,
            property_service_id=run.service.property_service_id,
            workflow_key=run.workflow_key.value,
            status=run.status.value,
            state=run.model_dump(mode="json"),
            created_at=run.started_at,
            updated_at=run.updated_at,
        )
        accepted = await self.session.scalar(
            statement.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "status": statement.excluded.status,
                    "state": statement.excluded.state,
                    "updated_at": statement.excluded.updated_at,
                },
                where=(WorkflowRun.session_id == state.session_id)
                & (WorkflowRun.property_id == state.property_id),
            ).returning(WorkflowRun.id)
        )
        if accepted is None:
            raise WorkflowError("WORKFLOW_SESSION_MISMATCH")
