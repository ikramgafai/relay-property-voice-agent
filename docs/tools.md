# Trusted tool execution — Phase 5

An `ActionRequest` declares an operation; it is not evidence that the operation happened.
Only `ToolExecutor.execute(session_id, request)` returning a verified `SUCCEEDED`
`ToolResult` establishes committed local success. The result contains the operation UUID,
typed output, execution/action identity, timing, and replay metadata. It contains no ORM object.
`PENDING`, `FAILED`, and `UNKNOWN_OUTCOME` carry no successful operation identity or output.

## Three distinct registries

The database service catalog determines what a property can route to. The workflow registry
determines which deterministic processes the application implements. The static tool registry
determines which trusted operations can execute. Database configuration never selects a Python
callable. Pet Services can remain routable with no dedicated workflow or executable tool.

`tools/registry.py` registers five typed wrappers:

| Action | Committed evidence | Meaning |
| --- | --- | --- |
| CREATE_MAINTENANCE_REQUEST | Maintenance UUID, OPEN | Local maintenance request recorded. |
| CREATE_EMERGENCY_INCIDENT | Incident UUID, OPEN, revision | Local incident recorded; no dispatch claim. |
| UPDATE_EMERGENCY_INCIDENT | Same incident UUID, new revision | Allowed hazard/severity fields replaced. |
| SCHEDULE_LEASING_TOUR | Tour UUID, REQUESTED, aware requested time | Local request, not a confirmed calendar booking. |
| CREATE_HUMAN_HANDOFF | Handoff UUID, REQUESTED, optional incident UUID | Local handoff request; `transfer_performed=false`. |

The provider-neutral `PropertyOperationsAdapter` and transaction/store protocols live in
`tools/protocol.py`. The local SQLAlchemy implementation lives in `db/operations.py` and
`db/tool_store.py`. Small wrappers share one registry module; transaction, validation,
contracts and storage remain separate. There is no external PMS integration.

## Authorization and lifecycle

1. Revalidate the existing typed ActionRequest, including its discriminated payload.
2. Start an executor-owned transaction and lock the persisted conversation session.
3. Load its workflow and fresh property/resident/unit/service context. Request identities must
   match the trusted session. Hold shared locks on configuration/ownership rows until commit.
4. Look up the durable execution by action identity and compare the canonical request hash.
   A successful prior receipt replays without re-executing, including after workflow supersession.
5. For a new attempt, require an exact persisted declaration. Reject arbitrary substituted payloads.
6. Record PENDING, then check active run, REQUESTED disposition, ACTION_REQUIRED status,
   enabled service/scenario, current workflow binding and confirmation policy.
7. Execute the registered typed tool in a savepoint. The adapter flushes but never commits.
8. Record the provisional success receipt and operation together, or a safe known failure after
   rolling back the operation savepoint. Commit the outer transaction.
9. Read the receipt and matching operation with a fresh database transaction. Only then return
   success and emit `TOOL_SUCCEEDED`.

The in-transaction success value is private: neither returning from an adapter nor flushing a
row establishes success. A pre-commit failure rolls back both operation and receipt. The tests
also inspect an independent connection before commit: neither row is visible and no success
event has been emitted.

Phase 4 represents unconfirmed actions as `pending_confirmation` in WAITING_CONFIRMATION.
A trusted confirmation turn moves the same request into REQUESTED/ACTION_REQUIRED. The
executor reads those persisted facts; flipping a request flag cannot grant authorization.
Cancelled and superseded unexecuted requests cannot execute. Historical committed receipts
remain execution truth and can replay; replay still requires valid source ownership/context.

Composite foreign keys enforce session/property, workflow/session/property, resident/property,
unit/property and operation/tool-source ownership. Handoffs also constrain incident/property.
The backend does not accept an ID simply because a model or client supplied it. This internal
executor is not an authenticated public API; transport identity/access control is a later concern.

## Idempotency and concurrency

Phase 4 already persists a stable ActionRequest UUID. The unique idempotency key is
`action:<UUID>`; `action_request_id` is independently unique. Hash version 1 is SHA-256 of
UTF-8 JSON containing the session UUID and the full Pydantic JSON-mode request, with recursively
sorted object keys, compact separators, and non-finite numbers forbidden. List order and string
contents remain meaningful. This is exact validated-payload identity, not semantic equivalence
of natural language or normalization of equivalent timezone offsets.

Same key/hash replays a committed result. Same key/different hash returns
`IDEMPOTENCY_CONFLICT` without exposing the previous success as the new request's result.
Known FAILED attempts may retry with the same identity and increment attempt_count. PENDING
or UNKNOWN receipts do not trigger another operation. Success replays increment replay_count.

Session row locks serialize tools against each other and Phase 4 revision writes. The executor
uses READ COMMITTED so a waiting transaction sees its predecessor's committed receipt.
The existing Phase 2/4 repeatable-read behavior is unchanged. Database uniqueness is an
additional guard; there is no unlocked check-then-insert and no process-local idempotency cache.
Different sessions can progress independently. Concurrent real PostgreSQL callers receive one
incident identity, one logical audit record and one incident row.

An emergency run can create only one incident, enforced by a unique workflow_id. A later update
has its own ActionRequest UUID and explicitly targets that incident within the same property,
session and workflow. Hazard text is replaced rather than repeatedly appended. Retrying the
update returns its original receipt/revision; it does not increment revision again. Replaying an
older create/update reports that operation's historical receipt, not a new current-state query.

## Minimal Phase 4 extensions

The existing ActionRequest is reused. Added contracts are an internal UPDATE_EMERGENCY_INCIDENT
action/payload, optional incident target on UserTurn/workflow state, optional timezone-aware
tour time, and optional source_workflow_id in transition business context. Defaults preserve
older stored snapshots. No service taxonomy or routing source change is required.

An explicit incident target plus hazard information produces an update request. A structured
`UserTurn.requested_tour_at` is preserved in the tour payload; legacy text such as “Saturday
afternoon” remains usable for conversation but fails tool validation without a structured time.
Changing tour details requires a new persisted confirmation. No model-inferred time is accepted.
The tour represents a request only; availability and actual calendar confirmation are future work.

Handoff context snapshots known property/resident/unit, prior business context, committed tool
identities and a real incident when the transition source supports it. Missing facts stay null.
No customer response is generated and no telephone transfer occurs.

## Failures, audit and observability

Safe codes distinguish validation, unsupported action, ownership/property violation,
ineligibility, missing confirmation, idempotency conflict, business constraints, database
failure and unknown outcome. Raw database exceptions and payload content are not logged.
Known tool failures persist a FAILED audit receipt when the outer transaction is available.
Invalid ownership/declarations and conflicts are logged but cannot overwrite the legitimate
audit row. If the database cannot commit, a durable failure row cannot be promised.

`tool_calls` stores validated request JSON, ownership, hash, latest result/timestamps,
attempt/replay counts, operation ID and safe error. It is one logical record per action, not
an append-only history of every retry. Structured events include action/execution/session/
workflow/property IDs, status, replay flag, total call duration, operation ID and safe error.
These support success/failure, replay and conflict counts without a new metrics platform.
The persisted duration covers the attempt through provisional outcome; log duration includes
commit and verification. No secrets or hidden reasoning are persisted.

A lost commit acknowledgment does not prove rollback. The executor reconciles once using a
new transaction that waits on the session lock. A verified receipt returns its actual result;
verified absence is a controlled database failure. Unavailable verification returns
UNKNOWN_OUTCOME without success evidence and without re-execution. A later local invocation
can safely reconcile through the same ledger and locking rules. The UNKNOWN response need not
match the database ledger status: the ledger may already contain an atomic successful commit.

External APIs cannot join this PostgreSQL transaction. A future adapter requires durable dispatch
intent, provider idempotency, reconciliation and a policy for unresolved outcomes. Do not plug
an irreversible external call into the local transaction strategy or blindly retry unknown
external outcomes. Local atomicity is not distributed exactly-once execution.

## Deliberate boundaries

The tool ledger is execution truth. Phase 5 left `completed_actions` empty; Phase 6 now updates
typed completion metadata through [verified receipt orchestration](orchestration.md). Routing, workflow decisions,
and model output never claim operational success. No final orchestrator, customer-response
generator, voice, audio transport, STT/TTS, calendar provider or telephony is added in Phase 5.
