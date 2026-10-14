"""Property persistence queries; ORM instances never cross this boundary."""

from copy import deepcopy
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Property, PropertyService, Resident, Service, ServiceScenario, Unit
from app.property.errors import PropertyContextError
from app.property.schemas import (
    EnabledServiceContext,
    PropertySummary,
    ResidentData,
    ResidentSummary,
    ScenarioContext,
    UnitSummary,
)


def property_summary(row: Property) -> PropertySummary:
    return PropertySummary(
        id=row.id, slug=row.slug, name=row.name, timezone=row.timezone, active=row.active
    )


def unit_summary(row: Unit) -> UnitSummary:
    return UnitSummary(id=row.id, property_id=row.property_id, number=row.number, active=row.active)


def resident_summary(row: Resident) -> ResidentSummary:
    return ResidentSummary(
        id=row.id,
        property_id=row.property_id,
        unit_id=row.unit_id,
        external_id=row.external_id,
        first_name=row.first_name,
        last_name=row.last_name,
        phone=row.phone,
        email=row.email,
        role=row.role,
        preferred_language=row.preferred_language,
        active=row.active,
    )


class SQLAlchemyPropertyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_property_by_id(self, property_id: UUID) -> PropertySummary | None:
        row = await self.session.get(Property, property_id)
        return property_summary(row) if row else None

    async def get_property_by_slug(self, slug: str) -> PropertySummary | None:
        row = await self.session.scalar(select(Property).where(Property.slug == slug))
        return property_summary(row) if row else None

    async def get_resident_by_id(self, resident_id: UUID) -> ResidentSummary | None:
        row = await self.session.get(Resident, resident_id)
        return resident_summary(row) if row else None

    async def get_unit_for_property(self, property_id: UUID, number: str) -> UnitSummary | None:
        row = await self.session.scalar(
            select(Unit).where(Unit.property_id == property_id, Unit.number == number)
        )
        return unit_summary(row) if row else None

    async def get_unit_by_id(self, property_id: UUID, unit_id: UUID) -> UnitSummary | None:
        row = await self.session.scalar(
            select(Unit).where(Unit.property_id == property_id, Unit.id == unit_id)
        )
        return unit_summary(row) if row else None

    async def load_service_scenarios(self, property_id: UUID) -> dict[UUID, list[ScenarioContext]]:
        rows = await self.session.scalars(
            select(ServiceScenario)
            .join(PropertyService)
            .where(
                PropertyService.property_id == property_id,
                PropertyService.enabled.is_(True),
                ServiceScenario.active.is_(True),
            )
            .order_by(ServiceScenario.key)
        )
        result: dict[UUID, list[ScenarioContext]] = {}
        for row in rows:
            result.setdefault(row.property_service_id, []).append(
                ScenarioContext(
                    id=row.id,
                    key=row.key,
                    name=row.name,
                    description=row.description,
                    examples=list(row.examples),
                )
            )
        return result

    async def get_enabled_services_for_property(
        self, property_id: UUID
    ) -> list[EnabledServiceContext]:
        rows = await self.session.execute(
            select(PropertyService, Service)
            .join(Service)
            .where(PropertyService.property_id == property_id, PropertyService.enabled.is_(True))
            .order_by(PropertyService.priority.desc(), Service.key)
        )
        scenarios = await self.load_service_scenarios(property_id)
        return [
            EnabledServiceContext(
                property_service_id=association.id,
                key=service.key,
                display_name=association.display_name
                if association.display_name is not None
                else service.name,
                description=association.description_override
                if association.description_override is not None
                else service.description,
                priority=association.priority,
                configuration=deepcopy(association.configuration),
                scenarios=scenarios.get(association.id, []),
            )
            for association, service in rows
        ]

    async def validate_resident_unit(self, property_id: UUID, unit_id: UUID | None) -> None:
        property_record = await self.get_property_by_id(property_id)
        if property_record is None or not property_record.active:
            raise PropertyContextError("Property is missing or inactive")
        if unit_id is not None:
            unit = await self.get_unit_by_id(property_id, unit_id)
            if unit is None or not unit.active:
                raise PropertyContextError(
                    "Unit must be active and belong to the resident property"
                )

    async def create_resident(self, data: ResidentData) -> ResidentSummary:
        await self.validate_resident_unit(data.property_id, data.unit_id)
        row = Resident(**data.model_dump())
        self.session.add(row)
        await self.session.flush()
        return resident_summary(row)

    async def set_resident_unit(self, resident_id: UUID, unit_id: UUID | None) -> ResidentSummary:
        row = await self.session.get(Resident, resident_id)
        if row is None or not row.active:
            raise PropertyContextError("Resident is missing or inactive")
        await self.validate_resident_unit(row.property_id, unit_id)
        row.unit_id = unit_id
        await self.session.flush()
        return resident_summary(row)
