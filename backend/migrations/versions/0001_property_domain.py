"""Create only the six Phase 2 property domain tables."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_property_domain"
down_revision = None
branch_labels = None
depends_on = None


def entity_columns() -> list[sa.Column[Any]]:
    # UUID4 is generated in Python by the application, not by a database extension.
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "properties",
        *entity_columns(),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_properties"),
    )
    op.create_index("ix_properties_slug", "properties", ["slug"], unique=True)
    op.create_table(
        "services",
        *entity_columns(),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_services"),
        sa.UniqueConstraint("key", name="uq_services_key"),
    )
    op.create_table(
        "property_services",
        *entity_columns(),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("description_override", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "configuration",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_property_services"),
        sa.UniqueConstraint("property_id", "service_id", name="uq_property_services_property_id"),
        sa.ForeignKeyConstraint(
            ["property_id"], ["properties.id"], name="fk_property_services_property_id_properties"
        ),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name="fk_property_services_service_id_services"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(configuration) = 'object'",
            name="ck_property_services_configuration_object",
        ),
    )
    op.create_index("ix_property_services_property_id", "property_services", ["property_id"])
    op.create_table(
        "service_scenarios",
        *entity_columns(),
        sa.Column("property_service_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "examples", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_service_scenarios"),
        sa.UniqueConstraint(
            "property_service_id", "key", name="uq_service_scenarios_property_service_id"
        ),
        sa.ForeignKeyConstraint(
            ["property_service_id"],
            ["property_services.id"],
            name="fk_service_scenarios_property_service_id_property_services",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(examples) = 'array'", name="ck_service_scenarios_examples_array"
        ),
    )
    op.create_index(
        "ix_service_scenarios_property_service_id", "service_scenarios", ["property_service_id"]
    )
    op.create_table(
        "units",
        *entity_columns(),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("number", sa.String(50), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_units"),
        sa.UniqueConstraint("property_id", "number", name="uq_units_property_id"),
        sa.UniqueConstraint("id", "property_id", name="uq_units_id_property_id"),
        sa.ForeignKeyConstraint(
            ["property_id"], ["properties.id"], name="fk_units_property_id_properties"
        ),
    )
    op.create_index("ix_units_property_id", "units", ["property_id"])
    op.create_table(
        "residents",
        *entity_columns(),
        sa.Column("property_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=True),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("last_name", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("preferred_language", sa.String(50), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_residents"),
        sa.UniqueConstraint("property_id", "external_id", name="uq_residents_property_external_id"),
        sa.ForeignKeyConstraint(
            ["property_id"], ["properties.id"], name="fk_residents_property_id_properties"
        ),
        sa.ForeignKeyConstraint(
            ["unit_id", "property_id"],
            ["units.id", "units.property_id"],
            name="fk_residents_unit_property",
        ),
    )
    op.create_index("ix_residents_property_id", "residents", ["property_id"])
    op.create_index("ix_residents_unit_id", "residents", ["unit_id"])


def downgrade() -> None:
    for table in (
        "residents",
        "units",
        "service_scenarios",
        "property_services",
        "services",
        "properties",
    ):
        op.drop_table(table)
