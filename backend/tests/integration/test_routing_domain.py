import hashlib
from pathlib import Path

import pytest
from app.db.models import Property, PropertyService, Service, ServiceScenario
from app.db.repository import SQLAlchemyPropertyRepository
from app.db.seed import seed_demo
from app.property.context import PropertyContextResolver
from app.routing.router import ServiceRouter
from app.routing.schemas import RoutingRequest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..support.routing import StubProvider, response

pytestmark = pytest.mark.integration


def routing_source_hash() -> str:
    folder = Path(__file__).resolve().parents[2] / "app" / "routing"
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(folder.glob("*.py")))).hexdigest()


@pytest.mark.parametrize(
    "key,name,utterance",
    [
        ("pet_services", "Pet Services", "How do I register my dog?"),
        ("package_lockers", "Package Lockers", "Where is my locker code?"),
    ],
)
async def test_database_only_service_becomes_routable(
    db_session: AsyncSession,
    key: str,
    name: str,
    utterance: str,
) -> None:
    await seed_demo(db_session)
    atlas = (
        await db_session.scalars(select(Property).where(Property.slug == "atlas-residence"))
    ).one()
    resolver = PropertyContextResolver(SQLAlchemyPropertyRepository(db_session))
    before = await resolver.resolve_by_property_and_unit(atlas.id, "204")
    assert key not in {s.key for s in before.enabled_services}
    source_hash = routing_source_hash()
    prior = await ServiceRouter(StubProvider(response())).route(
        RoutingRequest(utterance=utterance, property_context=before)
    )
    assert prior.decision == "ABSTAINED"
    service = Service(key=key, name=name, description="Configured resident assistance")
    db_session.add(service)
    await db_session.flush()
    association = PropertyService(
        property_id=atlas.id,
        service_id=service.id,
        enabled=True,
        description_override="Property-specific service description",
    )
    db_session.add(association)
    await db_session.flush()
    scenario = ServiceScenario(
        property_service_id=association.id,
        key="registration" if key == "pet_services" else "missing_code",
        name="Configured scenario",
        description="Handle this configured resident request.",
        examples=[utterance],
        active=True,
    )
    db_session.add(scenario)
    if key == "pet_services":
        db_session.add_all(
            [
                ServiceScenario(
                    property_service_id=association.id,
                    key="pet_policy",
                    name="Pet policy",
                    description="Permitted pet policy",
                    examples=["Are cats allowed?"],
                    active=True,
                ),
                ServiceScenario(
                    property_service_id=association.id,
                    key="pet_fees",
                    name="Pet fees",
                    description="Monthly pet fees",
                    examples=["What is the pet fee?"],
                    active=True,
                ),
            ]
        )
    await db_session.flush()
    fresh = await resolver.resolve_by_property_and_unit(atlas.id, "204")
    assert (
        next(s for s in fresh.enabled_services if s.key == key).description
        == "Property-specific service description"
    )
    result = await ServiceRouter().route(
        RoutingRequest(utterance=utterance, property_context=fresh)
    )
    assert result.service_key == key and result.property_service_id == association.id
    assert result.scenario_id == scenario.id and result.method == "CONFIGURED_EVIDENCE"
    assert source_hash == routing_source_hash()
    model_result = await ServiceRouter(
        StubProvider(response("ROUTE", association.id, scenario.id))
    ).route(
        RoutingRequest(utterance="A paraphrase requiring interpretation", property_context=fresh)
    )
    assert model_result.service_key == key and model_result.method == "MODEL_ASSISTED"
    print(f"Database-only {key}: {utterance} -> {result.service_key}; routing source unchanged")
    # Deactivated scenarios and disabled associations cannot influence fresh routing.
    await db_session.execute(
        update(ServiceScenario).where(ServiceScenario.id == scenario.id).values(active=False)
    )
    inactive = await resolver.resolve_by_property_and_unit(atlas.id, "204")
    invalid_scenario = await ServiceRouter(
        StubProvider(response("ROUTE", association.id, scenario.id))
    ).route(RoutingRequest(utterance=utterance, property_context=inactive))
    assert invalid_scenario.contract_violation == "invalid_scenario"
    await db_session.execute(
        update(PropertyService).where(PropertyService.id == association.id).values(enabled=False)
    )
    disabled = await resolver.resolve_by_property_and_unit(atlas.id, "204")
    invalid_service = await ServiceRouter(StubProvider(response("ROUTE", association.id))).route(
        RoutingRequest(utterance=utterance, property_context=disabled)
    )
    assert invalid_service.contract_violation == "invalid_service"


async def test_real_atlas_palm_isolation_and_stateless_demo(db_session: AsyncSession) -> None:
    await seed_demo(db_session)
    repository = SQLAlchemyPropertyRepository(db_session)
    resolver = PropertyContextResolver(repository)
    atlas_property = await repository.get_property_by_slug("atlas-residence")
    palm_property = await repository.get_property_by_slug("palm-heights")
    assert atlas_property and palm_property
    atlas = await resolver.resolve_by_property_and_unit(atlas_property.id, "204")
    palm = await resolver.resolve_by_property_and_unit(palm_property.id, "318")
    print("Atlas:", [s.key for s in atlas.enabled_services])
    print("Palm:", [s.key for s in palm.enabled_services])
    emergency = next(s for s in atlas.enabled_services if s.key == "emergency_maintenance")
    gas = next(sc for sc in emergency.scenarios if sc.key == "gas_smell")
    provider = StubProvider(response("ROUTE", emergency.property_service_id, gas.id))
    valid = await ServiceRouter(provider).route(
        RoutingRequest(utterance="I smell gas in my apartment.", property_context=atlas)
    )
    invalid = await ServiceRouter(provider).route(
        RoutingRequest(utterance="I smell gas in my apartment.", property_context=palm)
    )
    assert valid.service_key == "emergency_maintenance"
    assert invalid.decision == "ABSTAINED" and invalid.contract_violation == "invalid_service"
    nonurgent = next(s for s in atlas.enabled_services if s.key == "non_emergency_maintenance")
    ac = next(sc for sc in nonurgent.scenarios if sc.key == "ac_not_cooling")
    flood = next(sc for sc in emergency.scenarios if sc.key == "active_flooding")
    for utterance, service, scenario in [
        ("My AC isn't cooling.", nonurgent, ac),
        ("Actually water is pouring out of it.", emergency, flood),
    ]:
        routed = await ServiceRouter(
            StubProvider(response("ROUTE", service.property_service_id, scenario.id))
        ).route(RoutingRequest(utterance=utterance, property_context=atlas))
        assert routed.service_key == service.key
        print(utterance, "->", routed.service_key, routed.scenario_key)
