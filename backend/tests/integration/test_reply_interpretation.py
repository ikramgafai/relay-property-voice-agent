"""ADR 008 pending-reply interpretation through the real orchestrator, workflows, tools and PG."""

from uuid import uuid4

import pytest
from app.agent.orchestrator import TurnOrchestrator
from app.agent.reply_schemas import InterpretationBasis, InterpretationReason, ReplyOutcome
from app.agent.schemas import AgentTurnRequest, AgentTurnResult, Intent
from app.db.agent import SQLAlchemyTurnStore
from app.db.agent_models import AgentTurn
from app.db.operation_models import EmergencyIncident, Handoff, MaintenanceRequest, ToolCall
from app.providers.llm.protocol import ModelContractError, ModelUnavailable
from app.routing.router import ServiceRouter
from app.routing.schemas import DecisionType, RoutingMethod
from app.workflows.schemas import FieldName, Status, WorkflowDecisionType, WorkflowState
from sqlalchemy import delete, select

from tests.integration.test_agent import TextHarness
from tests.integration.test_agent import text_harness as text_harness  # noqa: F401
from tests.integration.test_tools import harness as harness  # noqa: F401
from tests.support.routing import response, turn

pytestmark = pytest.mark.integration


async def active(t: TextHarness) -> WorkflowState:
    state, _ = await t.store.load(t.source.session_id)
    assert state.active_workflow
    return state.active_workflow


async def no_effects(t: TextHarness) -> None:
    for model in (ToolCall, EmergencyIncident, MaintenanceRequest, Handoff):
        assert await t.source.count(model) == 0


def asked(result: AgentTurnResult) -> FieldName | None:
    decision = result.workflow_decision
    return decision.question.field if decision and decision.question else None


async def symptoms_pending(t: TextHarness) -> None:
    first = await t.say("My AC isn't cooling.", "non_emergency_maintenance")
    assert asked(first) == FieldName.SYMPTOMS


async def location_pending(t: TextHarness) -> None:
    await symptoms_pending(t)
    t.reply("WORKFLOW_ANSWER", "non_emergency_maintenance", "ac_not_cooling")
    confirm = await t.say("It's only blowing warm air.")
    assert confirm.response_plan.intent == Intent.CONFIRM
    escalated = await t.say("Actually water is pouring out of it.", "emergency_maintenance")
    assert asked(escalated) == FieldName.LOCATION
    run = await active(t)
    assert run.workflow_key.value == "emergency_maintenance"


async def test_live_misroute_living_room_ac_continues_emergency(t: TextHarness) -> None:
    await location_pending(t)
    before = await active(t)
    state, _ = await t.store.load(t.source.session_id)
    runs = len(state.workflows)
    # Exactly today's live model output: answer verdict + Non-Emergency / ac_not_cooling route.
    t.reply("WORKFLOW_ANSWER", "non_emergency_maintenance", "ac_not_cooling")
    result = await t.say("Living room AC.")
    assert result.interpretation
    assert result.interpretation.outcome == ReplyOutcome.WORKFLOW_ANSWER
    assert (
        result.interpretation.reason_code == InterpretationReason.ANSWER_PINNED_TO_ACTIVE_WORKFLOW
    )
    assert result.interpretation.proposal
    assert result.interpretation.proposal.service_key == "non_emergency_maintenance"
    assert result.routing_decision.method == RoutingMethod.TRUSTED_CONTINUATION
    assert result.routing_decision.service_key == "emergency_maintenance"
    assert result.routing_decision.scenario_id == before.service.scenario_id
    assert asked(result) == FieldName.HAZARD
    assert result.response_text == "What hazards have you noticed nearby?"
    run = await active(t)
    assert run.workflow_id == before.workflow_id and run.status == Status.WAITING_FOR_USER
    assert run.collected_fields[FieldName.LOCATION].value == "Living room AC."
    state, _ = await t.store.load(t.source.session_id)
    assert len(state.workflows) == runs  # No transition, no new maintenance run.
    assert not any(w.pending_confirmation for w in state.workflows)
    assert result.tool_results == []
    await no_effects(t)


async def test_natural_hazard_and_severity_create_exactly_one_incident(t: TextHarness) -> None:
    await location_pending(t)
    t.reply("WORKFLOW_ANSWER", "non_emergency_maintenance", "ac_not_cooling")
    await t.say("Living room AC.")
    t.reply("WORKFLOW_ANSWER")  # The model's own routing view abstains on a bare fragment.
    hazard = await t.say("No, nothing electrical.")
    assert asked(hazard) == FieldName.SEVERITY and not hazard.tool_results
    await no_effects(t)
    t.reply("WORKFLOW_ANSWER", "emergency_maintenance", "severe_water_leak")
    created = await t.say("It's continuous.")
    assert created.response_plan.intent == Intent.SUCCESS and created.response_plan.operation
    assert await t.source.count(EmergencyIncident) == await t.source.count(ToolCall) == 1
    assert await t.source.count(MaintenanceRequest) == 0
    async with t.source.database.session() as session:
        incident = await session.get(
            EmergencyIncident, created.response_plan.operation.operation_id
        )
        assert incident
        assert (incident.location_detail, incident.hazard_detail, incident.severity_detail) == (
            "Living room AC.",
            "No, nothing electrical.",
            "It's continuous.",
        )


async def test_break_in_new_intent_transitions_without_storing_location(t: TextHarness) -> None:
    await location_pending(t)
    before = await active(t)
    t.reply("NEW_INTENT", "emergency_non_maintenance", "break_in")
    result = await t.say("Actually someone is trying to break into my apartment.")
    assert result.interpretation and result.interpretation.outcome == ReplyOutcome.NEW_INTENT
    assert result.routing_decision.decision == DecisionType.ROUTED
    assert result.routing_decision.service_key == "emergency_non_maintenance"
    # Atlas has no implemented capability for it: honest unsupported reply, nothing executed.
    assert result.workflow_decision
    assert result.workflow_decision.decision_type == WorkflowDecisionType.UNSUPPORTED_WORKFLOW
    run = await active(t)
    assert run.workflow_id == before.workflow_id
    assert FieldName.LOCATION not in run.collected_fields
    await no_effects(t)
    # The emergency report's question stays open in durable state and can still be answered.
    t.reply("WORKFLOW_ANSWER", "non_emergency_maintenance", "ac_not_cooling")
    later = await t.say("Living room AC.")
    assert asked(later) == FieldName.HAZARD


async def test_break_in_with_answer_verdict_is_ambiguous(t: TextHarness) -> None:
    await location_pending(t)
    before = await active(t)
    t.reply("WORKFLOW_ANSWER", "emergency_non_maintenance", "break_in")
    result = await t.say("Actually someone is trying to break into my apartment.")
    assert result.interpretation and result.interpretation.outcome == ReplyOutcome.AMBIGUOUS
    assert (
        result.interpretation.reason_code
        == InterpretationReason.ANSWER_CONFLICTS_WITH_UNSUPPORTED_SERVICE
    )
    assert result.response_plan.intent == Intent.REPLY_CLARIFY
    assert "Where is the issue happening?" in result.response_text
    assert result.workflow_decision is None
    run = await active(t)
    assert run == before  # Not stored as location; no silent switch.
    await no_effects(t)


async def test_ambiguous_reply_clarifies_without_changing_workflow(t: TextHarness) -> None:
    await location_pending(t)
    before = await active(t)
    t.reply("AMBIGUOUS", "non_emergency_maintenance", "ac_not_cooling")
    result = await t.say("Hmm, the AC.")
    assert result.response_plan.intent == Intent.REPLY_CLARIFY
    assert result.response_text.startswith(
        "I still need one detail for your Emergency Maintenance request."
    )
    assert await active(t) == before
    await no_effects(t)


async def test_automatic_emergency_downgrade_is_blocked(t: TextHarness) -> None:
    await location_pending(t)
    before = await active(t)
    t.reply("NEW_INTENT", "non_emergency_maintenance", "ac_not_cooling")
    result = await t.say("The AC just isn't cooling the room.")
    assert result.interpretation
    assert (
        result.interpretation.reason_code
        == InterpretationReason.PROTECTED_WORKFLOW_DOWNGRADE_BLOCKED
    )
    assert result.response_plan.intent == Intent.REPLY_CLARIFY
    state, _ = await t.store.load(t.source.session_id)
    assert state.active_workflow == before
    assert not any(w.pending_confirmation for w in state.workflows)
    await no_effects(t)


async def test_escalation_is_not_suppressed_by_a_pending_routine_question(
    t: TextHarness,
) -> None:
    await symptoms_pending(t)
    before = await active(t)
    t.reply("WORKFLOW_ANSWER", "emergency_maintenance", "active_flooding")
    swallowed = await t.say("Water is pouring out of it.")
    assert swallowed.interpretation
    assert (
        swallowed.interpretation.reason_code
        == InterpretationReason.ANSWER_CONFLICTS_WITH_SAFETY_CRITICAL_SERVICE
    )
    run = await active(t)
    assert run == before and FieldName.SYMPTOMS not in run.collected_fields
    t.reply("NEW_INTENT", "emergency_maintenance", "active_flooding")
    escalated = await t.say("Water is pouring out of it.")
    assert escalated.interpretation
    assert escalated.interpretation.outcome == ReplyOutcome.NEW_INTENT
    run = await active(t)
    assert run.workflow_key.value == "emergency_maintenance"
    assert asked(escalated) == FieldName.LOCATION
    await no_effects(t)


@pytest.mark.parametrize(
    "failure,basis",
    [
        (ModelUnavailable("down"), InterpretationBasis.MODEL_UNAVAILABLE),
        (ModelContractError("bad"), InterpretationBasis.MODEL_CONTRACT_VIOLATION),
    ],
)
async def test_interpretation_failure_never_stores_or_switches(
    t: TextHarness, failure: Exception, basis: InterpretationBasis
) -> None:
    await location_pending(t)
    before = await active(t)
    t.provider.turn_result = failure
    result = await t.say("Living room AC.")
    assert result.interpretation and result.interpretation.basis == basis
    assert result.response_plan.intent == Intent.REPLY_CLARIFY
    assert await active(t) == before
    await no_effects(t)


async def test_model_catalog_violation_is_ambiguous(t: TextHarness) -> None:
    await location_pending(t)
    before = await active(t)
    t.provider.turn_result = turn("WORKFLOW_ANSWER", response("ROUTE", uuid4()))
    result = await t.say("Living room AC.")
    assert result.interpretation
    assert result.interpretation.reason_code == InterpretationReason.PROPOSAL_OUTSIDE_CATALOG
    assert await active(t) == before
    await no_effects(t)


async def test_explicit_cancel_closes_emergency_without_tools(t: TextHarness) -> None:
    await location_pending(t)
    calls = len(t.provider.turn_requests)
    result = await t.say("Cancel.")
    assert result.interpretation and result.interpretation.outcome == ReplyOutcome.CANCEL
    assert result.response_plan.intent == Intent.CANCELLED
    state, _ = await t.store.load(t.source.session_id)
    assert state.active_workflow and state.active_workflow.status == Status.CANCELLED
    assert len(t.provider.turn_requests) == calls  # Deterministic control; no model call.
    await no_effects(t)


async def test_handoff_transition_from_pending_emergency_question(t: TextHarness) -> None:
    await location_pending(t)
    t.reply("WORKFLOW_ANSWER", "human_handoff")
    unclear = await t.say("I want to talk to a person.")
    assert unclear.interpretation
    assert unclear.interpretation.reason_code == InterpretationReason.ANSWER_CONFLICTS_WITH_HANDOFF
    t.reply("NEW_INTENT", "human_handoff")
    ask = await t.say("I want to talk to a person.")
    assert ask.response_plan.intent == Intent.CONFIRM
    run = await active(t)
    assert run.workflow_key.value == "human_handoff" and run.business_context
    assert FieldName.ISSUE in run.business_context.fields
    created = await t.say("Yes, please.")
    assert "No live transfer" in created.response_text
    assert await t.source.count(Handoff) == 1 and await t.source.count(EmergencyIncident) == 0


async def test_replay_of_interpreted_answer_is_deterministic(t: TextHarness) -> None:
    await location_pending(t)
    t.reply("WORKFLOW_ANSWER", "non_emergency_maintenance", "ac_not_cooling")
    request = await t.request("Living room AC.")
    first = await t.agent.process(request)
    calls = len(t.provider.turn_requests)
    # A fresh orchestrator with no model at all returns the saved result; no re-interpretation.
    fresh = TurnOrchestrator(
        SQLAlchemyTurnStore(t.source.database), ServiceRouter(), t.source.executor
    )
    replay = await fresh.process(request)
    assert replay.replayed and replay.interpretation == first.interpretation
    assert replay.response_text == first.response_text
    assert len(t.provider.turn_requests) == calls
    run = await active(t)
    assert FieldName.LOCATION not in run.field_history  # Recorded once, not twice.
    await no_effects(t)


async def test_palm_pending_reply_cannot_use_atlas_catalog(t: TextHarness) -> None:
    from app.db.models import ConversationSession, WorkflowRun

    palm = await t.store.create_demo("Alex")
    try:
        _, context = await t.store.load(palm.session_id)
        leasing = next(s for s in context.enabled_services if s.key == "leasing")
        tour = next(s for s in leasing.scenarios if s.key == "schedule_tour")
        t.provider.result = response("ROUTE", leasing.property_service_id, tour.id)
        first = await t.agent.process(
            AgentTurnRequest(
                session_id=palm.session_id,
                turn_id=uuid4(),
                turn_number=1,
                expected_revision=palm.revision,
                utterance="Can I tour an apartment?",
            )
        )
        assert asked(first) == FieldName.TOUR_TIME
        atlas_emergency = next(
            s for s in t.source.context.enabled_services if s.key == "emergency_maintenance"
        )
        t.provider.turn_result = turn(
            "WORKFLOW_ANSWER", response("ROUTE", atlas_emergency.property_service_id)
        )
        reply = await t.agent.process(
            AgentTurnRequest(
                session_id=palm.session_id,
                turn_id=uuid4(),
                turn_number=2,
                expected_revision=first.revision,
                utterance="Tomorrow afternoon",
            )
        )
        assert reply.interpretation
        assert reply.interpretation.reason_code == InterpretationReason.PROPOSAL_OUTSIDE_CATALOG
        assert reply.routing_decision.property_service_id != atlas_emergency.property_service_id
        assert reply.workflow_decision is None and not reply.tool_results
        sent = t.provider.turn_requests[-1].model_dump_json()
        assert "emergency_maintenance" not in sent
    finally:
        async with t.source.database.session() as session:
            rows = await session.scalars(
                select(ToolCall).where(ToolCall.session_id == palm.session_id)
            )
            assert rows.first() is None
            await session.execute(delete(AgentTurn).where(AgentTurn.session_id == palm.session_id))
            await session.execute(
                delete(WorkflowRun).where(WorkflowRun.session_id == palm.session_id)
            )
            await session.execute(
                delete(ConversationSession).where(ConversationSession.id == palm.session_id)
            )


@pytest.fixture
def t(text_harness: TextHarness) -> TextHarness:
    return text_harness
