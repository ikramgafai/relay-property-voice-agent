# Text-turn orchestration — Phase 6

`TurnOrchestrator` coordinates property context, ServiceRouter, WorkflowEngine, ToolExecutor,
receipt reconciliation, ResponsePlan and rendering. It imports no ORM or operations adapter.
The HTTP/lifespan composition root supplies implementations. No voice or audio lifecycle is
present, and no customer-facing model can execute a tool.

## Input and trusted identity

`AgentTurnRequest` adds a session UUID to the transport `TurnInput`: turn UUID, sequential turn
number, expected session revision and nonblank utterance (maximum 8,000 characters). Extra
fields are forbidden. Property, resident, unit, workflow, ActionRequest, confirmation flags and
ToolResult cannot be supplied by the text client. The expected revision is a concurrency token,
not a grant of authority.

The store loads persisted session identity, requires an active session, then uses the existing
PropertyContextResolver. A resident reassignment that changes property or unit fails safely;
it cannot silently move this session. The minimal demo path supports the seeded residents.
Prospect/session creation without a resident is not implemented by this interface.

## Turn lifecycle and checkpoints

1. Acquire the per-session PostgreSQL transaction advisory guard. Concurrent attempts receive
   `SESSION_BUSY` (409), rather than waiting indefinitely or altering state concurrently.
2. Load an existing turn by UUID and compare the entire request. A completed exact replay returns
   its saved AgentTurnResult with `replayed=true`; no routing, tools or rendering run again.
3. For a new turn, require the next turn number, matching revision and no unfinished earlier turn.
   Create its audit/checkpoint row with a backend timestamp.
4. Resolve fresh context, call the existing router and persist its decision.
5. For clarification/abstention, skip WorkflowEngine and tools. Only the transport turn advances.
   For a route, interpret the reply to a current prompt and call WorkflowEngine. Save workflow
   state and its decision checkpoint in the same transaction.
6. ASK_USER and WAIT_FOR_CONFIRMATION produce plans without tools. REQUEST_ACTION persists a
   dispatch-started checkpoint before calling ToolExecutor.
7. Save the tool result. Independently verify successful receipts through Phase 5's receipt
   verification before reconciliation and response planning; mismatching/absent evidence becomes
   an uncertain outcome, not success.
8. Reconcile typed completion metadata, create a ResponsePlan and render it.
9. Atomically save reconciled state and the final AgentTurnResult. Only after that commit does
   the API return the response. The completed turn record compacts redundant working checkpoints.

The guard is an ordinary PostgreSQL primitive, not an external lock service or process-local
mutex. It is needed because holding the session row lock while invoking ToolExecutor would
deadlock against Phase 5's independently committed transaction. Its connection holds only the
advisory transaction lock; state/tool writes retain their existing session row locks, uniqueness
and revision checks. Connection loss releases the guard. UUIDs map to signed 64-bit keys; a
collision can cause conservative contention, never merge session state.

## Durable turns, not another business ledger

`agent_turns` fills the gap left by Phase 4's last-decision replay snapshot: it retains every
accepted text request, original timestamp, routing/workflow outcome, tool receipt identities,
response plan/text and timing. A unique `(session_id, turn_number)` constraint and a composite
session/property foreign key support the boundary. A focused reversible migration adds this
one table. No separate messages table is necessary yet: user text and assistant response are
already in the turn record. There is no event-sourced reconstruction of business state.

The record stores no PropertyContext or full ConversationState snapshot. WorkflowDecision's
embedded workflow-state copy is omitted; its question, transition and action declaration remain
auditable. On completion, transient route/decision/result copies are removed from the checkpoint
in favor of the final result. Tool receipts are included as typed output evidence while the
authoritative execution ledger remains tool_calls. No hidden reasoning or provider reasoning
tokens are stored. Transcript retention and access control still need production policy.

## Crash recovery and uncertainty

An interrupted turn can resume under its exact identity. A saved route is reused. Workflow state
and its checkpoint commit together, preventing a repeated transition after a process failure.
If dispatch was started but its result was not saved, recovery performs receipt verification
only. It does **not** execute the action again. A verified committed receipt is reconciled; no
verifiable receipt yields UNKNOWN_OUTCOME. This deliberately covers the crash gap before an
operation was actually sent: safety is preferred over speculative re-execution.

UNKNOWN_OUTCOME/PENDING retain unresolved action IDs in workflow state. A later relevant turn
can perform read-only receipt reconciliation; it never automatically resubmits that unresolved
action. A replay of the original completed text turn preserves its historical response, including
uncertainty. Known failed attempts do not populate completed_actions. Phase 5's action-level
idempotency remains available for subsequent explicitly authorized attempts.

Database failures return a safe storage error and leave checkpoints for the same turn identity.
A partially processed turn blocks a different next turn until recovered. Administrative recovery
for irrecoverable context changes or abandoned turns is a future operational concern; the system
does not discard uncertain work to keep the conversation moving.

## Routing and text interpretation

ADR 006 permits explicit/configured evidence, continuation policy, then constrained model
interpretation. The router now accepts an **internal-only** current-service/scenario hint for
recognized confirm/reject/unclear replies, or an explicit `Answer:` reply to its persisted
missing-field question. Unique configured evidence takes precedence; other configured evidence
still falls through to constrained interpretation. Every continuation ID passes normal current-
catalog validation. No property/service keyword taxonomy is added to the orchestrator.

For `Answer:` input, the prefix is removed before configured routing checks, so a configured
hazard or explicit new service is not hidden by the reply marker. Unknown free-form utterances
still use normal configured/model routing; clarification and abstention are never silently
overridden. The offline text path is deliberately conservative: `Answer:` gives an explicit way
to answer a prompt without requiring a live routing model. With a configured provider, ordinary
answers can also route to the current service. There is no claim of general language-extraction
coverage or improved live-model accuracy.

The field interpreter copies a routed reply verbatim into the field requested by WorkflowEngine.
It does not choose missing fields or infer a new unit. Tour time is structured only for an
explicit ISO timestamp with timezone offset; prose dates are not guessed. `Change:` can revise
the maintenance symptoms proposal and triggers fresh confirmation. Other amendment/extraction
policies remain future work.

## Confirmation

`ConfirmationInterpreter` is provider-neutral. Its deterministic implementation recognizes a
small set of clear positive/negative phrases, including “yes please,” “go ahead,” and “No, don't
create it.” Anything else is UNCLEAR; “maybe” keeps the confirmation question open. Mixed consent
and changes, such as “yes but change the unit,” cannot confirm.

Interpretation is applied only when routing still selects the current proposal's service and
scenario. Expected revision and sequential turn checks reject a stale yes after another turn
changes the proposal. Phase 4 remains authoritative: persisted pending_confirmation must match
before REQUESTED/ACTION_REQUIRED can authorize Phase 5. The interface cannot infer whether a
human misunderstood the latest prompt; it guarantees association with the latest acknowledged
backend revision, not mind-reading.

## Workflow reconciliation

The agent reconciliation helper translates a verified result into existing workflow state. A
CompletedAction stores action UUID, execution UUID, operation UUID, action type and completion
turn. Its request disposition becomes COMPLETED; the run becomes COMPLETED. Validators require
completion records to match completed declarations, with no duplicates. FAILED leaves the action
incomplete. UNKNOWN/PENDING retain the action identity and await reconciliation.

A committed emergency remains the same business incident when a later routed report changes its
hazard scenario. The backend selects the incident ID from completed metadata, never from the
utterance. The workflow emits UPDATE_EMERGENCY_INCIDENT and Phase 5 checks the target's ownership.
Other service transitions retain their existing rules. An unexecuted scenario transition still
behaves as Phase 4 specified. No new operational tool is added.

## ResponsePlan and rendering

The typed plan contains generic intent, property/service/unit display context, a backend question,
explicit operation status, and a successful OperationFact only when authorized. Request-action
alone yields PENDING. The application checks action/execution/operation identity against the
durable verified receipt before constructing a success plan. A fake executor success cannot
inject its invented reference into the response.

Deterministic rendering handles every intent. Successful operation references come exclusively
from verified facts. Tour wording says a request was recorded, not that a calendar booking is
confirmed. Handoff wording explicitly says no live transfer occurred. Unknown wording says the
request may have been submitted but its status cannot be confirmed; failure wording makes no
success claim. Raw database/provider exception text is never part of these templates.

The optional `ConstrainedModelRenderer` depends on a provider-neutral PhrasingProvider. For
non-operational responses it supplies backend-approved alternatives and accepts only an integer
index. Model-generated prose is never displayed. Success/failure/pending/unknown responses
bypass the model entirely. Timeout, refusal, malformed output, invented text or an out-of-range
index falls back to the deterministic baseline. This is intentionally limited model assistance,
not free-form paraphrasing controlled only by a prompt.

The compatible HTTP phrasing adapter reuses explicitly configured routing endpoint/model/key
settings and requires `RESPONSE_MODEL_ENABLED=true`. Default rendering is offline. No live model
is selected or called without configuration. Tests use deterministic or mocked providers.

Prior conversation text is never fed back as business truth. The current renderer does not need
history at all. The handoff plan includes a durable structured summary of known identity,
reported workflow fields and verified completed operations; the existing handoff row retains its
Phase 5 structured context and real incident reference. There is no speculative prose summary.

## Internal/demo HTTP interface

Endpoints are disabled when APP_ENV=production. This is a development guard, **not authentication**;
do not expose the demo endpoints publicly before transport authentication/authorization exists.

`POST /api/demo/sessions` with `{"resident":"Ikram"}` or `{"resident":"Alex"}` creates a session
from existing seed identity. It never seeds implicitly. The response supplies session_id,
revision and resolved display context. The backend chooses the property/resident/unit association.

`POST /api/sessions/{session_id}/turns`:

```json
{
  "turn_id": "a-new-client-generated-uuid",
  "turn_number": 1,
  "expected_revision": 1,
  "utterance": "My AC isn't cooling."
}
```

Use an actual UUID and the returned revision for each next turn. Reuse the **entire original
request** for a retry. A reused UUID with different contents, stale revision/number, busy session,
or unfinished previous turn returns 409. Missing sessions return 404; extra authority fields or
invalid payloads return 422; unavailable session storage returns a safe 503. CORS allows POST
only for configured origins. No authentication, browser UI or audio transport is introduced.

The response contains routing decision, optional workflow decision, typed tool results, response
plan/text, revision, turn status, replay flag and timings. Internal IDs in this demo contract are
intentional audit references; user-visible operational UUIDs are backend-generated references.

## Observability and remaining boundaries

Context, routing, workflow, tool and rendering timings are retained. Total processing timing
covers work through response construction; the final persistence commit happens afterward and
HTTP middleware measures the whole request. Existing component events remain; AGENT_TURN_COMPLETED
adds turn/session IDs, route/workflow outcome, action types, execution IDs and final intent after
the response/state commit. Transcript and hidden reasoning are excluded from logs.

No new tool, external PMS, streaming response, voice, STT/TTS, WebRTC/LiveKit, barge-in, agent
framework or Phase 7 work is included. Live routing calibration, broader field interpretation,
production timeout/retention/auth policies and operational reconciliation UX remain open.

## Phase 8.1: pending-reply interpretation

When the active workflow is `WAITING_FOR_USER` on a field, the orchestrator first runs
`agent/reply.py` ([ADR 008](decisions/008-pending-reply-interpretation.md)), before business
routing. A single advisory call (`TurnInterpretationProvider.interpret_turn`, implemented by the
existing OpenAI-compatible adapter) returns a reply verdict plus the model's routing view. The
unchanged router validates that view against the catalog. The backend policy then decides among
`WORKFLOW_ANSWER` (pinned `TRUSTED_CONTINUATION`; the verbatim utterance becomes the pending
field), `NEW_INTENT` (normal validated transition, except that an unfinished safety-critical run
is never downgraded automatically), `AMBIGUOUS` (backend re-ask; no engine call, no field, no
transition) and the deterministic `CANCEL` control. `answer:` remains an explicit deterministic
answer. The outcome is checkpointed as `TurnCheckpoint.interpretation`, returned as
`AgentTurnResult.interpretation`, logged as `TURN_INTERPRETED`, and reused on replay without a
model call. Confirmation handling and turns with no pending question are unchanged.
