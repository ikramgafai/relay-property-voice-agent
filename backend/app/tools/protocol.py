"""Provider-neutral operations and transaction boundaries. No SQLAlchemy types."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from app.tools.schemas import ExecutionContext, OperationOutput, ToolResult
from app.workflows.schemas import (
    ActionRequest,
    EmergencyPayload,
    EmergencyUpdatePayload,
    HandoffPayload,
    MaintenancePayload,
    TourPayload,
)


class PropertyOperationsAdapter(Protocol):
    async def create_maintenance(
        self, source: ExecutionContext, request: ActionRequest, payload: MaintenancePayload
    ) -> OperationOutput: ...
    async def create_emergency(
        self, source: ExecutionContext, request: ActionRequest, payload: EmergencyPayload
    ) -> OperationOutput: ...
    async def update_emergency(
        self, source: ExecutionContext, request: ActionRequest, payload: EmergencyUpdatePayload
    ) -> OperationOutput: ...
    async def schedule_tour(
        self, source: ExecutionContext, request: ActionRequest, payload: TourPayload
    ) -> OperationOutput: ...
    async def create_handoff(
        self, source: ExecutionContext, request: ActionRequest, payload: HandoffPayload
    ) -> OperationOutput: ...


class ToolTransaction(Protocol):
    operations: PropertyOperationsAdapter

    async def source(self, session_id: UUID, request: ActionRequest) -> ExecutionContext: ...
    async def existing(self, request: ActionRequest, digest: str) -> ToolResult | None: ...
    async def record(
        self, source: ExecutionContext, request: ActionRequest, digest: str, result: ToolResult
    ) -> None: ...
    def operation(self) -> AbstractAsyncContextManager[None]: ...


class ToolStore(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[ToolTransaction]: ...
    async def verify(
        self, session_id: UUID, request: ActionRequest, digest: str
    ) -> ToolResult | None: ...
