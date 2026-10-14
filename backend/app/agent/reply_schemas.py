"""Pending-reply interpretation contracts (ADR 008). Separate from business routing."""

from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.property.schemas import DomainModel, NonBlank
from app.routing.model import ModelRoutingRequest, ModelRoutingResponse
from app.routing.schemas import RoutingDecision
from app.workflows.schemas import FieldName

ReplyKind = Literal["WORKFLOW_ANSWER", "NEW_INTENT", "AMBIGUOUS"]


class TurnInterpretationRequest(DomainModel):
    """Model-facing projection: catalog, active workflow and backend-rendered question only."""

    routing: ModelRoutingRequest
    active_property_service_id: UUID
    active_service_name: NonBlank
    active_workflow: NonBlank
    pending_field: FieldName
    pending_question: NonBlank

    @model_validator(mode="after")
    def active_service_in_catalog(self) -> Self:
        if self.active_property_service_id not in {
            s.property_service_id for s in self.routing.services
        }:
            raise ValueError("Active service must belong to the supplied catalog")
        return self


class TurnInterpretationResponse(DomainModel):
    # Every field is required for strict JSON schema APIs. ``routing`` is the model's view of
    # the utterance on its own, using the unchanged property-scoped routing contract.
    reply_kind: ReplyKind
    routing: ModelRoutingResponse
    reason: NonBlank

    @model_validator(mode="after")
    def concise(self) -> Self:
        if len(self.reason) > 400:
            raise ValueError("Reason must be concise")
        return self


class ReplyOutcome(StrEnum):
    WORKFLOW_ANSWER = "WORKFLOW_ANSWER"
    NEW_INTENT = "NEW_INTENT"
    AMBIGUOUS = "AMBIGUOUS"
    CANCEL = "CANCEL"


class InterpretationBasis(StrEnum):
    ANSWER_PREFIX = "ANSWER_PREFIX"
    CANCEL_CONTROL = "CANCEL_CONTROL"
    CONFIGURED_EVIDENCE = "CONFIGURED_EVIDENCE"
    MODEL = "MODEL"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_CONTRACT_VIOLATION = "MODEL_CONTRACT_VIOLATION"


class InterpretationReason(StrEnum):
    EXPLICIT_ANSWER = "EXPLICIT_ANSWER"
    EXPLICIT_CANCEL = "EXPLICIT_CANCEL"
    CONFIGURED_REQUEST = "CONFIGURED_REQUEST"
    MODEL_WORKFLOW_ANSWER = "MODEL_WORKFLOW_ANSWER"
    ANSWER_PINNED_TO_ACTIVE_WORKFLOW = "ANSWER_PINNED_TO_ACTIVE_WORKFLOW"
    ANSWER_CONFLICTS_WITH_SAFETY_CRITICAL_SERVICE = "ANSWER_CONFLICTS_WITH_SAFETY_CRITICAL_SERVICE"
    ANSWER_CONFLICTS_WITH_UNSUPPORTED_SERVICE = "ANSWER_CONFLICTS_WITH_UNSUPPORTED_SERVICE"
    ANSWER_CONFLICTS_WITH_HANDOFF = "ANSWER_CONFLICTS_WITH_HANDOFF"
    MODEL_NEW_INTENT = "MODEL_NEW_INTENT"
    MODEL_REPORTED_AMBIGUOUS = "MODEL_REPORTED_AMBIGUOUS"
    PROTECTED_WORKFLOW_DOWNGRADE_BLOCKED = "PROTECTED_WORKFLOW_DOWNGRADE_BLOCKED"
    PROPOSAL_OUTSIDE_CATALOG = "PROPOSAL_OUTSIDE_CATALOG"
    ACTIVE_SERVICE_UNAVAILABLE = "ACTIVE_SERVICE_UNAVAILABLE"
    INTERPRETATION_UNAVAILABLE = "INTERPRETATION_UNAVAILABLE"


class TurnInterpretation(DomainModel):
    """Checkpointed policy outcome; replay reuses it instead of asking a model again."""

    outcome: ReplyOutcome
    basis: InterpretationBasis
    reason_code: InterpretationReason
    pending_field: FieldName
    active_property_service_id: UUID
    model_reply_kind: ReplyKind | None = None
    # Validated configured/model proposal before reply policy; diagnostic, never authority.
    proposal: RoutingDecision | None = None
    model_duration_ms: float = Field(default=0, ge=0)
