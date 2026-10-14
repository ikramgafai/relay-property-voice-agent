from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.property.schemas import PropertyContext, UnitSummary
from app.routing.router import ServiceRouter
from app.routing.schemas import RoutingDecision, RoutingRequest
from app.workflows.schemas import UserTurn

from evaluations.catalogs import catalogs
from tests.support.routing import StubProvider, response


def atlas_context() -> PropertyContext:
    context = catalogs()["atlas"]
    context.unit = UnitSummary(
        id=uuid4(), property_id=context.property.id, number="204", active=True
    )
    return context


async def route_for(
    context: PropertyContext,
    key: str,
    utterance: str = "A user report",
    scenario_key: str | None = None,
) -> RoutingDecision:
    service = next(s for s in context.enabled_services if s.key == key)
    scenario_id: UUID | None = next(
        (s.id for s in service.scenarios if s.key == scenario_key), None
    )
    return await ServiceRouter(
        StubProvider(
            response("ROUTE", service_id=service.property_service_id, scenario_id=scenario_id)
        )
    ).route(RoutingRequest(utterance=utterance, property_context=context))


def turn(number: int, utterance: str = "A user report", **kwargs: object) -> UserTurn:
    return UserTurn.model_validate(
        {
            "turn_number": number,
            "occurred_at": datetime(2026, 10, 11, tzinfo=UTC) + timedelta(seconds=number),
            "utterance": utterance,
            **kwargs,
        }
    )
