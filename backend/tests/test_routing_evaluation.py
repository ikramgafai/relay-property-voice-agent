import pytest
from app.routing.router import ServiceRouter
from app.routing.schemas import RoutingRequest

from evaluations.catalogs import catalogs
from evaluations.offline_provider import OfflineLexicalProvider
from evaluations.run_routing import evaluate, load_cases, run, summarize

from .support.routing import StubProvider, response


async def test_dataset_and_offline_evaluation_cover_all_outcomes() -> None:
    cases = load_cases()
    assert 120 <= len(cases) <= 250
    assert 8 <= len({case.property for case in cases}) <= 15
    report = await evaluate(OfflineLexicalProvider(), 1)
    metrics = report["metrics"]
    assert metrics["total_cases"] == len(cases)
    assert (
        metrics["invalid_service_selections"]
        == metrics["invalid_scenario_selections"]
        == metrics["cross_property_leakage"]
        == 0
    )
    assert 0 < metrics["model_fallback_rate"] < 1
    assert metrics["configured_evidence_direct_route_rate"] > 0
    assert metrics["model_assisted_route_rate"] > 0
    assert metrics["clarification_rate"] > 0 and metrics["abstention_rate"] > 0
    assert metrics["p95_total_ms"] >= metrics["p50_total_ms"] >= 0
    assert (
        metrics["overall_decision_accuracy"] < 1
    )  # Imperfect stub is not an expected-label oracle.


async def test_metrics_count_wrong_route_and_rejected_attempt_separately() -> None:
    contexts = catalogs()
    case = next(c for c in load_cases() if c.property == "palm" and c.tag == "out_of_scope")
    service = contexts["palm"].enabled_services[0]
    wrong = await ServiceRouter(StubProvider(response("ROUTE", service.property_service_id))).route(
        RoutingRequest(utterance=case.utterance, property_context=contexts["palm"])
    )
    metrics = summarize([case], [wrong], contexts)
    assert metrics["confident_wrong_route_count"] == 1
    assert metrics["overall_decision_accuracy"] == 0
    assert metrics["invalid_service_selections"] == 0  # Valid catalog, wrong meaning.
    foreign = contexts["atlas"].enabled_services[0].property_service_id
    rejected = await ServiceRouter(StubProvider(response("ROUTE", foreign))).route(
        RoutingRequest(utterance=case.utterance, property_context=contexts["palm"])
    )
    metrics = summarize([case], [rejected], contexts)
    assert metrics["rejected_model_contract_violations"] == 1
    assert metrics["invalid_service_selections"] == metrics["cross_property_leakage"] == 0


async def test_live_requires_explicit_configuration() -> None:
    with pytest.raises(ValueError, match="requires"):
        await run(True)
