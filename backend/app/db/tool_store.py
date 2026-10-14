"""Local commit ownership, audit persistence and post-commit evidence verification."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    ConversationSession,
    Property,
    PropertyService,
    Resident,
    ServiceScenario,
    Unit,
)
from app.db.operation_models import (
    EmergencyIncident,
    Handoff,
    LeasingTour,
    MaintenanceRequest,
    ToolCall,
)
from app.db.operations import SQLAlchemyPropertyOperationsAdapter, identity
from app.db.repository import SQLAlchemyPropertyRepository
from app.db.session import Database
from app.db.workflows import SQLAlchemyConversationRepository
from app.property.schemas import PropertyContext
from app.tools.errors import StorageFailure, ToolFailure
from app.tools.protocol import PropertyOperationsAdapter, ToolTransaction
from app.tools.schemas import ErrorCode, ExecutionContext, ToolResult, ToolStatus
from app.workflows.errors import WorkflowError
from app.workflows.schemas import ActionRequest, EmergencyPayload, MaintenancePayload


class SQLAlchemyToolTransaction:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.operations: PropertyOperationsAdapter = SQLAlchemyPropertyOperationsAdapter(session)

    async def source(self, session_id: UUID, request: ActionRequest) -> ExecutionContext:
        locked = await self.session.scalar(
            select(ConversationSession)
            .where(ConversationSession.id == session_id)
            .with_for_update()
        )
        if locked is None:
            raise ToolFailure(ErrorCode.OWNERSHIP)
        if (locked.property_id, locked.resident_id, locked.unit_id) != (
            request.payload.property_id,
            request.payload.resident_id,
            request.payload.unit_id,
        ):
            raise ToolFailure(ErrorCode.PROPERTY)
        state = await SQLAlchemyConversationRepository(self.session).get(
            session_id, locked.property_id
        )
        if state is None:
            raise ToolFailure(ErrorCode.OWNERSHIP)
        workflow = next(
            (w for w in state.workflows if w.workflow_id == request.source_workflow_id), None
        )
        if workflow is None:
            raise ToolFailure(ErrorCode.OWNERSHIP)
        # Hold shared locks against administrative changes until the operation commits.
        await self.session.execute(
            select(Property.id).where(Property.id == state.property_id).with_for_update(read=True)
        )
        if state.resident_id:
            await self.session.execute(
                select(Resident.id)
                .where(Resident.id == state.resident_id)
                .with_for_update(read=True)
            )
        if state.unit_id:
            await self.session.execute(
                select(Unit.id).where(Unit.id == state.unit_id).with_for_update(read=True)
            )
        await self.session.execute(
            select(PropertyService.id)
            .where(PropertyService.id == workflow.service.property_service_id)
            .with_for_update(read=True)
        )
        if workflow.service.scenario_id:
            await self.session.execute(
                select(ServiceScenario.id)
                .where(ServiceScenario.id == workflow.service.scenario_id)
                .with_for_update(read=True)
            )
        repository = SQLAlchemyPropertyRepository(self.session)
        property_record = await repository.get_property_by_id(state.property_id)
        resident = (
            await repository.get_resident_by_id(state.resident_id) if state.resident_id else None
        )
        unit = (
            await repository.get_unit_by_id(state.property_id, state.unit_id)
            if state.unit_id
            else None
        )
        if property_record is None or not property_record.active:
            raise ToolFailure(ErrorCode.PROPERTY)
        if state.resident_id and (resident is None or not resident.active):
            raise ToolFailure(ErrorCode.PROPERTY)
        if state.unit_id and (unit is None or not unit.active):
            raise ToolFailure(ErrorCode.PROPERTY)
        context = PropertyContext(
            property=property_record,
            resident=resident,
            unit=unit,
            enabled_services=await repository.get_enabled_services_for_property(state.property_id),
        )
        if (
            unit
            and isinstance(request.payload, (EmergencyPayload, MaintenancePayload))
            and request.payload.unit_number != unit.number
        ):
            raise ToolFailure(ErrorCode.PROPERTY)
        return ExecutionContext(session=state, workflow=workflow, property_context=context)

    async def existing(self, request: ActionRequest, digest: str) -> ToolResult | None:
        row = await self.session.scalar(
            select(ToolCall).where(ToolCall.action_request_id == request.action_id)
        )
        if row is None:
            return None
        if row.request_hash != digest:
            raise ToolFailure(ErrorCode.CONFLICT)
        if row.status == ToolStatus.SUCCEEDED:
            row.replay_count += 1
        if row.result_payload is None:
            raise StorageFailure()
        return ToolResult.model_validate(row.result_payload)

    async def record(
        self, source: ExecutionContext, request: ActionRequest, digest: str, result: ToolResult
    ) -> None:
        statement = insert(ToolCall).values(
            **identity(source, request),
            id=result.execution_id,
            action_type=request.action_type.value,
            idempotency_key=f"action:{request.action_id}",
            request_hash=digest,
            request_payload=request.model_dump(mode="json"),
            status=result.status.value,
            result_payload=result.model_dump(mode="json"),
            operation_id=result.operation_id,
            error_code=result.error_code.value if result.error_code else None,
            safe_error_message=result.safe_error_message,
            started_at=result.started_at,
            completed_at=result.completed_at,
            duration_ms=result.duration_ms,
        )
        changes = {
            name: getattr(statement.excluded, name)
            for name in (
                "status",
                "result_payload",
                "operation_id",
                "error_code",
                "safe_error_message",
                "started_at",
                "completed_at",
                "duration_ms",
            )
        }
        if result.status == ToolStatus.PENDING:
            changes["attempt_count"] = ToolCall.attempt_count + 1
        accepted = await self.session.scalar(
            statement.on_conflict_do_update(
                index_elements=["action_request_id"],
                set_=changes,
                where=(ToolCall.request_hash == digest)
                & (ToolCall.session_id == source.session.session_id),
            ).returning(ToolCall.id)
        )
        if accepted is None:
            raise ToolFailure(ErrorCode.CONFLICT)

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[None]:
        try:
            async with self.session.begin_nested():
                yield
        except IntegrityError:
            raise ToolFailure(ErrorCode.BUSINESS) from None
        except SQLAlchemyError:
            raise ToolFailure(ErrorCode.DATABASE) from None


class SQLAlchemyToolStore:
    def __init__(self, database: Database) -> None:
        # Locks plus READ COMMITTED let a waiter see the winner's committed receipt.
        # Phase 2/4 retain their existing repeatable-read aggregate transactions.
        self.sessions = async_sessionmaker(
            database.engine.execution_options(isolation_level="READ COMMITTED"),
            expire_on_commit=False,
        )

    async def _commit(self, session: AsyncSession) -> None:
        await session.commit()

    def _transaction(self, session: AsyncSession) -> SQLAlchemyToolTransaction:
        return SQLAlchemyToolTransaction(session)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[ToolTransaction]:
        try:
            async with self.sessions() as session:
                try:
                    await session.begin()
                    yield self._transaction(session)
                    await self._commit(session)
                except BaseException:
                    await session.rollback()
                    raise
        except (SQLAlchemyError, OSError, TimeoutError):
            raise StorageFailure() from None
        except (ValidationError, WorkflowError):
            raise ToolFailure(ErrorCode.VALIDATION) from None

    async def verify(
        self, session_id: UUID, request: ActionRequest, digest: str
    ) -> ToolResult | None:
        try:
            async with self.sessions() as session, session.begin():
                # Wait for any outstanding local transaction before deciding absence is final.
                await session.execute(
                    select(ConversationSession.id)
                    .where(ConversationSession.id == session_id)
                    .with_for_update()
                )
                row = await session.scalar(
                    select(ToolCall).where(
                        ToolCall.action_request_id == request.action_id,
                        ToolCall.session_id == session_id,
                        ToolCall.workflow_id == request.source_workflow_id,
                    )
                )
                if row is None:
                    return None
                if row.request_hash != digest:
                    raise ToolFailure(ErrorCode.CONFLICT)
                result = ToolResult.model_validate(row.result_payload)
                if result.status == ToolStatus.SUCCEEDED:
                    assert result.result is not None
                    models: dict[
                        str,
                        type[MaintenanceRequest]
                        | type[EmergencyIncident]
                        | type[LeasingTour]
                        | type[Handoff],
                    ] = {
                        "maintenance_request": MaintenanceRequest,
                        "emergency_incident": EmergencyIncident,
                        "leasing_tour": LeasingTour,
                        "handoff": Handoff,
                    }
                    model = models[result.result.kind]
                    exists = await session.scalar(
                        select(model.id).where(
                            model.id == result.operation_id,
                            model.property_id == row.property_id,
                            model.session_id == row.session_id,
                            model.workflow_id == row.workflow_id,
                        )
                    )
                    if (
                        exists is None
                        or row.operation_id != result.operation_id
                        or row.status != "SUCCEEDED"
                    ):
                        raise StorageFailure()
                return result
        except (SQLAlchemyError, OSError, TimeoutError, ValidationError):
            raise StorageFailure() from None
