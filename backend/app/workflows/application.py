"""Application boundary: load, advance, persist, and emit non-content event metadata."""

from time import perf_counter
from uuid import UUID

import structlog

from app.property.schemas import PropertyContext
from app.routing.schemas import RoutingDecision
from app.workflows.engine import WorkflowEngine
from app.workflows.repository import ConversationRepository
from app.workflows.schemas import ConversationState, Status, TurnResult, UserTurn


class WorkflowApplication:
    def __init__(self, repository: ConversationRepository) -> None:
        self.repository = repository
        self.engine = WorkflowEngine()

    async def process(
        self,
        session_id: UUID,
        context: PropertyContext,
        route: RoutingDecision,
        turn: UserTurn,
    ) -> TurnResult:
        started = perf_counter()
        state = await self.repository.get(session_id, context.property.id)
        state = state or ConversationState.start(session_id, context)
        result = self.engine.advance(context, route, state, turn)
        if state.turn_number == result.state.turn_number:
            return result
        result.state = await self.repository.save(result.state)
        workflow = result.state.active_workflow
        previous = state.active_workflow
        metadata = {
            "session_id": str(session_id),
            "property_id": str(context.property.id),
            "workflow_id": str(workflow.workflow_id) if workflow else None,
            "service": workflow.service.service_key if workflow else None,
            "status": workflow.status.value if workflow else None,
            "step": workflow.current_step if workflow else None,
            "decision_type": result.decision.decision_type.value,
            "action_type": result.decision.action_request.action_type.value
            if result.decision.action_request
            else None,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "revision": result.state.revision,
        }
        log = structlog.get_logger()
        if workflow and (previous is None or previous.workflow_id != workflow.workflow_id):
            log.info("WORKFLOW_STARTED", **metadata)
        if result.decision.transition:
            change = result.decision.transition
            log.info(
                "WORKFLOW_ESCALATED",
                **metadata,
                from_workflow=str(change.from_workflow),
                from_service=change.from_service.service_key,
                transition_reason=change.reason,
                preserved_fields=[f.value for f in change.preserved_fields],
                discarded_fields=[f.value for f in change.discarded_fields],
            )
        if workflow and (previous is None or previous.current_step != workflow.current_step):
            log.info("WORKFLOW_STEP_CHANGED", **metadata)
        if (
            workflow
            and workflow.status == Status.COMPLETED
            and (previous is None or previous.status != Status.COMPLETED)
        ):
            log.info("WORKFLOW_COMPLETED", **metadata)
        log.info("WORKFLOW_DECISION", **metadata)
        return result
