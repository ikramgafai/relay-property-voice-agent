"""ADR 008 reply policy, interpreter and contract tests; no database or network."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from app.agent.reply import PendingQuestion, TurnInterpreter, decide
from app.agent.reply_schemas import (
    InterpretationBasis,
    InterpretationReason,
    ReplyKind,
    ReplyOutcome,
    TurnInterpretationRequest,
    TurnInterpretationResponse,
)
from app.core.config import Settings
from app.property.schemas import PropertyContext
from app.providers.llm.openai_compatible import TURN_PROMPT, OpenAICompatibleRoutingProvider
from app.providers.llm.protocol import ModelContractError, ModelUnavailable
from app.routing.model import ModelRoutingRequest, ModelRoutingResponse
from app.routing.router import ServiceRouter
from app.routing.schemas import DecisionType, RoutingDecision, RoutingMethod, RoutingRequest
from app.workflows.registry import resolve_workflow
from app.workflows.schemas import FieldName, ServiceReference, Status, WorkflowState
from pydantic import SecretStr, ValidationError

from evaluations.catalogs import catalogs

from .support.routing import StubProvider, response, turn

ATLAS = catalogs()["atlas"]
PALM = catalogs()["palm"]


def service_id(context: PropertyContext, key: str) -> UUID:
    return next(s for s in context.enabled_services if s.key == key).property_service_id


def pending(context: PropertyContext, key: str, field: FieldName) -> PendingQuestion:
    service = next(s for s in context.enabled_services if s.key == key)
    definition = resolve_workflow(service)
    assert definition
    now = datetime.now(UTC)
    run = WorkflowState(
        workflow_id=uuid4(),
        started_turn=1,
        workflow_key=definition.key,
        service=ServiceReference(
            property_service_id=service.property_service_id, service_key=service.key
        ),
        status=Status.WAITING_FOR_USER,
        current_step=f"collect:{field.value}",
        missing_fields=[field],
        started_at=now,
        updated_at=now,
    )
    return PendingQuestion(run=run, field=field)


def proposal_for(context: PropertyContext, key: str | None) -> ModelRoutingResponse:
    if key is None:
        return response()
    return response("ROUTE", service_id(context, key))


async def proposal(context: PropertyContext, key: str | None) -> RoutingDecision:
    router = ServiceRouter(StubProvider(proposal_for(context, key)))
    return await router.route(RoutingRequest(utterance="Scripted reply", property_context=context))


@pytest.mark.parametrize(
    "active,kind,target,outcome,reason",
    [
        # The observed live misroute: answer verdict, routine sibling route -> pinned answer.
        (
            "emergency_maintenance",
            "WORKFLOW_ANSWER",
            "non_emergency_maintenance",
            ReplyOutcome.WORKFLOW_ANSWER,
            InterpretationReason.ANSWER_PINNED_TO_ACTIVE_WORKFLOW,
        ),
        (
            "emergency_maintenance",
            "WORKFLOW_ANSWER",
            "emergency_maintenance",
            ReplyOutcome.WORKFLOW_ANSWER,
            InterpretationReason.MODEL_WORKFLOW_ANSWER,
        ),
        (
            "emergency_maintenance",
            "WORKFLOW_ANSWER",
            None,
            ReplyOutcome.WORKFLOW_ANSWER,
            InterpretationReason.MODEL_WORKFLOW_ANSWER,
        ),
        # Case B: answer verdict conflicting with a service of unknown safety -> ambiguous.
        (
            "emergency_maintenance",
            "WORKFLOW_ANSWER",
            "emergency_non_maintenance",
            ReplyOutcome.AMBIGUOUS,
            InterpretationReason.ANSWER_CONFLICTS_WITH_UNSUPPORTED_SERVICE,
        ),
        (
            "emergency_maintenance",
            "WORKFLOW_ANSWER",
            "human_handoff",
            ReplyOutcome.AMBIGUOUS,
            InterpretationReason.ANSWER_CONFLICTS_WITH_HANDOFF,
        ),
        # Escalation is never swallowed as a routine answer.
        (
            "non_emergency_maintenance",
            "WORKFLOW_ANSWER",
            "emergency_maintenance",
            ReplyOutcome.AMBIGUOUS,
            InterpretationReason.ANSWER_CONFLICTS_WITH_SAFETY_CRITICAL_SERVICE,
        ),
        # Automatic downgrade of an unfinished safety-critical run is blocked.
        (
            "emergency_maintenance",
            "NEW_INTENT",
            "non_emergency_maintenance",
            ReplyOutcome.AMBIGUOUS,
            InterpretationReason.PROTECTED_WORKFLOW_DOWNGRADE_BLOCKED,
        ),
        # Case A and other explicit new intents follow normal validated routing.
        (
            "emergency_maintenance",
            "NEW_INTENT",
            "emergency_non_maintenance",
            ReplyOutcome.NEW_INTENT,
            InterpretationReason.MODEL_NEW_INTENT,
        ),
        (
            "emergency_maintenance",
            "NEW_INTENT",
            "human_handoff",
            ReplyOutcome.NEW_INTENT,
            InterpretationReason.MODEL_NEW_INTENT,
        ),
        (
            "non_emergency_maintenance",
            "NEW_INTENT",
            "emergency_maintenance",
            ReplyOutcome.NEW_INTENT,
            InterpretationReason.MODEL_NEW_INTENT,
        ),
        (
            "emergency_maintenance",
            "AMBIGUOUS",
            "emergency_maintenance",
            ReplyOutcome.AMBIGUOUS,
            InterpretationReason.MODEL_REPORTED_AMBIGUOUS,
        ),
    ],
)
async def test_reply_policy_matrix(
    active: str,
    kind: ReplyKind,
    target: str | None,
    outcome: ReplyOutcome,
    reason: InterpretationReason,
) -> None:
    question = pending(ATLAS, active, FieldName.LOCATION)
    assert decide(kind, await proposal(ATLAS, target), question, ATLAS) == (outcome, reason)


async def test_answer_conflicting_with_another_safety_critical_capability_is_ambiguous() -> None:
    # Bind a second Atlas service to the safety-critical capability through configuration.
    services = [
        s.model_copy(update={"configuration": {"workflow_key": "emergency_maintenance"}})
        if s.key == "emergency_non_maintenance"
        else s
        for s in ATLAS.enabled_services
    ]
    context = ATLAS.model_copy(update={"enabled_services": services})
    question = pending(context, "emergency_maintenance", FieldName.LOCATION)
    route = await proposal(context, "emergency_non_maintenance")
    assert decide("WORKFLOW_ANSWER", route, question, context) == (
        ReplyOutcome.AMBIGUOUS,
        InterpretationReason.ANSWER_CONFLICTS_WITH_SAFETY_CRITICAL_SERVICE,
    )
    # An explicit new intent may move between safety-critical capabilities.
    assert decide("NEW_INTENT", route, question, context) == (
        ReplyOutcome.NEW_INTENT,
        InterpretationReason.MODEL_NEW_INTENT,
    )


async def test_model_answer_is_pinned_to_active_service_and_scenario() -> None:
    question = pending(ATLAS, "emergency_maintenance", FieldName.LOCATION)
    stub = StubProvider(response())
    stub.turn_result = turn("WORKFLOW_ANSWER", proposal_for(ATLAS, "non_emergency_maintenance"))
    result = await TurnInterpreter(ServiceRouter(stub), stub).interpret(
        "Living room AC.", ATLAS, question
    )
    assert result.interpretation.outcome == ReplyOutcome.WORKFLOW_ANSWER
    assert result.interpretation.proposal
    assert result.interpretation.proposal.service_key == "non_emergency_maintenance"
    assert result.route.method == RoutingMethod.TRUSTED_CONTINUATION
    assert result.route.service_key == "emergency_maintenance"
    assert not stub.requests  # One interpretation call; the router did not call the model again.
    sent = stub.turn_requests[0]
    assert sent.pending_field == FieldName.LOCATION
    assert sent.pending_question == "Where is the issue happening?"
    assert sent.active_service_name == "Emergency Maintenance"
    assert sent.active_workflow == "emergency_maintenance"


@pytest.mark.parametrize(
    "failure,basis",
    [
        (ModelUnavailable("down"), InterpretationBasis.MODEL_UNAVAILABLE),
        (TimeoutError(), InterpretationBasis.MODEL_UNAVAILABLE),
        (ModelContractError("bad"), InterpretationBasis.MODEL_CONTRACT_VIOLATION),
    ],
)
async def test_interpretation_failure_is_ambiguous(failure: Exception, basis: object) -> None:
    question = pending(ATLAS, "emergency_maintenance", FieldName.LOCATION)
    stub = StubProvider(response())
    stub.turn_result = failure
    result = await TurnInterpreter(ServiceRouter(stub), stub).interpret(
        "Living room AC.", ATLAS, question
    )
    assert result.interpretation.outcome == ReplyOutcome.AMBIGUOUS
    assert result.interpretation.basis == basis
    assert result.interpretation.reason_code == InterpretationReason.INTERPRETATION_UNAVAILABLE
    assert result.route.decision == DecisionType.ABSTAINED and not stub.requests


async def test_without_provider_natural_reply_is_ambiguous_but_explicit_controls_work() -> None:
    question = pending(ATLAS, "emergency_maintenance", FieldName.LOCATION)
    interpreter = TurnInterpreter(ServiceRouter())
    natural = await interpreter.interpret("Living room AC.", ATLAS, question)
    assert natural.interpretation.outcome == ReplyOutcome.AMBIGUOUS
    prefixed = await interpreter.interpret("Answer: Living room AC", ATLAS, question)
    assert prefixed.interpretation.outcome == ReplyOutcome.WORKFLOW_ANSWER
    assert prefixed.interpretation.basis == InterpretationBasis.ANSWER_PREFIX
    assert prefixed.route.method == RoutingMethod.TRUSTED_CONTINUATION
    cancel = await interpreter.interpret("Cancel.", ATLAS, question)
    assert cancel.interpretation.outcome == ReplyOutcome.CANCEL
    assert cancel.route.service_key == "emergency_maintenance"


async def test_configured_routine_request_cannot_downgrade_without_model_call() -> None:
    question = pending(ATLAS, "emergency_maintenance", FieldName.LOCATION)
    stub = StubProvider(response())
    result = await TurnInterpreter(ServiceRouter(stub), stub).interpret(
        "My AC isn't cooling properly.", ATLAS, question
    )
    assert result.interpretation.basis == InterpretationBasis.CONFIGURED_EVIDENCE
    assert result.interpretation.outcome == ReplyOutcome.AMBIGUOUS
    assert (
        result.interpretation.reason_code
        == InterpretationReason.PROTECTED_WORKFLOW_DOWNGRADE_BLOCKED
    )
    assert not stub.requests and not stub.turn_requests


async def test_palm_reply_cannot_use_atlas_catalog() -> None:
    question = pending(PALM, "leasing", FieldName.TOUR_TIME)
    stub = StubProvider(response())
    # Deliberately malicious: an answer verdict carrying an Atlas-only service ID.
    stub.turn_result = turn("WORKFLOW_ANSWER", proposal_for(ATLAS, "emergency_maintenance"))
    result = await TurnInterpreter(ServiceRouter(stub), stub).interpret(
        "Tomorrow afternoon", PALM, question
    )
    assert result.interpretation.outcome == ReplyOutcome.AMBIGUOUS
    assert result.interpretation.reason_code == InterpretationReason.PROPOSAL_OUTSIDE_CATALOG
    assert result.route.property_service_id is None
    assert result.route.contract_violation == "invalid_service"
    sent = stub.turn_requests[0].model_dump_json()
    assert "emergency_maintenance" not in sent and "Atlas" not in sent


def test_interpretation_contracts_are_strict() -> None:
    schema = TurnInterpretationResponse.model_json_schema()
    assert set(schema["required"]) == set(TurnInterpretationResponse.model_fields)
    assert schema["additionalProperties"] is False
    nested = schema["$defs"]["ModelRoutingResponse"]
    assert nested["additionalProperties"] is False
    assert set(nested["required"]) == set(ModelRoutingResponse.model_fields)
    with pytest.raises(ValidationError):
        TurnInterpretationResponse.model_validate(
            {"reply_kind": "STORE_LOCATION", "routing": response().model_dump(), "reason": "x"}
        )
    with pytest.raises(ValidationError):
        TurnInterpretationResponse(reply_kind="AMBIGUOUS", routing=response(), reason="x" * 401)
    routing = ModelRoutingRequest.from_request(
        RoutingRequest(utterance="Living room", property_context=PALM)
    )
    with pytest.raises(ValidationError):
        TurnInterpretationRequest(
            routing=routing,
            active_property_service_id=service_id(ATLAS, "emergency_maintenance"),
            active_service_name="Emergency Maintenance",
            active_workflow="emergency_maintenance",
            pending_field=FieldName.LOCATION,
            pending_question="Where is the issue happening?",
        )


def adapter_settings() -> Settings:
    return Settings(
        routing_model_base_url="https://model.example/v1",
        routing_model_name="test-model",
        routing_model_api_key=SecretStr("test-secret"),
    )


def interpretation_request() -> TurnInterpretationRequest:
    return TurnInterpretationRequest(
        routing=ModelRoutingRequest.from_request(
            RoutingRequest(utterance="Living room AC.", property_context=ATLAS)
        ),
        active_property_service_id=service_id(ATLAS, "emergency_maintenance"),
        active_service_name="Emergency Maintenance",
        active_workflow="emergency_maintenance",
        pending_field=FieldName.LOCATION,
        pending_question="Where is the issue happening?",
    )


async def test_adapter_single_strict_interpretation_call() -> None:
    answer = turn("WORKFLOW_ANSWER", proposal_for(ATLAS, "non_emergency_maintenance"))
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        body = json.loads(req.content)
        assert body["response_format"]["json_schema"]["name"] == "turn_interpretation"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["messages"][0]["content"] == TURN_PROMPT
        user = json.loads(body["messages"][1]["content"])
        assert user["pending_question"] == "Where is the issue happening?"
        assert user["routing"]["utterance"] == "Living room AC."
        assert "tools" not in body and body["stream"] is False
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": answer.model_dump_json()}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleRoutingProvider(adapter_settings(), client)
        assert await provider.interpret_turn(interpretation_request()) == answer
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload,error",
    [
        (
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
            ModelUnavailable,
        ),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"reply_kind":"X"}'}}]},
            ModelContractError,
        ),
    ],
)
async def test_adapter_interpretation_errors_are_sanitized(
    payload: dict[str, object], error: type[Exception]
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleRoutingProvider(adapter_settings(), client)
        with pytest.raises(error) as raised:
            await provider.interpret_turn(interpretation_request())
    assert "test-secret" not in str(raised.value)
