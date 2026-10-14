import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from app.db.models import (
    ConversationSession,
    PropertyService,
    Resident,
    Service,
    ServiceScenario,
    WorkflowRun,
)
from app.db.repository import SQLAlchemyPropertyRepository
from app.db.seed import row_counts, seed_demo
from app.db.session import Database
from app.db.workflows import SQLAlchemyConversationRepository
from app.property.context import PropertyContextResolver
from app.property.schemas import PropertyContext
from app.routing.router import ServiceRouter
from app.routing.schemas import RoutingRequest
from app.workflows.application import WorkflowApplication
from app.workflows.errors import StaleRevision, WorkflowError
from app.workflows.schemas import ActionType, ConversationState, HandoffPayload
from app.workflows.schemas import FieldName as F
from app.workflows.schemas import WorkflowDecisionType as D
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from tests.support.workflows import route_for, turn

pytestmark = pytest.mark.integration


async def resident_context(session: AsyncSession, first_name: str = "Ikram") -> PropertyContext:
    resident = (
        await session.scalars(select(Resident).where(Resident.first_name == first_name))
    ).one()
    return await PropertyContextResolver(
        SQLAlchemyPropertyRepository(session)
    ).resolve_by_resident_id(resident.id)


async def test_flagship_ac_leak_emergency_handoff(db_session: AsyncSession) -> None:
    await seed_demo(db_session)
    context = await resident_context(db_session)
    assert context.resident and context.resident.first_name == "Ikram"
    assert context.unit and context.unit.number == "204"
    assert context.property.name == "Atlas Residence"
    counts = await row_counts(db_session)
    repository = SQLAlchemyConversationRepository(db_session)
    app = WorkflowApplication(repository)
    session_id = uuid4()
    maintenance = await route_for(context, "non_emergency_maintenance")
    emergency = await route_for(context, "emergency_maintenance")
    handoff = await route_for(context, "human_handoff")
    with capture_logs() as logs:
        result = await app.process(
            session_id, context, maintenance, turn(1, "My AC isn't cooling.")
        )
        assert result.decision.decision_type == D.ASK_USER
        result = await app.process(
            session_id,
            context,
            maintenance,
            turn(2, fields={F.SYMPTOMS: "Warm air despite low thermostat"}),
        )
        assert result.decision.decision_type == D.WAIT_FOR_CONFIRMATION
        assert result.state.active_workflow and result.state.active_workflow.pending_confirmation
        proposal = result.state.active_workflow.pending_confirmation.action_id
        result = await app.process(
            session_id, context, emergency, turn(3, "Actually water is pouring out of it.")
        )
        change = result.decision.transition
        assert change and proposal in change.superseded_action_ids
        assert set(change.preserved_fields) == {F.ISSUE, F.SYMPTOMS, F.UNIT}
        active = result.state.active_workflow
        assert active and active.field_history[F.ISSUE][0].value == "My AC isn't cooling."
        assert active.field_history[F.ISSUE][0].source_turn == 1
        assert active.collected_fields[F.ISSUE].source_turn == 3
        assert active.collected_fields[F.SYMPTOMS].source_turn == 2
        assert result.state.workflows[0].pending_confirmation is None
        result = await app.process(
            session_id,
            context,
            emergency,
            turn(
                4,
                fields={
                    F.LOCATION: "Living room AC",
                    F.HAZARD: "Water near electrical outlet",
                    F.SEVERITY: "Continuous flooding",
                },
            ),
        )
        incident = result.decision.action_request
        assert incident and incident.action_type == ActionType.EMERGENCY
        result = await app.process(
            session_id, context, handoff, turn(5, "I want to speak to someone.")
        )
        assert (
            result.decision.transition
            and incident.action_id in result.decision.transition.superseded_action_ids
        )
        result = await app.process(session_id, context, handoff, turn(6, confirmation=True))
        action = result.decision.action_request
        assert action and isinstance(action.payload, HandoffPayload)
        assert action.payload.business_context
        assert action.payload.business_context.service.service_key == "emergency_maintenance"
        assert (
            action.payload.business_context.fields[F.HAZARD].value == "Water near electrical outlet"
        )
        replayed = await app.process(session_id, context, handoff, turn(6, confirmation=True))
        assert replayed == result
    assert {entry["event"] for entry in logs} >= {
        "WORKFLOW_STARTED",
        "WORKFLOW_STEP_CHANGED",
        "WORKFLOW_ESCALATED",
    }
    assert "Water near" not in str(logs) and "Ikram" not in str(logs)
    assert [s.service_key for s in result.state.previous_services] == [
        "non_emergency_maintenance",
        "emergency_maintenance",
    ]
    assert all(not w.completed_actions for w in result.state.workflows)
    assert await row_counts(db_session) == counts
    assert await db_session.scalar(select(func.count()).select_from(ConversationSession)) == 1
    assert await db_session.scalar(select(func.count()).select_from(WorkflowRun)) == 3
    assert await repository.get(session_id, context.property.id) == result.state
    palm = await resident_context(db_session, "Alex")
    with pytest.raises(WorkflowError, match="SESSION_CONTEXT_MISMATCH"):
        await repository.get(session_id, palm.property.id)
    print(
        "Atlas / Ikram / 204: ASK_USER -> WAIT_FOR_CONFIRMATION -> emergency ASK_USER -> "
        "CREATE_EMERGENCY_INCIDENT request -> handoff confirmation -> "
        "CREATE_HUMAN_HANDOFF request; 3 workflow runs, 2 transitions, 0 executed actions"
    )


async def test_database_only_pet_service_routes_but_is_unsupported(
    db_session: AsyncSession,
) -> None:
    import app.routing

    root = Path(app.routing.__file__).parent
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob("*.py")}
    await seed_demo(db_session)
    context = await resident_context(db_session)
    service = Service(key="pet_services", name="Pet Services", description="Pet registration")
    db_session.add(service)
    await db_session.flush()
    association = PropertyService(property_id=context.property.id, service_id=service.id)
    db_session.add(association)
    await db_session.flush()
    db_session.add(
        ServiceScenario(
            property_service_id=association.id,
            key="register",
            name="Pet registration",
            description="Register your pet",
            examples=["How do I register my dog?"],
        )
    )
    await db_session.flush()
    fresh = await resident_context(db_session)
    route = await ServiceRouter().route(
        RoutingRequest(utterance="How do I register my dog?", property_context=fresh)
    )
    assert route.service_key == "pet_services"
    result = await WorkflowApplication(SQLAlchemyConversationRepository(db_session)).process(
        uuid4(), fresh, route, turn(1, "How do I register my dog?")
    )
    assert result.decision.decision_type == D.UNSUPPORTED_WORKFLOW
    assert not result.state.workflows
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob("*.py")}


async def test_restart_reload_and_competing_transactions(postgres_url: str) -> None:
    database = Database(postgres_url)
    session_id = uuid4()
    try:
        async with database.session() as session:
            await seed_demo(session)
            context = await resident_context(session)
            route = await route_for(context, "non_emergency_maintenance")
            result = await WorkflowApplication(SQLAlchemyConversationRepository(session)).process(
                session_id, context, route, turn(1)
            )
        await database.close()
        database = Database(postgres_url)  # fresh engine, sessions, repository and application
        async with database.session() as session:
            loaded = await SQLAlchemyConversationRepository(session).get(
                session_id, context.property.id
            )
            assert loaded == result.state
            replayed = await WorkflowApplication(SQLAlchemyConversationRepository(session)).process(
                session_id, context, route, turn(1)
            )
            assert replayed == result
        async with database.session() as loser:
            losing_repo = SQLAlchemyConversationRepository(loser)
            stale = await losing_repo.get(session_id, context.property.id)
            assert stale
            async with database.session() as winner:
                fresh = await SQLAlchemyConversationRepository(winner).get(
                    session_id, context.property.id
                )
                assert fresh
                saved = await SQLAlchemyConversationRepository(winner).save(fresh)
                assert saved.revision == fresh.revision + 1
            with pytest.raises(StaleRevision):
                await losing_repo.save(stale)
        async with database.session() as session:
            repo = SQLAlchemyConversationRepository(session)
            latest = await repo.get(session_id, context.property.id)
            assert latest and latest.revision == 2
            with pytest.raises(StaleRevision):
                await repo.save(result.state)  # stale CAS, independent of serialization errors
            assert (await repo.get(session_id, context.property.id)) == latest
    finally:
        async with database.session() as session:
            await session.execute(delete(WorkflowRun).where(WorkflowRun.session_id == session_id))
            await session.execute(
                delete(ConversationSession).where(ConversationSession.id == session_id)
            )
        await database.close()


async def test_atomic_failure_does_not_leave_session_revision(db_session: AsyncSession) -> None:
    await seed_demo(db_session)
    context = await resident_context(db_session)
    repo = SQLAlchemyConversationRepository(db_session)
    app = WorkflowApplication(repo)
    route = await route_for(context, "non_emergency_maintenance")
    first = await app.process(uuid4(), context, route, turn(1))
    forged = ConversationState.start(uuid4(), context)
    forged.workflows = first.state.workflows  # an existing run cannot move to another session
    with pytest.raises(WorkflowError, match="WORKFLOW_SESSION_MISMATCH"):
        await repo.save(forged)
    assert await repo.get(forged.session_id, context.property.id) is None
    assert await repo.get(first.state.session_id, context.property.id) == first.state


async def test_information_completion_and_repository_ownership(db_session: AsyncSession) -> None:
    await seed_demo(db_session)
    context = await resident_context(db_session)
    palm = await resident_context(db_session, "Alex")
    repo = SQLAlchemyConversationRepository(db_session)
    app = WorkflowApplication(repo)
    route = await route_for(context, "leasing", scenario_key="availability")
    with capture_logs() as logs:
        result = await app.process(uuid4(), context, route, turn(1))
    assert result.decision.decision_type == D.COMPLETE
    assert "WORKFLOW_COMPLETED" in {entry["event"] for entry in logs}
    handoff = await route_for(context, "human_handoff")
    result = await app.process(result.state.session_id, context, handoff, turn(2))
    assert result.state.workflows[0].status.value == "COMPLETED"
    forged = ConversationState.start(uuid4(), palm)
    forged.resident_id = context.resident.id if context.resident else None
    with pytest.raises(WorkflowError, match="RESIDENT_PROPERTY_MISMATCH"):
        await repo.save(forged)
    forged = ConversationState.start(uuid4(), palm)
    forged.workflows = [result.state.workflows[0]]
    with pytest.raises(WorkflowError, match="WORKFLOW_PROPERTY_MISMATCH"):
        await repo.save(forged)
