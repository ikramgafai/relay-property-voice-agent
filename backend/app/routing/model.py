"""Provider-neutral structured interpretation contract and minimal catalog projection."""

from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator

from app.property.schemas import DomainModel, NonBlank, ScenarioContext
from app.routing.schemas import RoutingRequest


class ModelService(DomainModel):
    property_service_id: UUID
    key: str
    name: str
    description: str
    scenarios: list[ScenarioContext]


class ModelRoutingRequest(DomainModel):
    utterance: str
    property_id: UUID
    property_name: str
    services: list[ModelService]

    @classmethod
    def from_request(cls, request: RoutingRequest) -> "ModelRoutingRequest":
        context = request.property_context
        return cls(
            utterance=request.utterance,
            property_id=context.property.id,
            property_name=context.property.name,
            services=[
                ModelService(
                    property_service_id=s.property_service_id,
                    key=s.key,
                    name=s.display_name,
                    description=s.description,
                    scenarios=s.scenarios,
                )
                for s in context.enabled_services
            ],
        ).model_copy(deep=True)


class ModelRoutingResponse(DomainModel):
    # Every field is required (nullable where appropriate) for strict JSON schema APIs.
    action: Literal["ROUTE", "CLARIFY", "ABSTAIN"]
    property_service_id: UUID | None
    scenario_id: UUID | None
    clarification_kind: Literal["CHOOSE_SERVICE", "DESCRIBE_REQUEST"] | None
    candidate_service_ids: list[UUID]
    reason: NonBlank

    @model_validator(mode="after")
    def consistent_action(self) -> Self:
        if self.action == "ROUTE":
            if self.property_service_id is None:
                raise ValueError("ROUTE requires a property-service ID")
        elif self.property_service_id is not None or self.scenario_id is not None:
            raise ValueError("Only ROUTE may select IDs")
        if self.action == "CLARIFY":
            if self.clarification_kind is None:
                raise ValueError("CLARIFY requires a question kind")
            if self.clarification_kind == "CHOOSE_SERVICE":
                if not 2 <= len(self.candidate_service_ids) <= 4:
                    raise ValueError("Choose between two to four services")
            elif self.candidate_service_ids:
                raise ValueError("Describe-request question has no service choices")
            if len(set(self.candidate_service_ids)) != len(self.candidate_service_ids):
                raise ValueError("Duplicate clarification choices")
        elif self.clarification_kind is not None or self.candidate_service_ids:
            raise ValueError("Only CLARIFY may provide clarification choices")
        if len(self.reason) > 400:
            raise ValueError("Reason must be concise")
        return self
