"""Property context resolution only; no intent interpretation or business execution."""

from uuid import UUID

from app.property.errors import PropertyContextError
from app.property.repository import PropertyRepository
from app.property.schemas import PropertyContext, PropertySummary


class PropertyContextResolver:
    def __init__(self, repository: PropertyRepository) -> None:
        self.repository = repository

    async def _property(self, property_id: UUID) -> PropertySummary:
        property_record = await self.repository.get_property_by_id(property_id)
        if property_record is None or not property_record.active:
            raise PropertyContextError("Property is missing or inactive")
        return property_record

    async def resolve_by_resident_id(self, resident_id: UUID) -> PropertyContext:
        resident = await self.repository.get_resident_by_id(resident_id)
        if resident is None or not resident.active:
            raise PropertyContextError("Resident is missing or inactive")
        property_record = await self._property(resident.property_id)
        unit = None
        if resident.unit_id is not None:
            unit = await self.repository.get_unit_by_id(property_record.id, resident.unit_id)
            if unit is None or not unit.active:
                raise PropertyContextError(
                    "Resident unit is missing, inactive, or belongs elsewhere"
                )
        return PropertyContext(
            property=property_record,
            resident=resident,
            unit=unit,
            enabled_services=await self.repository.get_enabled_services_for_property(
                property_record.id
            ),
        )

    async def resolve_by_property_and_unit(
        self, property_id: UUID, unit_number: str
    ) -> PropertyContext:
        property_record = await self._property(property_id)
        unit = await self.repository.get_unit_for_property(property_id, unit_number)
        if unit is None or not unit.active:
            raise PropertyContextError("Unit is missing or inactive for this property")
        return PropertyContext(
            property=property_record,
            unit=unit,
            resident=None,
            enabled_services=await self.repository.get_enabled_services_for_property(property_id),
        )
