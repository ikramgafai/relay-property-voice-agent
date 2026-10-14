from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Entity


class AgentTurn(Entity, Base):
    __tablename__ = "agent_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "property_id"],
            ["sessions.id", "sessions.property_id"],
            name="fk_agent_turns_session_property",
        ),
        UniqueConstraint("session_id", "turn_number", name="uq_agent_turns_session_number"),
        CheckConstraint("turn_number >= 1", name="positive_turn"),
    )
    session_id: Mapped[UUID]
    property_id: Mapped[UUID]
    turn_number: Mapped[int]
    finished: Mapped[bool] = mapped_column(default=False, server_default="false")
    checkpoint: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
