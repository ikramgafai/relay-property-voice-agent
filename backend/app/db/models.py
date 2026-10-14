"""Property domain and Phase 4 application state; no operational business records."""

from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Entity


class Property(Entity, Base):
    __tablename__ = "properties"
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class Service(Entity, Base):
    __tablename__ = "services"
    key: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)


class PropertyService(Entity, Base):
    __tablename__ = "property_services"
    __table_args__ = (
        UniqueConstraint("property_id", "service_id"),
        CheckConstraint("jsonb_typeof(configuration) = 'object'", name="configuration_object"),
    )
    property_id: Mapped[UUID] = mapped_column(ForeignKey("properties.id"), index=True)
    service_id: Mapped[UUID] = mapped_column(ForeignKey("services.id"))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    display_name: Mapped[str | None] = mapped_column(String(200))
    description_override: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(default=0, server_default="0")
    configuration: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class ServiceScenario(Entity, Base):
    __tablename__ = "service_scenarios"
    __table_args__ = (
        UniqueConstraint("property_service_id", "key"),
        CheckConstraint("jsonb_typeof(examples) = 'array'", name="examples_array"),
    )
    property_service_id: Mapped[UUID] = mapped_column(
        ForeignKey("property_services.id"), index=True
    )
    key: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    examples: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class Unit(Entity, Base):
    __tablename__ = "units"
    __table_args__ = (
        UniqueConstraint("property_id", "number"),
        UniqueConstraint("id", "property_id", name="uq_units_id_property_id"),
    )
    property_id: Mapped[UUID] = mapped_column(ForeignKey("properties.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    number: Mapped[str] = mapped_column(String(50))
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class Resident(Entity, Base):
    __tablename__ = "residents"
    __table_args__ = (
        UniqueConstraint("id", "property_id", name="uq_residents_id_property_id"),
        ForeignKeyConstraint(
            ["unit_id", "property_id"],
            ["units.id", "units.property_id"],
            name="fk_residents_unit_property",
        ),
        UniqueConstraint("property_id", "external_id", name="uq_residents_property_external_id"),
    )
    property_id: Mapped[UUID] = mapped_column(ForeignKey("properties.id"), index=True)
    unit_id: Mapped[UUID | None] = mapped_column(index=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(50))
    preferred_language: Mapped[str] = mapped_column(String(50))
    active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))


class ConversationSession(Entity, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("id", "property_id", name="uq_sessions_id_property_id"),
        ForeignKeyConstraint(
            ["unit_id", "property_id"],
            ["units.id", "units.property_id"],
            name="fk_sessions_unit_property",
        ),
        CheckConstraint("revision >= 1", name="positive_revision"),
        CheckConstraint("jsonb_typeof(state) = 'object'", name="state_object"),
    )
    property_id: Mapped[UUID] = mapped_column(ForeignKey("properties.id"), index=True)
    resident_id: Mapped[UUID | None] = mapped_column(ForeignKey("residents.id"))
    unit_id: Mapped[UUID | None]
    revision: Mapped[int]
    state: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)


class WorkflowRun(Entity, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint("id", "session_id", "property_id", name="uq_workflow_runs_identity"),
        ForeignKeyConstraint(
            ["session_id", "property_id"],
            ["sessions.id", "sessions.property_id"],
            name="fk_workflow_runs_session_property",
        ),
        CheckConstraint("jsonb_typeof(state) = 'object'", name="state_object"),
    )
    session_id: Mapped[UUID] = mapped_column(index=True)
    property_id: Mapped[UUID]
    property_service_id: Mapped[UUID] = mapped_column(ForeignKey("property_services.id"))
    workflow_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40))
    state: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
