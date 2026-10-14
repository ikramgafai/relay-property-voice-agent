"""Run with --offline (no network) or --live (explicit configured-provider requests)."""

import argparse
import asyncio
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from app.core.config import load_settings
from app.core.logging import configure_logging
from app.property.schemas import DomainModel, PropertyContext
from app.providers.llm.openai_compatible import OpenAICompatibleRoutingProvider
from app.providers.llm.protocol import RoutingModelProvider
from app.routing.router import ServiceRouter
from app.routing.schemas import DecisionType, RoutingDecision, RoutingMethod, RoutingRequest
from pydantic import Field, model_validator

from evaluations.catalogs import catalogs
from evaluations.offline_provider import OfflineLexicalProvider

DATASET = Path(__file__).with_name("cases.tsv")


class Case(DomainModel):
    id: str
    property: str
    decision: DecisionType
    service: str | None
    scenario: str | None
    tag: str
    utterance: str = Field(min_length=1)

    @model_validator(mode="after")
    def expected_selection(self) -> "Case":
        if (self.decision == DecisionType.ROUTED) != (self.service is not None):
            raise ValueError("Only expected routes specify a service")
        if self.scenario and not self.service:
            raise ValueError("Scenario requires a service")
        return self


def load_cases(path: Path = DATASET) -> list[Case]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    cases = [
        Case.model_validate(
            {
                **row,
                "id": f"case-{index:03d}",
                "service": None if row["service"] == "-" else row["service"],
                "scenario": None if row["scenario"] == "-" else row["scenario"],
            }
        )
        for index, row in enumerate(rows, 1)
    ]
    contexts = catalogs()
    for case in cases:
        context = contexts[case.property]
        if case.service:
            service = next(s for s in context.enabled_services if s.key == case.service)
            if case.scenario and case.scenario not in {sc.key for sc in service.scenarios}:
                raise ValueError(f"Unknown expected scenario in {case.id}")
    if len({(c.property, c.utterance) for c in cases}) != len(cases):
        raise ValueError("Duplicate evaluation cases")
    return cases


def correct(case: Case, decision: RoutingDecision) -> bool:
    return decision.decision == case.decision and (
        case.decision != DecisionType.ROUTED
        or (
            decision.service_key == case.service
            and (case.scenario is None or decision.scenario_key == case.scenario)
        )
    )


def percentile(values: list[float], quantile: float) -> float | None:
    return sorted(values)[max(0, math.ceil(quantile * len(values)) - 1)] if values else None


def summarize(
    cases: list[Case],
    decisions: list[RoutingDecision],
    contexts: dict[str, PropertyContext],
) -> dict[str, Any]:
    if len(cases) != len(decisions) or not cases:
        raise ValueError("Nonempty paired cases and decisions required")
    pairs = list(zip(cases, decisions, strict=True))
    invalid_services = invalid_scenarios = leakage = 0
    per_service: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "correct": 0})
    wrong_routes = []
    for case, decision in pairs:
        if case.service:
            per_service[case.service]["count"] += 1
            per_service[case.service]["correct"] += int(correct(case, decision))
        if decision.decision == DecisionType.ROUTED:
            context = contexts[case.property]
            service = next(
                (
                    s
                    for s in context.enabled_services
                    if s.property_service_id == decision.property_service_id
                ),
                None,
            )
            invalid = service is None or service.key != decision.service_key
            invalid_services += int(invalid)
            leakage += int(service is None or decision.property_id != context.property.id)
            if decision.scenario_id is not None:
                invalid_scenarios += int(
                    service is None
                    or not any(
                        sc.id == decision.scenario_id and sc.key == decision.scenario_key
                        for sc in service.scenarios
                    )
                )
            if case.decision != DecisionType.ROUTED or case.service != decision.service_key:
                wrong_routes.append(
                    {
                        "id": case.id,
                        "property": case.property,
                        "utterance": case.utterance,
                        "expected": case.decision,
                        "expected_service": case.service,
                        "actual_service": decision.service_key,
                    }
                )

    def accuracy(kind: DecisionType) -> dict[str, int | float | None]:
        subset = [(c, d) for c, d in pairs if c.decision == kind]
        count = len(subset)
        successes = sum(correct(c, d) for c, d in subset)
        return {"count": count, "correct": successes, "rate": successes / count if count else None}

    count = len(cases)
    total_times = [d.duration_ms for d in decisions]
    fallback = [d for d in decisions if d.model_fallback_used]
    return {
        "total_cases": count,
        "property_configurations": len({c.property for c in cases}),
        "route_accuracy": accuracy(DecisionType.ROUTED),
        "clarification_correctness": accuracy(DecisionType.CLARIFICATION_REQUIRED),
        "abstention_correctness": accuracy(DecisionType.ABSTAINED),
        "overall_decision_accuracy": sum(correct(c, d) for c, d in pairs) / count,
        "decision_type_accuracy": sum(c.decision == d.decision for c, d in pairs) / count,
        "invalid_service_selections": invalid_services,
        "invalid_scenario_selections": invalid_scenarios,
        "cross_property_leakage": leakage,
        "rejected_model_contract_violations": sum(
            d.contract_violation is not None for d in decisions
        ),
        "model_unavailable": sum(d.reason_code == "MODEL_UNAVAILABLE" for d in decisions),
        "model_fallback_rate": len(fallback) / count,
        "configured_evidence_direct_route_rate": sum(
            d.decision == DecisionType.ROUTED and d.method == RoutingMethod.CONFIGURED_EVIDENCE
            for d in decisions
        )
        / count,
        "model_assisted_route_rate": sum(
            d.decision == DecisionType.ROUTED and d.method == RoutingMethod.MODEL_ASSISTED
            for d in decisions
        )
        / count,
        "clarification_rate": sum(
            d.decision == DecisionType.CLARIFICATION_REQUIRED for d in decisions
        )
        / count,
        "abstention_rate": sum(d.decision == DecisionType.ABSTAINED for d in decisions) / count,
        "p50_total_ms": percentile(total_times, 0.5),
        "p95_total_ms": percentile(total_times, 0.95),
        "p50_model_assisted_total_ms": percentile([d.duration_ms for d in fallback], 0.5),
        "p95_model_assisted_total_ms": percentile([d.duration_ms for d in fallback], 0.95),
        "p50_model_stage_ms": percentile([d.model_duration_ms for d in fallback], 0.5),
        "p95_model_stage_ms": percentile([d.model_duration_ms for d in fallback], 0.95),
        "confident_wrong_route_count": len(wrong_routes),
        "confident_wrong_routes": wrong_routes,
        "per_service": dict(per_service),
    }


async def evaluate(provider: RoutingModelProvider, deadline_seconds: float) -> dict[str, Any]:
    cases = load_cases()
    contexts = catalogs()
    router = ServiceRouter(provider, timeout_seconds=deadline_seconds)
    decisions = [
        await router.route(
            RoutingRequest(
                utterance=case.utterance,
                property_context=contexts[case.property],
            )
        )
        for case in cases
    ]
    return {
        "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "metrics": summarize(cases, decisions, contexts),
        "cases": [
            {
                "expected": c.model_dump(mode="json"),
                "actual": d.model_dump(mode="json"),
                "correct": correct(c, d),
            }
            for c, d in zip(cases, decisions, strict=True)
        ],
    }


async def run(live: bool) -> dict[str, Any]:
    if not live:
        report = await evaluate(OfflineLexicalProvider(), 10)
        report["mode"] = "offline_lexical_test_double"
        report["limitation"] = (
            "Not a live LLM benchmark; measures configured routing and deterministic plumbing."
        )
        return report
    settings = load_settings()
    if not settings.routing_model_base_url or not settings.routing_model_name:
        raise ValueError("Live evaluation requires ROUTING_MODEL_BASE_URL and ROUTING_MODEL_NAME")
    async with httpx.AsyncClient(trust_env=False) as client:
        report = await evaluate(
            OpenAICompatibleRoutingProvider(settings, client),
            settings.routing_model_timeout_seconds,
        )
    report["mode"] = "live"
    report["model"] = settings.routing_model_name
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--offline", action="store_true", help="No network, deterministic test double"
    )
    mode.add_argument(
        "--live", action="store_true", help="Calls configured model; may incur API charges"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    configure_logging("WARNING")
    try:
        report = asyncio.run(run(args.live))
    except ValueError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": report["mode"], **report["metrics"]}, indent=2))


if __name__ == "__main__":
    main()
