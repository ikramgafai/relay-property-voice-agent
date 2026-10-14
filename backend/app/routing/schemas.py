"""Routing values: dynamic catalog identifiers, never an intent taxonomy."""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from app.property.schemas import DomainModel, NonBlank, PropertyContext


class DecisionType(StrEnum):
    ROUTED = "ROUTED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    ABSTAINED = "ABSTAINED"


class RoutingMethod(StrEnum):
    TRUSTED_CONTINUATION = "TRUSTED_CONTINUATION"
    CONFIGURED_EVIDENCE = "CONFIGURED_EVIDENCE"
    MODEL_ASSISTED = "MODEL_ASSISTED"


class ReasonCode(StrEnum):
    CURRENT_WORKFLOW_REPLY = "CURRENT_WORKFLOW_REPLY"
    EXPLICIT_CONFIGURED_MATCH = "EXPLICIT_CONFIGURED_MATCH"
    AMBIGUOUS_SERVICE = "AMBIGUOUS_SERVICE"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    NO_AVAILABLE_SERVICES = "NO_AVAILABLE_SERVICES"
    MODEL_SELECTED_SERVICE = "MODEL_SELECTED_SERVICE"
    MODEL_CONTRACT_VIOLATION = "MODEL_CONTRACT_VIOLATION"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


class EvidenceType(StrEnum):
    SERVICE_NAME = "SERVICE_NAME"
    SERVICE_DESCRIPTION = "SERVICE_DESCRIPTION"
    SCENARIO_NAME = "SCENARIO_NAME"
    SCENARIO_DESCRIPTION = "SCENARIO_DESCRIPTION"
    SCENARIO_EXAMPLE = "SCENARIO_EXAMPLE"
    MODEL_INTERPRETATION = "MODEL_INTERPRETATION"


class Evidence(DomainModel):
    kind: EvidenceType
    property_service_id: UUID
    scenario_id: UUID | None = None
    detail: NonBlank


class RoutingRequest(DomainModel):
    utterance: NonBlank = Field(max_length=8000)
    property_context: PropertyContext
    # Internal application-only hint, never exposed in the text API request.
    continuation_service_id: UUID | None = None
    continuation_scenario_id: UUID | None = None

    @model_validator(mode="after")
    def valid_catalog(self) -> Self:
        context = self.property_context
        if not context.property.active:
            raise ValueError("Routing requires an active property")
        services = context.enabled_services
        if len({s.property_service_id for s in services}) != len(services):
            raise ValueError("Duplicate property-service identity")
        if len({s.key for s in services}) != len(services):
            raise ValueError("Duplicate service key")
        scenarios = [sc for s in services for sc in s.scenarios]
        if len({sc.id for sc in scenarios}) != len(scenarios):
            raise ValueError("Duplicate scenario identity")
        for service in services:
            if not service.display_name.strip():
                raise ValueError("Service requires an effective name")
            if len({sc.key for sc in service.scenarios}) != len(service.scenarios):
                raise ValueError("Duplicate scoped scenario key")
        return self


class RoutingDecision(DomainModel):
    decision: DecisionType
    property_id: UUID
    property_service_id: UUID | None = None
    service_key: str | None = None
    service_name: str | None = None
    scenario_id: UUID | None = None
    scenario_key: str | None = None
    method: RoutingMethod | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    reason_code: ReasonCode
    reason: NonBlank
    clarification_question: str | None = None
    clarification_service_ids: list[UUID] = Field(default_factory=list)
    catalog_fingerprint: str
    duration_ms: float = Field(ge=0)
    configured_duration_ms: float = Field(default=0, ge=0)
    model_duration_ms: float = Field(default=0, ge=0)
    validation_duration_ms: float = Field(default=0, ge=0)
    model_fallback_used: bool = False
    contract_violation: str | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        selected = (self.property_service_id, self.service_key, self.service_name)
        if self.decision == DecisionType.ROUTED:
            if any(value is None for value in selected) or self.method is None:
                raise ValueError("Routed decisions require a service and method")
        elif any(value is not None for value in (*selected, self.scenario_id, self.scenario_key)):
            raise ValueError("Non-routes cannot select a service or scenario")
        if (self.scenario_id is None) != (self.scenario_key is None):
            raise ValueError("Scenario ID and key must be paired")
        if self.decision == DecisionType.CLARIFICATION_REQUIRED:
            if not self.clarification_question:
                raise ValueError("Clarification requires a question")
        elif self.clarification_question is not None or self.clarification_service_ids:
            raise ValueError("Only clarification may include a question or choices")
        return self
