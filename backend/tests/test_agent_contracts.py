from uuid import uuid4

import pytest
from app.agent.interpretation import Confirmation, DeterministicConfirmation
from app.agent.renderer import ConstrainedModelRenderer, DeterministicRenderer
from app.agent.response_plan import build_plan
from app.agent.schemas import AgentTurnRequest, Intent, ResponsePlan
from app.tools.schemas import ToolStatus
from app.workflows.engine import WorkflowEngine
from app.workflows.schemas import ConversationState, FieldName
from pydantic import ValidationError

from tests.support.workflows import atlas_context, route_for, turn


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Yes, please!", Confirmation.CONFIRM),
        ("go ahead", Confirmation.CONFIRM),
        ("No, don't create it.", Confirmation.REJECT),
        ("no thanks", Confirmation.REJECT),
        ("maybe", Confirmation.UNCLEAR),
        ("yes but change the unit", Confirmation.UNCLEAR),
        ("yes and book a tour", Confirmation.UNCLEAR),
    ],
)
def test_confirmation_is_conservative(text: str, expected: Confirmation) -> None:
    assert DeterministicConfirmation().interpret(text) == expected


@pytest.mark.parametrize(
    "field",
    [
        "property_id",
        "resident_id",
        "unit_id",
        "workflow_id",
        "tool_result",
        "action_request",
        "confirmation",
        "property_context",
    ],
)
def test_transport_rejects_authority_injection(field: str) -> None:
    payload = {
        "session_id": uuid4(),
        "turn_id": uuid4(),
        "turn_number": 1,
        "expected_revision": 1,
        "utterance": "Hello",
        field: str(uuid4()),
    }
    with pytest.raises(ValidationError):
        AgentTurnRequest.model_validate(payload)


async def test_action_request_and_workflow_decision_are_not_success() -> None:
    context = atlas_context()
    route = await route_for(context, "emergency_maintenance")
    outcome = WorkflowEngine().advance(
        context,
        route,
        ConversationState.start(uuid4(), context),
        turn(
            1,
            fields={
                FieldName.LOCATION: "AC",
                FieldName.HAZARD: "Leak",
                FieldName.SEVERITY: "Active",
            },
        ),
    )
    assert outcome.decision.action_request
    plan = build_plan(context, outcome.state, route, outcome.decision, None)
    assert plan.intent == Intent.PENDING and plan.operation is None
    assert "was created" not in await DeterministicRenderer().render(plan)
    with pytest.raises(ValidationError):
        ResponsePlan(
            intent=Intent.SUCCESS, property_name="Atlas", operation_status=ToolStatus.SUCCEEDED
        )


class Phrasing:
    def __init__(self, choice: int | Exception) -> None:
        self.choice = choice
        self.calls = 0

    async def choose(self, plan: ResponsePlan, alternatives: list[str]) -> int:
        self.calls += 1
        if isinstance(self.choice, Exception):
            raise self.choice
        return self.choice


@pytest.mark.parametrize("choice", [0, 1, 99, RuntimeError("provider secret")])
async def test_optional_phrasing_is_bounded_and_falls_back(choice: int | Exception) -> None:
    plan = ResponsePlan(intent=Intent.ASK, property_name="Atlas", question="What symptoms?")
    provider = Phrasing(choice)
    rendered = await ConstrainedModelRenderer(provider).render(plan)
    assert rendered in {"What symptoms?", "Let's clarify this. What symptoms?"}
    assert provider.calls == 1 and "secret" not in rendered


@pytest.mark.parametrize(
    "status,intent",
    [
        (ToolStatus.FAILED, Intent.FAILED),
        (ToolStatus.UNKNOWN_OUTCOME, Intent.UNKNOWN),
        (ToolStatus.PENDING, Intent.PENDING),
    ],
)
async def test_model_cannot_rewrite_critical_status(status: ToolStatus, intent: Intent) -> None:
    provider = Phrasing(RuntimeError("must never be called"))
    plan = ResponsePlan(intent=intent, property_name="Atlas", operation_status=status)
    text = await ConstrainedModelRenderer(provider).render(plan)
    assert provider.calls == 0 and text == await DeterministicRenderer().render(plan)
    assert "was created" not in text


def test_success_plan_rejects_missing_or_extraneous_reference() -> None:
    with pytest.raises(ValidationError):
        ResponsePlan.model_validate(
            {"intent": "ACTION_FAILED", "property_name": "Atlas", "operation_id": str(uuid4())}
        )


async def test_approved_model_response_uses_mocked_provider_transport() -> None:
    import httpx
    from app.core.config import Settings
    from app.providers.llm.phrasing import CompatiblePhrasingProvider

    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"index":1}'}}]},
        )

    settings = Settings(
        routing_model_base_url="https://example.test/v1",
        routing_model_name="test",
        response_model_enabled=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        renderer = ConstrainedModelRenderer(CompatiblePhrasingProvider(settings, client))
        result = await renderer.render(
            ResponsePlan(intent=Intent.ASK, property_name="Atlas", question="Where?")
        )
    assert result == "Let's clarify this. Where?"


async def test_success_reference_and_clause_are_deterministic() -> None:
    from app.agent.schemas import OperationFact
    from app.workflows.schemas import ActionType

    operation_id = uuid4()
    plan = ResponsePlan(
        intent=Intent.SUCCESS,
        property_name="Atlas",
        operation_status=ToolStatus.SUCCEEDED,
        operation=OperationFact(
            action_request_id=uuid4(),
            execution_id=uuid4(),
            operation_id=operation_id,
            action_type=ActionType.EMERGENCY,
        ),
    )
    provider = Phrasing(RuntimeError("must not run"))
    text = await ConstrainedModelRenderer(provider).render(plan)
    assert text == f"Your emergency incident was created. Reference: {operation_id}."
    assert provider.calls == 0
