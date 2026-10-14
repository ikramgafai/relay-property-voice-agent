"""Final backend authority over both configured and model proposals."""

import hashlib
import json
from uuid import UUID

from app.property.schemas import EnabledServiceContext, PropertyContext, ScenarioContext
from app.routing.model import ModelRoutingRequest
from app.routing.schemas import RoutingRequest


class CatalogViolation(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


def resolve_selection(
    context: PropertyContext,
    service_id: UUID,
    scenario_id: UUID | None,
) -> tuple[EnabledServiceContext, ScenarioContext | None]:
    service = next(
        (s for s in context.enabled_services if s.property_service_id == service_id), None
    )
    if service is None:
        raise CatalogViolation("invalid_service")
    scenario = None
    if scenario_id is not None:
        scenario = next((sc for sc in service.scenarios if sc.id == scenario_id), None)
        if scenario is None:
            raise CatalogViolation("invalid_scenario")
    return service, scenario


def clarification_question(context: PropertyContext, ids: list[UUID]) -> str:
    if not ids:
        return "What would you like help with at this property?"
    names = [resolve_selection(context, sid, None)[0].display_name for sid in ids]
    return "Is your request about " + ", ".join(names[:-1]) + " or " + names[-1] + "?"


def catalog_fingerprint(request: RoutingRequest) -> str:
    payload = ModelRoutingRequest.from_request(request).model_dump(
        mode="json", exclude={"utterance"}
    )
    # Canonical ordering means database ordering alone does not change the identifier.
    services = payload["services"]
    services.sort(key=lambda s: s["property_service_id"])
    for service in services:
        service["scenarios"].sort(key=lambda sc: sc["id"])
        for scenario in service["scenarios"]:
            scenario["examples"].sort()
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()
