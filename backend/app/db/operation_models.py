"""Local operational records and durable tool audit, with restrictive ownership FKs."""

from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy.schema import Constraint

from app.db.base import Base, Entity


def ownership(table: str) -> tuple[Constraint, ...]:
    return (
        ForeignKeyConstraint(
            ["session_id", "property_id"],
            ["sessions.id", "sessions.property_id"],
            name=f"fk_{table}_session_property",
        ),
        ForeignKeyConstraint(
            ["workflow_id", "session_id", "property_id"],
            ["workflow_runs.id", "workflow_runs.session_id", "workflow_runs.property_id"],
            name=f"fk_{table}_workflow_owner",
        ),
        ForeignKeyConstraint(
            ["resident_id", "property_id"],
            ["residents.id", "residents.property_id"],
            name=f"fk_{table}_resident_property",
        ),
        ForeignKeyConstraint(
            ["unit_id", "property_id"],
            ["units.id", "units.property_id"],
            name=f"fk_{table}_unit_property",
        ),
    )


class OperationIdentity:
    property_id: Mapped[UUID]
    resident_id: Mapped[UUID | None]
    unit_id: Mapped[UUID | None]
    session_id: Mapped[UUID]
    workflow_id: Mapped[UUID]
    action_request_id: Mapped[UUID]


class ToolCall(OperationIdentity, Entity, Base):
    __tablename__ = "tool_calls"
    __table_args__ = ownership("tool_calls") + (
        UniqueConstraint("action_request_id"),
        UniqueConstraint(
            "action_request_id",
            "session_id",
            "workflow_id",
            "property_id",
            name="uq_tool_calls_source",
        ),
        CheckConstraint(
            "status IN ('PENDING','SUCCEEDED','FAILED','UNKNOWN_OUTCOME')", name="status_values"
        ),
        CheckConstraint("attempt_count >= 1 AND replay_count >= 0", name="attempt_counts"),
        CheckConstraint(
            "(status = 'SUCCEEDED') = (operation_id IS NOT NULL)", name="success_operation"
        ),
    )
    action_type: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    request_payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(40))
    attempt_count: Mapped[int] = mapped_column(default=1, server_default="1")
    replay_count: Mapped[int] = mapped_column(default=0, server_default="0")
    result_payload: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    operation_id: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(String(80))
    safe_error_message: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float]


def operation_constraints(table: str) -> tuple[Constraint, ...]:
    return ownership(table) + (
        ForeignKeyConstraint(
            ["action_request_id", "session_id", "workflow_id", "property_id"],
            [
                "tool_calls.action_request_id",
                "tool_calls.session_id",
                "tool_calls.workflow_id",
                "tool_calls.property_id",
            ],
            name=f"fk_{table}_tool_source",
        ),
        UniqueConstraint("action_request_id"),
    )


class DomainOperation(OperationIdentity):
    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple[Constraint, ...]:
        return operation_constraints(cls.__name__.lower())


class MaintenanceRequest(DomainOperation, Entity, Base):
    __tablename__ = "maintenance_requests"
    description: Mapped[str] = mapped_column(Text)
    symptom_details: Mapped[str] = mapped_column(Text)
    unit_number: Mapped[str] = mapped_column(String(50))
    priority: Mapped[str] = mapped_column(String(20), default="NORMAL")
    status: Mapped[str] = mapped_column(String(30), default="OPEN")


class EmergencyIncident(DomainOperation, Entity, Base):
    __tablename__ = "emergency_incidents"
    __table_args__ = operation_constraints("emergencyincident") + (
        UniqueConstraint("id", "property_id", name="uq_emergency_incidents_id_property"),
    )
    # One create per emergency run, even if a caller mistakenly proposes another identity.
    workflow_id: Mapped[UUID] = mapped_column(unique=True)
    description: Mapped[str] = mapped_column(Text)
    location_detail: Mapped[str] = mapped_column(Text)
    hazard_detail: Mapped[str] = mapped_column(Text)
    severity_detail: Mapped[str] = mapped_column(Text)
    unit_number: Mapped[str] = mapped_column(String(50))
    scenario_key: Mapped[str | None] = mapped_column(String(120))
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(30), default="OPEN")


class LeasingTour(DomainOperation, Entity, Base):
    __tablename__ = "leasing_tours"
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    contact_detail: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="REQUESTED")


class Handoff(DomainOperation, Entity, Base):
    __tablename__ = "handoffs"
    __table_args__ = operation_constraints("handoff") + (
        ForeignKeyConstraint(
            ["incident_id", "property_id"],
            ["emergency_incidents.id", "emergency_incidents.property_id"],
            name="fk_handoffs_incident_property",
        ),
    )
    reason: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    incident_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(30), default="REQUESTED")
