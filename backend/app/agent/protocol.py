from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from app.agent.reply_schemas import TurnInterpretationRequest, TurnInterpretationResponse
from app.agent.schemas import AgentTurnRequest, AgentTurnResult, ResponsePlan, TurnCheckpoint
from app.property.schemas import PropertyContext
from app.tools.schemas import ToolResult
from app.workflows.schemas import ActionRequest, ConversationState


class TurnStore(Protocol):
    def guard(self, session_id: UUID) -> AbstractAsyncContextManager[None]: ...
    async def start(self, request: AgentTurnRequest) -> TurnCheckpoint: ...
    async def load(self, session_id: UUID) -> tuple[ConversationState, PropertyContext]: ...
    async def checkpoint(
        self, value: TurnCheckpoint, state: ConversationState | None = None
    ) -> ConversationState | None: ...
    async def finish(
        self, value: TurnCheckpoint, state: ConversationState, result: AgentTurnResult
    ) -> AgentTurnResult: ...
    async def verified(self, session_id: UUID, request: ActionRequest) -> ToolResult | None: ...


class ResponseRenderer(Protocol):
    async def render(self, plan: ResponsePlan) -> str: ...


class PhrasingProvider(Protocol):
    async def choose(self, plan: ResponsePlan, alternatives: list[str]) -> int: ...


class TurnInterpretationProvider(Protocol):
    """One-shot advisory interpretation of a reply to a pending workflow question (ADR 008)."""

    async def interpret_turn(
        self, request: TurnInterpretationRequest
    ) -> TurnInterpretationResponse: ...
