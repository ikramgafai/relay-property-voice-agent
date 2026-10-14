"""Deterministic lexical test double, NOT an LLM, semantic model or production fallback.

It receives no expected labels. Conservative overlap with supplied names/scenarios produces
repeatable imperfect outputs so metrics exercise real scoring, including wrong-route reports.
"""

import re
from uuid import UUID

from app.routing.model import ModelRoutingRequest, ModelRoutingResponse

STOP = frozenset(
    (
        "a an the i my is it are was do does can could would to of for in on at with and or "
        "me you please how what when where have has need help someone something this that "
        "about not no"
    ).split()
)


def tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z]+", value.casefold())) - STOP


class OfflineLexicalProvider:
    async def interpret(self, request: ModelRoutingRequest) -> ModelRoutingResponse:
        utterance = tokens(request.utterance)
        scores = []
        for service in request.services:
            choices: list[tuple[str, UUID | None]] = [
                (service.name + " " + service.description, None)
            ]
            choices.extend(
                (sc.name + " " + sc.description + " " + " ".join(sc.examples), sc.id)
                for sc in service.scenarios
            )
            scored = [
                (len(utterance & tokens(text)) / max(len(utterance | tokens(text)), 1), sid)
                for text, sid in choices
            ]
            score, scenario_id = max(scored, key=lambda pair: pair[0])
            scores.append((score, service.property_service_id, scenario_id))
        scores.sort(key=lambda row: row[0], reverse=True)
        action = "ABSTAIN"
        service_id = scenario_id = kind = None
        candidates = []
        if not utterance:
            action, kind = "CLARIFY", "DESCRIBE_REQUEST"
        elif scores and scores[0][0] >= 0.18:
            if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.08:
                action, kind = "CLARIFY", "CHOOSE_SERVICE"
                candidates = [row[1] for row in scores[:2]]
            elif scores[0][0] >= 0.32:
                action, service_id, scenario_id = "ROUTE", scores[0][1], scores[0][2]
            else:
                action, kind = "CLARIFY", "DESCRIBE_REQUEST"
        return ModelRoutingResponse.model_validate(
            {
                "action": action,
                "property_service_id": service_id,
                "scenario_id": scenario_id,
                "clarification_kind": kind,
                "candidate_service_ids": candidates,
                "reason": "Offline lexical test-double result; no model inference.",
            }
        )
