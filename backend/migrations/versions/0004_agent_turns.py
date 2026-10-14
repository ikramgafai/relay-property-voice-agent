"""Durable text-turn checkpoints and response replay."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_agent_turns"
down_revision = "0003_operational_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_turns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("finished", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "property_id"],
            ["sessions.id", "sessions.property_id"],
            name="fk_agent_turns_session_property",
        ),
        sa.UniqueConstraint("session_id", "turn_number", name="uq_agent_turns_session_number"),
        sa.CheckConstraint("turn_number >= 1", name="positive_turn"),
    )


def downgrade() -> None:
    op.drop_table("agent_turns")
