import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.db.models import ConversationSession, PropertyService, Resident, Unit, WorkflowRun
from app.db.operation_models import (
    EmergencyIncident,
    Handoff,
    LeasingTour,
    MaintenanceRequest,
    ToolCall,
)
from app.db.operations import SQLAlchemyPropertyOperationsAdapter
from app.db.repository import SQLAlchemyPropertyRepository
from app.db.seed import seed_demo
from app.db.session import Database
from app.db.tool_store import SQLAlchemyToolStore, SQLAlchemyToolTransaction
from app.db.workflows import SQLAlchemyConversationRepository
from app.property.context import PropertyContextResolver
from app.property.schemas import PropertyContext
from app.tools.errors import StorageFailure
from app.tools.executor import ToolExecutor
from app.tools.schemas import (
    EmergencyOutput,
    ErrorCode,
    ExecutionContext,
    OperationOutput,
    ToolResult,
    ToolStatus,
)
from app.workflows.application import WorkflowApplication
from app.workflows.schemas import ActionRequest, ActionType, EmergencyPayload, TurnResult
from app.workflows.schemas import FieldName as F
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from tests.support.workflows import route_for, turn

pytestmark = pytest.mark.integration


class Harness:
    def __init__(self, database: Database, context: PropertyContext) -> None:
        self.database = database
        self.context = context
        self.session_id = uuid4()
        self.executor = ToolExecutor(SQLAlchemyToolStore(database))

    async def advance(
        self,
        number: int,
        key: str = "emergency_maintenance",
        scenario: str | None = None,
        text: str = "Active AC water leak",
        **kwargs: object,
    ) -> TurnResult:
        route = await route_for(self.context, key, scenario_key=scenario)
        async with self.database.session() as session:
            return await WorkflowApplication(SQLAlchemyConversationRepository(session)).process(
                self.session_id, self.context, route, turn(number, text, **kwargs)
            )

    async def emergency(self) -> ActionRequest:
        result = await self.advance(
            1,
            fields={
                F.LOCATION: "Living room",
                F.HAZARD: "No known electrical contact",
                F.SEVERITY: "Continuous leak",
            },
        )
        assert result.decision.action_request
        return result.decision.action_request

    async def count(
        self,
        model: type[EmergencyIncident]
        | type[MaintenanceRequest]
        | type[LeasingTour]
        | type[Handoff]
        | type[ToolCall],
    ) -> int:
        async with self.database.session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.session_id == self.session_id)
                )
                or 0
            )


@pytest.fixture
async def harness(postgres_url: str) -> AsyncIterator[Harness]:
    database = Database(postgres_url)
    async with database.session() as session:
        await seed_demo(session)
        resident = (
            await session.scalars(select(Resident).where(Resident.first_name == "Ikram"))
        ).one()
        context = await PropertyContextResolver(
            SQLAlchemyPropertyRepository(session)
        ).resolve_by_resident_id(resident.id)
    value = Harness(database, context)
    try:
        yield value
    finally:
        async with database.session() as session:
            for model in (
                Handoff,
                LeasingTour,
                EmergencyIncident,
                MaintenanceRequest,
                ToolCall,
                WorkflowRun,
            ):
                await session.execute(delete(model).where(model.session_id == value.session_id))
            await session.execute(
                delete(ConversationSession).where(ConversationSession.id == value.session_id)
            )
        await database.close()


async def test_backend_flagship_create_retry_update_and_handoff(harness: Harness) -> None:
    first = await harness.advance(1, "non_emergency_maintenance", text="My AC isn't cooling.")
    assert first.decision.question and first.decision.question.field == F.SYMPTOMS
    await harness.advance(2, "non_emergency_maintenance", fields={F.SYMPTOMS: "Warm air"})
    third = await harness.advance(
        3,
        text="Actually water is pouring out of it.",
        fields={
            F.LOCATION: "Living room AC",
            F.HAZARD: "No known hazard yet",
            F.SEVERITY: "Active leak",
        },
    )
    assert third.decision.transition and third.decision.action_request
    request = third.decision.action_request
    with capture_logs() as logs:
        created = await harness.executor.execute(harness.session_id, request)
    assert created.status == ToolStatus.SUCCEEDED and created.operation_id
    assert any(e["event"] == "TOOL_SUCCEEDED" for e in logs)
    assert "Active leak" not in str(logs) and "Ikram" not in str(logs)
    for _ in range(3):
        replay = await harness.executor.execute(harness.session_id, request)
        assert replay.replayed and replay.operation_id == created.operation_id
    assert await harness.count(EmergencyIncident) == 1
    fourth = await harness.advance(
        4,
        text="Water is getting close to an electrical outlet.",
        fields={F.HAZARD: "Water near electrical outlet"},
        incident_id=created.operation_id,
    )
    update_request = fourth.decision.action_request
    assert update_request and update_request.action_type == ActionType.EMERGENCY_UPDATE
    updated = await harness.executor.execute(harness.session_id, update_request)
    assert updated.status == ToolStatus.SUCCEEDED and updated.operation_id == created.operation_id
    assert isinstance(updated.result, EmergencyOutput) and updated.result.revision == 2
    replay = await harness.executor.execute(harness.session_id, update_request)
    assert replay.replayed and replay.result == updated.result
    fifth = await harness.advance(5, "human_handoff", text="I want to speak to someone.")
    assert fifth.decision.transition
    sixth = await harness.advance(6, "human_handoff", confirmation=True)
    assert sixth.decision.action_request
    handoff = await harness.executor.execute(harness.session_id, sixth.decision.action_request)
    assert handoff.status == ToolStatus.SUCCEEDED and handoff.operation_id
    async with harness.database.session() as session:
        incident = await session.get(EmergencyIncident, created.operation_id)
        row = await session.get(Handoff, handoff.operation_id)
        assert incident and incident.hazard_detail == "Water near electrical outlet"
        assert incident.revision == 2
        assert row and row.incident_id == created.operation_id and row.status == "REQUESTED"
        assert (
            row.context["property_name"] == "Atlas Residence"
            and row.context["unit_number"] == "204"
        )
        assert row.context["resident_name"] == "Ikram Gafai"
        operations = row.context["committed_operations"]
        assert isinstance(operations, list) and len(operations) == 2
        call = (
            await session.scalars(
                select(ToolCall).where(ToolCall.action_request_id == request.action_id)
            )
        ).one()
        assert call.replay_count == 3 and call.attempt_count == 1
    assert await harness.count(EmergencyIncident) == 1
    assert await harness.count(Handoff) == 1
    # Historical execution truth survives Phase 4 supersession of request eligibility.
    replay = await harness.executor.execute(harness.session_id, request)
    assert replay.status == ToolStatus.SUCCEEDED and replay.operation_id == created.operation_id
    assert ToolResult.model_validate_json(handoff.model_dump_json()) == handoff
    assert "_sa_instance_state" not in handoff.model_dump_json()


async def test_concurrent_duplicate_execution_and_process_boundary(harness: Harness) -> None:
    request = await harness.emergency()
    results = await asyncio.gather(
        *(
            ToolExecutor(SQLAlchemyToolStore(harness.database)).execute(harness.session_id, request)
            for _ in range(2)
        )
    )
    assert all(r.status == ToolStatus.SUCCEEDED for r in results)
    assert results[0].operation_id == results[1].operation_id
    assert sum(r.replayed for r in results) == 1
    assert await harness.count(EmergencyIncident) == 1 and await harness.count(ToolCall) == 1
    fresh_database = Database(
        str(harness.database.engine.url.render_as_string(hide_password=False))
    )
    try:
        replay = await ToolExecutor(SQLAlchemyToolStore(fresh_database)).execute(
            harness.session_id, request
        )
        assert replay.replayed and replay.operation_id == results[0].operation_id
    finally:
        await fresh_database.close()


async def test_confirmation_is_persisted_and_maintenance_commits(harness: Harness) -> None:
    result = await harness.advance(1, "non_emergency_maintenance", fields={F.SYMPTOMS: "Warm air"})
    assert result.state.active_workflow and result.state.active_workflow.pending_confirmation
    proposal = result.state.active_workflow.pending_confirmation
    rejected = await harness.executor.execute(harness.session_id, proposal)
    assert rejected.error_code == ErrorCode.CONFIRMATION
    assert await harness.count(MaintenanceRequest) == 0
    forged = proposal.model_copy(update={"requires_confirmation": False})
    assert (await harness.executor.execute(harness.session_id, forged)).status == ToolStatus.FAILED
    confirmed = await harness.advance(2, "non_emergency_maintenance", confirmation=True)
    assert confirmed.decision.action_request
    success = await harness.executor.execute(harness.session_id, confirmed.decision.action_request)
    assert success.status == ToolStatus.SUCCEEDED
    async with harness.database.session() as session:
        assert await session.get(MaintenanceRequest, success.operation_id)
        call = (
            await session.scalars(
                select(ToolCall).where(ToolCall.action_request_id == proposal.action_id)
            )
        ).one()
        assert call.attempt_count == 2


async def test_payload_conflict_never_reuses_success(harness: Harness) -> None:
    request = await harness.emergency()
    success = await harness.executor.execute(harness.session_id, request)
    changed = request.model_copy(
        update={"payload": request.payload.model_copy(update={"hazard_detail": "Different facts"})}
    )
    conflict = await harness.executor.execute(harness.session_id, changed)
    assert conflict.error_code == ErrorCode.CONFLICT and conflict.operation_id is None
    assert await harness.count(EmergencyIncident) == 1
    assert (
        await harness.executor.execute(harness.session_id, request)
    ).operation_id == success.operation_id


async def test_tour_requires_explicit_aware_time(harness: Harness) -> None:
    await harness.advance(
        1,
        "leasing",
        "schedule_tour",
        fields={F.TOUR_TIME: "Saturday afternoon", F.CONTACT: "caller@example.test"},
    )
    confirmed = await harness.advance(2, "leasing", "schedule_tour", confirmation=True)
    assert confirmed.decision.action_request
    failed = await harness.executor.execute(harness.session_id, confirmed.decision.action_request)
    assert failed.error_code == ErrorCode.VALIDATION and await harness.count(LeasingTour) == 0
    requested_at = datetime(2027, 1, 2, 15, tzinfo=UTC)
    await harness.advance(3, "leasing", "schedule_tour", requested_tour_at=requested_at)
    confirmed = await harness.advance(4, "leasing", "schedule_tour", confirmation=True)
    assert confirmed.decision.action_request
    success = await harness.executor.execute(harness.session_id, confirmed.decision.action_request)
    assert success.status == ToolStatus.SUCCEEDED
    async with harness.database.session() as session:
        row = await session.get(LeasingTour, success.operation_id)
        assert row and row.requested_at == requested_at and row.status == "REQUESTED"


@pytest.mark.parametrize(
    "case", ["property", "unit", "session", "workflow", "cancelled", "superseded", "disabled"]
)
async def test_invalid_source_has_no_side_effect(harness: Harness, case: str) -> None:
    request = await harness.emergency()
    session_id = harness.session_id
    if case in {"property", "unit"}:
        async with harness.database.session() as session:
            palm_unit = (await session.scalars(select(Unit).where(Unit.number == "318"))).one()
        request = request.model_copy(
            update={
                "payload": request.payload.model_copy(
                    update={
                        "property_id" if case == "property" else "unit_id": palm_unit.property_id
                        if case == "property"
                        else palm_unit.id
                    }
                )
            }
        )
    elif case == "session":
        session_id = uuid4()
    elif case == "workflow":
        request = request.model_copy(update={"source_workflow_id": uuid4()})
    elif case == "cancelled":
        await harness.advance(2, cancel=True)
    elif case == "superseded":
        await harness.advance(2, "human_handoff")
    else:
        async with harness.database.session() as session:
            await session.execute(
                update(PropertyService)
                .where(
                    PropertyService.id
                    == next(
                        s.property_service_id
                        for s in harness.context.enabled_services
                        if s.key == "emergency_maintenance"
                    )
                )
                .values(enabled=False)
            )
    try:
        failed = await harness.executor.execute(session_id, request)
        assert failed.status == ToolStatus.FAILED and failed.operation_id is None
        assert await harness.count(EmergencyIncident) == 0
    finally:
        if case == "disabled":
            async with harness.database.session() as session:
                await session.execute(
                    update(PropertyService)
                    .where(
                        PropertyService.id
                        == next(
                            s.property_service_id
                            for s in harness.context.enabled_services
                            if s.key == "emergency_maintenance"
                        )
                    )
                    .values(enabled=True)
                )


class FailingAdapter(SQLAlchemyPropertyOperationsAdapter):
    async def create_emergency(
        self, source: ExecutionContext, request: ActionRequest, payload: EmergencyPayload
    ) -> OperationOutput:
        await super().create_emergency(source, request, payload)
        raise SQLAlchemyError("Sensitive database detail must not escape")


class FailingOperationStore(SQLAlchemyToolStore):
    def _transaction(self, session: AsyncSession) -> SQLAlchemyToolTransaction:
        transaction = super()._transaction(session)
        transaction.operations = FailingAdapter(session)
        return transaction


class FailingCommitStore(SQLAlchemyToolStore):
    async def _commit(self, session: AsyncSession) -> None:
        raise SQLAlchemyError("Simulated commit failure")


class LostCommitAcknowledgmentStore(SQLAlchemyToolStore):
    async def _commit(self, session: AsyncSession) -> None:
        await super()._commit(session)
        raise SQLAlchemyError("Commit acknowledgment lost")


@pytest.mark.parametrize("store_type", [FailingOperationStore, FailingCommitStore])
async def test_rollback_and_commit_failure_never_report_success(
    harness: Harness, store_type: type[SQLAlchemyToolStore]
) -> None:
    request = await harness.emergency()
    with capture_logs() as logs:
        failed = await ToolExecutor(store_type(harness.database)).execute(
            harness.session_id, request
        )
    assert failed.status == ToolStatus.FAILED and failed.operation_id is None
    assert failed.error_code == ErrorCode.DATABASE
    assert await harness.count(EmergencyIncident) == 0
    assert all(entry["event"] != "TOOL_SUCCEEDED" for entry in logs)
    assert "Sensitive database" not in failed.model_dump_json()
    if store_type is FailingOperationStore:
        async with harness.database.session() as session:
            call = (
                await session.scalars(
                    select(ToolCall).where(ToolCall.action_request_id == request.action_id)
                )
            ).one()
            assert call.status == "FAILED" and call.operation_id is None


async def test_lost_commit_ack_reconciles_without_second_write(harness: Harness) -> None:
    request = await harness.emergency()
    result = await ToolExecutor(LostCommitAcknowledgmentStore(harness.database)).execute(
        harness.session_id, request
    )
    assert result.status == ToolStatus.SUCCEEDED and result.replayed
    assert await harness.count(EmergencyIncident) == 1


class UnverifiableStore(SQLAlchemyToolStore):
    async def verify(
        self, session_id: UUID, request: ActionRequest, digest: str
    ) -> ToolResult | None:
        raise StorageFailure()


async def test_unavailable_commit_verification_is_unknown(harness: Harness) -> None:
    request = await harness.emergency()
    result = await ToolExecutor(UnverifiableStore(harness.database)).execute(
        harness.session_id, request
    )
    assert result.status == ToolStatus.UNKNOWN_OUTCOME and result.operation_id is None
    assert await harness.count(EmergencyIncident) == 1
    verified = await harness.executor.execute(harness.session_id, request)
    assert verified.status == ToolStatus.SUCCEEDED and verified.replayed


async def test_invalid_payload_and_unregistered_action_fail_safely(harness: Harness) -> None:
    request = await harness.emergency()
    unknown = request.model_copy(update={"action_type": "EXECUTE_ARBITRARY_CODE"})
    assert (
        await harness.executor.execute(harness.session_id, unknown)
    ).error_code == ErrorCode.UNSUPPORTED
    bad = request.model_copy(
        update={"payload": request.payload.model_copy(update={"issue_description": ""})}
    )
    assert (
        await harness.executor.execute(harness.session_id, bad)
    ).error_code == ErrorCode.VALIDATION
    assert await harness.count(EmergencyIncident) == 0


async def test_update_rejects_foreign_incident_and_never_creates(harness: Harness) -> None:
    request = await harness.emergency()
    created = await harness.executor.execute(harness.session_id, request)
    update_result = await harness.advance(2, fields={F.HAZARD: "New hazard"}, incident_id=uuid4())
    assert update_result.decision.action_request
    failed = await harness.executor.execute(
        harness.session_id, update_result.decision.action_request
    )
    assert failed.error_code == ErrorCode.OWNERSHIP
    assert await harness.count(EmergencyIncident) == 1
    async with harness.database.session() as session:
        incident = await session.get(EmergencyIncident, created.operation_id)
        assert incident and incident.revision == 1


async def test_maintenance_rejects_palm_unit_and_blank_description(harness: Harness) -> None:
    await harness.advance(1, "non_emergency_maintenance", fields={F.SYMPTOMS: "Warm air"})
    confirmed = await harness.advance(2, "non_emergency_maintenance", confirmation=True)
    request = confirmed.decision.action_request
    assert request
    async with harness.database.session() as session:
        palm = (await session.scalars(select(Unit).where(Unit.number == "318"))).one()
    cases: list[tuple[dict[str, UUID | str], ErrorCode]] = [
        ({"unit_id": palm.id}, ErrorCode.PROPERTY),
        ({"issue_description": "   "}, ErrorCode.VALIDATION),
    ]
    for changes, code in cases:
        invalid = request.model_copy(update={"payload": request.payload.model_copy(update=changes)})
        failed = await harness.executor.execute(harness.session_id, invalid)
        assert failed.error_code == code and failed.operation_id is None
    assert await harness.count(MaintenanceRequest) == 0


async def test_database_rejects_foreign_unit_without_application_validation(
    harness: Harness,
) -> None:
    request = await harness.emergency()
    created = await harness.executor.execute(harness.session_id, request)
    async with harness.database.session() as session:
        palm = (await session.scalars(select(Unit).where(Unit.number == "318"))).one()
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                await session.execute(
                    update(EmergencyIncident)
                    .where(EmergencyIncident.id == created.operation_id)
                    .values(unit_id=palm.id)
                )
    async with harness.database.session() as session:
        incident = await session.get(EmergencyIncident, created.operation_id)
        assert incident and harness.context.unit and incident.unit_id == harness.context.unit.id


async def test_second_create_in_same_run_hits_real_database_constraint(harness: Harness) -> None:
    request = await harness.emergency()
    first = await harness.executor.execute(harness.session_id, request)
    # New information without an explicit incident target produces another create declaration.
    # The DB guarantees one incident per run even when request identity is different.
    next_turn = await harness.advance(2, fields={F.HAZARD: "New hazard"})
    second = next_turn.decision.action_request
    assert second and second.action_id != request.action_id
    failed = await harness.executor.execute(harness.session_id, second)
    assert first.status == ToolStatus.SUCCEEDED and failed.error_code == ErrorCode.BUSINESS
    assert await harness.count(EmergencyIncident) == 1
    async with harness.database.session() as session:
        call = (
            await session.scalars(
                select(ToolCall).where(ToolCall.action_request_id == second.action_id)
            )
        ).one()
        assert call.status == "FAILED" and call.operation_id is None


async def test_success_is_not_visible_before_commit(harness: Harness) -> None:
    class ObservingStore(SQLAlchemyToolStore):
        async def _commit(self, session: AsyncSession) -> None:
            # Independent connection cannot see either the flushed operation or success receipt.
            assert await harness.count(EmergencyIncident) == 0
            assert await harness.count(ToolCall) == 0
            assert all(entry["event"] != "TOOL_SUCCEEDED" for entry in logs)
            await super()._commit(session)

    request = await harness.emergency()
    with capture_logs() as logs:
        result = await ToolExecutor(ObservingStore(harness.database)).execute(
            harness.session_id, request
        )
    assert result.status == ToolStatus.SUCCEEDED and result.operation_id
    assert await harness.count(EmergencyIncident) == 1 and await harness.count(ToolCall) == 1
