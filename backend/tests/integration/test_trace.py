"""Read-only demo session trace: authoritative durable state, allowlisted fields, no writes."""

import json
from uuid import uuid4

import httpx
import pytest
from app.agent.schemas import Intent
from app.db.agent_models import AgentTurn
from app.db.operation_models import EmergencyIncident, ToolCall
from app.db.trace import SQLAlchemyTraceReader, _turn
from app.main import create_app
from sqlalchemy import select

from tests.integration.test_agent import TextHarness
from tests.integration.test_agent import text_harness as text_harness  # noqa: F401 -- fixture
from tests.integration.test_tools import harness as harness  # noqa: F401 -- shared PG fixture

pytestmark = pytest.mark.integration


async def flagship(t: TextHarness) -> None:
    await t.maintenance_proposal()
    created = await t.agent.process(await t.emergency_ready())
    assert created.response_plan.intent == Intent.SUCCESS
    t.route("emergency_maintenance", "electrical_hazard")
    updated = await t.say("Wait, the water is getting close to an electrical outlet.")
    assert updated.response_plan.intent == Intent.SUCCESS


async def test_trace_projects_turns_tools_workflows_and_incident(text_harness: TextHarness) -> None:
    t = text_harness
    await flagship(t)
    trace = await SQLAlchemyTraceReader(t.source.database).read(t.source.session_id)
    assert trace is not None
    assert trace.property_name == t.source.context.property.name
    assert [turn.turn_number for turn in trace.turns] == list(range(1, len(trace.turns) + 1))
    assert all(turn.finished and turn.status == "COMPLETED" for turn in trace.turns)
    create, update = (turn for turn in trace.turns if turn.tool_results)
    # The create turn answered the pending question; the update turn routed to the hazard.
    assert create.interpretation and create.workflow_decision == "REQUEST_ACTION"
    assert create.route and create.route.service_key == "emergency_maintenance"
    assert create.action and create.action.action_type == "CREATE_EMERGENCY_INCIDENT"
    [created] = create.tool_results
    assert created.status == "SUCCEEDED" and created.result_revision == 1 and not created.replayed
    assert update.route and update.route.scenario_key == "electrical_hazard"
    [updated] = update.tool_results
    assert updated.action_type == "UPDATE_EMERGENCY_INCIDENT" and updated.result_revision == 2
    assert updated.operation_id == created.operation_id
    assert "routing" in create.timings_ms and "tools" in create.timings_ms
    # Receipts carry attempts and replays.
    assert [(c.action_type, c.status, c.attempts, c.replays) for c in trace.tool_calls] == [
        ("CREATE_EMERGENCY_INCIDENT", "SUCCEEDED", 1, 0),
        ("UPDATE_EMERGENCY_INCIDENT", "SUCCEEDED", 1, 0),
    ]
    assert [c.result_revision for c in trace.tool_calls] == [1, 2]
    [incident] = trace.incidents
    assert incident.incident_id == created.operation_id and incident.revision == 2
    assert incident.status == "OPEN" and "electrical outlet" in incident.hazard_detail
    emergency = next(w for w in trace.workflows if w.workflow_key == "emergency_maintenance")
    assert emergency.current_step and emergency.service_key == "emergency_maintenance"
    assert trace.maintenance_requests == [] and trace.handoffs == []


async def test_trace_excludes_payloads_rationale_and_utterances(text_harness: TextHarness) -> None:
    t = text_harness
    await flagship(t)
    trace = await SQLAlchemyTraceReader(t.source.database).read(t.source.session_id)
    assert trace is not None
    dumped = trace.model_dump_json()
    for forbidden in (
        "request_payload",
        "idempotency_key",
        "request_hash",
        "evidence",
        'reason"',
        "catalog_fingerprint",
        "utterance",
        "response_text",
        "My AC isn't cooling.",
        "It blows warm air.",
    ):
        assert forbidden not in dumped, forbidden


async def test_trace_is_read_only(text_harness: TextHarness) -> None:
    t = text_harness
    await flagship(t)
    reader = SQLAlchemyTraceReader(t.source.database)

    async def snapshot() -> tuple[object, ...]:
        async with t.source.database.session() as session:
            incident = (
                await session.scalars(
                    select(EmergencyIncident).where(
                        EmergencyIncident.session_id == t.source.session_id
                    )
                )
            ).one()
            calls = (
                await session.scalars(
                    select(ToolCall.replay_count, ToolCall.attempt_count).where(
                        ToolCall.session_id == t.source.session_id
                    )
                )
            ).all()
            return incident.revision, incident.updated_at, tuple(calls)

    before = await snapshot()
    first, second = await reader.read(t.source.session_id), await reader.read(t.source.session_id)
    assert first == second and await snapshot() == before
    assert await reader.read(uuid4()) is None


async def test_unfinished_turn_shows_a_requested_action_not_a_success(
    text_harness: TextHarness,
) -> None:
    t = text_harness
    await t.maintenance_proposal()
    await t.agent.process(await t.emergency_ready())
    async with t.source.database.session() as session:
        row = (
            await session.scalars(
                select(AgentTurn)
                .where(AgentTurn.session_id == t.source.session_id)
                .order_by(AgentTurn.turn_number.desc())
                .limit(1)
            )
        ).one()
        checkpoint = dict(row.checkpoint)
    # A turn mid-execution: workflow decision saved and dispatch recorded, no ToolResult yet.
    result = checkpoint.pop("result")
    assert isinstance(result, dict)
    checkpoint["decision"] = result["workflow_decision"]
    checkpoint["workflow_saved"] = True
    checkpoint["execution_started"] = True
    checkpoint["tool_result"] = None
    pending = AgentTurn(
        id=row.id,
        session_id=row.session_id,
        property_id=row.property_id,
        turn_number=row.turn_number,
        finished=False,
        checkpoint=checkpoint,
    )
    trace = _turn(pending)
    assert not trace.finished and trace.status is None and trace.revision is None
    assert trace.action and trace.action.action_type == "CREATE_EMERGENCY_INCIDENT"
    assert trace.workflow_decision == "REQUEST_ACTION" and trace.tool_results == []


async def test_trace_http_endpoint(text_harness: TextHarness) -> None:
    t = text_harness
    await flagship(t)
    app = create_app()
    app.state.database = t.source.database
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/demo/sessions/{t.source.session_id}/trace")
        assert response.status_code == 200
        body = response.json()
        assert body["incidents"][0]["revision"] == 2 and len(body["tool_calls"]) == 2
        assert set(json.loads(response.text)) == {
            "session_id",
            "revision",
            "property_name",
            "unit_number",
            "turns",
            "tool_calls",
            "workflows",
            "incidents",
            "maintenance_requests",
            "handoffs",
        }
        missing = await client.get(f"/api/demo/sessions/{uuid4()}/trace")
        assert missing.status_code == 404
        assert (
            await client.post(f"/api/demo/sessions/{t.source.session_id}/trace")
        ).status_code == 405
        app.state.settings.app_env = "production"
        blocked = await client.get(f"/api/demo/sessions/{t.source.session_id}/trace")
        assert blocked.status_code == 403
