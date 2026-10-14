from typing import Protocol
from uuid import UUID

from app.agent.schemas import AgentTurnRequest, AgentTurnResult
from app.realtime.schemas import Binding


class TurnProcessor(Protocol):
    async def process(self, request: AgentTurnRequest) -> AgentTurnResult: ...


class SessionLookup(Protocol):
    async def __call__(self, session_id: UUID) -> Binding: ...


class SpeechAttachment(Protocol):
    def on_response(self, response_text: str) -> None: ...
    async def close(self) -> None: ...
