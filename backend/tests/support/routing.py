import asyncio
from uuid import UUID

from app.agent.reply_schemas import (
    ReplyKind,
    TurnInterpretationRequest,
    TurnInterpretationResponse,
)
from app.routing.model import ModelRoutingRequest, ModelRoutingResponse


def response(
    action: str = "ABSTAIN",
    service_id: UUID | None = None,
    scenario_id: UUID | None = None,
    candidates: list[UUID] | None = None,
) -> ModelRoutingResponse:
    return ModelRoutingResponse.model_validate(
        {
            "action": action,
            "property_service_id": service_id,
            "scenario_id": scenario_id,
            "clarification_kind": ("CHOOSE_SERVICE" if candidates else "DESCRIBE_REQUEST")
            if action == "CLARIFY"
            else None,
            "candidate_service_ids": candidates or [],
            "reason": "Scripted external evidence.",
        }
    )


def turn(
    kind: ReplyKind, routing: ModelRoutingResponse | None = None
) -> TurnInterpretationResponse:
    return TurnInterpretationResponse(
        reply_kind=kind, routing=routing or response(), reason="Scripted external evidence."
    )


class StubProvider:
    def __init__(self, result: ModelRoutingResponse | Exception, *, delay: float = 0) -> None:
        self.result = result
        self.delay = delay
        self.requests: list[ModelRoutingRequest] = []
        self.turn_requests: list[TurnInterpretationRequest] = []
        # None: report NEW_INTENT with ``result`` as the routing view (pre-ADR 008 behavior).
        self.turn_result: TurnInterpretationResponse | Exception | None = None
        self.cancelled = False

    async def interpret_turn(
        self, request: TurnInterpretationRequest
    ) -> TurnInterpretationResponse:
        self.turn_requests.append(request)
        result = self.turn_result
        if result is None:
            if isinstance(self.result, Exception):
                raise self.result
            return turn("NEW_INTENT", self.result)
        if isinstance(result, Exception):
            raise result
        return result

    async def interpret(self, request: ModelRoutingRequest) -> ModelRoutingResponse:
        self.requests.append(request)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        except asyncio.CancelledError:
            self.cancelled = True
            raise
