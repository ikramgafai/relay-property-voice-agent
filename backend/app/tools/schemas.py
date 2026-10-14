"""Operational contracts. A committed ToolResult, never a request, establishes success."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.property.schemas import DomainModel, PropertyContext
from app.workflows.schemas import ActionType, ConversationState, WorkflowState


class ToolStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


class ErrorCode(StrEnum):
    VALIDATION = "VALIDATION_FAILED"
    OWNERSHIP = "INVALID_SOURCE_OWNERSHIP"
    PROPERTY = "CROSS_PROPERTY_VIOLATION"
    CONFIRMATION = "CONFIRMATION_REQUIRED"
    INELIGIBLE = "ACTION_NOT_EXECUTABLE"
    UNSUPPORTED = "UNSUPPORTED_ACTION"
    CONFLICT = "IDEMPOTENCY_CONFLICT"
    BUSINESS = "BUSINESS_CONSTRAINT"
    DATABASE = "DATABASE_FAILURE"
    UNKNOWN = "UNKNOWN_OUTCOME"


class MaintenanceOutput(DomainModel):
    kind: Literal["maintenance_request"] = "maintenance_request"
    operation_id: UUID
    status: Literal["OPEN"] = "OPEN"


class EmergencyOutput(DomainModel):
    kind: Literal["emergency_incident"] = "emergency_incident"
    operation_id: UUID
    status: Literal["OPEN"] = "OPEN"
    revision: int = Field(ge=1)


class TourOutput(DomainModel):
    kind: Literal["leasing_tour"] = "leasing_tour"
    operation_id: UUID
    status: Literal["REQUESTED"] = "REQUESTED"
    requested_at: AwareDatetime


class HandoffOutput(DomainModel):
    kind: Literal["handoff"] = "handoff"
    operation_id: UUID
    status: Literal["REQUESTED"] = "REQUESTED"
    incident_id: UUID | None = None
    transfer_performed: Literal[False] = False


OperationOutput = Annotated[
    MaintenanceOutput | EmergencyOutput | TourOutput | HandoffOutput, Field(discriminator="kind")
]


class ToolResult(DomainModel):
    execution_id: UUID
    action_request_id: UUID
    action_type: str
    status: ToolStatus
    operation_id: UUID | None = None
    result: OperationOutput | None = None
    error_code: ErrorCode | None = None
    safe_error_message: str | None = None
    replayed: bool = False
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    duration_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def coherent_outcome(self) -> Self:
        if self.status == ToolStatus.SUCCEEDED:
            if self.result is None or self.operation_id != self.result.operation_id:
                raise ValueError("Success requires an operation identity and typed result")
            if self.completed_at is None or self.error_code is not None:
                raise ValueError("Success requires completion without an error")
        elif self.result is not None or self.operation_id is not None:
            raise ValueError("Unverified outcomes cannot carry success evidence")
        return self


class ExecutionContext(DomainModel):
    session: ConversationState
    workflow: WorkflowState
    property_context: PropertyContext


OUTPUT_KINDS = {
    ActionType.MAINTENANCE: "maintenance_request",
    ActionType.EMERGENCY: "emergency_incident",
    ActionType.EMERGENCY_UPDATE: "emergency_incident",
    ActionType.TOUR: "leasing_tour",
    ActionType.HANDOFF: "handoff",
}
