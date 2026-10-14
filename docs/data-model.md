# Property data model and future entities

Phase 2 implements properties, services, property_services, service_scenarios, units and residents. Phase 4 adds sessions and workflow_runs. Phase 5 adds tool_calls, maintenance_requests, emergency_incidents, leasing_tours and handoffs. Phase 6 adds agent_turns. Separate messages and general events remain conceptual. Alembic's version table is migration bookkeeping, not a Relay domain entity.

Phase 3 adds the existing scenario UUID to the ORM-free context projection for routing validation. It adds no database tables or migrations; routing diagnostics use logs and evaluation artifacts, not routing_events persistence.

## Concrete Phase 2 schema

Every implemented table has Python-generated UUID4 IDs and timezone-aware created_at/updated_at. Application values and database sessions use UTC. Server defaults cover inserts; SQLAlchemy-managed updates refresh updated_at. Raw administrative SQL updates must set updated_at explicitly. No extensions, triggers, soft deletes, service enums or vectors are introduced.

| Table | Fields and integrity |
| --- | --- |
| properties | Unique indexed slug, name, IANA timezone, active. Domain validation checks timezone on seeds/context construction. |
| services | Unique dynamic key, name, description. |
| property_services | Property/service FKs, unique pair, enabled, nullable display_name/description_override, priority, JSONB configuration constrained to object. |
| service_scenarios | Property-service FK, unique (property_service_id, key), key, name, description, JSONB examples array, active. Domain values validate example strings. |
| units | Property FK, unique (property_id, number), nullable external_id, number, active; unique (id, property_id) supports ownership integrity. |
| residents | Property FK; nullable composite (unit_id, property_id) FK to units; external_id, first_name, last_name, phone, email, role, preferred_language, active. Unique (property_id, external_id) supports stable seed/import identity; multiple null external IDs remain allowed. |

The composite FK rejects cross-property resident/unit links even when repository validation is bypassed. Null unit is valid. Deletions restrict referenced rows by default; no administrative deletion API is added.

Migration `0001_property_domain` explicitly creates the six tables, indexes and constraints, with a reverse-order downgrade. Integration tests check empty → upgrade → downgrade → upgrade and ORM/migration parity. Application startup never creates schemas.

PropertyContext includes PropertySummary, optional ResidentSummary/UnitSummary and EnabledServiceContext values with property_service_id, key, effective name/description, priority, copied configuration and active scenarios. No ORM instances cross the contract. One current unique property/service association exists; configuration history/version columns remain deferred.

## Concrete Phase 4 schema

Migration `0002_workflow_state` adds only `sessions` and `workflow_runs`, with a reverse-order
downgrade that preserves all six property tables. Sessions require an already resolved property;
the unresolved pre-routing session remains a future transport concern.

| Table | Fields and integrity |
| --- | --- |
| sessions | UUID session ID; property/resident FKs; composite unit/property FK; positive revision; JSONB application snapshot; aware timestamps; unique (id, property_id). |
| workflow_runs | Deterministic UUID5 run ID; composite session/property FK; property-service FK; capability key/status; JSONB typed workflow state; aware start/update timestamps. |

The adapter validates resident and service ownership before saving. Session identity cannot change
through updates. A conditional session revision update and all run upserts share one transaction.
JSONB snapshots are validated against ORM-independent domain contracts on reload; the session
snapshot excludes runs, which are loaded from workflow_runs in start-turn order. Its last-decision
copy is an immutable command replay receipt. No messages or hidden reasoning are needed.
See [Workflows](workflows.md) for concurrency, replay, transition and request identity policies.

## Concrete Phase 5 schema

Migration `0003_operational_tools` adds five tables and supporting resident/property and
workflow/session/property unique constraints. It preserves earlier tables and reverses cleanly
to Phase 4. All operational IDs use UUID4; no MAX(id)+1 or display-number allocator exists.

| Table | Concrete fields and integrity |
| --- | --- |
| tool_calls | Unique action_request_id and idempotency_key; source session/workflow/property/resident/unit; action_type; canonical request_hash; validated request JSON; explicit status; attempt/replay counts; typed result JSON; optional operation ID; safe error; start/completion/duration and entity timestamps. Checks constrain status, counts and success/operation-ID coherence. |
| maintenance_requests | Unique source action UUID; description, symptom details, unit number, NORMAL priority and OPEN status. |
| emergency_incidents | Unique source action UUID and workflow UUID; description, location/hazard/severity, unit number, optional scenario key, revision and OPEN status. Updates keep the same incident and original create source. |
| leasing_tours | Unique source action UUID; required aware requested_at, contact detail, REQUESTED status. |
| handoffs | Unique source action UUID; reason, structured context, optional incident UUID, REQUESTED status. No transfer status is inferred. |

Each table has composite foreign keys for session/property, workflow/session/property,
resident/property and unit/property. Each operation additionally references the matching tool
call's action/session/workflow/property. Handoff incident/property is also constrained. FKs
restrict deletion; no cascading destruction is introduced. The local adapter sets the minimal
domain statuses; lifecycle administration is not implemented.

The tool call and local operation share one atomic transaction. The audit record is a logical
latest-result receipt with attempt/replay counters, not a complete attempt-event history.
Separate update calls refer to the original incident through operation_id/result JSON and
validated input. The adapter validates exact property/session/workflow ownership before updating.
The executor verifies the matching committed operation before returning success. See
[Tools](tools.md) for locking, reconciliation and external-provider limitations.

## Concrete Phase 6 schema

Migration `0004_agent_turns` adds only agent_turns. Its UUID is the transport turn ID; it has
aware entity timestamps, session/property composite FK, positive turn_number, unique
(session_id, turn_number), finished flag and validated checkpoint JSONB. Working checkpoints
retain the request, route, workflow decision and dispatch/result stage. Completed records retain
one final typed result/response and compact the working copies. They do not snapshot the complete
PropertyContext or ConversationState. User text and assistant text supply transcript/audit history
without another messages table. Business state never depends on replaying transcript text.

Session JSON adds an active flag (default true for historical snapshots). Workflow JSON adds
typed completed action metadata (action/execution/operation IDs, type, completion turn), a
COMPLETED request disposition and unresolved action references. No prior migration is edited.
Final completion reconciliation and the agent turn result share a transaction. Operational rows
still commit exclusively through Phase 5. See [Orchestration](orchestration.md) for crash gaps,
concurrency/revision handling, durable replay and retention limits.

## Entities

| Entity | Conceptual information and responsibility |
| --- | --- |
| `properties` | Property identity, display name, configuration version, and scoped knowledge/contact configuration. |
| `services` | Reusable service identity, label, description, and supported capability/workflow binding metadata. No property is assumed to enable all services. |
| `property_services` | Property-to-service association, enabled flag, property-specific description/settings, workflow binding and configuration version. |
| `service_scenarios` | Property-service-scoped examples, routing cues and escalation guidance; may derive from reusable templates but become scoped before routing. |
| `residents` | Property-scoped resident/contact reference, name, role and contact context. Prospect sessions need not have a resident. |
| `units` | Property-scoped unit identity and display label. |
| `sessions` | Conversation identity, resolved property reference, optional resident/unit, context version, lifecycle checkpoints and timestamps. Property can be unresolved only before the gate succeeds. |
| `messages` | Session/turn, speaker, finalized transcript or response, delivery status, timestamps; generated speech is not evidence of a business action. |
| `workflow_runs` | Session/property, current property-service, step, business status, collected context, transition history and linked business resources. |
| `tool_calls` | Workflow/session context, tool name, logical operation/idempotency identity, validated arguments reference, attempt history, status, committed result or normalized error. |
| `maintenance_requests` | Property, optional/required-by-workflow unit/contact, issue details, status, workflow and originating tool operation. |
| `emergency_incidents` | Property/unit/contact, severity/hazard facts, status, workflow, originating operation, updates and optional source maintenance request. |
| `handoffs` | Session/workflow/property, resident/contact, unit, current business service, incident, completed actions, summary, destination and creation/acceptance status. |
| `routing_events` | Session/turn, context/catalog version, eligible catalog reference, decision source, chosen property-service or abstention, confidence evidence and fallback reason. |
| `latency_events` | Session/turn/operation, measured stage, monotonic duration and observation timestamp. |

## Conceptual ER relationships

```text
Property 1 ── * PropertyService * ── 1 Service
PropertyService 1 ── * ServiceScenario
Property 1 ── * Unit
Property 1 ── * Resident
Property 1 ── * Session (session property optional until resolved)
Session 1 ── * Message
Session 1 ── * WorkflowRun
WorkflowRun * ── 1 PropertyService (current service)
WorkflowRun 1 ── * ToolCall
WorkflowRun 1 ── * MaintenanceRequest / EmergencyIncident / Handoff
MaintenanceRequest 0..1 ── 0..* EmergencyIncident (optional escalation source)
Session 1 ── * RoutingEvent / LatencyEvent
```

Relationships involving future entities remain conceptual. A session can have zero workflow runs and multiple sequential runs. In Phase 4 a service/scenario transition creates a new run and preserves the old run and explicit transition history. General provider latency events need no workflow or tool reference. A routing decision can abstain and therefore have no chosen service.

For the demo, a resident may reference one current unit, and a unit may have multiple residents; residents/prospects may have no known unit. Historical occupancy and multi-unit residency modeling remain future refinements. Sessions and business records retain the relevant contact/unit snapshot rather than depending on later changes to residency.

## Property-service relationship

**Property → PropertyService → Service** is the central relationship. A service exists globally without being available everywhere. Only enabled `property_services` for the resolved property enter a routing snapshot. Property-specific scenarios and workflow bindings are loaded from those associations.

Atlas has eight services and Palm six. General Information, Leasing and Human Handoff share canonical definitions in the seeds but retain distinct property-service associations and scenarios. Clean seed counts: 2 properties, 11 services, 14 property-services, 33 scenarios, 2 units and 2 residents. Examples/descriptions are data only; no classification is performed.

## Future integrity and consistency requirements

- Maintain one effective association per property/service per configuration version; disabled associations are ineligible for routing and execution.
- All resident, unit, property-service, workflow and business-resource references in an operation must belong to the resolved property. Validate at backend boundaries and enforce storage integrity where practical.
- A session cannot route while property/catalog resolution is pending or failed. Resident/unit nullability must be explicit rather than filled with fabricated identifiers.
- Handoffs preserve a factual snapshot and references to authoritative business records. They distinguish creation from destination acknowledgment.
- Record the logical write operation, result, and affected business state atomically for local writes. Concurrent retries with the same key return the same result; conflicting arguments under the same key fail explicitly.
- Updating EM-1042 is a new operation targeting that incident, not a replay of its create. Interruption must not remove tool results or resource links.
- External PMS effects may be pending or of unknown outcome; retain operation identity and reconcile before retrying. A failed response is not proof that the remote effect failed.
- Tool attempts, service transitions, and incident updates require traceable history. The precise history schema, key types, constraints, transaction mechanism, and retention policy remain undecided.

Realtime task handles, cancellation tokens, raw audio buffers, and queued speech are not business entities. Persisted execution checkpoints are metadata, not resumable in-process tasks.
