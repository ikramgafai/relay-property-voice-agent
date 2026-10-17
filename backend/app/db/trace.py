"""Read-only session trace for the internal voice demo.

Projects already-authoritative durable state (turn checkpoints, tool receipts, workflow runs and
operation records) into allowlisted structured fields. Nothing here writes, routes, executes
tools or exposes prompts, model rationale, request payloads, credentials or provider bodies.
"""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from pydantic import Field
from sqlalchemy import select

from app.agent.schemas import TurnCheckpoint
from app.db.agent_models import AgentTurn
from app.db.models import ConversationSession, Property, Unit, WorkflowRun
from app.db.operation_models import EmergencyIncident, Handoff, MaintenanceRequest, ToolCall
from app.db.session import Database
from app.property.schemas import DomainModel
from app.routing.schemas import RoutingDecision

TRACE_TURNS = 50  # most recent turns; the demo never needs the full history


class RouteTrace(DomainModel):
    decision: str
    service_key: str | None = None
    service_name: str | None = None
    scenario_key: str | None = None
    method: str | None = None
    reason_code: str


class ActionTrace(DomainModel):
    action_id: UUID
    action_type: str
    requires_confirmation: bool


class ToolResultTrace(DomainModel):
    execution_id: UUID
    action_request_id: UUID
    action_type: str
    status: str
    operation_id: UUID | None = None
    replayed: bool
    duration_ms: float
    error_code: str | None = None
    result_revision: int | None = None
    result_status: str | None = None


class TurnTrace(DomainModel):
    turn_id: UUID
    turn_number: int
    finished: bool
    status: str | None = None
    revision: int | None = None
    replayed: bool | None = None
    route: RouteTrace | None = None
    interpretation: str | None = None
    workflow_decision: str | None = None
    action: ActionTrace | None = None
    execution_started: bool = False
    tool_results: list[ToolResultTrace] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)


class ToolCallTrace(DomainModel):
    execution_id: UUID
    action_request_id: UUID
    action_type: str
    status: str
    attempts: int
    replays: int
    operation_id: UUID | None = None
    duration_ms: float
    error_code: str | None = None
    result_revision: int | None = None
    started_at: datetime
    completed_at: datetime | None = None


class WorkflowTrace(DomainModel):
    workflow_id: UUID
    workflow_key: str
    status: str
    current_step: str | None = None
    service_key: str | None = None
    scenario_key: str | None = None
    updated_at: datetime


class IncidentTrace(DomainModel):
    incident_id: UUID
    status: str
    revision: int
    scenario_key: str | None = None
    location_detail: str
    hazard_detail: str
    severity_detail: str
    created_at: datetime
    updated_at: datetime


class RecordTrace(DomainModel):
    record_id: UUID
    status: str
    created_at: datetime
    incident_id: UUID | None = None


class SessionTrace(DomainModel):
    session_id: UUID
    revision: int
    property_name: str
    unit_number: str | None = None
    turns: list[TurnTrace]
    tool_calls: list[ToolCallTrace]
    workflows: list[WorkflowTrace]
    incidents: list[IncidentTrace]
    maintenance_requests: list[RecordTrace]
    handoffs: list[RecordTrace]


def _route(decision: RoutingDecision | None) -> RouteTrace | None:
    if decision is None:
        return None
    return RouteTrace(
        decision=decision.decision.value,
        service_key=decision.service_key,
        service_name=decision.service_name,
        scenario_key=decision.scenario_key,
        method=decision.method.value if decision.method else None,
        reason_code=decision.reason_code.value,
    )


def _turn(row: AgentTurn) -> TurnTrace:
    checkpoint = TurnCheckpoint.model_validate(row.checkpoint)
    result = checkpoint.result
    decision = result.workflow_decision if result else checkpoint.decision
    interpretation = result.interpretation if result else checkpoint.interpretation
    action = decision.action_request if decision else None
    tools = result.tool_results if result else []
    if not result and checkpoint.tool_result:
        tools = [checkpoint.tool_result]
    return TurnTrace(
        turn_id=checkpoint.request.turn_id,
        turn_number=row.turn_number,
        finished=row.finished,
        status=result.status if result else None,
        revision=result.revision if result else None,
        replayed=result.replayed if result else None,
        route=_route(result.routing_decision if result else checkpoint.route),
        interpretation=interpretation.outcome.value if interpretation else None,
        workflow_decision=decision.decision_type.value if decision else None,
        action=ActionTrace(
            action_id=action.action_id,
            action_type=action.action_type.value,
            requires_confirmation=action.requires_confirmation,
        )
        if action
        else None,
        execution_started=checkpoint.execution_started,
        tool_results=[
            ToolResultTrace(
                execution_id=tool.execution_id,
                action_request_id=tool.action_request_id,
                action_type=tool.action_type,
                status=tool.status.value,
                operation_id=tool.operation_id,
                replayed=tool.replayed,
                duration_ms=tool.duration_ms,
                error_code=tool.error_code.value if tool.error_code else None,
                result_revision=getattr(tool.result, "revision", None),
                result_status=getattr(tool.result, "status", None),
            )
            for tool in tools
        ],
        timings_ms=result.timings_ms if result else {},
    )


def _revision(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    value = result.get("revision") if isinstance(result, dict) else None
    return value if isinstance(value, int) else None


def _text(state: Mapping[str, object], *path: str) -> str | None:
    node: object = state
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) else None


class SQLAlchemyTraceReader:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def read(self, session_id: UUID) -> SessionTrace | None:
        async with self.database.session() as session:
            conversation = await session.get(ConversationSession, session_id)
            if conversation is None:
                return None
            prop = await session.get(Property, conversation.property_id)
            unit = await session.get(Unit, conversation.unit_id) if conversation.unit_id else None
            turns = (
                await session.scalars(
                    select(AgentTurn)
                    .where(AgentTurn.session_id == session_id)
                    .order_by(AgentTurn.turn_number.desc())
                    .limit(TRACE_TURNS)
                )
            ).all()
            calls = (
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.session_id == session_id)
                    .order_by(ToolCall.started_at)
                )
            ).all()
            runs = (
                await session.scalars(
                    select(WorkflowRun)
                    .where(WorkflowRun.session_id == session_id)
                    .order_by(WorkflowRun.created_at)
                )
            ).all()
            incidents = (
                await session.scalars(
                    select(EmergencyIncident)
                    .where(EmergencyIncident.session_id == session_id)
                    .order_by(EmergencyIncident.created_at)
                )
            ).all()
            requests = (
                await session.scalars(
                    select(MaintenanceRequest)
                    .where(MaintenanceRequest.session_id == session_id)
                    .order_by(MaintenanceRequest.created_at)
                )
            ).all()
            handoffs = (
                await session.scalars(
                    select(Handoff)
                    .where(Handoff.session_id == session_id)
                    .order_by(Handoff.created_at)
                )
            ).all()
            return SessionTrace(
                session_id=session_id,
                revision=conversation.revision,
                property_name=prop.name if prop else "",
                unit_number=unit.number if unit else None,
                turns=[_turn(row) for row in reversed(turns)],
                tool_calls=[
                    ToolCallTrace(
                        execution_id=call.id,
                        action_request_id=call.action_request_id,
                        action_type=call.action_type,
                        status=call.status,
                        attempts=call.attempt_count,
                        replays=call.replay_count,
                        operation_id=call.operation_id,
                        duration_ms=call.duration_ms,
                        error_code=call.error_code,
                        result_revision=_revision(call.result_payload),
                        started_at=call.started_at,
                        completed_at=call.completed_at,
                    )
                    for call in calls
                ],
                workflows=[
                    WorkflowTrace(
                        workflow_id=run.id,
                        workflow_key=run.workflow_key,
                        status=run.status,
                        current_step=_text(run.state, "current_step"),
                        service_key=_text(run.state, "service", "service_key"),
                        scenario_key=_text(run.state, "service", "scenario_key"),
                        updated_at=run.updated_at,
                    )
                    for run in runs
                ],
                incidents=[
                    IncidentTrace(
                        incident_id=incident.id,
                        status=incident.status,
                        revision=incident.revision,
                        scenario_key=incident.scenario_key,
                        location_detail=incident.location_detail,
                        hazard_detail=incident.hazard_detail,
                        severity_detail=incident.severity_detail,
                        created_at=incident.created_at,
                        updated_at=incident.updated_at,
                    )
                    for incident in incidents
                ],
                maintenance_requests=[
                    RecordTrace(record_id=r.id, status=r.status, created_at=r.created_at)
                    for r in requests
                ],
                handoffs=[
                    RecordTrace(
                        record_id=h.id,
                        status=h.status,
                        created_at=h.created_at,
                        incident_id=h.incident_id,
                    )
                    for h in handoffs
                ],
            )
