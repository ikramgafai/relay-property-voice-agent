"""Read contract consumed by context resolution, with no persistence imports."""

from typing import Protocol
from uuid import UUID

from app.property.schemas import (
    EnabledServiceContext,
    PropertySummary,
    ResidentSummary,
    UnitSummary,
)


class PropertyRepository(Protocol):
    async def get_property_by_id(self, property_id: UUID) -> PropertySummary | None: ...
    async def get_property_by_slug(self, slug: str) -> PropertySummary | None: ...
    async def get_resident_by_id(self, resident_id: UUID) -> ResidentSummary | None: ...
    async def get_unit_for_property(self, property_id: UUID, number: str) -> UnitSummary | None: ...
    async def get_unit_by_id(self, property_id: UUID, unit_id: UUID) -> UnitSummary | None: ...
    async def get_enabled_services_for_property(
        self, property_id: UUID
    ) -> list[EnabledServiceContext]: ...
