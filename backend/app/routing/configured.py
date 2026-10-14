"""Conservative exact evidence, independent of service keys, priority and property names."""

import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from app.property.schemas import PropertyContext
from app.routing.schemas import Evidence, EvidenceType


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return " ".join(text.strip().rstrip(".!?").split())


@dataclass(frozen=True)
class ConfiguredMatch:
    service_id: UUID | None
    scenario_id: UUID | None
    evidence: list[Evidence]


def match_configured(utterance: str, context: PropertyContext) -> ConfiguredMatch:
    normalized = normalize(utterance)
    evidence: list[Evidence] = []
    for service in context.enabled_services:
        fields: list[tuple[EvidenceType, str, UUID | None]] = [
            (EvidenceType.SERVICE_NAME, service.display_name, None),
            (EvidenceType.SERVICE_DESCRIPTION, service.description, None),
        ]
        for scenario in service.scenarios:
            fields.extend(
                [
                    (EvidenceType.SCENARIO_NAME, scenario.name, scenario.id),
                    (EvidenceType.SCENARIO_DESCRIPTION, scenario.description, scenario.id),
                    *((EvidenceType.SCENARIO_EXAMPLE, ex, scenario.id) for ex in scenario.examples),
                ]
            )
        for kind, text, scenario_id in fields:
            value = normalize(text)
            # Tiny prose fragments are weak; explicit service/scenario labels remain eligible.
            is_name = kind in (EvidenceType.SERVICE_NAME, EvidenceType.SCENARIO_NAME)
            if not is_name and len(re.findall(r"\w+", value)) < 3:
                continue
            if value and normalized == value:
                evidence.append(
                    Evidence(
                        kind=kind,
                        property_service_id=service.property_service_id,
                        scenario_id=scenario_id,
                        detail="Normalized whole-utterance configured match",
                    )
                )
    services = {e.property_service_id for e in evidence}
    if len(services) != 1:
        return ConfiguredMatch(None, None, evidence)
    scenarios = {e.scenario_id for e in evidence if e.scenario_id is not None}
    # Comparable scenario matches also fall through, rather than picking by storage order.
    if len(scenarios) > 1:
        return ConfiguredMatch(None, None, evidence)
    return ConfiguredMatch(next(iter(services)), next(iter(scenarios), None), evidence)
