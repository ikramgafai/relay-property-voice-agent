# Contracts and terms

Domain contracts are Pydantic models that reject unknown fields. ORM classes and vendor SDK types
never appear in them. Internal action and capability enums exist; they are not a service
taxonomy. Paths are relative to `backend/app/`.

## Core contracts

| Contract | Meaning | Source |
| --- | --- | --- |
| PropertyContext | Property, optional resident/unit, and enabled service associations with effective labels, configuration and active scenarios | `property/schemas.py` |
| RoutingDecision | ROUTED, CLARIFICATION_REQUIRED or ABSTAINED; validated selection, evidence, method, reason, catalog fingerprint, timings | `routing/schemas.py` |
| ModelRoutingResponse | Untrusted structured model proposal; validated against the supplied catalog before use | `routing/model.py` |
| TurnInterpretationRequest / Response | One advisory model call for a pending workflow question: reply kind plus the model's routing view of the utterance alone | `agent/reply_schemas.py` |
| TurnInterpretation | Checkpointed outcome (WORKFLOW_ANSWER, NEW_INTENT, AMBIGUOUS, CANCEL), basis, reason, pending field and validated proposal; reused on replay | `agent/reply_schemas.py` |
| WorkflowDecision | ASK_USER, WAIT_FOR_CONFIRMATION, REQUEST_ACTION, COMPLETE, CANCEL, AWAIT_ROUTING or UNSUPPORTED_WORKFLOW | `workflows/schemas.py` |
| ConversationState / WorkflowState | Durable identity, revision, runs, collected facts with provenance, transitions, proposals, completed references | `workflows/schemas.py` |
| ActionRequest | Stable action UUID, action type, discriminated payload, confirmation requirement, source workflow and turn. A declaration, not an execution | `workflows/schemas.py` |
| ToolResult | Execution and action IDs, status, operation UUID and typed output when verified, safe error, replay and timing metadata | `tools/schemas.py` |
| AgentTurnRequest | Session/turn UUID, sequential turn number, expected revision, bounded utterance; no client-supplied property or action authority | `agent/schemas.py` |
| AgentTurnResult | Route, interpretation, workflow and tool results, ResponsePlan, text, revision, COMPLETED/UNRESOLVED, replay flag. COMPLETED is not tool success | `agent/schemas.py` |
| ResponsePlan | Intent, display context, question, explicit operation status and a verified OperationFact when authorized | `agent/schemas.py`, `agent/response_plan.py` |
| RealtimeSession | Ephemeral connection IDs, state, owned tasks and locks, output queue, speech attachment, last trusted response | `realtime/session.py` |
| AudioFrame / AudioFormat | PCM bytes, sequence, sample rate, mono, `pcm_s16le` | `realtime/media/protocol.py` |
| STTEvent | SPEECH_STARTED, PARTIAL, FINAL or ERROR with stream UUID, turn order, transcript, provider/model | `providers/stt/schemas.py` |
| SpeechContext | Allowlisted keyterms plus the last trusted response text | `providers/stt/schemas.py` |
| SpeechRequest | Speech output ID, trusted presentation text (≤ 4,000 chars) and optional language tag; no business objects | `providers/tts/schemas.py` |
| StreamingTTSProvider | Declared PCM format plus a cancellable async stream of raw PCM bytes; raises safe `TTSFailure` | `providers/tts/protocol.py` |
| MediaAudioSink | Outbound audio: connect, capture with backpressure, clear (interruption seam), close | `realtime/media/protocol.py` |
| SpeechPresentation / PresentationEvent | What a session may ask of speech output, and the STARTED/ENDED lifecycle it receives back | `realtime/presentation.py` |
| SpeechOutputRecord | Ephemeral per-presentation diagnostics: IDs, timestamps, status, safe code, counters; never text | `realtime/output.py` |

The realtime wire protocol is defined in `realtime/schemas.py` and described in
[realtime](realtime.md).

## Terms

| Term | Meaning in Relay |
| --- | --- |
| PropertyService | A property's association with a service definition, with its own labels, configuration and scenarios |
| Scenario | A configured example situation inside a property service, used as routing evidence |
| Workflow capability | An implemented deterministic process a service can bind to. Configuration can bind capabilities but cannot create them |
| Safety-critical capability | A capability (emergency maintenance) whose unfinished run is never left automatically for a routine workflow |
| Tool receipt | The durable `tool_calls` record used to verify a committed operation |
| Idempotency | Retrying the same logical request, identified by action ID and canonical request hash, does not repeat its effect |
| UNKNOWN_OUTCOME | An effect that can be neither confirmed nor ruled out; reconciled before anything else is attempted |
| Reconciliation | Recovering truthful status from durable receipts and operation rows |
| Checkpoint | Persisted turn progress (route, interpretation, dispatch started) used for safe recovery |
| Revision / CAS | Session version checked on every conditional update to prevent lost writes |
| Advisory guard | PostgreSQL per-session transaction lock around one orchestrated turn |
| Business replay | Returning the original durable result for an exactly repeated request |
| Control plane | WebSocket commands, events and trusted response text |
| Media plane | LiveKit audio transport, with no business authority |
| Partial / final | Revisable display transcript / provider end-of-turn transcript eligible to become a turn |
| Backpressure | Explicit bounded behavior when a consumer is slower than its producer |
