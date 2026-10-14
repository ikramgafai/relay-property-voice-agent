"""Static trusted operation dispatch; configuration cannot name Python callables."""

from collections.abc import Awaitable, Callable

from app.tools.errors import ToolFailure
from app.tools.protocol import PropertyOperationsAdapter
from app.tools.schemas import ErrorCode, ExecutionContext, OperationOutput
from app.workflows.schemas import (
    ActionRequest,
    ActionType,
    EmergencyPayload,
    EmergencyUpdatePayload,
    HandoffPayload,
    MaintenancePayload,
    TourPayload,
)

Tool = Callable[
    [PropertyOperationsAdapter, ExecutionContext, ActionRequest], Awaitable[OperationOutput]
]


async def maintenance(
    adapter: PropertyOperationsAdapter, source: ExecutionContext, request: ActionRequest
) -> OperationOutput:
    if not isinstance(request.payload, MaintenancePayload):
        raise ToolFailure(ErrorCode.VALIDATION)
    return await adapter.create_maintenance(source, request, request.payload)


async def emergency(
    adapter: PropertyOperationsAdapter, source: ExecutionContext, request: ActionRequest
) -> OperationOutput:
    if not isinstance(request.payload, EmergencyPayload):
        raise ToolFailure(ErrorCode.VALIDATION)
    return await adapter.create_emergency(source, request, request.payload)


async def update_emergency(
    adapter: PropertyOperationsAdapter, source: ExecutionContext, request: ActionRequest
) -> OperationOutput:
    if not isinstance(request.payload, EmergencyUpdatePayload):
        raise ToolFailure(ErrorCode.VALIDATION)
    return await adapter.update_emergency(source, request, request.payload)


async def tour(
    adapter: PropertyOperationsAdapter, source: ExecutionContext, request: ActionRequest
) -> OperationOutput:
    if not isinstance(request.payload, TourPayload) or request.payload.requested_at is None:
        raise ToolFailure(ErrorCode.VALIDATION)
    return await adapter.schedule_tour(source, request, request.payload)


async def handoff(
    adapter: PropertyOperationsAdapter, source: ExecutionContext, request: ActionRequest
) -> OperationOutput:
    if not isinstance(request.payload, HandoffPayload):
        raise ToolFailure(ErrorCode.VALIDATION)
    return await adapter.create_handoff(source, request, request.payload)


TOOLS: dict[ActionType, Tool] = {
    ActionType.MAINTENANCE: maintenance,
    ActionType.EMERGENCY: emergency,
    ActionType.EMERGENCY_UPDATE: update_emergency,
    ActionType.TOUR: tour,
    ActionType.HANDOFF: handoff,
}
