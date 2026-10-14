import ast
from pathlib import Path
from uuid import uuid4

import pytest
from app.routing.router import ServiceRouter
from app.routing.schemas import DecisionType, RoutingRequest
from app.routing.validation import CatalogViolation
from app.workflows.engine import WorkflowEngine
from app.workflows.errors import WorkflowError
from app.workflows.schemas import (
    ActionType,
    ConversationState,
    EmergencyPayload,
    HandoffPayload,
    Status,
)
from app.workflows.schemas import (
    FieldName as F,
)
from app.workflows.schemas import (
    WorkflowDecisionType as D,
)
from tests.support.routing import StubProvider, response
from tests.support.workflows import atlas_context, route_for, turn

from evaluations.catalogs import catalogs


async def test_maintenance_confirmation_replay_and_provenance() -> None:
    context = atlas_context()
    route = await route_for(context, "non_emergency_maintenance")
    engine = WorkflowEngine()
    initial = ConversationState.start(uuid4(), context)
    first = engine.advance(context, route, initial, turn(1, "My AC isn't cooling."))
    assert not initial.workflows  # caller's state is never mutated
    assert first.decision.decision_type == D.ASK_USER
    assert first.decision.question and first.decision.question.field == F.SYMPTOMS
    workflow = first.state.active_workflow
    assert workflow and workflow.collected_fields[F.UNIT].value == "204"
    assert workflow.collected_fields[F.UNIT].source == "property_context"
    second = engine.advance(context, route, first.state, turn(2, fields={F.SYMPTOMS: "Warm air"}))
    assert second.decision.decision_type == D.WAIT_FOR_CONFIRMATION
    workflow = second.state.active_workflow
    assert workflow and workflow.pending_confirmation
    assert workflow.collected_fields[F.SYMPTOMS].source_turn == 2
    confirmed = engine.advance(context, route, second.state, turn(3, confirmation=True))
    action = confirmed.decision.action_request
    assert action and action.action_type == ActionType.MAINTENANCE
    assert action.action_id == workflow.pending_confirmation.action_id
    repeated = engine.advance(context, route, confirmed.state, turn(3, confirmation=True))
    assert repeated == confirmed
    assert repeated.state.active_workflow
    assert len(repeated.state.active_workflow.requested_actions) == 1
    assert not repeated.state.active_workflow.completed_actions
    assert repeated.state.active_workflow.status == Status.ACTION_REQUIRED
    with pytest.raises(WorkflowError, match="REPLAY_CONTENT_MISMATCH"):
        engine.advance(context, route, confirmed.state, turn(3, "changed", confirmation=True))
    with pytest.raises(WorkflowError, match="TURN_OUT_OF_ORDER"):
        engine.advance(context, route, confirmed.state, turn(1))
    assert (
        ConversationState.model_validate_json(confirmed.state.model_dump_json()) == confirmed.state
    )


async def test_emergency_then_handoff_preserves_context_and_supersedes_action() -> None:
    context = atlas_context()
    engine = WorkflowEngine()
    route = await route_for(context, "emergency_maintenance")
    result = engine.advance(
        context,
        route,
        ConversationState.start(uuid4(), context),
        turn(
            1,
            "Water is pouring out",
            fields={F.LOCATION: "Kitchen", F.HAZARD: "Near wiring", F.SEVERITY: "Continuous flow"},
        ),
    )
    action = result.decision.action_request
    assert action and isinstance(action.payload, EmergencyPayload)
    assert action.action_type == ActionType.EMERGENCY and not action.requires_confirmation
    handoff = await route_for(context, "human_handoff")
    result = engine.advance(context, handoff, result.state, turn(2, "I want to speak to someone"))
    assert result.decision.transition
    assert action.action_id in result.decision.transition.superseded_action_ids
    assert result.state.workflows[0].requested_actions[0].disposition == "SUPERSEDED"
    result = engine.advance(context, handoff, result.state, turn(3, confirmation=True))
    action = result.decision.action_request
    assert action and isinstance(action.payload, HandoffPayload)
    assert action.payload.business_context
    assert action.payload.business_context.service.service_key == "emergency_maintenance"
    assert action.payload.business_context.fields[F.HAZARD].value == "Near wiring"
    assert all(not w.completed_actions for w in result.state.workflows)


@pytest.mark.parametrize("action", ["CLARIFY", "ABSTAIN"])
async def test_uncertain_routing_does_not_start_workflow(action: str) -> None:
    context = atlas_context()
    route = await ServiceRouter(StubProvider(response(action))).route(
        RoutingRequest(utterance="unclear", property_context=context)
    )
    result = WorkflowEngine().advance(
        context, route, ConversationState.start(uuid4(), context), turn(1)
    )
    assert result.decision.decision_type == D.AWAIT_ROUTING
    assert result.state.active_workflow is None and not result.state.workflows


async def test_dynamic_pet_service_and_registry_configuration() -> None:
    context = catalogs()["oakwood"]
    route = await ServiceRouter().route(
        RoutingRequest(utterance="How do I register my dog?", property_context=context)
    )
    assert route.decision == DecisionType.ROUTED and route.service_key == "pet_services"
    state = ConversationState.start(uuid4(), context)
    result = WorkflowEngine().advance(context, route, state, turn(1))
    assert result.decision.decision_type == D.UNSUPPORTED_WORKFLOW
    assert not result.state.workflows
    context.enabled_services[0].configuration["workflow_key"] = "human_handoff"
    result = WorkflowEngine().advance(context, route, state, turn(1))
    assert result.decision.decision_type == D.WAIT_FOR_CONFIRMATION
    context.enabled_services[0].configuration["workflow_key"] = "invented"
    result = WorkflowEngine().advance(context, route, state, turn(1))
    assert result.decision.decision_type == D.UNSUPPORTED_WORKFLOW


@pytest.mark.parametrize(
    "scenario", ["availability", "pricing", "schedule_tour", "application_question"]
)
async def test_leasing_branches(scenario: str) -> None:
    context = atlas_context()
    route = await route_for(context, "leasing", scenario_key=scenario)
    engine = WorkflowEngine()
    result = engine.advance(context, route, ConversationState.start(uuid4(), context), turn(1))
    if scenario == "application_question":
        assert result.decision.decision_type == D.UNSUPPORTED_WORKFLOW
    elif scenario != "schedule_tour":
        assert result.decision.decision_type == D.COMPLETE
        assert result.decision.message_intent == scenario
        assert result.decision.action_request is None
    else:
        assert result.decision.decision_type == D.ASK_USER
        result = engine.advance(
            context,
            route,
            result.state,
            turn(
                2,
                fields={
                    F.TOUR_TIME: "Saturday afternoon",
                    F.CONTACT: "caller@example.test",
                },
            ),
        )
        assert result.decision.decision_type == D.WAIT_FOR_CONFIRMATION
        result = engine.advance(context, route, result.state, turn(3, confirmation=True))
        assert result.decision.action_request
        assert result.decision.action_request.action_type == ActionType.TOUR


async def test_cancel_decline_and_edit_invalidate_confirmation() -> None:
    context = atlas_context()
    route = await route_for(context, "non_emergency_maintenance")
    engine = WorkflowEngine()
    result = engine.advance(
        context,
        route,
        ConversationState.start(uuid4(), context),
        turn(1, fields={F.SYMPTOMS: "Warm air"}),
    )
    workflow = result.state.active_workflow
    assert workflow and workflow.pending_confirmation
    original = workflow.pending_confirmation.action_id
    edited = engine.advance(context, route, result.state, turn(2, fields={F.SYMPTOMS: "No air"}))
    assert edited.state.active_workflow and edited.state.active_workflow.pending_confirmation
    assert edited.state.active_workflow.pending_confirmation.action_id != original
    for control in ({"cancel": True}, {"confirmation": False}):
        cancelled = engine.advance(
            context,
            route,
            edited.state,
            turn(3, cancel=control.get("cancel", False), confirmation=control.get("confirmation")),
        )
        assert cancelled.decision.decision_type == D.CANCEL
        assert cancelled.state.active_workflow
        assert cancelled.state.active_workflow.pending_confirmation is None
        assert not cancelled.state.active_workflow.requested_actions


async def test_property_catalog_and_identity_checks() -> None:
    context = atlas_context()
    route = await route_for(context, "emergency_maintenance")
    engine = WorkflowEngine()
    palm = catalogs()["palm"]
    with pytest.raises(WorkflowError, match="SESSION_CONTEXT_MISMATCH"):
        engine.advance(palm, route, ConversationState.start(uuid4(), context), turn(1))
    with pytest.raises(WorkflowError, match="ROUTE_PROPERTY_MISMATCH"):
        engine.advance(palm, route, ConversationState.start(uuid4(), palm), turn(1))
    state = ConversationState.start(uuid4(), context)
    forged = route.model_copy(update={"property_service_id": uuid4()})
    with pytest.raises(CatalogViolation):
        engine.advance(context, forged, state, turn(1))
    with pytest.raises(WorkflowError, match="ROUTE_SELECTION_MISMATCH"):
        engine.advance(
            context, route.model_copy(update={"service_key": "invented"}), state, turn(1)
        )
    with pytest.raises(WorkflowError, match="UNIT_CONTEXT"):
        engine.advance(context, route, state, turn(1, fields={F.UNIT: "318"}))
    context.enabled_services.pop()
    with pytest.raises(WorkflowError, match="STALE_ROUTING_CATALOG"):
        engine.advance(context, route, state, turn(1))


async def test_unrelated_transition_discards_fields_and_confirmation() -> None:
    context = atlas_context()
    engine = WorkflowEngine()
    route = await route_for(context, "leasing", scenario_key="schedule_tour")
    result = engine.advance(
        context,
        route,
        ConversationState.start(uuid4(), context),
        turn(
            1,
            fields={
                F.TOUR_TIME: "Saturday",
                F.CONTACT: "Contact me",
            },
        ),
    )
    emergency = await route_for(context, "emergency_maintenance")
    result = engine.advance(context, emergency, result.state, turn(2, "A leak"))
    change = result.decision.transition
    assert change and set(change.discarded_fields) == {F.TOUR_TIME, F.CONTACT}
    assert len(change.superseded_action_ids) == 1
    assert result.state.workflows[0].pending_confirmation is None
    assert (
        result.state.active_workflow
        and F.CONTACT not in result.state.active_workflow.collected_fields
    )


@pytest.mark.parametrize("action", ["CLARIFY", "ABSTAIN"])
async def test_uncertain_route_preserves_active_proposal_without_requesting(action: str) -> None:
    context = atlas_context()
    engine = WorkflowEngine()
    route = await route_for(context, "non_emergency_maintenance")
    first = engine.advance(
        context,
        route,
        ConversationState.start(uuid4(), context),
        turn(1, fields={F.SYMPTOMS: "Warm air"}),
    )
    uncertain = await ServiceRouter(StubProvider(response(action))).route(
        RoutingRequest(utterance="unclear", property_context=context)
    )
    result = engine.advance(context, uncertain, first.state, turn(2))
    assert result.decision.decision_type == D.AWAIT_ROUTING
    assert result.decision.action_request is None
    assert result.state.workflows == first.state.workflows


async def test_explicit_new_request_does_not_reuse_old_fields_or_action() -> None:
    context = atlas_context()
    engine = WorkflowEngine()
    route = await route_for(context, "non_emergency_maintenance")
    result = engine.advance(
        context,
        route,
        ConversationState.start(uuid4(), context),
        turn(1, "AC issue", fields={F.SYMPTOMS: "Warm air"}),
    )
    result = engine.advance(context, route, result.state, turn(2, confirmation=True))
    previous = result.decision.action_request
    assert previous
    result = engine.advance(context, route, result.state, turn(3, "Door issue", new_request=True))
    assert result.decision.decision_type == D.ASK_USER
    assert result.decision.transition and result.decision.transition.reason == "NEW_REQUEST"
    active = result.state.active_workflow
    assert active and active.workflow_id != previous.source_workflow_id
    assert F.SYMPTOMS not in active.collected_fields
    assert active.collected_fields[F.ISSUE].value == "Door issue"
    assert result.state.workflows[0].requested_actions[0].disposition == "SUPERSEDED"


def test_workflow_package_has_no_effect_or_orm_dependencies() -> None:
    import app.workflows

    root = Path(app.workflows.__file__).parent
    forbidden = {"sqlalchemy", "httpx", "asyncpg", "socket", "subprocess", "requests"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(name.split(".")[0] not in forbidden for name in names)
            assert all(
                not name.startswith(("app.db", "app.providers", "app.tools")) for name in names
            )
