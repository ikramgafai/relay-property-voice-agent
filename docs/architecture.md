# Architecture

## Status and invariants

This architecture was accepted in Phase 0 and implemented incrementally. As of Phase 8.1:
property context, constrained routing, durable workflows, trusted local tools,
[text-turn orchestration](orchestration.md), [realtime sessions](realtime.md),
[speech input](speech.md) and [pending-reply interpretation](decisions/008-pending-reply-interpretation.md)
are implemented. TTS, playback, barge-in, external PMS integration and live handoff delivery are
not. The "Concrete Phase N composition" sections at the end record how each phase mapped onto
this design; sections that describe target behavior say so.

1. Resolve caller → property → unit/contact context → enabled services → route → workflow.
   A missing property never permits another property's catalog; an unknown resident or unit
   stays explicitly unresolved.
2. Only the property's enabled services and active scenarios can be selected, regardless of
   wording or model confidence.
3. The database service catalog, the workflow capability registry and the tool registry are
   separate. Configuration can bind implemented capabilities; it cannot create executable
   behavior. Service keys and names are data; routing has no property-name branches.
4. Models propose; the backend validates. Invented or foreign IDs are rejected. Clarification
   and abstention are valid outcomes; a confident wrong route is worse than lower coverage.
5. ActionRequest is not success. Only a verified, committed ToolResult authorizes a success
   response. FAILED stays failed; PENDING and UNKNOWN_OUTCOME stay unconfirmed. A handoff
   request is not a live transfer and a tour request is not a calendar booking.
6. Retries keep their logical identity. Changed arguments under the same identity are rejected.
   Unknown writes are reconciled, never blindly re-executed.
7. Durable business state is separate from cancellable execution and presentation. Disconnect,
   media failure or lost delivery cannot erase a committed write or prove that a write failed.
8. Provider SDKs stay inside adapters. LiveKit transports media, AssemblyAI transcribes, models
   interpret; Relay decides and executes.
9. STT partials never create application turns. Only provider-final end-of-turn text is
   admitted.
10. Queues and tasks have bounds and owners. Partial transcripts may be dropped; finals and
    business completion keep explicit delivery and backpressure-failure semantics.
11. Client metadata, logs, transcripts and model history are not authoritative state. Logs carry
    no secrets, raw audio or transcripts by default.
12. A reply to a pending workflow question is interpreted by the agent layer, not the router.
    The model verdict is advisory; a verdict that conflicts with a safety-critical, handoff or
    unknown-capability route is clarified. An unfinished safety-critical workflow is never
    downgraded automatically (ADR 008).
13. Failures have explicit outcomes; silence is not success.

## Module responsibilities

| Module/path | Owns | Boundary |
| --- | --- | --- |
| `backend/app/main.py` | Application factory and composition root. | The `core/lifecycle.py` lifespan wires the database, providers, orchestrator and realtime manager; settings live in `core/`. |
| `api/` | HTTP health/readiness, internal demo session and turn endpoints, voice token endpoint, request boundary validation. | No business policy, provider SDK calls, or direct business writes. |
| `realtime/` | Ephemeral sessions, connection ownership, tracked tasks, turn submission, cancellation, ordered typed events and bounded queues. Playback remains future work. | Invokes TurnOrchestrator; no routing/workflow/tool logic or business-state duplication. |
| `property/` | Property resolution, residents, units, contact context, enabled services, configuration, and property knowledge. | Returns a property-scoped context; rejects cross-property references and exposes unresolved fields explicitly. |
| `routing/` | Eligible-service selection, configured evidence, model-proposal validation, clarification/abstention decisions, routing evidence. | Consumes only the enabled catalog; proposes routing without executing business actions. |
| `workflows/` | Business workflow state, allowed steps, required fields, service transitions, escalation rules, completion policy. | Validates router/agent proposals and authorizes tool requests; persists through repositories. |
| `agent/` | Turn sequencing, pending-reply and confirmation interpretation, verified receipt reconciliation, response planning and constrained rendering. | Uses ORM-free storage protocols and ToolExecutor; no direct operational writes or model authority over truth. |
| `tools/` | Schema-validated backend functions for maintenance, emergency, leasing, and handoff; a provider-neutral operations adapter (implemented locally in `db/`); idempotent business writes. | Checks property, resource references, workflow eligibility, and arguments before effects; returns structured outcomes. |
| `providers/` | Normalized provider interfaces and adapter composition. | Vendor formats, SDKs, and provider errors do not leak into business modules. |
| `providers/stt/` | Audio-to-transcript streams and partial/final/error normalization. | Partial text is not a durable command or authorization for a write. |
| `providers/tts/` | Reserved, empty. Planned: text-to-audio streams, cancellation, and stream completion/failure. | Audio generation completion is distinct from browser playback completion. |
| `providers/llm/` | One-shot structured model calls (routing proposals, pending-reply interpretation, optional phrasing choice) with timeouts and normalized errors. No streaming. | No provider-managed execution of backend tools. |
| `providers/embeddings/` | Reserved, empty. A possible future embedding abstraction. | Reserved; no embedding provider or retrieval system is required by V1. |
| `db/` | Async sessions, property/workflow/operational models, repository and local operations adapters, audit transactions and seeds; migrations in `backend/migrations/`. | Implements storage and authorized local mutations behind tool protocols; no routing or model interpretation. |
| `metrics/` | Reserved, empty. Timing and outcome diagnostics are currently structured log fields. | Telemetry is not the authoritative business ledger. |
| `core/` | Configuration, logging conventions, common types and errors. | Small shared primitives; no hidden service orchestration. |
| `backend/tests/` | Infrastructure tests and real PostgreSQL migration, constraint, seed, context and readiness tests. | Database tests use an isolated project/database, never the development volume. |
| `frontend/` | A single manual test page: microphone capture via LiveKit, control WebSocket, partial/final and response text display. No playback. | No authoritative workflow state or backend effects. |
| `evaluations/` | Phase 3 routing dataset, synthetic contexts, offline double, explicit live CLI and measured reports. | Expected labels and test-double algorithms never enter production routing. Transition/action/handoff evaluations remain future work. |
| `docs/`, `docs/decisions/` | Architecture, scope, contracts, failure behavior, and ADRs. | Conceptual specifications, not executable contracts. |
| `.github/workflows/` | CI: lockfile install, Ruff, format check, strict mypy, and the isolated `compose.test.yml` suite. | Validation only; no release or deployment automation. |

## Orchestration and dependency boundaries

The composition root wires interfaces to adapters and repositories. API and realtime modules receive browser input, supervise tasks, and submit turns to TurnOrchestrator. Property resolution gates the router. The router proposes an eligible service; the workflow layer checks transitions and required context and declares any ActionRequest. Workflow policy authorizes the operation, the tool validates its input and target context, and the local operations adapter executes it. The tool's verified committed result updates workflow/conversation state before the response reports success.

Provider dependencies flow through abstractions: STT is used by realtime speech, and the model provider by routing (with only the eligible catalog) and by the agent layer for reply interpretation and optional phrasing. TTS is planned for realtime presentation; embeddings have no consumer. Business modules do not import vendor SDKs. Phase 2 places the ORM-free repository protocol with its consumer in `property/`, implemented by `db/repository.py`. Persistence imports domain values but never invokes context orchestration. Phase 4 workflows and Phase 5 tools likewise own explicit ORM-free protocols implemented by database adapters.

Realtime owns scheduling and cancellation, workflows own business decisions, tools own external/business mutations, and repositories own storage mechanics. Conversational interpretation cannot bypass this chain.

## Property context gate

The demo resolves the property from the seeded resident chosen at session creation; the session's identity is persisted and cannot silently change. Caller identity is context, not proof of identity. A resident/contact lookup must remain within the selected property. A prospect may have no resident or unit record, represented explicitly; workflows requiring a unit must clarify before writing. Prospect onboarding is not implemented.

A context snapshot includes the property, resident and unit resolution status, and enabled property-service records with their configuration and active scenarios. It carries a diagnostic catalog fingerprint, not a durable configuration version. Missing property information prevents routing. Existing incidents keep their original property; they are never reassigned automatically.

Each turn loads a fresh context. Before any write, the tool boundary revalidates resource ownership and current service enablement, so a service disabled after the route was chosen is not executed under the old route.

## Data-driven routing cascade

See [ADR 006](decisions/006-routing-cascade.md). Phase 3 implements this sequence for independent utterances. Phase 4 validates the result and applies explicit downstream workflow transitions; it does not replace the router. Phase 8.1 adds pending-reply interpretation in the agent layer, before routing validation ([ADR 008](decisions/008-pending-reply-interpretation.md)):

1. Gate on resolved property and a nonempty, validated enabled catalog.
2. Consider explicit service requests, configured scenario cues, and workflow continuation/transition rules within that catalog.
3. If unresolved, use model-assisted interpretation constrained to the same eligible choices and an explicit abstain outcome.
4. Validate the proposed service against the catalog and workflow transition policy. Confidence is evidence for a decision, not permission for an action.
5. Clarify ambiguity or offer an enabled Human Handoff path. An unavailable service must be explained, never fabricated. If no valid service or handoff is available, communicate the limitation and end or use an independently configured contact fallback.

Phase 3 uses unique normalized whole-utterance configured matches, with overlap falling through to constrained interpretation. It uses no confidence probability and zero automatic model retries, with a default ten-second fallback deadline. Each outcome records its catalog fingerprint, reason and source. Semantic matching optimizations and calibration remain open. Hazard escalation is a validated route to another enabled service followed by a workflow transition; an unfinished safety-critical workflow is never downgraded automatically. The model cannot invent emergency policy.

Atlas enables separate Emergency Maintenance and Non-Emergency Maintenance services. Palm exposes Maintenance, Security, and Parking instead. Palm's Maintenance workflow may raise urgency internally; it must not route to Atlas's disabled/unavailable labels. The router code stays the same; catalog entries, scenarios, and workflow bindings differ.

## Workflow architecture

A workflow run tracks its property, service, step, collected fields, business status, related resources, and committed actions. The session execution state does not substitute for workflow state. Transitions are governed by configured service bindings and backend escalation policy, not free-form model text. Only implemented, allowlisted workflow/tool capabilities can be bound by configuration.

For the Atlas demo (`EM-1042` is an illustrative identifier; incidents use UUIDs), a maintenance workflow transitions to Emergency Maintenance when the new leak information warrants escalation. Preserve the run's service history and related resource references. The emergency create operation records EM-1042 once. Subsequent hazard information updates EM-1042. If a non-emergency request already exists in a later implementation, link its escalation to the incident rather than losing or duplicating the earlier record.

Human handoff is a structured tool action. It snapshots property, resident/contact, unit, current business service, incident, confirmed completed actions, and a concise summary of relevant conversation facts. Pending/unknown actions remain explicitly pending/unknown. A created handoff and an accepted human transfer are distinct outcomes; no response may imply an accepted transfer based on creation alone.

## Provider abstractions

Implemented protocols: `StreamingSTTProvider` (open, send audio, receive partial/final/error events, update context, close), `MediaAudioSource` for LiveKit input, and one-shot structured model protocols (`RoutingModelProvider`, `TurnInterpretationProvider`, `PhrasingProvider`) with timeouts and normalized errors. Model calls do not stream tokens. Adapter tests cover malformed output, refusals, timeouts and stream cleanup. Planned: a streaming TTS protocol emitting audio chunks with explicit completion and cancellation. Embeddings remain unspecified.

## Application state versus realtime execution state

| Application/conversation state | Realtime execution state |
| --- | --- |
| Property, resident/contact and unit references; context version | Connection handles and liveness |
| Final transcript messages; delivery status and conversation summary | Partial transcripts and audio buffers |
| Current service, workflow progress and escalation history | Turn/generation identifiers and active task handles |
| Tool operation identities, statuses and committed results | Cancellation tokens, pending playback and generation |
| Maintenance request, incident and handoff records | Current session execution state and latency clocks |

Application state is persisted as required for recovery; realtime state is reconstructed on reconnect. Session metadata can persist lifecycle checkpoints, but cannot serialize or restore live task handles. Generated text/audio is presentation; a record that it was generated is not proof it was heard. Response records should distinguish generated, partially played, fully played, and cancelled/unknown delivery.

## Barge-in and committed effects

Target design; not implemented. There is no TTS or playback yet (see [roadmap](roadmap.md)).

On detected user speech during SPEAKING, record USER_INTERRUPTED, transition through INTERRUPTED, stop browser playback/flush queued audio, cancel TTS, and cancel any now-unnecessary model generation. Advance the turn/generation identifier and reject late chunks or stale proposals. Capture the interrupting audio while cancellation is processed so the beginning of the new utterance is not lost, then listen and process its final transcript.

Cancellation targets presentation and unused computation. It does not roll back a successful tool transaction, clear an incident ID, or mark an acknowledged write as unexecuted. Writes in progress must finish or be reconciled by operation identity. A user interruption is never enough evidence to retry a create action. Rebuild the next turn from authoritative tool outcomes, even if no corresponding spoken confirmation reached the caller.

Write tools need stable operation identities scoped to the property and business operation. Retries of the same logical create reuse an idempotency key and return the recorded result. A new update to an existing incident uses its own operation key plus the incident reference. Concurrent duplicate attempts must converge through an atomic persistence constraint/transaction; an in-memory flag is insufficient. External effects cannot share a database transaction, so the future PMS boundary must support reconciliation and an explicit unknown/pending outcome. Do not promise exactly-once delivery across systems.

For EM-1042, the tool commit survives cancellation of speech about its creation. The next finalized hazard utterance requests an update referencing EM-1042. The handoff subsequently references that same committed incident and confirmed update.

## Observability and verification

Structured logs correlate sessions, turns, workflow runs and tool operations, with per-stage turn timings (context, routing, workflow, tools, rendering), routing outcomes and STT stream diagnostics. Speech-to-response and TTS first-audio latency are planned measurements; model calls are one-shot, so there is no first-token latency. Durable tool outcomes are authoritative if telemetry emission fails. Explicit failure events and normalized errors support diagnosis without logging raw audio or unrestricted resident information by default.

The Phase 0 verification plan required exercising both property taxonomies with the same router, unresolved context, disabled/stale service selection, escalation, barge-in before/during/after a tool commit, late provider output, duplicate creates, uncertain commits, reconnect, and handoff truthfulness. Phases 1–8.1 cover these except barge-in around a tool commit, which needs audio output. See [failure modes](failure-modes.md) and [verification records](verification/README.md).

## Phase 1 frozen foundation

Python 3.12, FastAPI, Uvicorn, uv/pyproject.toml, Pydantic v2/pydantic-settings, structlog, pytest/pytest-asyncio/HTTPX, Ruff, mypy, Docker and Compose are selected. PostgreSQL 17, SQLAlchemy 2.x async, asyncpg and Alembic persist six property tables, two application-state tables, five operational/audit tables and one text-turn table.

The factory composes infrastructure routes/core services. Lifespan owns and disposes the async engine. The outer ASGI middleware still covers error responses. `/ready` checks startup and bounded database connectivity; `/health` remains liveness-only. Routing event names are emitted as structured logs. Phase 7 implements the text realtime envelope/session states; the broader voice event taxonomy remains conceptual. There is no persisted realtime event ledger.

## Phase 2 property context

PropertyContextResolver accepts a resident UUID or property UUID plus unit number. SQLAlchemyPropertyRepository explicitly filters property ownership, enabled associations and active scenarios, then returns Pydantic values with copied JSON. The resolver imports no ORM classes or provider SDKs, and service keys remain data rather than an enum. Missing/inactive records fail explicitly. Unitless residents are supported; property/unit lookup does not infer a resident. Empty catalogs accurately reflect configuration and produce a controlled Phase 3 routing abstention.

Database.session owns one repeatable-read transaction, giving coherent reads across the resident, catalog and scenarios. Repositories flush writes without committing. Callers do not share sessions between concurrent tasks. Context values survive session closure. Durable configuration versioning/reload signaling remain future work; no revision token is fabricated in Phase 2.

Resident creation/unit reassignment validates property ownership before writing; a composite foreign key backs this without triggers. UUID4s are Python-generated and timestamps are timezone-aware UTC. Seeds/migrations are explicit commands. Seed inserts preserve existing custom configuration. Zoneinfo plus tzdata validates IANA property timezones on Windows and Unix.

Scenario ownership already matches Phase 0: each ServiceScenario belongs to a PropertyService. No superseding ADR is needed; ADRs 001–006 remain unchanged.

## Unresolved decisions

Recorded in Phase 0. Resolutions since then are noted inline.

| Decision | Required before |
| --- | --- |
| Browser/audio transport | Resolved in Phase 8: LiveKit (input only). |
| STT/TTS/LLM providers and normalized streaming/cancellation contracts | STT resolved in Phase 8 (AssemblyAI behind StreamingSTTProvider); models use an OpenAI-compatible structured-output adapter; TTS open. |
| External-provider idempotency and reconciliation | External writes; local repository/transaction/idempotency contracts are implemented through Phase 5. |
| Exact property resolution inputs and conflict/unknown-caller UX | First property-context flow. |
| Configuration representation, versioning/update mechanism, workflow definitions | Service configuration implementation. |
| Routing confidence calibration, thresholds, cascade precedence, clarification/retry bounds | Precedence resolved in Phase 3 (no confidence thresholds, no automatic retries); calibration pending a valid live benchmark. |
| Emergency escalation policy, approved language, and per-property contact procedures | Emergency workflow implementation. |
| Handoff destination, acknowledgment mechanism, and live transfer versus recorded callback behavior | Human handoff implementation. |
| Reconnect grace periods, retention/redaction policy, timeouts and telemetry sink | Persistent voice sessions and operational logging. |
| Exact delivery milestone tracking and latency/evaluation targets | Voice quality acceptance. |

These choices must preserve accepted ADR invariants. Phase 3 routing uses a provider-neutral one-shot interface directly under `providers/llm/`; no general agent orchestration is required. Backend-rendered clarification is documented in ADR 007. The catalog fingerprint is diagnostic, not a durable revision. Phase 4 adds downstream transition policy and optimistic session revisions. Phase 5 adds the separately authorized tool execution boundary without altering the routing cascade.

## Concrete Phase 4 composition

`WorkflowApplication` loads the conversation aggregate through `ConversationRepository`, calls
the pure `WorkflowEngine`, and persists accepted state through `SQLAlchemyConversationRepository`.
The caller owns the database transaction. The engine receives existing PropertyContext and
RoutingDecision values; no ORM or provider dependency enters the workflow package. The registry
only lists implemented capabilities, while routing remains open to database-configured services.

Sessions and workflow runs survive restart. Compare-and-swap revisions and normalized PostgreSQL
serialization conflicts prevent lost updates. Same-turn replay returns the saved decision without
another request. Typed action requests remain pending future inputs, and completed action lists
are constrained to empty. Transitions explicitly preserve compatible reported facts, supersede
unexecuted proposals, and keep the emergency business context separate from a handoff destination.

## Concrete Phase 5 composition

The internal caller explicitly submits a persisted ActionRequest to ToolExecutor. Its ToolStore
protocol is implemented by SQLAlchemyToolStore; the transaction exposes a provider-neutral
PropertyOperationsAdapter implemented by SQLAlchemyPropertyOperationsAdapter. Tools own policy
and execution coordination; the local adapter flushes operational rows. The store commits the
operation and audit receipt atomically, then verifies both from a fresh transaction before the
executor returns success. Session locks, unique action IDs and canonical hashes protect retries.
No automatic routing-to-workflow-to-tool-to-response orchestration is introduced.

ADR 006 is preserved: context gates routing, configured/explicit cues precede constrained model
interpretation, and service proposals must belong to the property's catalog. Phase 5 does not
change cascade order, prompts, thresholds, clarification or abstention. It adds a downstream
authorization boundary that rechecks current service configuration before a new write. The
database service catalog, workflow capabilities and trusted tool capabilities stay separate.
ADRs 001/005 remain enforced by fresh property context and ownership checks; ADR 003 is made
concrete by backend-only mutations and committed evidence. The provider-neutral protocol follows
ADR 002, and the durable ledger preserves ADR 004's separation from cancellable presentation.

Phase 5 left completed_actions empty. Phase 6 now consumes verified ToolResults to populate typed
completion records and generate responses. The tool ledger remains operational truth. Local
handoff and tour REQUESTED rows do not imply a phone transfer or calendar booking.

## Concrete Phase 6 composition

TurnOrchestrator coordinates existing context/routing/workflow/tool components and depends on
TurnStore for durable checkpoints. SQLAlchemyTurnStore implements it with agent_turns plus the
existing session/workflow repository. Final reconciled state and response replay commit together.
A PostgreSQL per-session transaction advisory guard spans the separate tool commits without
holding the session row lock needed by ToolExecutor. Revision checks remain authoritative.

ResponsePlan is the truth-to-language boundary. Only independently verified successful tool
receipts populate OperationFact. Critical clauses use deterministic templates; optional model
assistance chooses an approved non-operational phrasing index and cannot generate factual prose.
History is not used as business truth. Failure and UNKNOWN_OUTCOME remain distinct.

ADR 006's configured evidence still precedes interpretation. An internal persisted-prompt hint
now supports conservative confirmation/explicit-answer continuation, with catalog validation
and configured evidence taking precedence. Clarification/abstention skip workflows/tools.
Verified emergency updates preserve the incident across a changed hazard scenario. Dynamic
services remain routable without matching workflow/tool capabilities. See [Orchestration](orchestration.md)
for API, confirmation, recovery and the limits of the offline text interpretation path.

## Concrete Phase 7 composition

Lifespan creates RealtimeSessionManager with a trusted persisted-session lookup and the existing
TurnOrchestrator. A short membership lock covers bind reservations; turn execution never holds
a global lock. One connection per conversation is enforced in this process, and one turn per
connection is accepted at a time. The PostgreSQL guard remains authoritative across transports.

The WebSocket adapter owns IO tasks and the session owns its turn task. Disconnect cancels/awaits
execution, clears buffers and removes the manager reference. Committed effects persist. Reconnect
loads existing state and resubmits the original turn identity for Phase 6 receipt reconciliation
or saved-result replay. The output queue has bounded wait and explicit replay-required failure.
No migration, domain taxonomy, business tool, provider integration or audio state is added.

ADR 006 is invoked unchanged, including safe uncertainty and property-local catalog validation.
ADR 004 is concretized by ephemeral task ownership alongside durable checkpoints/receipts.
See [Realtime](realtime.md) for cancellation/commit semantics and remaining deployment decisions.

## Concrete Phase 8 composition

LiveKitAudioSource converts the bound caller microphone into neutral PCM AudioFrames.
AssemblyAIStreaming implements StreamingSTTProvider over the documented v3 WebSocket API.
SpeechPipeline owns both and submits only provider-final text to RealtimeSession. The existing
TurnOrchestrator, routing cascade, WorkflowEngine and ToolExecutor remain authoritative.
LiveKit AgentSession and the AssemblyAI Voice Agent API are not used.

Property vocabulary is allowlisted from trusted PropertyContext. The saved user-facing response
supplies bounded agent_context. Neither transcription nor context is operational truth. ADR 006
is reused unchanged, including catalog validation and safe uncertainty. Workflow capability
registration remains independent of dynamic service discovery. No migration is added.

Media/STT failure closes ephemeral resources while durable actions survive. Speech input is
implemented; TTS, playback and barge-in remain future decisions. Earlier unresolved transport/STT
choices above are now resolved to LiveKit and AssemblyAI for this phase. Public authorization,
distributed ownership, data retention and measured voice quality remain unresolved.
See [Speech](speech.md) and [Phase 8 verification](verification/phase-8-verification.md).

## Rejected patterns

These have been considered and are excluded by design:

- A global service enum or the workflow registry used as a whitelist of valid routes.
- Treating model output, transcribed words or client metadata as backend truth.
- Calling routing, workflows or tools from STT/TTS callbacks or provisional transcripts.
- Reporting success from a proposal, an uncommitted transaction, a queued event or an audio chunk.
- Retrying an unknown outcome blindly, or reusing an action identity with changed arguments.
- Reconstructing business state from chat history; leaking ORM or vendor SDK types into domain
  contracts.
- Unbounded queues, detached tasks, leaked billable streams, or locks that serialize all sessions.
- Conflating business replay with speech replay, or cancellation with rollback.
- Treating fake-provider tests as evidence of live speech or model quality.
- Replacing Relay's orchestration with a framework agent loop (LiveKit AgentSession, a hosted
  voice-agent API), which would move business authority into the provider.
