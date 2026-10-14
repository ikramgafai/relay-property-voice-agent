"""Local PostgreSQL operations adapter. Flushes only; executor/store owns commit."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.operation_models import (
    EmergencyIncident,
    Handoff,
    LeasingTour,
    MaintenanceRequest,
    ToolCall,
)
from app.tools.errors import ToolFailure
from app.tools.schemas import (
    EmergencyOutput,
    ErrorCode,
    ExecutionContext,
    HandoffOutput,
    MaintenanceOutput,
    OperationOutput,
    TourOutput,
)
from app.workflows.schemas import (
    ActionRequest,
    EmergencyPayload,
    EmergencyUpdatePayload,
    HandoffPayload,
    MaintenancePayload,
    TourPayload,
)


def identity(source: ExecutionContext, request: ActionRequest) -> dict[str, Any]:
    return {
        "property_id": source.session.property_id,
        "resident_id": source.session.resident_id,
        "unit_id": source.session.unit_id,
        "session_id": source.session.session_id,
        "workflow_id": source.workflow.workflow_id,
        "action_request_id": request.action_id,
    }


class SQLAlchemyPropertyOperationsAdapter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_maintenance(
        self, source: ExecutionContext, request: ActionRequest, payload: MaintenancePayload
    ) -> OperationOutput:
        row = MaintenanceRequest(
            **identity(source, request),
            description=payload.issue_description,
            symptom_details=payload.symptom_details,
            unit_number=payload.unit_number,
            priority="NORMAL",
            status="OPEN",
        )
        self.session.add(row)
        await self.session.flush()
        return MaintenanceOutput(operation_id=row.id)

    async def create_emergency(
        self, source: ExecutionContext, request: ActionRequest, payload: EmergencyPayload
    ) -> OperationOutput:
        row = EmergencyIncident(
            **identity(source, request),
            description=payload.issue_description,
            location_detail=payload.location_detail,
            hazard_detail=payload.hazard_detail,
            severity_detail=payload.severity_detail,
            unit_number=payload.unit_number,
            scenario_key=source.workflow.service.scenario_key,
            status="OPEN",
        )
        self.session.add(row)
        await self.session.flush()
        return EmergencyOutput(operation_id=row.id, revision=row.revision)

    async def update_emergency(
        self, source: ExecutionContext, request: ActionRequest, payload: EmergencyUpdatePayload
    ) -> OperationOutput:
        row = await self.session.scalar(
            select(EmergencyIncident)
            .where(
                EmergencyIncident.id == payload.incident_id,
                EmergencyIncident.property_id == source.session.property_id,
                EmergencyIncident.session_id == source.session.session_id,
                EmergencyIncident.workflow_id == source.workflow.workflow_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ToolFailure(ErrorCode.OWNERSHIP)
        row.hazard_detail = payload.hazard_detail
        if payload.severity_detail is not None:
            row.severity_detail = payload.severity_detail
        row.revision += 1
        await self.session.flush()
        return EmergencyOutput(operation_id=row.id, revision=row.revision)

    async def schedule_tour(
        self, source: ExecutionContext, request: ActionRequest, payload: TourPayload
    ) -> OperationOutput:
        if payload.requested_at is None:
            raise ToolFailure(ErrorCode.VALIDATION)
        row = LeasingTour(
            **identity(source, request),
            requested_at=payload.requested_at,
            contact_detail=payload.contact_detail,
            status="REQUESTED",
        )
        self.session.add(row)
        await self.session.flush()
        return TourOutput(operation_id=row.id, requested_at=row.requested_at)

    async def create_handoff(
        self, source: ExecutionContext, request: ActionRequest, payload: HandoffPayload
    ) -> OperationOutput:
        business = payload.business_context
        incident = None
        if business and business.source_workflow_id:
            incident = await self.session.scalar(
                select(EmergencyIncident).where(
                    EmergencyIncident.property_id == source.session.property_id,
                    EmergencyIncident.session_id == source.session.session_id,
                    EmergencyIncident.workflow_id == business.source_workflow_id,
                )
            )
        context = source.property_context
        executions = (
            await self.session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.session_id == source.session.session_id,
                    ToolCall.property_id == source.session.property_id,
                    ToolCall.status == "SUCCEEDED",
                )
                .order_by(ToolCall.started_at)
            )
        ).all()
        snapshot = {
            "property_name": context.property.name,
            "resident_name": f"{context.resident.first_name} {context.resident.last_name}"
            if context.resident
            else None,
            "unit_number": context.unit.number if context.unit else None,
            "business_context": business.model_dump(mode="json") if business else None,
            "incident_id": str(incident.id) if incident else None,
            "committed_operations": [
                {
                    "execution_id": str(e.id),
                    "action_type": e.action_type,
                    "operation_id": str(e.operation_id),
                }
                for e in executions
            ],
        }
        row = Handoff(
            **identity(source, request),
            reason=payload.reason,
            context=snapshot,
            incident_id=incident.id if incident else None,
            status="REQUESTED",
        )
        self.session.add(row)
        await self.session.flush()
        return HandoffOutput(operation_id=row.id, incident_id=row.incident_id)
