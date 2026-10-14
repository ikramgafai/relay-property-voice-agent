"""Application contracts. No ORM, model history, or operational execution state."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from app.property.schemas import DomainModel, NonBlank, PropertyContext


class WorkflowKey(StrEnum):
    EMERGENCY = "emergency_maintenance"
    MAINTENANCE = "non_emergency_maintenance"
    LEASING = "leasing"
    HANDOFF = "human_handoff"


class Status(StrEnum):
    ACTIVE = "ACTIVE"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ESCALATED = "ESCALATED"


class WorkflowDecisionType(StrEnum):
    ASK_USER = "ASK_USER"
    WAIT_FOR_CONFIRMATION = "WAIT_FOR_CONFIRMATION"
    REQUEST_ACTION = "REQUEST_ACTION"
    COMPLETE = "COMPLETE"
    CANCEL = "CANCEL"
    AWAIT_ROUTING = "AWAIT_ROUTING"
    UNSUPPORTED_WORKFLOW = "UNSUPPORTED_WORKFLOW"


class ActionType(StrEnum):
    MAINTENANCE = "CREATE_MAINTENANCE_REQUEST"
    EMERGENCY = "CREATE_EMERGENCY_INCIDENT"
    EMERGENCY_UPDATE = "UPDATE_EMERGENCY_INCIDENT"
    TOUR = "SCHEDULE_LEASING_TOUR"
    HANDOFF = "CREATE_HUMAN_HANDOFF"


class FieldName(StrEnum):
    ISSUE = "issue_description"
    SYMPTOMS = "symptom_details"
    LOCATION = "location_detail"
    HAZARD = "hazard_detail"
    SEVERITY = "severity_detail"
    UNIT = "unit_number"
    TOUR_TIME = "preferred_tour_time"
    TOUR_AT = "requested_tour_at"
    CONTACT = "contact_detail"
    HANDOFF_REASON = "handoff_reason"


class CollectedField(DomainModel):
    value: NonBlank = Field(max_length=8000)
    source_turn: int = Field(ge=1)
    source: Literal["user", "property_context"] = "user"


class ServiceReference(DomainModel):
    property_service_id: UUID
    service_key: NonBlank
    scenario_id: UUID | None = None
    scenario_key: str | None = None

    @model_validator(mode="after")
    def paired_scenario(self) -> Self:
        if (self.scenario_id is None) != (self.scenario_key is None):
            raise ValueError("Scenario identity and key must be paired")
        return self


class BusinessContext(DomainModel):
    service: ServiceReference
    source_workflow_id: UUID | None = None
    fields: dict[FieldName, CollectedField]
    field_history: dict[FieldName, list[CollectedField]] = Field(default_factory=dict)


class ActionContext(DomainModel):
    property_id: UUID
    resident_id: UUID | None
    unit_id: UUID | None


class MaintenancePayload(ActionContext):
    kind: Literal[ActionType.MAINTENANCE] = ActionType.MAINTENANCE
    issue_description: NonBlank
    symptom_details: NonBlank
    unit_number: NonBlank


class EmergencyPayload(ActionContext):
    kind: Literal[ActionType.EMERGENCY] = ActionType.EMERGENCY
    issue_description: NonBlank
    location_detail: NonBlank
    hazard_detail: NonBlank
    severity_detail: NonBlank
    unit_number: NonBlank
    symptom_details: str | None = None


class TourPayload(ActionContext):
    kind: Literal[ActionType.TOUR] = ActionType.TOUR
    preferred_tour_time: NonBlank
    contact_detail: NonBlank
    requested_at: AwareDatetime | None = None


class EmergencyUpdatePayload(ActionContext):
    kind: Literal[ActionType.EMERGENCY_UPDATE] = ActionType.EMERGENCY_UPDATE
    incident_id: UUID
    hazard_detail: NonBlank
    severity_detail: NonBlank | None = None


class HandoffPayload(ActionContext):
    kind: Literal[ActionType.HANDOFF] = ActionType.HANDOFF
    reason: NonBlank
    business_context: BusinessContext | None


Payload = Annotated[
    MaintenancePayload | EmergencyPayload | EmergencyUpdatePayload | TourPayload | HandoffPayload,
    Field(discriminator="kind"),
]


class ActionRequest(DomainModel):
    action_id: UUID
    action_type: ActionType
    payload: Payload
    requires_confirmation: bool
    source_workflow_id: UUID
    source_turn: int = Field(ge=1)

    @model_validator(mode="after")
    def matches_payload(self) -> Self:
        if self.action_type != self.payload.kind:
            raise ValueError("Action type must match its typed payload")
        return self


class RequestRecord(DomainModel):
    request: ActionRequest
    disposition: Literal["REQUESTED", "SUPERSEDED", "CANCELLED", "COMPLETED"] = "REQUESTED"


class CompletedAction(DomainModel):
    action_request_id: UUID
    execution_id: UUID
    operation_id: UUID
    action_type: ActionType
    completion_turn: int = Field(ge=1)


class WorkflowState(DomainModel):
    workflow_id: UUID
    started_turn: int = Field(ge=1)
    workflow_key: WorkflowKey
    service: ServiceReference
    status: Status = Status.ACTIVE
    current_step: str = "start"
    collected_fields: dict[FieldName, CollectedField] = Field(default_factory=dict)
    field_history: dict[FieldName, list[CollectedField]] = Field(default_factory=dict)
    missing_fields: list[FieldName] = Field(default_factory=list)
    pending_confirmation: ActionRequest | None = None
    requested_actions: list[RequestRecord] = Field(default_factory=list)
    completed_actions: list[CompletedAction] = Field(default_factory=list)
    unresolved_action_ids: list[UUID] = Field(default_factory=list)
    business_context: BusinessContext | None = None
    target_incident_id: UUID | None = None
    started_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def consistent_requests(self) -> Self:
        active = [r.request for r in self.requested_actions if r.disposition == "REQUESTED"]
        if len(active) > 1 or (active and self.pending_confirmation):
            raise ValueError("Only one outstanding action proposal per workflow")
        if (self.status == Status.WAITING_FOR_CONFIRMATION) != (
            self.pending_confirmation is not None
        ):
            raise ValueError("Confirmation status must match the proposal")
        if (self.status == Status.ACTION_REQUIRED) != bool(active):
            raise ValueError("Action status must match requested actions")
        requests = [r.request for r in self.requested_actions]
        if self.pending_confirmation:
            requests.append(self.pending_confirmation)
        if any(r.source_workflow_id != self.workflow_id for r in requests):
            raise ValueError("Request belongs to a different workflow")
        if len({r.action_id for r in requests}) != len(requests):
            raise ValueError("Duplicate action identity")
        completed_ids = {c.action_request_id for c in self.completed_actions}
        if len(completed_ids) != len(self.completed_actions):
            raise ValueError("Duplicate completed action")
        completed_requests = {
            r.request.action_id: r.request.action_type
            for r in self.requested_actions
            if r.disposition == "COMPLETED"
        }
        if completed_ids != set(completed_requests) or any(
            completed_requests[c.action_request_id] != c.action_type for c in self.completed_actions
        ):
            raise ValueError("Completion must match a completed request")
        if not set(self.unresolved_action_ids).issubset({r.action_id for r in requests}):
            raise ValueError("Unresolved receipt requires a known request")
        if self.updated_at < self.started_at:
            raise ValueError("Workflow timestamps out of order")
        return self


class Transition(DomainModel):
    from_service: ServiceReference
    to_service: ServiceReference
    from_workflow: UUID
    to_workflow: UUID
    reason: Literal[
        "ROUTED_SERVICE_CHANGED",
        "ROUTED_SCENARIO_CHANGED",
        "WORKFLOW_BINDING_CHANGED",
        "NEW_REQUEST",
    ]
    preserved_fields: list[FieldName]
    discarded_fields: list[FieldName]
    superseded_action_ids: list[UUID]
    source_turn: int
    timestamp: AwareDatetime


class Question(DomainModel):
    field: FieldName
    question_type: str


class WorkflowDecision(DomainModel):
    decision_type: WorkflowDecisionType
    workflow_state: WorkflowState | None = None
    question: Question | None = None
    action_request: ActionRequest | None = None
    transition: Transition | None = None
    reason_code: str
    message_intent: str | None = None


class ConversationState(DomainModel):
    session_id: UUID
    property_id: UUID
    resident_id: UUID | None = None
    unit_id: UUID | None = None
    turn_number: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)
    active: bool = True
    current_service: ServiceReference | None = None
    previous_services: list[ServiceReference] = Field(default_factory=list)
    active_workflow_id: UUID | None = None
    workflows: list[WorkflowState] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)
    last_turn_digest: str | None = None
    last_decision: WorkflowDecision | None = None

    @model_validator(mode="after")
    def consistent_aggregate(self) -> Self:
        ids = {w.workflow_id for w in self.workflows}
        if len(ids) != len(self.workflows):
            raise ValueError("Duplicate workflow identity")
        if self.active_workflow_id is not None and self.active_workflow_id not in ids:
            raise ValueError("Active workflow is missing")
        if self.active_workflow and self.current_service != self.active_workflow.service:
            raise ValueError("Current service must match active workflow")
        for workflow in self.workflows:
            requests = [r.request for r in workflow.requested_actions]
            if workflow.pending_confirmation:
                requests.append(workflow.pending_confirmation)
            for request in requests:
                payload = request.payload
                if (payload.property_id, payload.resident_id, payload.unit_id) != (
                    self.property_id,
                    self.resident_id,
                    self.unit_id,
                ):
                    raise ValueError("Action context must match the session")
        return self

    @classmethod
    def start(cls, session_id: UUID, context: PropertyContext) -> Self:
        return cls(
            session_id=session_id,
            property_id=context.property.id,
            resident_id=context.resident.id if context.resident else None,
            unit_id=context.unit.id if context.unit else None,
        )

    @property
    def active_workflow(self) -> WorkflowState | None:
        return next((w for w in self.workflows if w.workflow_id == self.active_workflow_id), None)


class UserTurn(DomainModel):
    turn_number: int = Field(ge=1)
    occurred_at: AwareDatetime
    utterance: NonBlank = Field(max_length=8000)
    fields: dict[FieldName, NonBlank] = Field(default_factory=dict)
    confirmation: bool | None = None
    cancel: bool = False
    new_request: bool = False
    incident_id: UUID | None = None
    requested_tour_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def unambiguous_controls(self) -> Self:
        if (self.incident_id is not None or self.requested_tour_at is not None) and (
            self.cancel or self.confirmation is not None or self.new_request
        ):
            raise ValueError("Operation details cannot accompany confirmation/cancel/new-request")
        if self.cancel and (self.confirmation is not None or self.new_request or self.fields):
            raise ValueError("Cancel must be a standalone control")
        if self.confirmation is not None and (self.fields or self.new_request):
            raise ValueError("Confirmation must refer to the unchanged proposal")
        return self


class TurnResult(DomainModel):
    state: ConversationState
    decision: WorkflowDecision


def record_field(workflow: WorkflowState, name: FieldName, field: CollectedField) -> None:
    previous = workflow.collected_fields.get(name)
    if previous == field:
        return
    if previous is not None:
        workflow.field_history.setdefault(name, []).append(previous)
    workflow.collected_fields[name] = field
