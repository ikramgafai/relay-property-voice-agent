"""Text transport and orchestration values; no client-supplied business authority."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.agent.reply_schemas import TurnInterpretation
from app.property.schemas import DomainModel, NonBlank
from app.routing.schemas import RoutingDecision
from app.tools.schemas import ToolResult, ToolStatus
from app.workflows.schemas import ActionType, FieldName, WorkflowDecision


class TurnInput(DomainModel):
    turn_id: UUID
    turn_number: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    utterance: NonBlank = Field(max_length=8000)


class AgentTurnRequest(TurnInput):
    session_id: UUID


class Intent(StrEnum):
    ASK = "ASK_FOR_INFORMATION"
    CONFIRM = "ASK_FOR_CONFIRMATION"
    CLARIFY = "ROUTING_CLARIFICATION"
    UNABLE = "UNABLE_TO_ROUTE"
    SUCCESS = "ACTION_SUCCEEDED"
    FAILED = "ACTION_FAILED"
    UNKNOWN = "ACTION_OUTCOME_UNKNOWN"
    PENDING = "ACTION_PENDING"
    CANCELLED = "REQUEST_CANCELLED"
    INFORMATION = "INFORMATIONAL_RESPONSE"
    UNSUPPORTED = "UNSUPPORTED_WORKFLOW"
    REPLY_CLARIFY = "WORKFLOW_REPLY_CLARIFICATION"


class OperationFact(DomainModel):
    action_request_id: UUID
    execution_id: UUID
    action_type: ActionType
    operation_id: UUID
    status: Literal[ToolStatus.SUCCEEDED] = ToolStatus.SUCCEEDED


class ResponsePlan(DomainModel):
    intent: Intent
    property_name: NonBlank
    unit_number: str | None = None
    service_name: str | None = None
    question: str | None = None
    missing_field: FieldName | None = None
    operation_status: ToolStatus | None = None
    operation: OperationFact | None = None
    summary: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def factual_status(self) -> Self:
        if self.intent == Intent.SUCCESS:
            if self.operation is None or self.operation_status != ToolStatus.SUCCEEDED:
                raise ValueError("Success requires a verified operation fact")
        elif self.operation is not None or self.operation_status == ToolStatus.SUCCEEDED:
            raise ValueError("Only successful plans can contain operation facts")
        return self


class AgentTurnResult(DomainModel):
    session_id: UUID
    turn_id: UUID
    turn_number: int
    routing_decision: RoutingDecision
    interpretation: TurnInterpretation | None = None
    workflow_decision: WorkflowDecision | None
    tool_results: list[ToolResult] = Field(default_factory=list)
    response_plan: ResponsePlan
    response_text: NonBlank
    revision: int
    status: Literal["COMPLETED", "UNRESOLVED"] = "COMPLETED"
    replayed: bool = False
    timings_ms: dict[str, float] = Field(default_factory=dict)


class TurnCheckpoint(DomainModel):
    request: AgentTurnRequest
    occurred_at: AwareDatetime
    route: RoutingDecision | None = None
    # Saved with ``route``; replay never re-interprets a pending-question reply.
    interpretation: TurnInterpretation | None = None
    decision: WorkflowDecision | None = None
    workflow_saved: bool = False
    execution_started: bool = False
    tool_result: ToolResult | None = None
    result: AgentTurnResult | None = None


class DemoSession(DomainModel):
    session_id: UUID
    revision: int
    property_name: str
    resident_name: str
    unit_number: str | None
