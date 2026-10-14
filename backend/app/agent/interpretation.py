"""Conservative text controls and verbatim answers to an existing question."""

import re
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from app.agent.reply import answer_text
from app.agent.reply_schemas import ReplyOutcome, TurnInterpretation
from app.agent.schemas import AgentTurnRequest
from app.routing.schemas import DecisionType, RoutingDecision
from app.workflows.schemas import (
    ActionType,
    ConversationState,
    FieldName,
    Status,
    UserTurn,
    WorkflowKey,
)


class Confirmation(StrEnum):
    CONFIRM = "CONFIRM"
    REJECT = "REJECT"
    UNCLEAR = "UNCLEAR"


class ConfirmationInterpreter(Protocol):
    def interpret(self, text: str) -> Confirmation: ...


class DeterministicConfirmation:
    def interpret(self, text: str) -> Confirmation:
        normalized = re.sub(r"[^\w\s]", "", text.casefold()).strip()
        if normalized in {
            "yes",
            "yes please",
            "please do",
            "go ahead",
            "yes go ahead",
            "confirm",
            "that is correct",
        }:
            return Confirmation.CONFIRM
        if normalized in {
            "no",
            "no thanks",
            "no thank you",
            "no dont create it",
            "dont create it",
            "cancel",
            "cancel it",
        }:
            return Confirmation.REJECT
        return Confirmation.UNCLEAR


def continuation(state: ConversationState, text: str, interpreter: ConfirmationInterpreter) -> bool:
    run = state.active_workflow
    if run is None:
        return False
    if run.pending_confirmation:
        return interpreter.interpret(text) != Confirmation.UNCLEAR or text.casefold().strip(
            " .?!"
        ) in {"maybe", "not sure", "perhaps"}
    # Replies to an open field question are interpreted by agent/reply.py (ADR 008); this
    # legacy path only serves checkpoints recorded before that stage existed.
    return run.status == Status.WAITING_FOR_USER and text.casefold().startswith("answer:")


def workflow_turn(
    request: AgentTurnRequest,
    occurred_at: AwareDatetime,
    state: ConversationState,
    route: RoutingDecision,
    interpreter: ConfirmationInterpreter,
    interpretation: TurnInterpretation | None = None,
) -> UserTurn:
    turn = UserTurn(
        turn_number=request.turn_number, occurred_at=occurred_at, utterance=request.utterance
    )
    if interpretation is not None:
        # The checkpointed reply decision is authoritative for this turn's workflow input.
        if interpretation.outcome == ReplyOutcome.CANCEL:
            turn.cancel = True
        elif interpretation.outcome == ReplyOutcome.WORKFLOW_ANSWER:
            value = answer_text(request.utterance)
            turn.fields[interpretation.pending_field] = value
            if interpretation.pending_field == FieldName.TOUR_TIME and "T" in value:
                try:
                    turn.requested_tour_at = TypeAdapter(AwareDatetime).validate_python(value)
                except ValidationError:
                    pass
        return turn
    run = state.active_workflow
    if (
        run is None
        or route.decision != DecisionType.ROUTED
        or route.property_service_id != run.service.property_service_id
    ):
        return turn
    if route.scenario_id != run.service.scenario_id and not (
        run.workflow_key == WorkflowKey.EMERGENCY and run.completed_actions
    ):
        return turn
    if run.pending_confirmation:
        answer = interpreter.interpret(request.utterance)
        if answer != Confirmation.UNCLEAR:
            turn.confirmation = answer == Confirmation.CONFIRM
        elif request.utterance.casefold().startswith("change:"):
            # Explicitly revise a supported collected field; Phase 4 retires the old proposal.
            field = FieldName.SYMPTOMS if run.workflow_key == WorkflowKey.MAINTENANCE else None
            revised = request.utterance.partition(":")[2].strip()
            if field and revised:
                turn.fields[field] = revised
        return turn
    if (
        run.status == Status.WAITING_FOR_USER
        and state.last_decision
        and state.last_decision.question
    ):
        value = (
            request.utterance.partition(":")[2].strip()
            if request.utterance.casefold().startswith("answer:")
            else request.utterance
        )
        turn.fields[state.last_decision.question.field] = value
        if state.last_decision.question.field == FieldName.TOUR_TIME:
            try:
                # Only an explicit ISO timestamp with offset; never infer time from prose.
                if "T" in value:
                    turn.requested_tour_at = TypeAdapter(AwareDatetime).validate_python(value)
            except ValidationError:
                pass
    if run.workflow_key == WorkflowKey.EMERGENCY and run.completed_actions:
        created = next(
            (c for c in run.completed_actions if c.action_type == ActionType.EMERGENCY), None
        )
        if created:
            turn.incident_id = created.operation_id
            turn.fields[FieldName.HAZARD] = request.utterance
    return turn
