"""Implemented requirements and payload construction, independent of service catalogs."""

from dataclasses import dataclass
from datetime import datetime

from app.workflows.schemas import (
    ActionContext,
    EmergencyPayload,
    EmergencyUpdatePayload,
    FieldName,
    HandoffPayload,
    MaintenancePayload,
    Payload,
    TourPayload,
    WorkflowKey,
    WorkflowState,
)


@dataclass(frozen=True)
class Definition:
    key: WorkflowKey
    required: tuple[FieldName, ...]
    confirmation: bool
    # Internal capability policy, not a service taxonomy. An unfinished safety-critical run is
    # never left automatically for a routine workflow; see ADR 008.
    safety_critical: bool = False
    # Receives prior business context on transition (transitions.py), so it may follow any run.
    preserves_business_context: bool = False

    def payload(self, workflow: WorkflowState, context: ActionContext) -> Payload:
        values = {name: field.value for name, field in workflow.collected_fields.items()}
        common = context.model_dump()
        if self.key == WorkflowKey.EMERGENCY:
            if workflow.target_incident_id is not None:
                return EmergencyUpdatePayload(
                    **common,
                    incident_id=workflow.target_incident_id,
                    hazard_detail=values[FieldName.HAZARD],
                    severity_detail=values[FieldName.SEVERITY],
                )
            return EmergencyPayload(
                **common,
                issue_description=values[FieldName.ISSUE],
                symptom_details=values.get(FieldName.SYMPTOMS),
                location_detail=values[FieldName.LOCATION],
                hazard_detail=values[FieldName.HAZARD],
                severity_detail=values[FieldName.SEVERITY],
                unit_number=values[FieldName.UNIT],
            )
        if self.key == WorkflowKey.MAINTENANCE:
            return MaintenancePayload(
                **common,
                issue_description=values[FieldName.ISSUE],
                symptom_details=values[FieldName.SYMPTOMS],
                unit_number=values[FieldName.UNIT],
            )
        if self.key == WorkflowKey.LEASING:
            return TourPayload(
                **common,
                preferred_tour_time=values[FieldName.TOUR_TIME],
                contact_detail=values[FieldName.CONTACT],
                requested_at=datetime.fromisoformat(values[FieldName.TOUR_AT])
                if FieldName.TOUR_AT in values
                else None,
            )
        return HandoffPayload(
            **common,
            reason=values[FieldName.HANDOFF_REASON],
            business_context=workflow.business_context,
        )


DEFINITIONS = {
    WorkflowKey.EMERGENCY: Definition(
        WorkflowKey.EMERGENCY,
        (FieldName.ISSUE, FieldName.UNIT, FieldName.LOCATION, FieldName.HAZARD, FieldName.SEVERITY),
        False,
        safety_critical=True,
    ),
    WorkflowKey.MAINTENANCE: Definition(
        WorkflowKey.MAINTENANCE, (FieldName.ISSUE, FieldName.UNIT, FieldName.SYMPTOMS), True
    ),
    WorkflowKey.LEASING: Definition(
        WorkflowKey.LEASING, (FieldName.TOUR_TIME, FieldName.CONTACT), True
    ),
    WorkflowKey.HANDOFF: Definition(
        WorkflowKey.HANDOFF, (FieldName.HANDOFF_REASON,), True, preserves_business_context=True
    ),
}
