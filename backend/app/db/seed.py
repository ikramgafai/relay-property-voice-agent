"""Insert missing demo records by natural key; never delete or overwrite user configuration."""

import asyncio
import json
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import load_settings
from app.db.base import Base
from app.db.models import Property, PropertyService, Resident, Service, ServiceScenario, Unit
from app.db.repository import SQLAlchemyPropertyRepository
from app.db.seed_data import ATLAS_SCENARIOS, PALM_SCENARIOS, SERVICE_NAMES
from app.db.session import Database
from app.property.schemas import PropertyData, ResidentData

DOMAIN_MODELS = (Property, Service, PropertyService, ServiceScenario, Unit, Resident)


async def insert_missing(
    session: AsyncSession, model: type[Base], keys: dict[str, Any], values: dict[str, Any]
) -> UUID:
    await session.execute(
        insert(model).values(**keys, **values).on_conflict_do_nothing(index_elements=list(keys))
    )
    statement = select(model.__table__.c.id).filter_by(**keys)
    result = await session.scalar(statement)
    if not isinstance(result, UUID):
        raise RuntimeError("Seed natural-key lookup failed")
    return result


async def row_counts(session: AsyncSession) -> dict[str, int]:
    return {
        model.__tablename__: int(await session.scalar(select(func.count()).select_from(model)) or 0)
        for model in DOMAIN_MODELS
    }


async def seed_demo(session: AsyncSession) -> None:
    repository = SQLAlchemyPropertyRepository(session)
    for slug, name, timezone, number, first, last, scenarios in (
        (
            "atlas-residence",
            "Atlas Residence",
            "America/New_York",
            "204",
            "Ikram",
            "Gafai",
            ATLAS_SCENARIOS,
        ),
        (
            "palm-heights",
            "Palm Heights",
            "America/Los_Angeles",
            "318",
            "Alex",
            "Morgan",
            PALM_SCENARIOS,
        ),
    ):
        data = PropertyData(slug=slug, name=name, timezone=timezone)
        property_id = await insert_missing(
            session, Property, {"slug": slug}, data.model_dump(exclude={"slug"})
        )
        unit_id = await insert_missing(
            session, Unit, {"property_id": property_id, "number": number}, {"active": True}
        )
        resident_data = ResidentData(
            property_id=property_id,
            unit_id=unit_id,
            external_id=f"relay-demo-{first.lower()}",
            first_name=first,
            last_name=last,
        )
        await repository.validate_resident_unit(property_id, unit_id)
        await insert_missing(
            session,
            Resident,
            {"property_id": property_id, "external_id": resident_data.external_id},
            resident_data.model_dump(exclude={"property_id", "external_id"}),
        )
        for key, examples in scenarios.items():
            display_name = SERVICE_NAMES[key]
            service_id = await insert_missing(
                session,
                Service,
                {"key": key},
                {
                    "name": display_name,
                    "description": f"Property services for {display_name.lower()}.",
                },
            )
            association_id = await insert_missing(
                session,
                PropertyService,
                {"property_id": property_id, "service_id": service_id},
                {
                    "enabled": True,
                    "display_name": display_name,
                    "priority": 0,
                    "configuration": {},
                },
            )
            for item in examples:
                await insert_missing(
                    session,
                    ServiceScenario,
                    {"property_service_id": association_id, "key": item.key},
                    {
                        "name": item.name,
                        "description": item.description,
                        "examples": list(item.examples),
                        "active": True,
                    },
                )


async def run_seed() -> None:
    database = Database(load_settings().database_url.get_secret_value())
    try:
        async with database.session() as session:
            await seed_demo(session)
            counts = await row_counts(session)
        print(json.dumps(counts, sort_keys=True))
    finally:
        await database.close()


def main() -> None:
    asyncio.run(run_seed())
