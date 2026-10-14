import asyncio
import inspect
from uuid import uuid4

import pytest
from app.providers.llm.protocol import ModelContractError
from app.routing import configured, router
from app.routing.model import ModelRoutingRequest, ModelRoutingResponse
from app.routing.router import ServiceRouter
from app.routing.schemas import DecisionType, RoutingDecision, RoutingRequest
from app.routing.validation import catalog_fingerprint
from pydantic import ValidationError
from structlog.testing import capture_logs

from evaluations.catalogs import catalogs

from .support.routing import StubProvider, response


@pytest.mark.parametrize("utterance", ["", "  \n\t", "x" * 8001])
def test_invalid_utterance(utterance: str) -> None:
    with pytest.raises(ValidationError):
        RoutingRequest(utterance=utterance, property_context=catalogs()["atlas"])


def test_invalid_context_and_duplicate_identities() -> None:
    with pytest.raises(ValidationError):
        RoutingRequest.model_validate({"utterance": "hello", "property_context": {}})
    context = catalogs()["atlas"]
    context.property.active = False
    with pytest.raises(ValidationError):
        RoutingRequest(utterance="hello", property_context=context)
    context.property.active = True
    context.enabled_services.append(context.enabled_services[0])
    with pytest.raises(ValidationError):
        RoutingRequest(utterance="hello", property_context=context)


@pytest.mark.parametrize(
    "field",
    ["service_name", "service_description", "scenario_name", "scenario_description", "example"],
)
async def test_all_configured_evidence_types(field: str) -> None:
    context = catalogs()["oakwood"]
    service = context.enabled_services[0]
    scenario = service.scenarios[0]
    text = {
        "service_name": service.display_name,
        "service_description": service.description,
        "scenario_name": scenario.name,
        "scenario_description": scenario.description,
        "example": scenario.examples[0],
    }[field]
    provider = StubProvider(RuntimeError("should never run"))
    result = await ServiceRouter(provider).route(
        RoutingRequest(utterance=text, property_context=context)
    )
    assert result.decision == "ROUTED" and result.service_key == "pet_services"
    assert result.method == "CONFIGURED_EVIDENCE" and not provider.requests
    assert result.evidence
    if field.startswith("scenario") or field == "example":
        assert result.scenario_id == scenario.id


@pytest.mark.parametrize("utterance", ["I need account help.", "Account help"])
async def test_overlap_falls_through(utterance: str) -> None:
    context = catalogs()["lofts"]
    provider = StubProvider(
        response(
            "CLARIFY", candidates=[s.property_service_id for s in context.enabled_services[:2]]
        )
    )
    result = await ServiceRouter(provider).route(
        RoutingRequest(utterance=utterance, property_context=context)
    )
    assert result.decision == "CLARIFICATION_REQUIRED" and len(provider.requests) == 1
    assert result.clarification_question == "Is your request about Resident Desk or Billing Desk?"
    assert len(result.evidence) >= 2


async def test_overlapping_scenarios_same_service_also_fall_through() -> None:
    context = catalogs()["oakwood"]
    context.enabled_services[0].scenarios[1].examples = ["How do I register my dog?"]
    provider = StubProvider(response("CLARIFY"))
    result = await ServiceRouter(provider).route(
        RoutingRequest(utterance="How do I register my dog?", property_context=context)
    )
    assert result.decision == "CLARIFICATION_REQUIRED" and provider.requests


@pytest.mark.parametrize(
    "utterance",
    [
        "dog",
        "I do not need Pet Services",
        "Pet Services and New Home Team",
        "I need someone to come over.",
    ],
)
async def test_weak_negated_or_multiple_requests_are_not_direct_routes(utterance: str) -> None:
    provider = StubProvider(response("CLARIFY"))
    result = await ServiceRouter(provider).route(
        RoutingRequest(utterance=utterance, property_context=catalogs()["oakwood"])
    )
    assert result.decision == "CLARIFICATION_REQUIRED" and result.model_fallback_used


async def test_empty_catalog_never_invokes_model() -> None:
    provider = StubProvider(RuntimeError("must not run"))
    result = await ServiceRouter(provider).route(
        RoutingRequest(utterance="Help", property_context=catalogs()["empty"])
    )
    assert result.reason_code == "NO_AVAILABLE_SERVICES" and not provider.requests


async def test_model_valid_route_and_minimal_scoped_payload() -> None:
    context = catalogs()["palm"]
    service = context.enabled_services[0]
    provider = StubProvider(response("ROUTE", service.property_service_id, service.scenarios[0].id))
    result = await ServiceRouter(provider).route(
        RoutingRequest(utterance="The cooling system is weak", property_context=context)
    )
    assert result.service_key == "maintenance" and result.method == "MODEL_ASSISTED"
    payload = provider.requests[0].model_dump_json()
    assert (
        "emergency_maintenance" not in payload
        and "resident" not in ModelRoutingRequest.model_fields
    )
    assert {s.property_service_id for s in provider.requests[0].services} == {
        s.property_service_id for s in context.enabled_services
    }
    assert result.scenario_id == service.scenarios[0].id


@pytest.mark.parametrize(
    "kind",
    ["invented", "foreign_service", "foreign_scenario", "wrong_relationship", "clarification"],
)
async def test_backend_rejects_invalid_model_choices(kind: str) -> None:
    contexts = catalogs()
    context = contexts["palm"]
    service = context.enabled_services[0]
    proposal = response("ROUTE", service.property_service_id)
    expected = "invalid_service"
    if kind == "invented":
        proposal.property_service_id = uuid4()
    elif kind == "foreign_service":
        proposal.property_service_id = contexts["atlas"].enabled_services[0].property_service_id
    elif kind in ("foreign_scenario", "wrong_relationship"):
        proposal.scenario_id = (
            contexts["atlas"].enabled_services[0].scenarios[0].id
            if kind == "foreign_scenario"
            else context.enabled_services[1].scenarios[0].id
        )
        expected = "invalid_scenario"
    else:
        proposal = response("CLARIFY", candidates=[service.property_service_id, uuid4()])
    result = await ServiceRouter(StubProvider(proposal)).route(
        RoutingRequest(utterance="Unmatched input", property_context=context)
    )
    assert result.decision == "ABSTAINED" and result.reason_code == "MODEL_CONTRACT_VIOLATION"
    assert result.contract_violation == expected and result.property_service_id is None


@pytest.mark.parametrize(
    "failure", [ModelContractError("secret"), ValueError("secret"), RuntimeError("secret")]
)
async def test_failures_are_sanitized(failure: Exception) -> None:
    with capture_logs() as logs:
        result = await ServiceRouter(StubProvider(failure)).route(
            RoutingRequest(utterance="Unmatched", property_context=catalogs()["atlas"])
        )
    assert result.decision == "ABSTAINED" and result.property_service_id is None
    assert "secret" not in str(logs) + result.model_dump_json()
    assert logs[-1]["event"] == "ROUTING_FAILED"


async def test_timeout_cancels_provider_and_does_not_guess() -> None:
    provider = StubProvider(response(), delay=1)
    result = await ServiceRouter(provider, timeout_seconds=0.01).route(
        RoutingRequest(utterance="Unmatched", property_context=catalogs()["atlas"])
    )
    assert result.reason_code == "MODEL_UNAVAILABLE" and provider.cancelled
    assert result.duration_ms < 500


async def test_caller_cancellation_propagates() -> None:
    provider = StubProvider(response(), delay=10)
    task = asyncio.create_task(
        ServiceRouter(provider).route(
            RoutingRequest(utterance="Unmatched", property_context=catalogs()["atlas"])
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled


async def test_mutated_model_output_is_revalidated() -> None:
    proposal = response()
    proposal.action = "ROUTE"
    result = await ServiceRouter(StubProvider(proposal)).route(
        RoutingRequest(utterance="Unmatched", property_context=catalogs()["atlas"])
    )
    assert result.reason_code == "MODEL_CONTRACT_VIOLATION"


async def test_missing_provider_abstains_and_out_of_scope_is_distinct() -> None:
    request = RoutingRequest(utterance="Write a poem", property_context=catalogs()["atlas"])
    assert (await ServiceRouter().route(request)).reason_code == "MODEL_UNAVAILABLE"
    assert (
        await ServiceRouter(StubProvider(response())).route(request)
    ).reason_code == "OUT_OF_SCOPE"


async def test_catalog_snapshot_cannot_be_mutated_by_provider() -> None:
    context = catalogs()["palm"]
    service = context.enabled_services[0]

    class MutatingProvider:
        async def interpret(self, request: ModelRoutingRequest) -> ModelRoutingResponse:
            context.enabled_services.clear()
            request.services.clear()
            return response("ROUTE", service.property_service_id)

    result = await ServiceRouter(MutatingProvider()).route(
        RoutingRequest(utterance="Unmatched", property_context=context)
    )
    assert result.service_key == "maintenance"


async def test_independent_turns_can_change_service() -> None:
    context = catalogs()["atlas"]
    router_instance = ServiceRouter()
    first = await router_instance.route(
        RoutingRequest(utterance="My AC isn't cooling properly.", property_context=context)
    )
    second = await router_instance.route(
        RoutingRequest(
            utterance="Water is pouring across my kitchen floor.", property_context=context
        )
    )
    assert first.service_key == "non_emergency_maintenance"
    assert second.service_key == "emergency_maintenance"


def test_fingerprint_tracks_catalog_not_order_or_utterance() -> None:
    request = RoutingRequest(utterance="hello", property_context=catalogs()["atlas"])
    original = catalog_fingerprint(request)
    request.utterance = "other"
    request.property_context.enabled_services.reverse()
    assert catalog_fingerprint(request) == original
    request.property_context.enabled_services[0].description += " changed"
    assert catalog_fingerprint(request) != original


async def test_events_and_timings() -> None:
    with capture_logs() as logs:
        result = await ServiceRouter(StubProvider(response("CLARIFY"))).route(
            RoutingRequest(utterance="Help", property_context=catalogs()["atlas"])
        )
    assert [row["event"] for row in logs] == [
        "ROUTING_STARTED",
        "ROUTING_FALLBACK",
        "ROUTING_FALLBACK",
    ]
    assert all(row["property_id"] == str(result.property_id) for row in logs)
    assert result.duration_ms >= result.model_duration_ms
    assert logs[-1]["model_fallback_used"] is True
    assert "utterance" not in str(logs)


def test_routing_source_has_no_catalog_or_database_dependency() -> None:
    source = inspect.getsource(configured) + inspect.getsource(router)
    assert all(
        term not in source
        for term in (
            "emergency_maintenance",
            "pet_services",
            "atlas",
            "sqlalchemy",
            "app.db",
            "openai_compatible",
        )
    )


def test_nonroute_contract_cannot_have_selected_service() -> None:
    with pytest.raises(ValidationError):
        RoutingDecision(
            decision=DecisionType.ABSTAINED,
            property_id=uuid4(),
            property_service_id=uuid4(),
            reason_code="OUT_OF_SCOPE",
            reason="No match",
            catalog_fingerprint="hash",
            duration_ms=0,
        )
