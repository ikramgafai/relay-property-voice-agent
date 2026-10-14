# Realtime text sessions — Phase 7

`/api/realtime/{session_id}` is an **internal/demo-only WebSocket**. It binds an existing
durable Relay conversation. `APP_ENV=production` rejects the upgrade with policy code 1008,
consistent with the disabled production demo HTTP routes. This is not authentication or
authorization for public deployment. Use a trusted local/internal network. No browser origin
policy or public identity verification is implemented here. Phase 8 speech input is described in [Speech](speech.md); TTS and live handoff remain unimplemented.

## Ownership and architecture

`RealtimeSession` owns connection/realtime UUIDs, the durable conversation UUID, execution
state, timestamps, an emission counter, one active turn task, a bounded output queue and a
stop signal. Its binding contains only revision/turn-number metadata for the initial handshake.
It does not cache property identity, service catalogs, workflow state, tool results or history.

`RealtimeSessionManager` reserves/binds, looks up, terminates and releases active connections.
Its short lock protects membership, including simultaneous bind reservations. It never holds
that lock during database lookup or turn execution. App lifespan supplies a trusted database
lookup and the **same TurnOrchestrator** used by the text API. Shutdown signals sessions and
cancels/awaits their owned turns before disposing the database and provider client.

`websocket.py` validates transport messages and owns reader, sender and stop-wait tasks. All
are retained, cancelled and awaited in its termination path. `session.py` owns the separate
turn task. Cancelling a reader while it is acknowledging cancellation cannot inject a second
cancellation into the turn's database cleanup. There are no detached tasks or socket references
in the manager. Application code must remain cooperatively cancellable; Python cannot forcibly
stop an arbitrary non-cooperative coroutine safely.

The processor protocol invokes `process(AgentTurnRequest) -> AgentTurnResult`. Response text,
intent, revision, outcome and replay flag are forwarded from that authoritative result. No
rendering, routing, workflow or tool execution is reproduced in realtime code. In particular,
an accepted turn or queued response is never evidence of operational success.

ADR 006 remains unchanged: context gates the routing cascade; configured requests/hazards and
trusted continuation precede constrained model interpretation; catalog and transition validation
remain in their existing components. A Palm socket cannot inject Atlas service authority.
The workflow registry is still separate from the database service catalog. Dynamic Pet Services
can remain routable even without an operational workflow. Phase 7 adds no taxonomy or registry.
ADR 004's separation of durable application truth from cancellable execution is now concrete;
ADRs 001/003/005 remain enforced by Phase 4–6 context, workflow and tool boundaries.

## Version 1 protocol

One Pydantic envelope has explicit discriminated client/server payload unions. Unknown fields
are forbidden, including nested authority-injection fields. The JSON boundary is the only place
untyped messages are parsed. The application receives typed values.

| Field | Contract |
| --- | --- |
| `schema_version` | Literal `1`; defaults to 1. |
| `event_id` | UUID; generated if omitted. Every server emission gets a fresh UUID. |
| `session_id` | Durable conversation UUID; must match the bound URL. |
| `sequence` | Nonnegative integer. Server assigns increasing values starting at 1 per connection. |
| `event_type` | Must exactly equal the discriminator `payload.kind`. |
| `timestamp` | Timezone-aware timestamp; generated in UTC if omitted. |
| `turn_id` | Required to match the payload turn ID for turn/cancellation messages. Otherwise nullable. |
| `payload` | Explicit client/server union, selected by `kind`. |

Client sequence/time/event ID are diagnostic input, never business authority or replay identity.
The server serializes sequence allocation and enqueueing under one lock. Sequences need not
be contiguous after an interrupted enqueue. Reconnect creates fresh realtime/connection UUIDs
and resets the sequence. Durable replay generates new transport event IDs for the same saved
application turn; event IDs are not operation idempotency keys. There is no persisted event log,
event-offset resume or exactly-once delivery claim.

| Client event | Payload beyond `kind` |
| --- | --- |
| `SESSION_START` | None. Emits handshake revision and next turn number, then IDLE. |
| `CLIENT_TEXT_TURN` | `turn_id`, `turn_number >= 1`, `expected_revision >= 1`, nonblank `utterance` (at most 8,000 characters). Same Phase 6 input contract. |
| `CLIENT_CANCEL_TURN` | `turn_id` identifying the active turn. |
| `PING` | None. `PONG.ping_event_id` correlates the received event. |
| `SESSION_END` | None. Terminates this realtime connection, not the durable conversation. |

| Server event | Meaning |
| --- | --- |
| `SESSION_STARTED` | Realtime/connection UUIDs, durable revision, next turn number. |
| `SESSION_STATE_CHANGED` | Typed execution state. |
| `TURN_STARTED` | Accepted task submission, not business success. |
| `AGENT_RESPONSE` | Saved/authoritative response text and intent, status, revision, replay flag. |
| `TURN_COMPLETED` | Turn ID/number, revision, COMPLETED or UNRESOLVED status, replay flag. Completion does not imply a successful operation. |
| `TURN_CANCELLED` | Execution task stopped; always `reconciliation_required: true`. Never a rollback promise. |
| `ERROR` | Fixed code and generic safe message. No raw exceptions or model reasoning. |
| `PONG` | Heartbeat correlation. No complex liveness subsystem. |
| `SESSION_ENDED` | Reason for ending this connection; best effort when transport still works. |

Turn-related started/response/completed/cancelled/error events carry the same application
`turn_id`. State events belong to the connection. PING and SESSION_END work before start;
text/cancel requires a started session. Repeated start returns SESSION_ALREADY_STARTED.

## Execution states and concurrency

```text
CONNECTED --SESSION_START--> IDLE --text turn--> PROCESSING_TURN
                               ^                    |
                               +-- result/error/cancel
Any live state --end/disconnect/fatal/backpressure--> ENDING --> ENDED
```

IDLE is text-input readiness, not audio LISTENING. PROCESSING_TURN summarizes Phase 6 processing
without falsely exposing THINKING/EXECUTING_TOOL substate timing. There is no SPEAKING state or
HANDED_OFF transition: a local handoff record is not an acknowledged live transfer. Future voice
states refine this execution model; the Phase 0 broader state machine remains the target design.

One active connection per durable conversation is enforced **within one application process**.
A second connection receives DUPLICATE_CONNECTION and closes; it cannot terminate the first.
After cleanup a new connection can bind. Run this demo with one worker. Cross-worker ownership
requires a later design; PostgreSQL's existing per-conversation guard still protects writes
against competing HTTP/other-process turns. The local manager is not a distributed lock.

A second text turn while the first task is active receives SESSION_BUSY. There is no turn
backlog. Independent conversations execute concurrently. New application turns must use the
last returned revision and next turn number; exact replay retains **all original request fields**.

## Cancellation, disconnect and replay

Execution cancellation stops the owned task and awaits its cleanup. Before processing it may
leave no checkpoint. During non-side-effect work, persisted Phase 6 checkpoints remain resumable.
Before commit the existing transaction handles rollback. During an uncertain commit, outcome
must be recovered from durable receipts; lack of a response does not establish failure.

After an operation commits, cancellation/disconnect cannot undo it. Phase 6 records execution
intent before dispatch and verifies the persisted tool receipt on retry. It does not automatically
redispatch an unknown action. A verified commit is reconciled into the workflow/response;
unverified outcomes remain unresolved. The caller must replay the same original turn ID,
number, expected revision and utterance, even if the new handshake reports a newer revision.
Changing that payload under the same ID yields TURN_CONFLICT.

After `process()` returns a durable result, cancellation cannot suppress the result task: it
returns TURN_ALREADY_COMPLETED while output is pending. After the task is gone it returns
TURN_NOT_ACTIVE. Neither invents a business cancellation. SESSION_END/disconnect may still stop
remaining delivery; replay recovers the saved result. Future **presentation cancellation** will
stop generation/TTS/playback independently; it is not implemented in Phase 7.

Disconnect stops input, cancels/awaits the active turn, stops/awaits IO tasks, releases the
manager entry and clears output buffers. It cannot deliver SESSION_ENDED to an absent peer.
Client end emits ENDING, ENDED and SESSION_ENDED when delivery is possible. It does not mark
the durable conversation inactive. Reconnect always loads persisted application state; old
WebSocket events are never used to reconstruct business truth.

Recoverable protocol/turn conflicts emit safe ERROR and return to IDLE. Missing/inactive/context-
changed durable sessions encountered during a turn emit ERROR and terminate. Unexpected processor
exceptions emit INTERNAL_ERROR and retain the connection; subsequent work remains subject to
Phase 6's durable replay/incomplete-turn guard. All failures clear active task ownership.

## Bounds, backpressure and safe failure

The default queue holds **32 events**. An enqueue may wait **1 second** for space. All Phase 7
events are critical: no normal-path dropping or coalescing occurs. A full queue that fails to
drain signals termination and logs the failed event ID/type/sequence. Socket sends have a
**5-second** timeout. Explicit client-end/fatal-session draining is bounded to **2 seconds**.
Queue saturation, send stalls or drain timeout close with **1013 / BACKPRESSURE_REPLAY_REQUIRED**.
Send exceptions close with 1011; ordinary end/shutdown closes with 1000; admission rejection
uses 1008. A failed network may prevent even the close frame from arriving.

On teardown, undelivered buffers are discarded with an explicit count/replay-required log.
Inflight send failures log their event identity separately. This is connection failure, not
silent successful delivery. Reconnect plus application turn replay is the recovery mechanism.
No acknowledgement or exactly-once network delivery exists. Phase 8 admits at most one pending
STT_PARTIAL into an empty queue; all other events retain critical delivery. See Speech for the
exact transient drop policy.

The adapter rejects binary frames and text messages over **32,768 UTF-8 bytes** with INVALID_EVENT.
The ASGI server also needs its own frame limit in any deployment: validation runs after receipt.
Unknown event types produce UNSUPPORTED_EVENT. Blank/missing/mismatched IDs, malformed JSON and
injected property/resident/unit/decision/action/result fields produce INVALID_EVENT. Heartbeat
continues after recoverable validation failures. Errors contain only fixed public codes/messages.

Structured logs measure active sessions, duplicate attempts, duration, turn duration/cancellation,
event errors, busy replies and queue pressure. Emission/delivery logs carry connection/session,
event, sequence and turn correlation. Logs omit utterances, response text, secrets and raw
exceptions. These are structured log measurements, not a new metrics service or business ledger.

## Local example

Start the existing seeded development backend, then create an internal conversation using
`POST /api/demo/sessions` with `{"resident":"Ikram"}`. Connect a WebSocket client to
`ws://127.0.0.1:8000/api/realtime/<returned-session-id>` and send:

```json
{"session_id":"<returned-session-id>","sequence":1,"event_type":"SESSION_START","payload":{"kind":"SESSION_START"}}
```

Use the handshake revision and next turn number (replace UUID placeholders with real UUIDs):

```json
{"session_id":"<returned-session-id>","sequence":2,"event_type":"CLIENT_TEXT_TURN","turn_id":"<new-turn-uuid>","payload":{"kind":"CLIENT_TEXT_TURN","turn_id":"<new-turn-uuid>","turn_number":1,"expected_revision":1,"utterance":"My AC isn't cooling."}}
```

For a normal first turn, server sequence is 1 SESSION_STARTED, 2 IDLE, 3 PROCESSING_TURN,
4 TURN_STARTED, 5 AGENT_RESPONSE, 6 TURN_COMPLETED, 7 IDLE. The routing result depends on
configured evidence/model availability; the verification flagship uses the deterministic
routing-provider seam, not a live-model classification claim.

No new database migration is needed. `sessions`, workflow rows, `agent_turns` and tool receipts
already persist everything required for business recovery. Realtime objects disappear on exit.
Future STT can submit a final transcript through the same validated turn entry point; TTS can
consume the authoritative response event. Future media transport and separately owned generation/
TTS tasks must preserve this durable-state boundary. Phase 8 now implements final speech input; TTS remains unimplemented.


## Phase 8 speech attachment

SpeechPipeline is owned by the bound RealtimeSession. Final speech and client text share an input
admission gate and the same processor. Partials never enter application turns. STT_PARTIAL is
transient; STT_FINAL and all existing business events remain critical. Media failure closes STT
without cancelling an accepted application turn. Control-session teardown closes media/STT and
retains existing cancellation/replay semantics. The session caches only the latest trusted
response text for STT context, not business truth. See [Speech](speech.md) for complete contracts.
