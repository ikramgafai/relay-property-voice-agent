"""ADR 006 cascade. No persistence, vendor SDK, workflow state or operational effects."""

import asyncio
from time import perf_counter

import structlog
from pydantic import ValidationError

from app.providers.llm.protocol import ModelContractError, RoutingModelProvider
from app.routing.configured import match_configured
from app.routing.model import ModelRoutingRequest, ModelRoutingResponse
from app.routing.schemas import (
    DecisionType,
    Evidence,
    EvidenceType,
    ReasonCode,
    RoutingDecision,
    RoutingMethod,
    RoutingRequest,
)
from app.routing.validation import (
    CatalogViolation,
    catalog_fingerprint,
    clarification_question,
    resolve_selection,
)


class ServiceRouter:
    def __init__(
        self, provider: RoutingModelProvider | None = None, *, timeout_seconds: float = 10
    ) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Routing timeout must be in (0, 60]")
        self.provider = provider
        self.timeout_seconds = timeout_seconds

    async def route(
        self,
        request: RoutingRequest,
        *,
        model_proposal: ModelRoutingResponse | Exception | None = None,
        pin_continuation: bool = False,
    ) -> RoutingDecision:
        """Select and validate a service for the current property.

        ``model_proposal`` supplies an already obtained model routing proposal (or the failure
        raised while obtaining it) so one interpretation call is not repeated; it receives
        exactly the same validation as a provider result. ``pin_continuation`` validates the
        persisted continuation service ahead of configured evidence; callers use it only after
        deciding that the utterance answers that workflow's pending question (ADR 008).
        """
        started = perf_counter()
        # Revalidate and snapshot mutable Pydantic input before any asynchronous work.
        request = RoutingRequest.model_validate(request.model_dump())
        context = request.property_context
        result = RoutingDecision(
            decision=DecisionType.ABSTAINED,
            property_id=context.property.id,
            reason_code=ReasonCode.NO_AVAILABLE_SERVICES,
            reason="The current property has no available services.",
            catalog_fingerprint=catalog_fingerprint(request),
            duration_ms=0,
        )
        log = structlog.get_logger(__name__).bind(
            property_id=str(context.property.id),
            catalog_fingerprint=result.catalog_fingerprint,
        )
        log.info("ROUTING_STARTED")
        if context.enabled_services:
            tick = perf_counter()
            match = match_configured(request.utterance, context)
            result.configured_duration_ms = (perf_counter() - tick) * 1000
            result.evidence = match.evidence
            if pin_continuation and request.continuation_service_id is not None:
                result.method = RoutingMethod.TRUSTED_CONTINUATION
                proposal = ModelRoutingResponse(
                    action="ROUTE",
                    property_service_id=request.continuation_service_id,
                    scenario_id=request.continuation_scenario_id,
                    clarification_kind=None,
                    candidate_service_ids=[],
                    reason="Answer to the persisted workflow question",
                )
            elif match.service_id is not None:
                result.method = RoutingMethod.CONFIGURED_EVIDENCE
                proposal = ModelRoutingResponse(
                    action="ROUTE",
                    property_service_id=match.service_id,
                    scenario_id=match.scenario_id,
                    clarification_kind=None,
                    candidate_service_ids=[],
                    reason="Unique configured match",
                )
            elif request.continuation_service_id is not None and not match.evidence:
                result.method = RoutingMethod.TRUSTED_CONTINUATION
                proposal = ModelRoutingResponse(
                    action="ROUTE",
                    property_service_id=request.continuation_service_id,
                    scenario_id=request.continuation_scenario_id,
                    clarification_kind=None,
                    candidate_service_ids=[],
                    reason="Reply to a persisted workflow prompt",
                )
            else:
                result.method = RoutingMethod.MODEL_ASSISTED
                result.model_fallback_used = True
                log.info(
                    "ROUTING_FALLBACK",
                    reason_code=(
                        "AMBIGUOUS_SERVICE" if match.evidence else "INSUFFICIENT_INFORMATION"
                    ),
                    stage="model_interpretation",
                )
                tick = perf_counter()
                proposal = None
                try:
                    if isinstance(model_proposal, Exception):
                        raise model_proposal
                    if model_proposal is not None:
                        raw = model_proposal
                    else:
                        if self.provider is None:
                            raise RuntimeError("No configured provider")
                        async with asyncio.timeout(self.timeout_seconds):
                            raw = await self.provider.interpret(
                                ModelRoutingRequest.from_request(request)
                            )
                    # Even a provider returning a constructed/mutated model cannot skip validation.
                    proposal = ModelRoutingResponse.model_validate(raw.model_dump())
                except (ModelContractError, ValidationError, AttributeError):
                    result.reason_code = ReasonCode.MODEL_CONTRACT_VIOLATION
                    result.reason = "Model output did not satisfy the routing contract."
                    result.contract_violation = "malformed_output"
                except Exception:
                    # Provider boundary: exception messages may contain credentials or raw output.
                    # asyncio.CancelledError is a BaseException and propagates to the caller.
                    result.reason_code = ReasonCode.MODEL_UNAVAILABLE
                    result.reason = "Model interpretation was unavailable; no route was guessed."
                finally:
                    result.model_duration_ms = (perf_counter() - tick) * 1000
            if proposal is not None:
                tick = perf_counter()
                try:
                    if proposal.action == "ROUTE":
                        assert proposal.property_service_id is not None
                        service, scenario = resolve_selection(
                            context,
                            proposal.property_service_id,
                            proposal.scenario_id,
                        )
                        result.decision = DecisionType.ROUTED
                        result.property_service_id = service.property_service_id
                        result.service_key = service.key
                        result.service_name = service.display_name
                        result.scenario_id = scenario.id if scenario else None
                        result.scenario_key = scenario.key if scenario else None
                        result.reason_code = (
                            ReasonCode.CURRENT_WORKFLOW_REPLY
                            if result.method == RoutingMethod.TRUSTED_CONTINUATION
                            else ReasonCode.MODEL_SELECTED_SERVICE
                            if result.model_fallback_used
                            else ReasonCode.EXPLICIT_CONFIGURED_MATCH
                        )
                        result.reason = "Selected a validated service from the current catalog."
                        if result.model_fallback_used:
                            result.evidence.append(
                                Evidence(
                                    kind=EvidenceType.MODEL_INTERPRETATION,
                                    property_service_id=service.property_service_id,
                                    scenario_id=result.scenario_id,
                                    detail=proposal.reason,
                                )
                            )
                    elif proposal.action == "CLARIFY":
                        # Expose backend-generated text only, never arbitrary model questions.
                        question = clarification_question(context, proposal.candidate_service_ids)
                        result.decision = DecisionType.CLARIFICATION_REQUIRED
                        result.clarification_question = question
                        result.clarification_service_ids = proposal.candidate_service_ids
                        result.reason_code = (
                            ReasonCode.AMBIGUOUS_SERVICE
                            if proposal.candidate_service_ids
                            else ReasonCode.INSUFFICIENT_INFORMATION
                        )
                        result.reason = "More information is needed to select an available service."
                    else:
                        result.reason_code = ReasonCode.OUT_OF_SCOPE
                        result.reason = "Model interpretation found no supported route."
                except CatalogViolation as exc:
                    result.reason_code = ReasonCode.MODEL_CONTRACT_VIOLATION
                    result.reason = "Model selection was outside the supplied catalog."
                    result.contract_violation = exc.kind
                finally:
                    result.validation_duration_ms = (perf_counter() - tick) * 1000
        result.duration_ms = (perf_counter() - started) * 1000
        result = RoutingDecision.model_validate(result.model_dump())
        event = "SERVICE_ROUTED" if result.decision == DecisionType.ROUTED else "ROUTING_FALLBACK"
        if result.reason_code in (
            ReasonCode.MODEL_UNAVAILABLE,
            ReasonCode.MODEL_CONTRACT_VIOLATION,
        ):
            event = "ROUTING_FAILED"
        log.info(
            event,
            **result.model_dump(
                mode="json",
                exclude={
                    "property_id",
                    "catalog_fingerprint",
                    "evidence",
                    "reason",
                    "service_name",
                    "clarification_question",
                },
            ),
        )
        return result
