# ADR 008: Pending-reply interpretation and safety-critical transition protection

## Status

Accepted and implemented in Phase 8.1. Complements ADR 006 (routing cascade) and ADR 007
(backend-rendered clarification); neither is replaced.

## Context

Real microphone testing (Phase 8 live validation, 2026-10-14) reproduced the same failure twice.
Relay asked "Where is the issue happening?" during an Emergency Maintenance workflow. The caller
said "Living room AC." Replies to a pending question were understood only with an `Answer:`
prefix that speech never produces. Without it, the routing model saw the fragment with no
conversation context and correctly classified it, on its own, as Non-Emergency Maintenance /
`ac_not_cooling`. The workflow then superseded the active emergency run and proposed routine
maintenance. Nothing executed, but an active emergency was silently downgraded by a location
reply. Even when the model picked the right service, a reply counted only when its scenario
matched exactly. Emergency scenarios flipped between `active_flooding`, `severe_water_leak` and
`electrical_hazard`, so the question repeated.

## Decision

### Separate question, separate owner

ServiceRouter keeps exactly one responsibility: which enabled property service a request
belongs to. A new agent-layer stage (`agent/reply.py`) answers a different question: is this
utterance answering the current workflow question, or expressing another request? It runs only
when the active workflow is `WAITING_FOR_USER` on a field. The field comes from durable workflow
state (`missing_fields[0]`), not from model history or client input. All other turns use the
unchanged ADR 006 cascade.

Order: PropertyContext → current workflow context → reply interpretation → routing validation
→ workflow engine. Outcomes are `WORKFLOW_ANSWER`, `NEW_INTENT`, `AMBIGUOUS`, and the
deterministic control `CANCEL`.

### One model call

A separate interpretation call followed by routing would add about 1.5 s to new-intent turns.
So the agent-level contract `TurnInterpretationRequest`/`TurnInterpretationResponse` asks once.
The request carries the property catalog projection, the active service and capability, the
pending field and the backend-rendered pending question. The response returns `reply_kind` and
`routing`, the model's view of the utterance *on its own*, expressed in the unchanged
property-scoped `ModelRoutingResponse` contract. The routing part is always present (ABSTAIN when
there is none) because the policy needs it to detect conflicts. The existing OpenAI-compatible
adapter implements it with strict JSON schema output. The routing part is passed to
`ServiceRouter.route(..., model_proposal=...)` and receives exactly the same catalog validation as
a fresh provider result; the router does not call the model again.

### Backend policy

The policy is deterministic and evaluated in order:

1. An exact cancel phrase leads to `CANCEL`, using the engine's existing cancel path. An `answer:`
   prefix leads to `WORKFLOW_ANSWER`. Neither needs a model.
2. A unique configured whole-utterance match is an explicit catalog request. It leads to
   `NEW_INTENT` (subject to rule 5) without a model call.
3. If the proposal violates the catalog (invented or foreign ID), the whole response is
   untrusted and the outcome is `AMBIGUOUS`.
4. A model `WORKFLOW_ANSWER` stands when its own routing view is the active service or no route.
   If the routing view names a different service, the verdict conflicts with it. The answer is
   pinned to the active workflow only when that other service has an implemented workflow that is
   neither `safety_critical` nor `preserves_business_context` (handoff). That case is a
   context-free reading of a fragment, which is exactly the observed misroute. A conflict with a
   safety-critical capability, a handoff, or a service without an implemented capability (unknown
   safety) leads to `AMBIGUOUS`. A break-in sentence is never stored as a location because the
   model also called it an answer.
5. `NEW_INTENT` with a validated route proceeds to the normal workflow transition, except that an
   unfinished safety-critical run (no committed action) is never left automatically for a routine
   implemented workflow. That case becomes `AMBIGUOUS`. Moving to another safety-critical
   capability, to handoff or to a service without a workflow remains allowed. A genuine downgrade
   needs an explicit "cancel" first.
6. A model-reported `AMBIGUOUS`, an unavailable provider, a timeout or a contract violation leads
   to `AMBIGUOUS`. When in doubt, nothing is stored and nothing switches.

`WORKFLOW_ANSWER` routes with `pin_continuation`: the router validates the active run's own
service and scenario (`TRUSTED_CONTINUATION`). The engine then records the verbatim utterance as
the pending field. The model never extracts or rewrites values. `AMBIGUOUS` consumes the turn
without invoking the engine and returns a backend template that repeats the pending question and
offers "describe a different request" or "cancel".

### Safety-critical capability policy

`Definition.safety_critical` (emergency maintenance) and `Definition.preserves_business_context`
(human handoff) are internal properties of implemented workflow capabilities, like the existing
`confirmation` flag. They are not a service taxonomy. Services remain property data, and a
service without a capability has no assumed safety level.

### Replay

`TurnInterpretation` (outcome, basis, reason, pending field, model verdict, validated proposal,
timing) is checkpointed with the final route in `TurnCheckpoint` and returned in
`AgentTurnResult`. Recovery and exact replay reuse it and never ask a model again. Older
checkpoints without it follow the previous path. No migration is required (JSONB checkpoint).

## Alternatives considered

- **Make the active workflow sticky.** Treat every reply as an answer while a question is
  pending. Fixes the observed misroute but swallows genuine escalations ("someone is breaking
  in") as field values.
- **Give the router conversation context.** Mixes two questions (which service, and is this an
  answer) into one component, and the router's catalog validation would then depend on
  conversational state.
- **Two model calls: classify the reply, then route.** Cleaner separation but adds a second
  round trip (about 1.5 s measured per call) to every new-intent turn during a workflow.
- **Have the model extract the field value.** Rewritten values would be model-authored facts;
  the backend stores the verbatim utterance instead.

## Consequences

- Natural spoken answers work without `Answer:` when a model is configured. Live validation of
  `openai/gpt-5.4-mini` reproduced the original misroute and it was pinned correctly.
- The model remains advisory. It cannot select services outside the catalog, cannot create
  question text, and cannot execute anything. Tools still run only from an engine `REQUEST_ACTION`
  built from backend state.
- An answer verdict that conflicts with an urgent route asks again rather than escalating
  automatically. This costs one turn, but an emergency is never swallowed as routine data.
- Without a model, natural replies receive the clarification. Explicit `answer:` and "cancel"
  still work. This is safe but less convenient offline.
- If the model wrongly calls a genuinely new request an answer, it is stored verbatim as the
  field. Emergency creation requires no confirmation, so this risk is accepted and mitigated by
  the conflict rules and later hazard updates. Hazard details inside an otherwise valid answer are
  kept only as that field's text.
- The `routing_decision` of an `AMBIGUOUS` turn records the validated proposal for diagnosis. The
  interpretation outcome, not that route, determines behavior.
- Reply-kind accuracy is not yet benchmarked; labelled reply cases and a paced live evaluation are
  pending. Multi-field replies, value extraction and editing earlier answers are out of scope.
