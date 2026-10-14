"""Authorization from stored workflow facts, not caller flags."""

import hashlib
import json
from uuid import UUID

from app.tools.errors import ToolFailure
from app.tools.schemas import ErrorCode, ExecutionContext
from app.workflows.registry import resolve_workflow
from app.workflows.schemas import ActionRequest, ActionType, Status, WorkflowKey

CAPABILITIES = {
    ActionType.MAINTENANCE: WorkflowKey.MAINTENANCE,
    ActionType.EMERGENCY: WorkflowKey.EMERGENCY,
    ActionType.EMERGENCY_UPDATE: WorkflowKey.EMERGENCY,
    ActionType.TOUR: WorkflowKey.LEASING,
    ActionType.HANDOFF: WorkflowKey.HANDOFF,
}


def request_hash(session_id: UUID, request: ActionRequest) -> str:
    canonical = {
        "version": 1,
        "session_id": str(session_id),
        "request": request.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def authorize(source: ExecutionContext, request: ActionRequest) -> None:
    workflow = source.workflow
    if (
        workflow.pending_confirmation
        and workflow.pending_confirmation.action_id == request.action_id
    ):
        raise ToolFailure(ErrorCode.CONFIRMATION)
    if (
        source.session.active_workflow_id != workflow.workflow_id
        or workflow.status != Status.ACTION_REQUIRED
    ):
        raise ToolFailure(ErrorCode.INELIGIBLE)
    record = next(
        (r for r in workflow.requested_actions if r.request.action_id == request.action_id), None
    )
    if record is None or record.disposition != "REQUESTED":
        raise ToolFailure(ErrorCode.INELIGIBLE)
    if request_hash(source.session.session_id, record.request) != request_hash(
        source.session.session_id, request
    ):
        raise ToolFailure(ErrorCode.VALIDATION)
    if CAPABILITIES.get(request.action_type) != workflow.workflow_key:
        raise ToolFailure(ErrorCode.UNSUPPORTED)
    service = next(
        (
            s
            for s in source.property_context.enabled_services
            if s.property_service_id == workflow.service.property_service_id
        ),
        None,
    )
    if service is None:
        raise ToolFailure(ErrorCode.INELIGIBLE)
    definition = resolve_workflow(service)
    if definition is None or definition.key != workflow.workflow_key:
        raise ToolFailure(ErrorCode.INELIGIBLE)
    if workflow.service.scenario_id is not None and not any(
        s.id == workflow.service.scenario_id for s in service.scenarios
    ):
        raise ToolFailure(ErrorCode.INELIGIBLE)
    if definition.confirmation != request.requires_confirmation:
        raise ToolFailure(ErrorCode.CONFIRMATION)
    # REQUESTED + ACTION_REQUIRED is Phase 4's persisted confirmation grant.
    # A caller-provided requires_confirmation=False cannot change that stored grant.


def validate_declaration(source: ExecutionContext, request: ActionRequest) -> None:
    proposals = [r.request for r in source.workflow.requested_actions]
    if source.workflow.pending_confirmation:
        proposals.append(source.workflow.pending_confirmation)
    stored = next((p for p in proposals if p.action_id == request.action_id), None)
    if stored is None or request_hash(source.session.session_id, stored) != request_hash(
        source.session.session_id, request
    ):
        raise ToolFailure(ErrorCode.VALIDATION)
