"""Translate verified tool receipts into workflow completion metadata."""

from app.tools.schemas import ToolResult, ToolStatus
from app.workflows.errors import WorkflowError
from app.workflows.schemas import ActionRequest, CompletedAction, ConversationState, Status


def reconcile(
    state: ConversationState, request: ActionRequest, result: ToolResult
) -> ConversationState:
    state = state.model_copy(deep=True)
    run = next((w for w in state.workflows if w.workflow_id == request.source_workflow_id), None)
    if (
        run is None
        or result.action_request_id != request.action_id
        or result.action_type != request.action_type
    ):
        raise WorkflowError("RECEIPT_IDENTITY_MISMATCH")
    record = next((r for r in run.requested_actions if r.request == request), None)
    if record is None:
        raise WorkflowError("RECEIPT_REQUEST_MISSING")
    if result.status == ToolStatus.SUCCEEDED:
        assert result.operation_id is not None
        if not any(c.action_request_id == request.action_id for c in run.completed_actions):
            run.completed_actions.append(
                CompletedAction(
                    action_request_id=request.action_id,
                    execution_id=result.execution_id,
                    operation_id=result.operation_id,
                    action_type=request.action_type,
                    completion_turn=state.turn_number,
                )
            )
        record.disposition = "COMPLETED"
        run.status = Status.COMPLETED
        run.current_step = "operation_committed"
        run.unresolved_action_ids = [x for x in run.unresolved_action_ids if x != request.action_id]
    elif result.status in {ToolStatus.UNKNOWN_OUTCOME, ToolStatus.PENDING}:
        if request.action_id not in run.unresolved_action_ids:
            run.unresolved_action_ids.append(request.action_id)
        run.current_step = "await_reconciliation"
    else:
        run.current_step = "operation_failed"
    return ConversationState.model_validate(state.model_dump())
