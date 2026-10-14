"""Deterministic process state. Returns requests; has no executor or I/O dependency."""

import hashlib
import json
from uuid import uuid5

from app.property.schemas import PropertyContext
from app.routing.schemas import DecisionType, RoutingDecision, RoutingRequest
from app.routing.validation import catalog_fingerprint, resolve_selection
from app.workflows.definitions import Definition
from app.workflows.errors import WorkflowError
from app.workflows.registry import resolve_workflow
from app.workflows.schemas import (
    ActionContext,
    ActionRequest,
    CollectedField,
    ConversationState,
    FieldName,
    Question,
    RequestRecord,
    ServiceReference,
    Status,
    TurnResult,
    UserTurn,
    WorkflowDecision,
    WorkflowKey,
    WorkflowState,
    record_field,
)
from app.workflows.schemas import (
    WorkflowDecisionType as D,
)
from app.workflows.transitions import retire, transition


def input_digest(context: PropertyContext, route: RoutingDecision, turn: UserTurn) -> str:
    # Runtime timing and model explanation are not part of the application command identity.
    data = {
        "context": context.model_dump(mode="json"),
        "turn": turn.model_dump(mode="json"),
        "route": route.model_dump(
            mode="json",
            include={
                "decision",
                "property_id",
                "property_service_id",
                "scenario_id",
                "service_key",
                "scenario_key",
                "catalog_fingerprint",
                "clarification_service_ids",
            },
        ),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class WorkflowEngine:
    def advance(
        self,
        context: PropertyContext,
        route: RoutingDecision,
        state: ConversationState,
        turn: UserTurn,
    ) -> TurnResult:
        # Typed values are mutable; validate copies again at the application boundary.
        context = PropertyContext.model_validate(context.model_dump())
        route = RoutingDecision.model_validate(route.model_dump())
        turn = UserTurn.model_validate(turn.model_dump())
        state = ConversationState.model_validate(state.model_dump())
        request = RoutingRequest(utterance=turn.utterance, property_context=context)
        identity = (
            context.property.id,
            context.resident.id if context.resident else None,
            context.unit.id if context.unit else None,
        )
        if (state.property_id, state.resident_id, state.unit_id) != identity:
            raise WorkflowError("SESSION_CONTEXT_MISMATCH")
        if route.property_id != context.property.id:
            raise WorkflowError("ROUTE_PROPERTY_MISMATCH")
        if route.catalog_fingerprint != catalog_fingerprint(request):
            raise WorkflowError("STALE_ROUTING_CATALOG")
        service = None
        if route.decision == DecisionType.ROUTED:
            assert route.property_service_id is not None
            service, scenario = resolve_selection(
                context, route.property_service_id, route.scenario_id
            )
            if (route.service_key, route.service_name, route.scenario_key) != (
                service.key,
                service.display_name,
                scenario.key if scenario else None,
            ):
                raise WorkflowError("ROUTE_SELECTION_MISMATCH")
        for candidate in route.clarification_service_ids:
            resolve_selection(context, candidate, None)
        digest = input_digest(context, route, turn)
        if turn.turn_number == state.turn_number and state.last_decision is not None:
            if digest != state.last_turn_digest:
                raise WorkflowError("REPLAY_CONTENT_MISMATCH")
            return TurnResult(
                state=state.model_copy(deep=True),
                decision=state.last_decision.model_copy(deep=True),
            )
        if turn.turn_number != state.turn_number + 1:
            raise WorkflowError("TURN_OUT_OF_ORDER")
        result_state = state.model_copy(deep=True)
        old = result_state.active_workflow
        if old and turn.occurred_at < old.updated_at:
            raise WorkflowError("TURN_TIME_REVERSED")
        if turn.cancel:
            if old:
                retire(old, cancelled=True)
                old.updated_at = turn.occurred_at
            decision = WorkflowDecision(
                decision_type=D.CANCEL, workflow_state=old, reason_code="USER_CANCELLED"
            )
        elif service is None:
            decision = WorkflowDecision(
                decision_type=D.AWAIT_ROUTING,
                workflow_state=old,
                reason_code=route.decision.value,
            )
        else:
            definition = resolve_workflow(service)
            if definition is None:
                decision = WorkflowDecision(
                    decision_type=D.UNSUPPORTED_WORKFLOW,
                    workflow_state=old,
                    reason_code="NO_IMPLEMENTED_CAPABILITY",
                )
            else:
                reference = ServiceReference(
                    property_service_id=service.property_service_id,
                    service_key=service.key,
                    scenario_id=route.scenario_id,
                    scenario_key=route.scenario_key,
                )
                new_run = (
                    old is None
                    or turn.new_request
                    or (old.service != reference or old.workflow_key != definition.key)
                )
                if (
                    old
                    and old.completed_actions
                    and turn.incident_id is not None
                    and old.workflow_key == definition.key == WorkflowKey.EMERGENCY
                    and old.service.property_service_id == reference.property_service_id
                    and not turn.new_request
                ):
                    # New hazard evidence refines the committed incident, even if its
                    # configured scenario changes. It is not a new emergency business run.
                    new_run = False
                if new_run and turn.confirmation is not None:
                    raise WorkflowError("CONFIRMATION_HAS_NO_MATCHING_PROPOSAL")
                change = None
                if new_run:
                    workflow = WorkflowState(
                        workflow_id=uuid5(state.session_id, f"workflow:{turn.turn_number}"),
                        started_turn=turn.turn_number,
                        workflow_key=definition.key,
                        service=reference,
                        started_at=turn.occurred_at,
                        updated_at=turn.occurred_at,
                    )
                    if old:
                        change = transition(old, workflow, turn)
                        result_state.transitions.append(change)
                        result_state.previous_services.append(old.service.model_copy(deep=True))
                    result_state.workflows.append(workflow)
                    result_state.active_workflow_id = workflow.workflow_id
                else:
                    assert old is not None
                    workflow = old
                    workflow.service = reference
                result_state.current_service = reference
                decision = self._process(context, definition, workflow, turn, new_run)
                decision.transition = change
        result_state.turn_number = turn.turn_number
        result_state.last_turn_digest = digest
        result_state.last_decision = decision.model_copy(deep=True)
        return TurnResult(state=result_state, decision=decision.model_copy(deep=True))

    def _process(
        self,
        context: PropertyContext,
        definition: Definition,
        workflow: WorkflowState,
        turn: UserTurn,
        new_run: bool,
    ) -> WorkflowDecision:
        if (
            workflow.status == Status.COMPLETED
            and workflow.workflow_key == WorkflowKey.EMERGENCY
            and turn.incident_id is not None
        ):
            workflow.status = Status.ACTIVE
        if workflow.status in {Status.COMPLETED, Status.CANCELLED}:
            if turn.fields or turn.confirmation is not None:
                raise WorkflowError("TERMINAL_WORKFLOW_REQUIRES_NEW_REQUEST")
            return WorkflowDecision(
                decision_type=D.COMPLETE if workflow.status == Status.COMPLETED else D.CANCEL,
                workflow_state=workflow,
                reason_code="TERMINAL_WORKFLOW",
            )
        if turn.confirmation is not None and workflow.pending_confirmation is None:
            raise WorkflowError("CONFIRMATION_HAS_NO_MATCHING_PROPOSAL")
        allowed = set(definition.required)
        if turn.incident_id is not None:
            if (
                definition.key != WorkflowKey.EMERGENCY
                or new_run
                or FieldName.HAZARD not in turn.fields
            ):
                raise WorkflowError("INCIDENT_UPDATE_REQUIRES_ACTIVE_EMERGENCY_AND_HAZARD")
            workflow.target_incident_id = turn.incident_id
        if turn.requested_tour_at is not None:
            if definition.key != WorkflowKey.LEASING:
                raise WorkflowError("TOUR_TIME_REQUIRES_LEASING")
            turn.fields[FieldName.TOUR_AT] = turn.requested_tour_at.isoformat()
        if definition.key == WorkflowKey.LEASING:
            allowed.add(FieldName.TOUR_AT)
            if FieldName.TOUR_AT in turn.fields and turn.requested_tour_at is None:
                raise WorkflowError("USE_TYPED_TOUR_TIME_INPUT")
        if definition.key == WorkflowKey.EMERGENCY:
            allowed.add(FieldName.SYMPTOMS)
        if set(turn.fields) - allowed:
            raise WorkflowError("FIELD_NOT_SUPPORTED_BY_WORKFLOW")
        if context.unit and FieldName.UNIT in turn.fields:
            if turn.fields[FieldName.UNIT] != context.unit.number:
                raise WorkflowError("UNIT_CONTEXT_CANNOT_BE_OVERRIDDEN")
        if turn.fields:
            # A revised proposal needs fresh confirmation and a new identity.
            retire(workflow)
            workflow.status = Status.ACTIVE
        for key, value in turn.fields.items():
            record_field(workflow, key, CollectedField(value=value, source_turn=turn.turn_number))
        if (
            context.unit
            and FieldName.UNIT in allowed
            and FieldName.UNIT not in workflow.collected_fields
        ):
            record_field(
                workflow,
                FieldName.UNIT,
                CollectedField(
                    value=context.unit.number,
                    source_turn=turn.turn_number,
                    source="property_context",
                ),
            )
        if new_run:
            initial = (
                FieldName.HANDOFF_REASON
                if definition.key == WorkflowKey.HANDOFF
                else FieldName.ISSUE
            )
            if initial in allowed and initial not in turn.fields:
                # Verbatim user report, never inferred extraction. Preserve prior value in history.
                record_field(
                    workflow,
                    initial,
                    CollectedField(value=turn.utterance, source_turn=turn.turn_number),
                )
        workflow.updated_at = turn.occurred_at
        if definition.key == WorkflowKey.LEASING:
            if workflow.service.scenario_key in {"availability", "pricing"}:
                workflow.status = Status.COMPLETED
                workflow.current_step = "information_intent_ready"
                return WorkflowDecision(
                    decision_type=D.COMPLETE,
                    workflow_state=workflow,
                    reason_code="INFORMATIONAL_INTENT",
                    message_intent=workflow.service.scenario_key,
                )
            if workflow.service.scenario_key != "schedule_tour":
                return WorkflowDecision(
                    decision_type=D.UNSUPPORTED_WORKFLOW,
                    workflow_state=workflow,
                    reason_code="UNSUPPORTED_LEASING_SCENARIO",
                )
        workflow.missing_fields = [
            f for f in definition.required if f not in workflow.collected_fields
        ]
        if workflow.missing_fields:
            field = workflow.missing_fields[0]
            workflow.status = Status.WAITING_FOR_USER
            workflow.current_step = f"collect:{field.value}"
            return WorkflowDecision(
                decision_type=D.ASK_USER,
                workflow_state=workflow,
                question=Question(field=field, question_type=f"{definition.key}:{field}"),
                reason_code="MISSING_REQUIRED_FIELD",
            )
        if workflow.pending_confirmation and turn.confirmation is False:
            retire(workflow, cancelled=True)
            return WorkflowDecision(
                decision_type=D.CANCEL, workflow_state=workflow, reason_code="PROPOSAL_DECLINED"
            )
        active = next(
            (r.request for r in workflow.requested_actions if r.disposition == "REQUESTED"), None
        )
        action = active or workflow.pending_confirmation
        if action is None:
            payload = definition.payload(
                workflow,
                ActionContext(
                    property_id=context.property.id,
                    resident_id=context.resident.id if context.resident else None,
                    unit_id=context.unit.id if context.unit else None,
                ),
            )
            action = ActionRequest(
                action_id=uuid5(workflow.workflow_id, f"action:{turn.turn_number}"),
                action_type=payload.kind,
                payload=payload,
                requires_confirmation=definition.confirmation,
                source_workflow_id=workflow.workflow_id,
                source_turn=turn.turn_number,
            )
        if action.requires_confirmation and active is None and turn.confirmation is not True:
            workflow.pending_confirmation = action
            workflow.status = Status.WAITING_FOR_CONFIRMATION
            workflow.current_step = "confirm_action"
            return WorkflowDecision(
                decision_type=D.WAIT_FOR_CONFIRMATION,
                workflow_state=workflow,
                reason_code="EXPLICIT_CONFIRMATION_REQUIRED",
            )
        workflow.pending_confirmation = None
        if active is None:
            workflow.requested_actions.append(RequestRecord(request=action))
        workflow.status = Status.ACTION_REQUIRED
        workflow.current_step = "await_future_executor"
        return WorkflowDecision(
            decision_type=D.REQUEST_ACTION,
            workflow_state=workflow,
            action_request=action,
            reason_code="ACTION_REQUESTED_NOT_EXECUTED",
        )
