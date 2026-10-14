"""Critical clauses always come from backend templates, even with model assistance."""

import asyncio

import structlog

from app.agent.protocol import PhrasingProvider
from app.agent.schemas import Intent, ResponsePlan
from app.workflows.schemas import ActionType


class DeterministicRenderer:
    async def render(self, plan: ResponsePlan) -> str:
        plan = ResponsePlan.model_validate(plan.model_dump())
        if plan.intent in {Intent.ASK, Intent.CONFIRM, Intent.CLARIFY, Intent.REPLY_CLARIFY}:
            return plan.question or "Could you clarify what you need?"
        if plan.intent == Intent.SUCCESS:
            assert plan.operation
            clauses = {
                ActionType.MAINTENANCE: "Your maintenance request was created.",
                ActionType.EMERGENCY: "Your emergency incident was created.",
                ActionType.EMERGENCY_UPDATE: (
                    "Your emergency incident was updated with the reported information."
                ),
                ActionType.TOUR: (
                    "Your tour request was recorded. A calendar booking is not yet confirmed."
                ),
                ActionType.HANDOFF: (
                    "Your human handoff request was created. No live transfer has occurred."
                ),
            }
            return (
                f"{clauses[plan.operation.action_type]} Reference: {plan.operation.operation_id}."
            )
        return {
            Intent.FAILED: "I couldn't complete that request.",
            Intent.UNKNOWN: (
                "The request may have been submitted, but I can't confirm its status yet. "
                "I won't submit it again automatically."
            ),
            Intent.PENDING: "The request is pending. I can't confirm completion yet.",
            Intent.CANCELLED: (
                "I won't proceed with that proposal. "
                "Previously completed requests remain unchanged."
            ),
            Intent.UNABLE: "I couldn't safely select a service. Please describe the help you need.",
            Intent.UNSUPPORTED: (
                f"{plan.service_name or 'This service'} is available in the property catalog, "
                "but I cannot carry out a dedicated workflow for it yet."
            ),
            Intent.INFORMATION: (
                "This workflow is complete. I have no additional verified information to provide."
            ),
        }[plan.intent]


class ConstrainedModelRenderer:
    def __init__(self, provider: PhrasingProvider, timeout_seconds: float = 5) -> None:
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.fallback = DeterministicRenderer()

    async def render(self, plan: ResponsePlan) -> str:
        baseline = await self.fallback.render(plan)
        # No provider can rewrite success, failure, pending or uncertainty clauses.
        if plan.operation_status is not None:
            return baseline
        alternatives = [baseline, f"Let's clarify this. {baseline}"]
        try:
            async with asyncio.timeout(self.timeout_seconds):
                choice = await self.provider.choose(plan, alternatives)
            if type(choice) is not int or not 0 <= choice < len(alternatives):
                raise ValueError("Invalid approved phrasing choice")
            return alternatives[choice]
        except Exception:
            structlog.get_logger().warning(
                "RESPONSE_RENDERER_FALLBACK", reason_code="INVALID_OR_UNAVAILABLE_PHRASING"
            )
            return baseline
