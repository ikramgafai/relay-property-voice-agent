"""Explicit preservation and supersession policy for workflow changes."""

from uuid import UUID

from app.workflows.schemas import (
    BusinessContext,
    FieldName,
    Status,
    Transition,
    UserTurn,
    WorkflowKey,
    WorkflowState,
)

MAINTENANCE_FIELDS = {FieldName.ISSUE, FieldName.SYMPTOMS, FieldName.UNIT}
BUSINESS_FIELDS = MAINTENANCE_FIELDS | {
    FieldName.LOCATION,
    FieldName.HAZARD,
    FieldName.SEVERITY,
    FieldName.TOUR_TIME,
    FieldName.CONTACT,
}


def retire(workflow: WorkflowState, *, cancelled: bool = False) -> list[UUID]:
    identities: list[UUID] = []
    if workflow.pending_confirmation is not None:
        identities.append(workflow.pending_confirmation.action_id)
        workflow.pending_confirmation = None
    for record in workflow.requested_actions:
        if record.disposition == "REQUESTED":
            identities.append(record.request.action_id)
            record.disposition = "CANCELLED" if cancelled else "SUPERSEDED"
    workflow.missing_fields = []
    if workflow.status not in {Status.COMPLETED, Status.CANCELLED}:
        workflow.status = Status.CANCELLED if cancelled else Status.ESCALATED
        workflow.current_step = "cancelled" if cancelled else "superseded"
    return identities


def transition(old: WorkflowState, new: WorkflowState, turn: UserTurn) -> Transition:
    preserved: set[FieldName] = set()
    maintenance = {WorkflowKey.MAINTENANCE, WorkflowKey.EMERGENCY}
    if not turn.new_request and old.workflow_key in maintenance and new.workflow_key in maintenance:
        preserved = set(old.collected_fields) & MAINTENANCE_FIELDS
    if not turn.new_request and new.workflow_key == WorkflowKey.HANDOFF:
        preserved = set(old.collected_fields) & BUSINESS_FIELDS
        new.business_context = (
            old.business_context.model_copy(deep=True)
            if old.business_context
            else BusinessContext(
                service=old.service.model_copy(deep=True),
                source_workflow_id=old.workflow_id,
                fields={
                    k: v.model_copy(deep=True)
                    for k, v in old.collected_fields.items()
                    if k in preserved
                },
                field_history={
                    k: [v.model_copy(deep=True) for v in values]
                    for k, values in old.field_history.items()
                    if k in preserved
                },
            )
        )
    else:
        new.collected_fields = {
            k: v.model_copy(deep=True) for k, v in old.collected_fields.items() if k in preserved
        }
        new.field_history = {
            k: [v.model_copy(deep=True) for v in values]
            for k, values in old.field_history.items()
            if k in preserved
        }
    ids = retire(old)
    old.updated_at = turn.occurred_at
    return Transition(
        from_service=old.service,
        to_service=new.service,
        from_workflow=old.workflow_id,
        to_workflow=new.workflow_id,
        reason="NEW_REQUEST"
        if turn.new_request
        else (
            "ROUTED_SERVICE_CHANGED"
            if old.service.property_service_id != new.service.property_service_id
            else (
                "ROUTED_SCENARIO_CHANGED"
                if old.service != new.service
                else "WORKFLOW_BINDING_CHANGED"
            )
        ),
        preserved_fields=sorted(preserved),
        discarded_fields=sorted(set(old.collected_fields) - preserved),
        superseded_action_ids=ids,
        source_turn=turn.turn_number,
        timestamp=turn.occurred_at,
    )
