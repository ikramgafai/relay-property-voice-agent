# Phase 4 workflow and application state

`WorkflowEngine.advance(PropertyContext, RoutingDecision, ConversationState, UserTurn)` is a
deterministic function returning `TurnResult(state, decision)`. It copies and revalidates inputs,
does not call an LLM or database, and has no executor. `WorkflowApplication.process` composes
that engine with `ConversationRepository` and structured logs. The SQLAlchemy adapter stores
application state only. There is no new HTTP endpoint or conversational agent in this phase.

## Relationship to ADR 006

The existing router still performs configured evidence → constrained model interpretation →
backend catalog validation. Phase 4 consumes its actual `RoutingDecision`; it neither reclassifies
utterances nor adds service names to a routing taxonomy. Before advancing state it checks the
session identity, property ID, existing catalog fingerprint, selected service/scenario ownership,
and ID/key/name agreement using Phase 3 validation. An invalid or stale route fails explicitly.

ADR 006's downstream workflow transition responsibility is now concrete: a valid new routed
service can replace the active workflow under the preservation policy below. A previous workflow
never suppresses a newly routed emergency. `CLARIFICATION_REQUIRED` and `ABSTAINED` yield
`AWAIT_ROUTING`, preserving any existing workflow without advancing it or requesting an action.
This is not a new continuation classifier, hazard detector, or semantic benchmark. Those routing
decisions remain upstream. Live-model semantic verification remains pending as documented in
Phase 3. ADRs 001–007 are unchanged.

## Explicit state and input

`ConversationState` holds session/property/resident/unit IDs, ordered turn number, persistence
revision, current service/scenario reference, previous service references, active workflow ID,
workflow runs, transitions, and the latest accepted command digest/decision receipt. Property,
resident and unit identity are fixed for a session; changing them requires a new session.
The current service describes the active workflow, including while routing is unresolved or
unsupported. Unsupported routing does not falsely relabel an existing workflow.

`WorkflowState` holds its stable ID, implementation key, property-service/scenario reference,
start turn, status, step, collected/missing fields, field history, confirmation proposal,
requested-action dispositions, optional handoff business context, and aware timestamps.
Phase 4 originally constrained `completed_actions` to empty. Phase 6 now permits typed completion
metadata only through verified receipt reconciliation; Phase 4 engine calls still never execute
tools or manufacture completions. See [Orchestration](orchestration.md).

`CollectedField` contains a bounded string value, source turn and `user` or `property_context`
provenance. Updating a field retains its prior value/provenance in `field_history`. These are
reported facts, not verified measurements or hidden reasoning. No confidence scores are added.

`UserTurn` carries an explicit turn number, aware timestamp, utterance, typed field updates,
optional boolean confirmation, cancel, and new-request control. The initial issue/handoff reason
is the user's verbatim utterance. Later details must be explicitly supplied as field updates;
there is no natural-language extraction or yes/no interpretation. Unknown field names, fields
outside the capability, conflicting controls, and attempts to override a known unit fail.
Unknown units are collected as text without inventing a database unit ID.

## Registry and implemented capabilities

`registry.py` maps an enabled service's `configuration.workflow_key` to an implemented capability.
When absent, its service key is a conventional binding for the four implemented keys. An explicit
unknown/non-string binding returns unsupported, rather than falling back. These bindings do not
limit what services can exist or route. A database-only Pet Services addition still routes through
Phase 3, then returns `UNSUPPORTED_WORKFLOW` unless explicitly bound to an existing capability.
Palm's generic `maintenance` is deliberately unsupported by default; an administrator may bind it
to a supported capability without exposing Atlas services or modifying routing source.

The four small definitions share the engine's collection/confirmation logic. Requirements and
typed payload construction live in `definitions.py`; the registry, transitions, persistence
protocol and application composition are separate modules.

| Capability | Required facts | Result |
| --- | --- | --- |
| Emergency maintenance | Issue, unit, location, hazard, severity; symptoms optionally preserved | `CREATE_EMERGENCY_INCIDENT` request, without a confirmation delay |
| Non-emergency maintenance | Issue, unit, symptoms | Confirm → `CREATE_MAINTENANCE_REQUEST` request |
| Leasing / schedule_tour | Preferred time and contact | Confirm → `SCHEDULE_LEASING_TOUR` request |
| Leasing / availability or pricing | Routed scenario | `COMPLETE` with information intent only; no invented inventory/prices |
| Human handoff | Reason; property/identity from context; prior business snapshot if available | Confirm → `CREATE_HUMAN_HANDOFF` request |

Leasing without a supported scenario returns `UNSUPPORTED_WORKFLOW` with reason
`UNSUPPORTED_LEASING_SCENARIO`. Preferred tour time is a reported preference string, not a
validated appointment; Phase 5 must resolve availability/timezone and operational validation.
An unknown hazard can be explicitly reported as unknown. There is no fabricated severity
inference, emergency dispatch, safety advice, or operational urgency policy hidden in the engine.

## Decisions and action boundary

`ASK_USER` includes a structured field/question type. `WAIT_FOR_CONFIRMATION` retains a typed
proposal in workflow state. A later `confirmation=True` produces `REQUEST_ACTION`; false cancels
the proposal. Edits supersede an outstanding request/proposal and require fresh confirmation.
Confirmation cannot accompany edits, start another workflow, or confirm without a pending proposal.

`COMPLETE` currently means the informational intent is ready, not that an operational action
succeeded. `CANCEL` cancels application intent only. `AWAIT_ROUTING` and `UNSUPPORTED_WORKFLOW`
carry no executable request. Escalation is represented by an explicit transition attached to the
resulting ASK/CONFIRM/REQUEST decision and the old run's `ESCALATED` status, avoiding a second
decision that would conceal the new workflow's required next step.

`ActionRequest` has an action UUID, internal action type, discriminated typed payload, confirmation
requirement, source workflow and source turn. Its type must match the payload kind. IDs use UUID5
of session/start turn for workflows and workflow/proposal turn for actions. Confirmation and retries
retain the proposal identity; edited proposals get a new identity. All payloads carry the bound
property/resident/unit IDs. The internal action enum is not a routing service taxonomy.

There is no incident, maintenance request, appointment or handoff record. `ACTION_REQUIRED` remains
pending; no tool result is fabricated. A repeated later turn with unchanged state may return the
same request identity, which a future executor must deduplicate and revalidate. Neither a log nor
an ActionRequest proves an action occurred. Phase 5 must implement execution, idempotency and
authoritative result handling separately.

## Transitions and preservation

Each service/scenario/binding change starts a new run. Explicit `new_request=True` also starts a
new run, even for the same service. A transition records both service references and run IDs,
reason, source turn/time, preserved/discarded field names and superseded proposal/request IDs.
Old runs and service history remain available.

- Maintenance → emergency preserves issue, symptoms and unit with provenance/history. The new
  emergency report becomes the current issue and the AC report remains in issue history.
- Emergency → handoff snapshots the emergency service and allowlisted issue/unit/symptom/location/
  hazard/severity fields plus their histories. Leasing time/contact can also be preserved for a
  leasing handoff. The handoff destination service is separate from this business context.
- Unrelated workflows discard incompatible fields. Explicit new requests do not inherit prior
  issue fields. Known unit context is reapplied from PropertyContext when required.
- Pending confirmations are removed; requested-but-unexecuted actions become `SUPERSEDED` or
  `CANCELLED`. They never migrate into a new run. Terminal completed/cancelled runs keep their
  terminal status. There are no committed actions to reverse in this phase.

## Persistence and concurrency

Migration `0002_workflow_state` adds only `sessions` and `workflow_runs`. This realizes the Phase 0
application-state entities without adding messages, tools, incidents, appointments, handoffs or
event tables. In-memory state would lose required continuity and request identities on restart.

`sessions` contains immutable identity FKs, revision, timestamps and a JSONB application snapshot
excluding workflow runs. `workflow_runs` contains run/session/property/service identity, key,
status, timestamps and typed JSONB state. Runs are reconstructed in start-turn order. The latest
decision is intentionally a replay receipt in the session snapshot, not a second mutable workflow
authority. No LLM history, audio buffers, task handles or hidden reasoning are stored.

The adapter checks resident/service ownership, uses property/unit and session/property database
constraints, and rejects moving an existing run into another session. It stores all runs and the
session update inside one savepoint within the caller's transaction. A compare-and-swap revision
prevents silent lost updates; competing creates use a unique session ID. PostgreSQL serialization
failures under the project's `REPEATABLE READ` isolation become `StaleRevision`. The caller must
rollback/end that transaction and reload before retrying; there are no automatic blind retries.
Callers should use `Database.session()` so reads see a consistent aggregate snapshot.

The latest identical turn returns the saved decision without incrementing revision or duplicating
requests. A conflicting same-number turn or older/out-of-order turn is rejected. Retry must retain
the original timestamp, fields and equivalent context. Catalog changes fail closed and require
fresh routing, not replay under stale configuration. Historical-turn replay beyond the latest
receipt is deliberately not a durable command ledger. No public API accepts arbitrary saved state.

Typical composition (the caller supplies the existing route and typed turn):

```python
async with database.session() as session:
    application = WorkflowApplication(SQLAlchemyConversationRepository(session))
    result = await application.process(session_id, property_context, routing_decision, user_turn)
# Commit succeeds here. Consume result.decision as structured intent; do not execute it in Phase 4.
```

## Events and verification

The application emits `WORKFLOW_STARTED`, `WORKFLOW_STEP_CHANGED`, `WORKFLOW_ESCALATED`,
`WORKFLOW_COMPLETED`, plus `WORKFLOW_DECISION`, with IDs, service, status, step, decision/action
type, revision and duration. Transition logs include from-workflow/service, reason and field names,
never field values. Identical replay emits no duplicate lifecycle events. Logs are observations
inside the caller's transaction, not a committed event ledger; transaction rollback can invalidate
an observation. Final operational success is never logged.

Unit tests demonstrate all four capabilities, safe uncertain/unsupported routes, confirmation,
cancellation, property rejection, edits and replay. PostgreSQL tests exercise database-only Pet
Services, Atlas/Ikram/204 AC → leak → emergency → handoff, reload through a fresh engine, stale
updates, atomic failure, and full migration reversibility. See [verification](verification/phase-4-verification.md)
for actual executed results and remaining work.

## Remaining decisions

Phase 5 must define executor claim/reconciliation rules, business validation, tool results and
operational resource links. Later phases must address authentication/session access, context
refresh/moving residents, retention/encryption policy for reported personal data, catalog revisions,
long-session history limits, natural-language field extraction, richer leasing branches, and live
model semantic calibration. This phase does not implement those systems.

## Phase 8.1: capability safety policy

`Definition` gains two internal capability properties: `safety_critical` (emergency maintenance)
and `preserves_business_context` (human handoff). They are not a service taxonomy, and a service
without an implemented capability has no assumed safety level. The pending-reply policy
([ADR 008](decisions/008-pending-reply-interpretation.md)) uses them so an unfinished
safety-critical run is never left automatically for a routine workflow, and an answer verdict
that conflicts with a safety-critical, handoff or unknown-capability route is clarified instead of
stored. The engine, transitions and persistence are unchanged. A pending question is derived from
the active run (`WAITING_FOR_USER`, first missing field), so it survives an unsupported or
ambiguous interim turn.
