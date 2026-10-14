import json
from datetime import timedelta
from uuid import uuid4

import pytest
from app.db.models import Property, PropertyService, Resident, Service, ServiceScenario, Unit
from app.db.repository import SQLAlchemyPropertyRepository
from app.db.seed import DOMAIN_MODELS, row_counts, seed_demo
from app.property.context import PropertyContextResolver
from app.property.errors import PropertyContextError
from app.property.schemas import PropertyContext, ResidentData
from pydantic import ValidationError
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def test_migration_round_trip_and_only_expected_tables(db_session: AsyncSession) -> None:
    # The session fixture verifies empty -> head -> base -> head and Alembic model parity.
    connection = await db_session.connection()
    tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    assert set(tables) == {model.__tablename__ for model in DOMAIN_MODELS} | {
        "alembic_version",
        "sessions",
        "workflow_runs",
        "tool_calls",
        "maintenance_requests",
        "emergency_incidents",
        "leasing_tours",
        "handoffs",
        "agent_turns",
    }


async def test_seed_is_idempotent_and_preserves_records(db_session: AsyncSession) -> None:
    await seed_demo(db_session)
    before = await row_counts(db_session)
    ids = {
        model.__tablename__: set(await db_session.scalars(select(model.id)))
        for model in DOMAIN_MODELS
    }
    await db_session.execute(
        update(Property).where(Property.slug == "atlas-residence").values(name="Custom Atlas")
    )
    await seed_demo(db_session)
    after = await row_counts(db_session)
    assert (
        before
        == after
        == {
            "properties": 2,
            "services": 11,
            "property_services": 14,
            "service_scenarios": 33,
            "units": 2,
            "residents": 2,
        }
    )
    assert ids == {
        model.__tablename__: set(await db_session.scalars(select(model.id)))
        for model in DOMAIN_MODELS
    }
    assert (
        await db_session.scalar(select(Property.name).where(Property.slug == "atlas-residence"))
        == "Custom Atlas"
    )
    print(f"seed counts first={before}; second={after}")


async def test_both_residents_and_distinct_property_catalogs(db_session: AsyncSession) -> None:
    await seed_demo(db_session)
    repository = SQLAlchemyPropertyRepository(db_session)
    resolver = PropertyContextResolver(repository)
    results = {}
    for first, slug, number, timezone in (
        ("Ikram", "atlas-residence", "204", "America/New_York"),
        ("Alex", "palm-heights", "318", "America/Los_Angeles"),
    ):
        resident = (
            await db_session.scalars(select(Resident).where(Resident.first_name == first))
        ).one()
        context = await resolver.resolve_by_resident_id(resident.id)
        property_record = await repository.get_property_by_slug(slug)
        assert property_record is not None and property_record.id == context.property.id
        assert context.property.timezone == timezone
        assert context.unit is not None and context.unit.number == number
        assert context.resident is not None and context.resident.first_name == first
        assert (
            context.resident.role == "resident" and context.resident.preferred_language == "English"
        )
        assert PropertyContext.model_validate_json(context.model_dump_json()) == context
        assert "_sa_instance_state" not in context.model_dump_json()
        assert resident.id.version == 4
        assert resident.created_at.utcoffset() == timedelta(0)
        scoped_ids = set(
            await db_session.scalars(
                select(PropertyService.id).where(PropertyService.property_id == context.property.id)
            )
        )
        assert {service.property_service_id for service in context.enabled_services} <= scoped_ids
        by_unit = await resolver.resolve_by_property_and_unit(context.property.id, number)
        assert by_unit.resident is None and by_unit.unit == context.unit
        assert by_unit.enabled_services == context.enabled_services
        results[slug] = {service.key for service in context.enabled_services}
        print(
            json.dumps(
                {
                    "resident": first,
                    "unit": number,
                    "property": slug,
                    "services": sorted(results[slug]),
                }
            )
        )
    assert results["atlas-residence"] == {
        "emergency_maintenance",
        "non_emergency_maintenance",
        "emergency_non_maintenance",
        "leasing",
        "renewal",
        "resident_support",
        "general_information",
        "human_handoff",
    }
    assert results["palm-heights"] == {
        "maintenance",
        "security",
        "parking",
        "leasing",
        "general_information",
        "human_handoff",
    }
    assert "emergency_maintenance" not in results["palm-heights"]


async def test_disabled_services_inactive_scenarios_and_property_specific_examples(
    db_session: AsyncSession,
) -> None:
    await seed_demo(db_session)
    repository = SQLAlchemyPropertyRepository(db_session)
    atlas = (
        await db_session.scalars(select(Property).where(Property.slug == "atlas-residence"))
    ).one()
    palm = (await db_session.scalars(select(Property).where(Property.slug == "palm-heights"))).one()
    atlas_services = await repository.get_enabled_services_for_property(atlas.id)
    palm_services = await repository.get_enabled_services_for_property(palm.id)
    atlas_leasing = next(item for item in atlas_services if item.key == "leasing")
    palm_leasing = next(item for item in palm_services if item.key == "leasing")
    assert (
        next(item for item in atlas_leasing.scenarios if item.key == "schedule_tour").examples
        != palm_leasing.scenarios[0].examples
    )
    await db_session.execute(
        update(ServiceScenario)
        .where(
            ServiceScenario.property_service_id == atlas_leasing.property_service_id,
            ServiceScenario.key == "schedule_tour",
        )
        .values(active=False)
    )
    disabled = next(item for item in atlas_services if item.key == "renewal")
    await db_session.execute(
        update(PropertyService)
        .where(PropertyService.id == disabled.property_service_id)
        .values(enabled=False)
    )
    context = await PropertyContextResolver(repository).resolve_by_property_and_unit(
        atlas.id, "204"
    )
    assert "renewal" not in {item.key for item in context.enabled_services}
    leasing = next(item for item in context.enabled_services if item.key == "leasing")
    assert {item.key for item in leasing.scenarios} == {
        "availability",
        "pricing",
        "application_question",
    }
    assert all(item.examples for service in context.enabled_services for item in service.scenarios)


async def test_dynamic_catalog_overrides_priority_and_detached_context(
    db_session: AsyncSession,
) -> None:
    await seed_demo(db_session)
    atlas = (
        await db_session.scalars(select(Property).where(Property.slug == "atlas-residence"))
    ).one()
    service = Service(
        key="package_lockers", name="Package Lockers", description="Canonical description"
    )
    db_session.add(service)
    await db_session.flush()
    association = PropertyService(
        property_id=atlas.id,
        service_id=service.id,
        priority=99,
        display_name="Atlas Parcel Help",
        description_override="Atlas-specific description",
        configuration={"desk": {"hours": "daytime"}},
        enabled=True,
    )
    db_session.add(association)
    await db_session.flush()
    db_session.add(
        ServiceScenario(
            property_service_id=association.id,
            key="missing_code",
            name="Missing code",
            description="Questions about a missing locker code",
            examples=["Where is my locker code?"],
        )
    )
    await db_session.flush()
    context = await PropertyContextResolver(
        SQLAlchemyPropertyRepository(db_session)
    ).resolve_by_property_and_unit(atlas.id, "204")
    first = context.enabled_services[0]
    assert first.key == "package_lockers" and first.display_name == "Atlas Parcel Help"
    assert (
        first.description == "Atlas-specific description"
        and first.scenarios[0].key == "missing_code"
    )
    first.configuration["desk"] = "changed outside ORM"
    assert association.configuration == {"desk": {"hours": "daytime"}}
    db_session.expunge_all()
    assert json.loads(context.model_dump_json())["enabled_services"][0]["key"] == "package_lockers"


async def test_cross_property_relationship_rejected_at_repository_and_database(
    db_session: AsyncSession,
) -> None:
    await seed_demo(db_session)
    resident = (
        await db_session.scalars(select(Resident).where(Resident.first_name == "Ikram"))
    ).one()
    palm_unit = (await db_session.scalars(select(Unit).where(Unit.number == "318"))).one()
    repository = SQLAlchemyPropertyRepository(db_session)
    original_unit = resident.unit_id
    with pytest.raises(PropertyContextError):
        await repository.set_resident_unit(resident.id, palm_unit.id)
    with pytest.raises(PropertyContextError):
        await repository.create_resident(
            ResidentData(
                property_id=resident.property_id,
                unit_id=palm_unit.id,
                first_name="Bad",
                last_name="Association",
            )
        )
    assert resident.unit_id == original_unit
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(Resident).where(Resident.id == resident.id).values(unit_id=palm_unit.id)
            )
    with pytest.raises(PropertyContextError):
        await PropertyContextResolver(repository).resolve_by_property_and_unit(
            resident.property_id, "318"
        )


async def test_unknown_inactive_and_unitless_context(db_session: AsyncSession) -> None:
    await seed_demo(db_session)
    repository = SQLAlchemyPropertyRepository(db_session)
    resolver = PropertyContextResolver(repository)
    with pytest.raises(PropertyContextError):
        await resolver.resolve_by_resident_id(uuid4())
    with pytest.raises(PropertyContextError):
        await resolver.resolve_by_property_and_unit(uuid4(), "204")
    atlas = (
        await db_session.scalars(select(Property).where(Property.slug == "atlas-residence"))
    ).one()
    resident = await repository.create_resident(
        ResidentData(property_id=atlas.id, first_name="Unitless", last_name="Contact")
    )
    assert (await resolver.resolve_by_resident_id(resident.id)).unit is None
    for model, row_id in ((Resident, resident.id), (Property, atlas.id)):
        await db_session.execute(update(model).where(model.id == row_id).values(active=False))
        with pytest.raises(PropertyContextError):
            await resolver.resolve_by_resident_id(resident.id)
        await db_session.execute(update(model).where(model.id == row_id).values(active=True))
    unit = (await db_session.scalars(select(Unit).where(Unit.property_id == atlas.id))).one()
    await db_session.execute(update(Unit).where(Unit.id == unit.id).values(active=False))
    with pytest.raises(PropertyContextError):
        await resolver.resolve_by_property_and_unit(atlas.id, unit.number)


@pytest.mark.parametrize(
    "kind", ["property", "service", "association", "scenario", "unit", "resident"]
)
async def test_natural_key_constraints(db_session: AsyncSession, kind: str) -> None:
    await seed_demo(db_session)
    property_record = (await db_session.scalars(select(Property))).first()
    assert property_record is not None
    service = (await db_session.scalars(select(Service))).first()
    assert service is not None
    association = (await db_session.scalars(select(PropertyService))).first()
    assert association is not None
    scenario_record = (await db_session.scalars(select(ServiceScenario))).first()
    assert scenario_record is not None
    unit = (await db_session.scalars(select(Unit))).first()
    assert unit is not None
    resident = (await db_session.scalars(select(Resident))).first()
    assert resident is not None
    candidates = {
        "property": Property(slug=property_record.slug, name="Duplicate", timezone="UTC"),
        "service": Service(key=service.key, name="Duplicate", description="Duplicate"),
        "association": PropertyService(
            property_id=association.property_id, service_id=association.service_id
        ),
        "scenario": ServiceScenario(
            property_service_id=scenario_record.property_service_id,
            key=scenario_record.key,
            name="Duplicate",
            description="Duplicate",
            examples=[],
        ),
        "unit": Unit(property_id=unit.property_id, number=unit.number),
        "resident": Resident(
            property_id=resident.property_id,
            external_id=resident.external_id,
            first_name="Duplicate",
            last_name="Resident",
            role="resident",
            preferred_language="English",
        ),
    }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(candidates[kind])
            await db_session.flush()


async def test_context_contract_rejects_cross_property_data(db_session: AsyncSession) -> None:
    await seed_demo(db_session)
    repository = SQLAlchemyPropertyRepository(db_session)
    resident = (
        await db_session.scalars(select(Resident).where(Resident.first_name == "Ikram"))
    ).one()
    context = await PropertyContextResolver(repository).resolve_by_resident_id(resident.id)
    payload = context.model_dump()
    payload["unit"]["property_id"] = uuid4()
    with pytest.raises(ValidationError):
        PropertyContext.model_validate(payload)
