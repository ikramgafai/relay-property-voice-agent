"""Plan authorized statements, independently of natural-language rendering."""

from app.agent.reply_schemas import ReplyOutcome, TurnInterpretation
from app.agent.schemas import Intent, OperationFact, ResponsePlan
from app.property.schemas import PropertyContext
from app.routing.schemas import DecisionType, RoutingDecision
from app.tools.schemas import ToolResult, ToolStatus
from app.workflows.schemas import (
    ActionType,
    ConversationState,
    FieldName,
    WorkflowDecision,
    WorkflowDecisionType,
)

QUESTIONS = {
    FieldName.ISSUE: "What issue are you experiencing?",
    FieldName.SYMPTOMS: "What symptoms have you noticed?",
    FieldName.LOCATION: "Where is the issue happening?",
    FieldName.HAZARD: "What hazards have you noticed nearby?",
    FieldName.SEVERITY: "How severe is the issue right now?",
    FieldName.UNIT: "Which unit is affected?",
    FieldName.TOUR_TIME: "When would you like to request a tour?",
    FieldName.TOUR_AT: "What exact date, time and timezone would you like?",
    FieldName.CONTACT: "What contact details should accompany the request?",
    FieldName.HANDOFF_REASON: "What would you like help from a person with?",
}


def reply_clarification(service_name: str, field: FieldName) -> str:
    """Backend-rendered re-ask for an unresolved reply; never model-authored (ADR 008)."""
    return (
        f"I still need one detail for your {service_name} request. {QUESTIONS[field]} "
        "If you meant a different request, please describe it, or say cancel to stop "
        "this request."
    )


def build_plan(
    context: PropertyContext,
    state: ConversationState,
    route: RoutingDecision,
    decision: WorkflowDecision | None,
    verified: ToolResult | None,
    interpretation: TurnInterpretation | None = None,
) -> ResponsePlan:
    plan = ResponsePlan(
        intent=Intent.UNABLE,
        property_name=context.property.name,
        unit_number=context.unit.number if context.unit else None,
        service_name=route.service_name,
    )
    if interpretation is not None and interpretation.outcome == ReplyOutcome.AMBIGUOUS:
        active = next(
            (
                s
                for s in context.enabled_services
                if s.property_service_id == interpretation.active_property_service_id
            ),
            None,
        )
        plan.intent = Intent.REPLY_CLARIFY
        plan.service_name = active.display_name if active else None
        plan.missing_field = interpretation.pending_field
        plan.question = reply_clarification(
            plan.service_name or "current", interpretation.pending_field
        )
        return ResponsePlan.model_validate(plan.model_dump())
    if route.decision == DecisionType.CLARIFICATION_REQUIRED:
        plan.intent = Intent.CLARIFY
        plan.question = route.clarification_question
    elif route.decision == DecisionType.ABSTAINED:
        return plan
    elif verified is not None:
        plan.operation_status = verified.status
        if verified.status == ToolStatus.SUCCEEDED:
            request = decision.action_request if decision else None
            if (
                request is None
                or request.action_id != verified.action_request_id
                or request.action_type != verified.action_type
            ):
                raise ValueError("Unmatched execution receipt")
            assert verified.operation_id
            plan.intent = Intent.SUCCESS
            plan.operation = OperationFact(
                action_request_id=request.action_id,
                execution_id=verified.execution_id,
                action_type=request.action_type,
                operation_id=verified.operation_id,
            )
            if request.action_type == ActionType.HANDOFF:
                run = state.active_workflow
                business = run.business_context if run else None
                plan.summary = [f"Property: {context.property.name}"]
                if context.resident:
                    plan.summary.append(
                        f"Resident: {context.resident.first_name} {context.resident.last_name}"
                    )
                if context.unit:
                    plan.summary.append(f"Unit: {context.unit.number}")
                if business:
                    plan.summary.extend(
                        f"Reported {key.value}: {value.value}"
                        for key, value in business.fields.items()
                    )
                plan.summary.extend(
                    f"Verified {c.action_type.value}: {c.operation_id}"
                    for w in state.workflows
                    for c in w.completed_actions
                )
        else:
            plan.intent = {
                ToolStatus.FAILED: Intent.FAILED,
                ToolStatus.UNKNOWN_OUTCOME: Intent.UNKNOWN,
                ToolStatus.PENDING: Intent.PENDING,
            }[verified.status]
    elif decision:
        match decision.decision_type:
            case WorkflowDecisionType.ASK_USER:
                assert decision.question
                plan.intent = Intent.ASK
                plan.missing_field = decision.question.field
                plan.question = QUESTIONS[decision.question.field]
            case WorkflowDecisionType.WAIT_FOR_CONFIRMATION:
                plan.intent = Intent.CONFIRM
                run = state.active_workflow
                action = (
                    run.pending_confirmation.action_type
                    if run and run.pending_confirmation
                    else None
                )
                noun = (
                    {
                        ActionType.MAINTENANCE: "maintenance request",
                        ActionType.TOUR: "tour request",
                        ActionType.HANDOFF: "human handoff request",
                    }.get(action, "request")
                    if action is not None
                    else "request"
                )
                plan.question = f"Would you like me to create the {noun}?"
            case WorkflowDecisionType.REQUEST_ACTION:
                plan.intent = Intent.PENDING
                plan.operation_status = ToolStatus.PENDING
            case WorkflowDecisionType.CANCEL:
                plan.intent = Intent.CANCELLED
            case WorkflowDecisionType.UNSUPPORTED_WORKFLOW:
                plan.intent = Intent.UNSUPPORTED
            case WorkflowDecisionType.COMPLETE:
                plan.intent = Intent.INFORMATION
    return ResponsePlan.model_validate(plan.model_dump())
