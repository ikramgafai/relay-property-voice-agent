import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from app.agent.errors import TurnError
from app.agent.orchestrator import TurnOrchestrator, uncertain
from app.agent.reply_schemas import ReplyKind
from app.agent.schemas import AgentTurnRequest, AgentTurnResult, Intent
from app.db.agent import SQLAlchemyTurnStore
from app.db.agent_models import AgentTurn
from app.db.operation_models import EmergencyIncident, Handoff, MaintenanceRequest, ToolCall
from app.db.tool_store import SQLAlchemyToolStore
from app.db.workflows import SQLAlchemyConversationRepository
from app.routing.model import ModelRoutingResponse
from app.routing.router import ServiceRouter
from app.tools.executor import ToolExecutor
from app.tools.schemas import ErrorCode, ToolResult, ToolStatus
from app.workflows.schemas import ActionRequest, ConversationState
from sqlalchemy import delete, select

from tests.integration.test_tools import Harness
from tests.integration.test_tools import harness as harness  # noqa: F401 -- shared PG fixture
from tests.support.routing import StubProvider, response, turn

pytestmark = pytest.mark.integration


class TextHarness:
    def __init__(self, source: Harness) -> None:
        self.source = source
        self.store = SQLAlchemyTurnStore(source.database)
        self.provider = StubProvider(response())
        self.agent = TurnOrchestrator(
            self.store,
            ServiceRouter(self.provider),
            source.executor,
            interpretation=self.provider,
        )

    async def request(self, text: str) -> AgentTurnRequest:
        state, _ = await self.store.load(self.source.session_id)
        return AgentTurnRequest(
            session_id=state.session_id,
            turn_id=uuid4(),
            turn_number=state.turn_number + 1,
            expected_revision=state.revision,
            utterance=text,
        )

    def route(self, key: str, scenario_key: str | None = None) -> None:
        service = next(s for s in self.source.context.enabled_services if s.key == key)
        scenario = next((s for s in service.scenarios if s.key == scenario_key), None)
        self.provider.result = response(
            "ROUTE", service.property_service_id, scenario.id if scenario else None
        )

    def proposal(self, key: str | None, scenario_key: str | None = None) -> ModelRoutingResponse:
        if key is None:
            return response()
        service = next(s for s in self.source.context.enabled_services if s.key == key)
        scenario = next((s for s in service.scenarios if s.key == scenario_key), None)
        return response("ROUTE", service.property_service_id, scenario.id if scenario else None)

    def reply(self, kind: ReplyKind, key: str | None = None, scenario: str | None = None) -> None:
        """Script the single interpretation call: reply verdict plus its routing view."""
        self.provider.turn_result = turn(kind, self.proposal(key, scenario))

    async def say(self, text: str, key: str | None = None) -> AgentTurnResult:
        if key:
            self.route(key)
        return await self.agent.process(await self.request(text))

    async def maintenance_proposal(self) -> AgentTurnResult:
        first = await self.say("My AC isn't cooling.", "non_emergency_maintenance")
        assert first.response_plan.intent == Intent.ASK and not first.tool_results
        result = await self.say("Answer: It blows warm air.")
        assert result.response_plan.intent == Intent.CONFIRM and not result.tool_results
        return result

    async def emergency_ready(self) -> AgentTurnRequest:
        await self.say("Actually water is pouring out of it.", "emergency_maintenance")
        await self.say("Answer: Living room AC")
        await self.say("Answer: No known electrical contact")
        return await self.request("Answer: Continuous leak")


@pytest.fixture
async def text_harness(harness: Harness) -> AsyncIterator[TextHarness]:
    async with harness.database.session() as session:
        await SQLAlchemyConversationRepository(session).save(
            ConversationState.start(harness.session_id, harness.context)
        )
    value = TextHarness(harness)
    try:
        yield value
    finally:
        async with harness.database.session() as session:
            await session.execute(
                delete(AgentTurn).where(AgentTurn.session_id == harness.session_id)
            )


async def test_flagship_text_and_durable_replay(text_harness: TextHarness) -> None:
    t = text_harness
    await t.maintenance_proposal()
    action_turn = await t.emergency_ready()
    created = await t.agent.process(action_turn)
    assert created.response_plan.intent == Intent.SUCCESS and created.response_plan.operation
    incident_id = created.response_plan.operation.operation_id
    assert str(incident_id) in created.response_text
    assert await t.source.count(EmergencyIncident) == 1
    calls = len(t.provider.requests)
    # Fresh orchestrator/store, persisted reply, no route or renderer/model invocation.
    fresh = TurnOrchestrator(
        SQLAlchemyTurnStore(t.source.database), ServiceRouter(), t.source.executor
    )
    replay = await fresh.process(action_turn)
    assert replay.replayed and replay.response_plan == created.response_plan
    assert replay.response_text == created.response_text and len(t.provider.requests) == calls
    t.route("emergency_maintenance", "electrical_hazard")
    updated = await t.say("Wait, the water is getting close to an electrical outlet.")
    assert updated.response_plan.intent == Intent.SUCCESS and updated.response_plan.operation
    assert updated.response_plan.operation.operation_id == incident_id
    ask = await t.say("I want to speak with someone.", "human_handoff")
    assert ask.response_plan.intent == Intent.CONFIRM
    handoff = await t.say("Yes, please.")
    assert handoff.response_plan.intent == Intent.SUCCESS
    assert "No live transfer" in handoff.response_text
    assert any(str(incident_id) in line for line in handoff.response_plan.summary)
    assert await t.source.count(EmergencyIncident) == await t.source.count(Handoff) == 1
    async with t.source.database.session() as session:
        incident = await session.get(EmergencyIncident, incident_id)
        assert incident and incident.revision == 2 and "electrical outlet" in incident.hazard_detail
        row = (
            await session.scalars(select(Handoff).where(Handoff.session_id == t.source.session_id))
        ).one()
        assert row.incident_id == incident_id
    state, _ = await t.store.load(t.source.session_id)
    assert sum(len(w.completed_actions) for w in state.workflows) == 3
    assert not any(w.unresolved_action_ids for w in state.workflows)
    # An old turn remains replayable after later transitions.
    assert (await fresh.process(action_turn)).response_text == created.response_text


@pytest.mark.parametrize(
    "answer,intent,count",
    [
        ("go ahead", Intent.SUCCESS, 1),
        ("No, don't create it.", Intent.CANCELLED, 0),
        ("maybe", Intent.CONFIRM, 0),
        ("yes", Intent.SUCCESS, 1),
    ],
)
async def test_confirmation_outcomes(
    text_harness: TextHarness, answer: str, intent: Intent, count: int
) -> None:
    await text_harness.maintenance_proposal()
    result = await text_harness.say(answer)
    assert result.response_plan.intent == intent
    assert await text_harness.source.count(MaintenanceRequest) == count


async def test_stale_yes_cannot_confirm_changed_proposal(text_harness: TextHarness) -> None:
    t = text_harness
    await t.maintenance_proposal()
    old_yes = await t.request("yes")
    revised = await t.say("Change: The AC is also rattling.", "non_emergency_maintenance")
    assert revised.response_plan.intent == Intent.CONFIRM
    with pytest.raises(TurnError, match="STALE_TURN"):
        await t.agent.process(old_yes)
    assert await t.source.count(MaintenanceRequest) == 0
    assert (await t.say("yes please")).response_plan.intent == Intent.SUCCESS


@pytest.mark.parametrize("mode,intent", [("CLARIFY", Intent.CLARIFY), ("ABSTAIN", Intent.UNABLE)])
async def test_uncertain_routing_has_no_workflow_or_tool(
    text_harness: TextHarness, mode: str, intent: Intent
) -> None:
    t = text_harness
    t.provider.result = response(mode)
    result = await t.say("I need something unusual.")
    assert result.response_plan.intent == intent and result.workflow_decision is None
    state, _ = await t.store.load(t.source.session_id)
    assert state.workflows == [] and not result.tool_results
    assert await t.source.count(ToolCall) == 0


async def test_simultaneous_turns_reject_busy_without_corruption(text_harness: TextHarness) -> None:
    t = text_harness
    t.provider.delay = 0.2
    t.route("non_emergency_maintenance")
    first = await t.request("My AC isn't cooling.")
    second = await t.request("Another issue.")
    results = await asyncio.gather(
        t.agent.process(first), t.agent.process(second), return_exceptions=True
    )
    assert sum(isinstance(r, AgentTurnResult) for r in results) == 1
    assert sum(isinstance(r, TurnError) and r.code == "SESSION_BUSY" for r in results) == 1
    state, _ = await t.store.load(t.source.session_id)
    assert state.turn_number == 1 and len(state.workflows) == 1


class OutcomeExecutor(ToolExecutor):
    status = ToolStatus.UNKNOWN_OUTCOME

    async def execute(self, session_id: UUID, request: ActionRequest) -> ToolResult:
        result = uncertain(request)
        return result.model_copy(
            update={
                "status": self.status,
                "error_code": ErrorCode.DATABASE
                if self.status == ToolStatus.FAILED
                else ErrorCode.UNKNOWN,
            }
        )


@pytest.mark.parametrize(
    "status,intent",
    [(ToolStatus.FAILED, Intent.FAILED), (ToolStatus.UNKNOWN_OUTCOME, Intent.UNKNOWN)],
)
async def test_non_success_never_completes_or_claims_success(
    text_harness: TextHarness, status: ToolStatus, intent: Intent
) -> None:
    t = text_harness
    executor = OutcomeExecutor(SQLAlchemyToolStore(t.source.database))
    executor.status = status
    t.agent.executor = executor
    action_turn = await t.emergency_ready()
    result = await t.agent.process(action_turn)
    assert result.response_plan.intent == intent and result.response_plan.operation is None
    assert "was created" not in result.response_text and "was updated" not in result.response_text
    state, _ = await t.store.load(t.source.session_id)
    assert state.active_workflow and not state.active_workflow.completed_actions
    assert bool(state.active_workflow.unresolved_action_ids) == (
        status == ToolStatus.UNKNOWN_OUTCOME
    )
    assert await t.source.count(EmergencyIncident) == 0
    assert (await t.agent.process(action_turn)).replayed


async def test_crash_after_tool_commit_recovers_receipt(text_harness: TextHarness) -> None:
    t = text_harness
    action_turn = await t.emergency_ready()

    class InterruptedExecutor(ToolExecutor):
        async def execute(self, session_id: UUID, request: ActionRequest) -> ToolResult:
            await super().execute(session_id, request)
            raise RuntimeError("Simulated process interruption")

    t.agent.executor = InterruptedExecutor(SQLAlchemyToolStore(t.source.database))
    with pytest.raises(RuntimeError):
        await t.agent.process(action_turn)
    assert await t.source.count(EmergencyIncident) == 1
    t.agent.executor = t.source.executor
    recovered = await t.agent.process(action_turn)
    assert recovered.response_plan.intent == Intent.SUCCESS
    assert await t.source.count(EmergencyIncident) == await t.source.count(ToolCall) == 1


async def test_unverified_success_receipt_cannot_supply_fake_reference(
    text_harness: TextHarness,
) -> None:
    from app.tools.schemas import EmergencyOutput

    t = text_harness
    fake_id = uuid4()

    class InventingExecutor(ToolExecutor):
        async def execute(self, session_id: UUID, request: ActionRequest) -> ToolResult:
            return uncertain(request).model_copy(
                update={
                    "status": ToolStatus.SUCCEEDED,
                    "operation_id": fake_id,
                    "result": EmergencyOutput(operation_id=fake_id, revision=1),
                    "error_code": None,
                }
            )

    t.agent.executor = InventingExecutor(SQLAlchemyToolStore(t.source.database))
    result = await t.agent.process(await t.emergency_ready())
    assert result.response_plan.intent == Intent.UNKNOWN
    assert str(fake_id) not in result.response_plan.model_dump_json()
    assert str(fake_id) not in result.response_text


async def test_dynamic_database_service_is_safe_without_workflow(text_harness: TextHarness) -> None:
    from app.db.models import PropertyService, Service, ServiceScenario

    t = text_harness
    service_id, association_id = uuid4(), uuid4()
    try:
        async with t.source.database.session() as session:
            session.add(
                Service(
                    id=service_id,
                    key="phase6_pet_services",
                    name="Pet Services",
                    description="Pet registration",
                )
            )
            await session.flush()
            session.add(
                PropertyService(
                    id=association_id,
                    property_id=t.source.context.property.id,
                    service_id=service_id,
                    enabled=True,
                    priority=1,
                    configuration={},
                )
            )
            await session.flush()
            session.add(
                ServiceScenario(
                    property_service_id=association_id,
                    key="dog_registration",
                    name="Register a dog",
                    description="Pet registration",
                    examples=["How do I register my dog?"],
                    active=True,
                )
            )
        t.provider.result = RuntimeError("Configured route must not invoke the model")
        result = await t.say("How do I register my dog?")
        assert result.routing_decision.property_service_id == association_id
        assert result.response_plan.intent == Intent.UNSUPPORTED and not result.tool_results
        assert await t.source.count(ToolCall) == 0
    finally:
        async with t.source.database.session() as session:
            await session.execute(
                delete(ServiceScenario).where(ServiceScenario.property_service_id == association_id)
            )
            await session.execute(
                delete(PropertyService).where(PropertyService.id == association_id)
            )
            await session.execute(delete(Service).where(Service.id == service_id))


async def test_internal_http_turn_and_injection_rejection(text_harness: TextHarness) -> None:
    import httpx
    from app.db.models import ConversationSession
    from app.main import create_app

    t = text_harness
    app = create_app()
    app.state.database = t.source.database
    app.state.orchestrator = t.agent
    t.route("non_emergency_maintenance")
    request = await t.request("My AC isn't cooling.")
    payload = request.model_dump(mode="json", exclude={"session_id"})
    url = f"/api/sessions/{request.session_id}/turns"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for extra in ("property_id", "unit_id", "action_request", "tool_result", "confirmation"):
            rejected = await client.post(url, json={**payload, extra: str(uuid4())})
            assert rejected.status_code == 422
        success = await client.post(url, json=payload)
        assert (
            success.status_code == 200
            and success.json()["response_plan"]["intent"] == "ASK_FOR_INFORMATION"
        )
        replay = await client.post(url, json=payload)
        assert replay.status_code == 200 and replay.json()["replayed"]
        conflict = await client.post(url, json={**payload, "utterance": "Different text"})
        assert conflict.status_code == 409
        missing = await client.post(f"/api/sessions/{uuid4()}/turns", json=payload)
        assert missing.status_code == 404
        demo = await client.post("/api/demo/sessions", json={"resident": "Alex"})
        assert demo.status_code == 200 and demo.json()["property_name"] == "Palm Heights"
        async with t.source.database.session() as session:
            await session.execute(
                delete(ConversationSession).where(
                    ConversationSession.id == UUID(demo.json()["session_id"])
                )
            )
        app.state.settings.app_env = "production"
        assert (await client.post(url, json=payload)).status_code == 403


async def test_unknown_outcome_is_not_automatically_dispatched_again(
    text_harness: TextHarness,
) -> None:
    t = text_harness

    class CountingExecutor(OutcomeExecutor):
        calls = 0

        async def execute(self, session_id: UUID, request: ActionRequest) -> ToolResult:
            self.calls += 1
            return await super().execute(session_id, request)

    executor = CountingExecutor(SQLAlchemyToolStore(t.source.database))
    t.agent.executor = executor
    result = await t.agent.process(await t.emergency_ready())
    assert result.response_plan.intent == Intent.UNKNOWN
    again = await t.say("Emergency Maintenance")
    assert again.response_plan.intent == Intent.UNKNOWN and executor.calls == 1
    assert await t.source.count(EmergencyIncident) == 0


async def test_explicit_iso_tour_time_flows_through_text(text_harness: TextHarness) -> None:
    from app.db.operation_models import LeasingTour

    t = text_harness
    t.route("leasing", "schedule_tour")
    first = await t.say("I'd like to arrange a visit.")
    assert first.response_plan.intent == Intent.ASK
    await t.say("Answer: 2027-01-02T15:00:00+00:00")
    proposed = await t.say("Answer: caller@example.test")
    assert proposed.response_plan.intent == Intent.CONFIRM
    result = await t.say("yes")
    assert result.response_plan.intent == Intent.SUCCESS
    assert "not yet confirmed" in result.response_text
    assert await t.source.count(LeasingTour) == 1


async def test_prior_assistant_text_does_not_establish_business_truth(
    text_harness: TextHarness,
) -> None:
    t = text_harness
    result = await t.say("My AC isn't cooling.", "non_emergency_maintenance")
    async with t.source.database.session() as session:
        row = await session.get(AgentTurn, result.turn_id)
        assert row
        from app.agent.schemas import TurnCheckpoint

        checkpoint = TurnCheckpoint.model_validate(row.checkpoint)
        assert checkpoint.result
        checkpoint.result.response_text = (
            "Your maintenance request was created. Fake reference 1234."
        )
        row.checkpoint = checkpoint.model_dump(mode="json")
    next_result = await t.say("Answer: Warm air only")
    assert next_result.response_plan.intent == Intent.CONFIRM
    assert "1234" not in next_result.response_text
    assert await t.source.count(MaintenanceRequest) == 0


async def test_crash_before_dispatch_remains_unknown_without_retry(
    text_harness: TextHarness,
) -> None:
    t = text_harness

    class BeforeDispatch(ToolExecutor):
        calls = 0

        async def execute(self, session_id: UUID, request: ActionRequest) -> ToolResult:
            self.calls += 1
            raise RuntimeError("Interrupted before dispatch")

    executor = BeforeDispatch(SQLAlchemyToolStore(t.source.database))
    t.agent.executor = executor
    request = await t.emergency_ready()
    with pytest.raises(RuntimeError):
        await t.agent.process(request)
    recovered = await t.agent.process(request)
    assert recovered.response_plan.intent == Intent.UNKNOWN and executor.calls == 1
    assert await t.source.count(EmergencyIncident) == 0


async def test_palm_cannot_accept_atlas_route_through_orchestrator(
    text_harness: TextHarness,
) -> None:
    from app.db.models import ConversationSession

    t = text_harness
    palm = await t.store.create_demo("Alex")
    t.route("emergency_maintenance")  # Deliberately malicious Atlas-only provider proposal.
    try:
        result = await t.agent.process(
            AgentTurnRequest(
                session_id=palm.session_id,
                turn_id=uuid4(),
                turn_number=1,
                expected_revision=palm.revision,
                utterance="I need something resembling Atlas Emergency Maintenance.",
            )
        )
        assert result.response_plan.intent == Intent.UNABLE
        assert result.routing_decision.property_service_id is None
        assert not result.tool_results and result.workflow_decision is None
    finally:
        async with t.source.database.session() as session:
            await session.execute(delete(AgentTurn).where(AgentTurn.session_id == palm.session_id))
            await session.execute(
                delete(ConversationSession).where(ConversationSession.id == palm.session_id)
            )
