"""Sequence authoritative components. Storage and side effects remain behind boundaries."""

from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

import structlog

from app.agent.errors import TurnError
from app.agent.interpretation import (
    ConfirmationInterpreter,
    DeterministicConfirmation,
    continuation,
    workflow_turn,
)
from app.agent.protocol import ResponseRenderer, TurnInterpretationProvider, TurnStore
from app.agent.reconciliation import reconcile
from app.agent.renderer import DeterministicRenderer
from app.agent.reply import TurnInterpreter, pending_question
from app.agent.reply_schemas import ReplyOutcome
from app.agent.response_plan import build_plan
from app.agent.schemas import AgentTurnRequest, AgentTurnResult, TurnCheckpoint
from app.routing.router import ServiceRouter
from app.routing.schemas import DecisionType, RoutingRequest
from app.tools.executor import ToolExecutor
from app.tools.schemas import ErrorCode, ToolResult, ToolStatus
from app.workflows.engine import WorkflowEngine
from app.workflows.schemas import ActionRequest


def uncertain(request: ActionRequest) -> ToolResult:
    return ToolResult(
        execution_id=uuid4(),
        action_request_id=request.action_id,
        action_type=request.action_type.value,
        status=ToolStatus.UNKNOWN_OUTCOME,
        error_code=ErrorCode.UNKNOWN,
        safe_error_message="Execution status requires reconciliation.",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        duration_ms=0,
    )


class TurnOrchestrator:
    def __init__(
        self,
        store: TurnStore,
        router: ServiceRouter,
        executor: ToolExecutor,
        renderer: ResponseRenderer | None = None,
        confirmation: ConfirmationInterpreter | None = None,
        interpretation: TurnInterpretationProvider | None = None,
    ) -> None:
        self.store, self.router, self.executor = store, router, executor
        self.renderer = renderer or DeterministicRenderer()
        self.confirmation = confirmation or DeterministicConfirmation()
        self.replies = TurnInterpreter(router, interpretation)
        self.engine = WorkflowEngine()

    async def process(self, request: AgentTurnRequest) -> AgentTurnResult:
        request = AgentTurnRequest.model_validate(request.model_dump())
        async with self.store.guard(request.session_id):
            checkpoint = await self.store.start(request)
            if checkpoint.result:
                return checkpoint.result.model_copy(update={"replayed": True}, deep=True)
            return await self._advance(checkpoint)

    async def _advance(self, checkpoint: TurnCheckpoint) -> AgentTurnResult:
        start = perf_counter()
        timings: dict[str, float] = {}
        request = checkpoint.request
        tick = perf_counter()
        state, context = await self.store.load(request.session_id)
        timings["context"] = (perf_counter() - tick) * 1000
        structlog.get_logger().info(
            "PROPERTY_CONTEXT_LOADED",
            session_id=str(request.session_id),
            property_id=str(state.property_id),
        )
        if checkpoint.route is None:
            pending = pending_question(state)
            if pending is not None:
                # Workflow-reply interpretation precedes business routing (ADR 008).
                interpreted = await self.replies.interpret(request.utterance, context, pending)
                checkpoint.interpretation = interpreted.interpretation
                checkpoint.route = interpreted.route
            else:
                routing_text = request.utterance
                if routing_text.casefold().startswith("answer:"):
                    routing_text = routing_text.partition(":")[2].strip() or request.utterance
                routing_request = RoutingRequest(utterance=routing_text, property_context=context)
                run = state.active_workflow
                if run and continuation(state, request.utterance, self.confirmation):
                    routing_request.continuation_service_id = run.service.property_service_id
                    routing_request.continuation_scenario_id = run.service.scenario_id
                checkpoint.route = await self.router.route(routing_request)
            await self.store.checkpoint(checkpoint)
        route = checkpoint.route
        interpretation = checkpoint.interpretation
        ambiguous = interpretation is not None and interpretation.outcome == ReplyOutcome.AMBIGUOUS
        timings["routing"] = route.duration_ms
        tick = perf_counter()
        if not checkpoint.workflow_saved:
            if (
                state.turn_number != request.turn_number - 1
                or state.revision != request.expected_revision
            ):
                raise TurnError("STALE_TURN")
            if route.decision == DecisionType.ROUTED and not ambiguous:
                turn = workflow_turn(
                    request,
                    checkpoint.occurred_at,
                    state,
                    route,
                    self.confirmation,
                    interpretation,
                )
                outcome = self.engine.advance(context, route, state, turn)
                state = outcome.state
                checkpoint.decision = outcome.decision.model_copy(update={"workflow_state": None})
            else:
                # No workflow invocation for clarification/abstention/ambiguous replies; consume
                # the transport turn only. The pending question remains open in durable state.
                state.turn_number = request.turn_number
            checkpoint.workflow_saved = True
            saved = await self.store.checkpoint(checkpoint, state)
            assert saved is not None
            state = saved
        timings["workflow"] = (perf_counter() - tick) * 1000
        decision = checkpoint.decision
        action = decision.action_request if decision else None
        tick = perf_counter()
        if action:
            if checkpoint.tool_result is None:
                unresolved = (
                    state.active_workflow
                    and action.action_id in state.active_workflow.unresolved_action_ids
                )
                if checkpoint.execution_started or unresolved:
                    # Crash recovery is read-only reconciliation, never another dispatch.
                    checkpoint.tool_result = await self.store.verified(
                        request.session_id, action
                    ) or uncertain(action)
                else:
                    checkpoint.execution_started = True
                    await self.store.checkpoint(checkpoint)
                    checkpoint.tool_result = await self.executor.execute(request.session_id, action)
                await self.store.checkpoint(checkpoint)
            receipt = checkpoint.tool_result
            if receipt.status == ToolStatus.SUCCEEDED:
                verified = await self.store.verified(request.session_id, action)
                if (
                    verified is None
                    or verified.operation_id != receipt.operation_id
                    or verified.execution_id != receipt.execution_id
                ):
                    receipt = uncertain(action)
                else:
                    receipt = verified
                checkpoint.tool_result = receipt
            state = reconcile(state, action, receipt)
        timings["tools"] = (perf_counter() - tick) * 1000
        plan = build_plan(context, state, route, decision, checkpoint.tool_result, interpretation)
        tick = perf_counter()
        try:
            text = await self.renderer.render(plan)
        except Exception:
            text = await DeterministicRenderer().render(plan)
        timings["rendering"] = (perf_counter() - tick) * 1000
        timings["total"] = (perf_counter() - start) * 1000
        result = AgentTurnResult(
            session_id=request.session_id,
            turn_id=request.turn_id,
            turn_number=request.turn_number,
            routing_decision=route,
            interpretation=interpretation,
            workflow_decision=decision,
            tool_results=[checkpoint.tool_result] if checkpoint.tool_result else [],
            response_plan=plan,
            response_text=text,
            revision=state.revision,
            status="UNRESOLVED"
            if checkpoint.tool_result
            and checkpoint.tool_result.status in {ToolStatus.PENDING, ToolStatus.UNKNOWN_OUTCOME}
            else "COMPLETED",
            timings_ms=timings,
        )
        result = await self.store.finish(checkpoint, state, result)
        structlog.get_logger().info(
            "AGENT_TURN_COMPLETED",
            session_id=str(request.session_id),
            turn_id=str(request.turn_id),
            routing_result=route.decision.value,
            reply_outcome=interpretation.outcome.value if interpretation else None,
            workflow_decision=decision.decision_type.value if decision else None,
            action_types=[r.action_type for r in result.tool_results],
            tool_execution_ids=[str(r.execution_id) for r in result.tool_results],
            response_intent=plan.intent.value,
            duration_ms=timings["total"],
        )
        return result
