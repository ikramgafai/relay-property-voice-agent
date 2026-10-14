"""Local tool audit and five trusted operations (four domain tables).

Revision ID: 0003_operational_tools
Revises: 0002_workflow_state
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import Constraint

revision = "0003_operational_tools"
down_revision = "0002_workflow_state"
branch_labels = None
depends_on = None


def identity_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("resident_id", sa.Uuid()),
        sa.Column("unit_id", sa.Uuid()),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("action_request_id", sa.Uuid(), nullable=False),
    ]


def owner_constraints(label: str) -> list[Constraint]:
    return [
        sa.ForeignKeyConstraint(
            ["session_id", "property_id"],
            ["sessions.id", "sessions.property_id"],
            name=f"fk_{label}_session_property",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id", "session_id", "property_id"],
            ["workflow_runs.id", "workflow_runs.session_id", "workflow_runs.property_id"],
            name=f"fk_{label}_workflow_owner",
        ),
        sa.ForeignKeyConstraint(
            ["resident_id", "property_id"],
            ["residents.id", "residents.property_id"],
            name=f"fk_{label}_resident_property",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id", "property_id"],
            ["units.id", "units.property_id"],
            name=f"fk_{label}_unit_property",
        ),
    ]


def domain_constraints(label: str) -> list[Constraint]:
    return owner_constraints(label) + [
        sa.ForeignKeyConstraint(
            ["action_request_id", "session_id", "workflow_id", "property_id"],
            [
                "tool_calls.action_request_id",
                "tool_calls.session_id",
                "tool_calls.workflow_id",
                "tool_calls.property_id",
            ],
            name=f"fk_{label}_tool_source",
        ),
        sa.UniqueConstraint("action_request_id"),
    ]


def upgrade() -> None:
    op.create_unique_constraint("uq_residents_id_property_id", "residents", ["id", "property_id"])
    op.create_unique_constraint(
        "uq_workflow_runs_identity", "workflow_runs", ["id", "session_id", "property_id"]
    )
    op.create_table(
        "tool_calls",
        *identity_columns(),
        *owner_constraints("tool_calls"),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("replay_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_payload", JSONB()),
        sa.Column("operation_id", sa.Uuid()),
        sa.Column("error_code", sa.String(80)),
        sa.Column("safe_error_message", sa.String(200)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.UniqueConstraint("action_request_id"),
        sa.UniqueConstraint(
            "action_request_id",
            "session_id",
            "workflow_id",
            "property_id",
            name="uq_tool_calls_source",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SUCCEEDED','FAILED','UNKNOWN_OUTCOME')", name="status_values"
        ),
        sa.CheckConstraint("attempt_count >= 1 AND replay_count >= 0", name="attempt_counts"),
        sa.CheckConstraint(
            "(status = 'SUCCEEDED') = (operation_id IS NOT NULL)", name="success_operation"
        ),
    )
    op.create_table(
        "maintenance_requests",
        *identity_columns(),
        *domain_constraints("maintenancerequest"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("symptom_details", sa.Text(), nullable=False),
        sa.Column("unit_number", sa.String(50), nullable=False),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
    )
    op.create_table(
        "emergency_incidents",
        *identity_columns(),
        *domain_constraints("emergencyincident"),
        sa.UniqueConstraint("workflow_id"),
        sa.UniqueConstraint("id", "property_id", name="uq_emergency_incidents_id_property"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("location_detail", sa.Text(), nullable=False),
        sa.Column("hazard_detail", sa.Text(), nullable=False),
        sa.Column("severity_detail", sa.Text(), nullable=False),
        sa.Column("unit_number", sa.String(50), nullable=False),
        sa.Column("scenario_key", sa.String(120)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(30), nullable=False),
    )
    op.create_table(
        "leasing_tours",
        *identity_columns(),
        *domain_constraints("leasingtour"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contact_detail", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
    )
    op.create_table(
        "handoffs",
        *identity_columns(),
        *domain_constraints("handoff"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("context", JSONB(), nullable=False),
        sa.Column("incident_id", sa.Uuid()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.ForeignKeyConstraint(
            ["incident_id", "property_id"],
            ["emergency_incidents.id", "emergency_incidents.property_id"],
            name="fk_handoffs_incident_property",
        ),
    )


def downgrade() -> None:
    for table in (
        "handoffs",
        "leasing_tours",
        "emergency_incidents",
        "maintenance_requests",
        "tool_calls",
    ):
        op.drop_table(table)
    op.drop_constraint("uq_workflow_runs_identity", "workflow_runs", type_="unique")
    op.drop_constraint("uq_residents_id_property_id", "residents", type_="unique")
