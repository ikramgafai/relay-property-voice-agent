"""Durable conversation and workflow state only.

Revision ID: 0002_workflow_state
Revises: 0001_property_domain
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_workflow_state"
down_revision = "0001_property_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("property_id", sa.Uuid(), sa.ForeignKey("properties.id"), nullable=False),
        sa.Column("resident_id", sa.Uuid(), sa.ForeignKey("residents.id")),
        sa.Column("unit_id", sa.Uuid()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("id", "property_id", name="uq_sessions_id_property_id"),
        sa.ForeignKeyConstraint(
            ["unit_id", "property_id"],
            ["units.id", "units.property_id"],
            name="fk_sessions_unit_property",
        ),
        sa.CheckConstraint("revision >= 1", name="positive_revision"),
        sa.CheckConstraint("jsonb_typeof(state) = 'object'", name="state_object"),
    )
    op.create_index("ix_sessions_property_id", "sessions", ["property_id"])
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column(
            "property_service_id", sa.Uuid(), sa.ForeignKey("property_services.id"), nullable=False
        ),
        sa.Column("workflow_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "property_id"],
            ["sessions.id", "sessions.property_id"],
            name="fk_workflow_runs_session_property",
        ),
        sa.CheckConstraint("jsonb_typeof(state) = 'object'", name="state_object"),
    )
    op.create_index("ix_workflow_runs_session_id", "workflow_runs", ["session_id"])


def downgrade() -> None:
    op.drop_table("workflow_runs")
    op.drop_table("sessions")
