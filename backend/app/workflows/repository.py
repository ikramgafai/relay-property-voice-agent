"""Domain-side durable aggregate boundary."""

from typing import Protocol
from uuid import UUID

from app.workflows.schemas import ConversationState


class ConversationRepository(Protocol):
    async def get(self, session_id: UUID, property_id: UUID) -> ConversationState | None: ...

    async def save(self, state: ConversationState) -> ConversationState:
        """Atomically save all runs using state.revision; return incremented revision."""
        ...
