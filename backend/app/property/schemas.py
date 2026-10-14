"""Provider- and ORM-independent property contracts. Service keys are data, not enums."""

from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

NonBlank = Annotated[str, Field(min_length=1, strict=True)]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PropertyData(DomainModel):
    slug: NonBlank
    name: NonBlank
    timezone: NonBlank
    active: bool = True

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown IANA timezone") from None
        return value


class PropertySummary(PropertyData):
    id: UUID


class UnitSummary(DomainModel):
    id: UUID
    property_id: UUID
    number: NonBlank
    active: bool


class ResidentData(DomainModel):
    property_id: UUID
    unit_id: UUID | None = None
    external_id: str | None = None
    first_name: NonBlank
    last_name: NonBlank
    phone: str | None = None
    email: str | None = None
    role: NonBlank = "resident"
    preferred_language: NonBlank = "English"
    active: bool = True


class ResidentSummary(ResidentData):
    id: UUID


class ScenarioContext(DomainModel):
    id: UUID
    key: NonBlank
    name: NonBlank
    description: str
    examples: list[str]


class EnabledServiceContext(DomainModel):
    property_service_id: UUID
    key: NonBlank
    display_name: str
    description: str
    priority: int
    configuration: dict[str, JsonValue]
    scenarios: list[ScenarioContext]


class PropertyContext(DomainModel):
    property: PropertySummary
    resident: ResidentSummary | None = None
    unit: UnitSummary | None = None
    enabled_services: list[EnabledServiceContext]

    @model_validator(mode="after")
    def validate_ownership(self) -> "PropertyContext":
        if self.unit is not None and self.unit.property_id != self.property.id:
            raise ValueError("Unit belongs to another property")
        if self.resident is not None:
            if self.resident.property_id != self.property.id:
                raise ValueError("Resident belongs to another property")
            if self.resident.unit_id != (self.unit.id if self.unit else None):
                raise ValueError("Resident and unit context do not match")
        return self
