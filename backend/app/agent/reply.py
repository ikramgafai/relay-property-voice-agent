"""Interpret replies to a pending workflow question (ADR 008).

ServiceRouter answers "which enabled property service is this request for?". This module answers
"is the utterance answering the current workflow question, or expressing another request?". One
advisory model call serves both: its routing part is validated by the unchanged router, and the
backend reply policy below decides. Nothing here persists state, mutates workflows or runs tools.
"""

import asyncio
import re
from dataclasses import dataclass
from time import perf_counter
from uuid import UUID

import structlog
from pydantic import ValidationError

from app.agent.protocol import TurnInterpretationProvider
from app.agent.reply_schemas import (
    InterpretationBasis,
    InterpretationReason,
    ReplyKind,
    ReplyOutcome,
    TurnInterpretation,
    TurnInterpretationRequest,
    TurnInterpretationResponse,
)
from app.agent.response_plan import QUESTIONS
from app.property.schemas import PropertyContext
from app.providers.llm.protocol import ModelContractError
from app.routing.configured import match_configured
from app.routing.model import ModelRoutingRequest, ModelRoutingResponse
from app.routing.router import ServiceRouter
from app.routing.schemas import DecisionType, RoutingDecision, RoutingRequest
from app.workflows.definitions import Definition
from app.workflows.registry import resolve_workflow
from app.workflows.schemas import ConversationState, FieldName, Status, WorkflowState

ANSWER_PREFIX = "answer:"
# Deterministic control vocabulary for closing the current request; not a service keyword list.
CANCEL_PHRASES = frozenset(
    {"cancel", "cancel it", "cancel that", "cancel this", "cancel the request", "cancel my request"}
)


@dataclass(frozen=True)
class PendingQuestion:
    run: WorkflowState
    field: FieldName


@dataclass(frozen=True)
class Interpreted:
    interpretation: TurnInterpretation
    route: RoutingDecision


def pending_question(state: ConversationState) -> PendingQuestion | None:
    """The active run's open field question, derived from durable workflow state only."""
    run = state.active_workflow
    if (
        run is None
        or run.status != Status.WAITING_FOR_USER
        or run.pending_confirmation is not None
        or not run.missing_fields
    ):
        return None
    return PendingQuestion(run=run, field=run.missing_fields[0])


def is_cancel(text: str) -> bool:
    return re.sub(r"[^\w\s]", "", text.casefold()).strip() in CANCEL_PHRASES


def answer_text(text: str) -> str:
    """Verbatim field value; an explicit legacy prefix is removed, nothing else is extracted."""
    if text.casefold().startswith(ANSWER_PREFIX):
        return text.partition(":")[2].strip() or text
    return text


def _definition(context: PropertyContext, service_id: UUID | None) -> Definition | None:
    service = next(
        (s for s in context.enabled_services if s.property_service_id == service_id), None
    )
    return resolve_workflow(service) if service else None


def decide(
    kind: ReplyKind,
    proposal: RoutingDecision,
    pending: PendingQuestion,
    context: PropertyContext,
) -> tuple[ReplyOutcome, InterpretationReason]:
    """Backend reply policy over an advisory verdict and a catalog-validated proposal."""
    if proposal.contract_violation is not None:
        # A proposal outside this property's catalog makes the whole response untrustworthy.
        return ReplyOutcome.AMBIGUOUS, InterpretationReason.PROPOSAL_OUTSIDE_CATALOG
    if kind == "AMBIGUOUS":
        return ReplyOutcome.AMBIGUOUS, InterpretationReason.MODEL_REPORTED_AMBIGUOUS
    active_id = pending.run.service.property_service_id
    routed = proposal.decision == DecisionType.ROUTED
    different = routed and proposal.property_service_id != active_id
    target = _definition(context, proposal.property_service_id) if different else None
    if kind == "WORKFLOW_ANSWER":
        if not different:
            return ReplyOutcome.WORKFLOW_ANSWER, InterpretationReason.MODEL_WORKFLOW_ANSWER
        # The verdict conflicts with the model's own route. Pin only when that route is an
        # implemented routine workflow: a context-free reading of a fragment ("Living room AC.").
        if target is None:
            return (
                ReplyOutcome.AMBIGUOUS,
                InterpretationReason.ANSWER_CONFLICTS_WITH_UNSUPPORTED_SERVICE,
            )
        if target.safety_critical:
            return (
                ReplyOutcome.AMBIGUOUS,
                InterpretationReason.ANSWER_CONFLICTS_WITH_SAFETY_CRITICAL_SERVICE,
            )
        if target.preserves_business_context:
            return ReplyOutcome.AMBIGUOUS, InterpretationReason.ANSWER_CONFLICTS_WITH_HANDOFF
        return ReplyOutcome.WORKFLOW_ANSWER, InterpretationReason.ANSWER_PINNED_TO_ACTIVE_WORKFLOW
    return protect(pending, proposal, context, InterpretationReason.MODEL_NEW_INTENT)


def protect(
    pending: PendingQuestion,
    proposal: RoutingDecision,
    context: PropertyContext,
    reason: InterpretationReason,
) -> tuple[ReplyOutcome, InterpretationReason]:
    """New intent may not automatically leave an unfinished safety-critical run for routine work."""
    active = _definition(context, pending.run.service.property_service_id)
    if (
        proposal.decision == DecisionType.ROUTED
        and proposal.property_service_id != pending.run.service.property_service_id
        and active is not None
        and active.safety_critical
        and not pending.run.completed_actions
    ):
        target = _definition(context, proposal.property_service_id)
        if (
            target is not None
            and not target.safety_critical
            and not target.preserves_business_context
        ):
            return (
                ReplyOutcome.AMBIGUOUS,
                InterpretationReason.PROTECTED_WORKFLOW_DOWNGRADE_BLOCKED,
            )
    return ReplyOutcome.NEW_INTENT, reason


class TurnInterpreter:
    def __init__(
        self,
        router: ServiceRouter,
        provider: TurnInterpretationProvider | None = None,
    ) -> None:
        self.router = router
        self.provider = provider

    async def interpret(
        self,
        utterance: str,
        context: PropertyContext,
        pending: PendingQuestion,
    ) -> Interpreted:
        run = pending.run
        tick = perf_counter()
        base = RoutingRequest(utterance=answer_text(utterance), property_context=context)
        record = {
            "pending_field": pending.field,
            "active_property_service_id": run.service.property_service_id,
        }
        if is_cancel(utterance) or utterance.casefold().startswith(ANSWER_PREFIX):
            cancel = is_cancel(utterance)
            return await self._pinned(
                base,
                pending,
                TurnInterpretation(
                    outcome=ReplyOutcome.CANCEL if cancel else ReplyOutcome.WORKFLOW_ANSWER,
                    basis=InterpretationBasis.CANCEL_CONTROL
                    if cancel
                    else InterpretationBasis.ANSWER_PREFIX,
                    reason_code=InterpretationReason.EXPLICIT_CANCEL
                    if cancel
                    else InterpretationReason.EXPLICIT_ANSWER,
                    **record,
                ),
            )
        if match_configured(utterance, context).service_id is not None:
            # An explicit configured catalog request is stronger evidence than any reply reading.
            proposal = await self.router.route(base)
            outcome, reason = protect(
                pending, proposal, context, InterpretationReason.CONFIGURED_REQUEST
            )
            return self._finish(
                TurnInterpretation(
                    outcome=outcome,
                    basis=InterpretationBasis.CONFIGURED_EVIDENCE,
                    reason_code=reason,
                    proposal=proposal,
                    **record,
                ),
                proposal,
            )
        kind: ReplyKind | None = None
        model_proposal: ModelRoutingResponse | Exception
        try:
            if self.provider is None:
                raise RuntimeError("No configured interpretation provider")
            service = next(
                (
                    s
                    for s in context.enabled_services
                    if s.property_service_id == run.service.property_service_id
                ),
                None,
            )
            if service is None:
                raise RuntimeError("Active service is no longer enabled")
            async with asyncio.timeout(self.router.timeout_seconds):
                response = await self.provider.interpret_turn(
                    TurnInterpretationRequest(
                        routing=ModelRoutingRequest.from_request(base),
                        active_property_service_id=service.property_service_id,
                        active_service_name=service.display_name,
                        active_workflow=run.workflow_key.value,
                        pending_field=pending.field,
                        pending_question=QUESTIONS[pending.field],
                    )
                )
            # Even a provider returning a constructed/mutated model cannot skip validation.
            response = TurnInterpretationResponse.model_validate(response.model_dump())
            kind = response.reply_kind
            model_proposal = response.routing
            basis = InterpretationBasis.MODEL
        except (ModelContractError, ValidationError, AttributeError) as exc:
            model_proposal, basis = exc, InterpretationBasis.MODEL_CONTRACT_VIOLATION
        except Exception as exc:
            # Provider boundary: messages may contain credentials or raw output; never logged.
            model_proposal, basis = exc, InterpretationBasis.MODEL_UNAVAILABLE
        model_ms = (perf_counter() - tick) * 1000
        # The unchanged router validates the model's routing part against this catalog.
        proposal = await self.router.route(base, model_proposal=model_proposal)
        if kind is None:
            outcome, reason = (
                ReplyOutcome.AMBIGUOUS,
                InterpretationReason.INTERPRETATION_UNAVAILABLE,
            )
        else:
            outcome, reason = decide(kind, proposal, pending, context)
        interpretation = TurnInterpretation(
            outcome=outcome,
            basis=basis,
            reason_code=reason,
            model_reply_kind=kind,
            proposal=proposal,
            model_duration_ms=model_ms,
            **record,
        )
        if outcome == ReplyOutcome.WORKFLOW_ANSWER:
            return await self._pinned(base, pending, interpretation)
        return self._finish(interpretation, proposal)

    async def _pinned(
        self,
        base: RoutingRequest,
        pending: PendingQuestion,
        interpretation: TurnInterpretation,
    ) -> Interpreted:
        """Continue on the active run's own service and scenario, still catalog-validated."""
        request = base.model_copy(
            update={
                "continuation_service_id": pending.run.service.property_service_id,
                "continuation_scenario_id": pending.run.service.scenario_id,
            }
        )
        route = await self.router.route(request, pin_continuation=True)
        if route.decision != DecisionType.ROUTED:
            interpretation = interpretation.model_copy(
                update={
                    "outcome": ReplyOutcome.AMBIGUOUS,
                    "reason_code": InterpretationReason.ACTIVE_SERVICE_UNAVAILABLE,
                }
            )
        return self._finish(interpretation, route)

    @staticmethod
    def _finish(interpretation: TurnInterpretation, route: RoutingDecision) -> Interpreted:
        interpretation = TurnInterpretation.model_validate(interpretation.model_dump())
        structlog.get_logger().info(
            "TURN_INTERPRETED",
            outcome=interpretation.outcome.value,
            basis=interpretation.basis.value,
            reason_code=interpretation.reason_code.value,
            model_reply_kind=interpretation.model_reply_kind,
            pending_field=interpretation.pending_field.value,
            proposal_decision=interpretation.proposal.decision.value
            if interpretation.proposal
            else None,
            proposal_service_key=interpretation.proposal.service_key
            if interpretation.proposal
            else None,
            model_duration_ms=round(interpretation.model_duration_ms, 3),
        )
        return Interpreted(interpretation=interpretation, route=route)
