# Event model

Phase 7 implements the versioned text WebSocket protocol in [Realtime](realtime.md). Its actual
envelope is `schema_version`, `event_id`, `session_id`, per-connection `sequence`, `event_type`,
timezone-aware `timestamp`, nullable `turn_id`, and a discriminated typed `payload`. Server
emissions get fresh event UUIDs, including durable turn replays. The `timestamp` name concretizes
the earlier conceptual `occurred_at`; sequences reset on reconnect. No event table is added.

Implemented client events: SESSION_START, CLIENT_TEXT_TURN, CLIENT_CANCEL_TURN, PING, SESSION_END.
Implemented server events: SESSION_STARTED, SESSION_STATE_CHANGED, TURN_STARTED, AGENT_RESPONSE,
TURN_COMPLETED, TURN_CANCELLED, ERROR, PONG, SESSION_ENDED. TURN_COMPLETED includes COMPLETED or
UNRESOLVED application status; it is not a tool-success claim. Transport delivery is separate
from durable outcome, and event IDs never substitute for turn/action idempotency identities.

The richer voice taxonomy below remains the Phase 0 design, not the Phase 7 wire schema.
Property/workflow/tool authority fields from that telemetry design are not client input.

Phase 4 emits workflow lifecycle concepts through structured logs, with no event table. See
[Workflows](workflows.md#events-and-verification) for concrete metadata and transaction semantics.
WORKFLOW_COMPLETED currently represents a completed informational intent only; no operational
success or external handoff acknowledgment is emitted. Requested actions remain distinct from
executed actions. Phase 5/6 subsequently implement verified tool execution and workflow completion.
The broader envelope and voice events below remain conceptual beyond the subset above.

Events are conceptual contracts for future implementation. They describe facts and outcomes; they do not authorize writes. Business records and durable tool outcomes remain authoritative even if telemetry delivery fails.

## Envelope

| Field | Meaning |
| --- | --- |
| `event_id` | Unique event identity, stable across redelivery. |
| `event_type`, `schema_version` | Taxonomy name and version of the envelope/payload contract. |
| `occurred_at` | UTC timestamp; not a substitute for monotonic duration measurement. |
| `session_id`, `sequence` | Session identity and session-local emission order; sequence is not a global clock. |
| `turn_id`, `generation_id` | Nullable identifiers for the user turn and cancellable output generation. |
| `property_id`, `context_version` | Nullable before context resolution; attached once known. |
| `workflow_run_id`, `operation_id` | Nullable workflow and logical tool-operation correlation. |
| `trace_id`, `causation_id` | Trace correlation and triggering event identity when available. |
| `source` | Module emitting the fact. |
| `payload` | Event-specific structured data, including normalized error details where applicable. |

Failures carry an error code, stage, retryability and known/unknown outcome, with a sanitized explanation. Tool payloads distinguish definite rejection/failure from an uncertain remote or commit outcome. Idempotency keys belong to logical operations, not event IDs. Retries get attempt IDs while retaining the operation identity.

## Complete taxonomy

| Event | Producer | Meaning / key payload |
| --- | --- | --- |
| SESSION_STARTED | realtime | Accepted session; transport and start reason. |
| SESSION_ENDED | realtime | Finalized session; end reason and pending-operation references. |
| PROPERTY_CONTEXT_LOADING | property | Context resolution begun; resolution source. |
| PROPERTY_CONTEXT_LOADED | property | Valid snapshot; property, resolution statuses, enabled property-service IDs and version. |
| PROPERTY_CONTEXT_FAILED | property | Missing/conflicting context or catalog load failure; routing is blocked. |
| USER_SPEECH_STARTED | realtime | Speech detected; input segment reference. |
| USER_SPEECH_ENDED | realtime | Input segment ended; timing. |
| STT_PARTIAL | providers/stt | Provisional transcript segment; not an executable command. |
| STT_FINAL | providers/stt | Final transcript message/segment reference and language if known. |
| STT_FAILED | providers/stt | Transcription failure/disconnect; segment and error. |
| ROUTING_STARTED | routing | Eligible catalog/version fixed for this attempt. |
| SERVICE_ROUTED | routing | Validated property-service, decision source and confidence evidence. |
| ROUTING_FALLBACK | routing | Clarification, abstention, unavailable-service or enabled-handoff decision with reason. |
| ROUTING_FAILED | routing | Technical failure or invalid selection; no valid route accepted. |
| WORKFLOW_STARTED | workflows | Run and initial property-service established. |
| WORKFLOW_STEP_CHANGED | workflows | Prior/new step, accepted service transition where relevant, and reason. |
| WORKFLOW_ESCALATED | workflows | Urgency/service escalation accepted; prior/new service and policy reason. |
| WORKFLOW_COMPLETED | workflows | Business outcome finalized; related resource references. |
| LLM_STARTED | providers/llm | Reasoning/generation attempt begun; purpose and generation ID. |
| LLM_FIRST_TOKEN | providers/llm | First output token observed; elapsed time. |
| LLM_COMPLETED | providers/llm | Generation ended; finish reason and usage if available. |
| LLM_CANCELLED | providers/llm | Cancellation acknowledged; reason and elapsed cancellation time. |
| LLM_FAILED | providers/llm | Generation failed; normalized error. |
| TOOL_STARTED | tools | Operation attempt accepted for validation/execution; tool and attempt ID. |
| TOOL_SUCCEEDED | tools | Confirmed durable result; operation, business resource and replay indicator. |
| TOOL_FAILED | tools | Validation/execution failure or unknown outcome; error and reconciliation requirement. |
| TTS_STARTED | providers/tts | Speech generation stream begun; response reference. |
| TTS_FIRST_AUDIO | providers/tts | First audio available; elapsed time, not proof of audible playback. |
| TTS_COMPLETED | providers/tts | Audio generation finished; playback may still be queued. |
| TTS_CANCELLED | providers/tts | Speech generation cancelled; reason and generation ID. |
| TTS_FAILED | providers/tts | Speech generation failed; normalized error. |
| USER_INTERRUPTED | realtime | Barge-in detected; interrupted generation and last playback position if known. |
| HANDOFF_CREATED | tools | Handoff record committed; destination and structured context references. |
| SESSION_ERROR | realtime/core | Session-level failure or recovery condition; stage, error and recovery decision. |

Producer names identify logical ownership; the implementation may publish normalized adapter events through a shared event emitter. Playback acknowledgment details, handoff acceptance, and operation reconciliation may require additional versioned events in a future phase. HANDOFF_CREATED alone does not mean a human accepted the conversation.

## Ordering and delivery

PROPERTY_CONTEXT_LOADED must precede ROUTING_STARTED for the same context version. SERVICE_ROUTED references that eligible catalog. TOOL_SUCCEEDED is emitted only after confirmed durable success. A duplicate delivery of that event must not cause a duplicate tool action.

Provider streams can interleave. Cancellation can race with normal completion: an already completed LLM/TTS attempt need not emit a second terminal event. One attempt has one terminal outcome; late chunks are discarded by generation ID. USER_INTERRUPTED still records the interruption even if synthesis already finished but playback remains active. A later operation reconciliation is a separate attempt/outcome record, not a rewrite of history.

No global exactly-once event guarantee is assumed. Consumers deduplicate by event ID and use correlation/sequence information to reconstruct timelines. A future durable publication/reconciliation strategy must close commit-versus-event delivery gaps. Raw audio, full tool arguments, and resident contact data are excluded from default telemetry; references and sanitized summaries are preferred.

## Example payloads

Illustrative IDs, timestamps, and versions below are not persisted fixtures. The first example shows the full envelope; the remaining examples show event-specific payloads that use that envelope in future implementation.

```json
{
  "event_id": "evt-context-1",
  "event_type": "PROPERTY_CONTEXT_LOADED",
  "schema_version": 1,
  "occurred_at": "2026-10-07T12:00:00Z",
  "session_id": "session-demo",
  "sequence": 2,
  "turn_id": null,
  "generation_id": null,
  "property_id": "atlas",
  "context_version": "atlas-v1",
  "workflow_run_id": null,
  "operation_id": null,
  "trace_id": "trace-demo",
  "causation_id": "evt-context-loading-1",
  "source": "property",
  "payload": {
    "resident_ref": "resident-demo",
    "unit_ref": "unit-204",
    "contact_status": "resolved",
    "unit_status": "resolved",
    "enabled_property_service_ids": [
      "atlas-emergency-maintenance", "atlas-non-emergency-maintenance",
      "atlas-emergency-non-maintenance", "atlas-leasing", "atlas-renewal",
      "atlas-resident-support", "atlas-general-information", "atlas-human-handoff"
    ]
  }
}
```

SERVICE_ROUTED payload:

```json
{
  "property_service_id": "atlas-emergency-maintenance",
  "catalog_version": "atlas-v1",
  "decision_source": "workflow_escalation",
  "confidence": null,
  "reason_code": "active_water_leak"
}
```

TOOL_SUCCEEDED payload for the incident creation:

```json
{
  "tool_name": "create_emergency_incident",
  "operation_id": "op-create-incident-1",
  "attempt_id": "attempt-1",
  "outcome": "committed",
  "resource_type": "emergency_incident",
  "resource_id": "EM-1042",
  "idempotent_replay": false
}
```

USER_INTERRUPTED payload:

```json
{
  "interrupted_generation_id": "generation-3",
  "next_turn_id": "turn-4",
  "cancel_targets": ["playback", "tts", "unnecessary_llm_generation"],
  "retained_operation_ids": ["op-create-incident-1"]
}
```

TOOL_FAILED payload for an uncertain write outcome:

```json
{
  "tool_name": "update_emergency_incident",
  "operation_id": "op-update-incident-1",
  "attempt_id": "attempt-1",
  "resource_id": "EM-1042",
  "error": {
    "code": "COMMIT_ACK_TIMEOUT",
    "stage": "persistence",
    "retryable": false,
    "outcome": "unknown"
  },
  "reconciliation_required": true
}
```

HANDOFF_CREATED payload uses references to a restricted business record rather than duplicating all resident data in telemetry:

```json
{
  "handoff_id": "handoff-demo",
  "property_ref": "atlas",
  "resident_ref": "resident-demo",
  "unit_ref": "unit-204",
  "current_property_service_id": "atlas-emergency-maintenance",
  "incident_ref": "EM-1042",
  "completed_operation_ids": ["op-create-incident-1", "op-update-incident-1"],
  "summary_ref": "handoff-demo/summary",
  "status": "created",
  "transfer_accepted": false
}
```

The last example assumes the update has subsequently been confirmed; an unresolved update must not appear in completed operations.

## Implemented Phase 8 additions

The version-1 server union additionally includes STT_PARTIAL, STT_FINAL, STT_FAILED,
USER_SPEECH_STARTED and USER_SPEECH_ENDED. These are server-only: clients cannot submit them.
Partials carry speech_turn_id, transcript, revision and is_final=false and may be dropped.
Finals carry speech/application IDs, transcript, provider/model, optional language and
end_of_turn=true. Accepted finals also carry original turn_number/expected_revision for replay.
No recognition confidence is invented. All events except STT_PARTIAL remain critical.

USER_SPEECH_STARTED forwards provider SpeechStarted timing once a turn identity is available;
USER_SPEECH_ENDED denotes provider final endpoint, not exact acoustic stop. STT_FAILED contains
a safe code and reconnect/repeat instruction. Stream/turn identity suppresses duplicate finals;
transport event identity is still not business idempotency. The broader conceptual taxonomy
above remains future design outside these implemented subsets. See [Speech](speech.md).
