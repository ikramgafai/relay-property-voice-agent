# Failure modes

Written in Phase 0 as behavioral requirements. Implemented since: missing context gating, STT
disconnect (STT_FAILED, fresh stream, no partial promotion), model timeout and invalid output
(abstain or clarify, no automatic retry), invalid tool arguments, database failure and
UNKNOWN_OUTCOME reconciliation, duplicate actions, control/media disconnect with durable
reconnect, and ambiguous routing (clarify or abstain; no clarification budget yet). Not
implemented: TTS failure and barge-in, because there is no audio output; external
PMS reconciliation, because there is no external PMS. Event names below are conceptual; the
implemented wire events are listed in [realtime](realtime.md) and [speech](speech.md).

All retries must be bounded. Preserve confirmed business outcomes and avoid claiming success for
pending or unknown operations.

| Failure | Expected behavior | Events/evidence |
| --- | --- | --- |
| Missing property context | Block routing and service tools. Ask for property selection/clarification; reload context. Do not substitute a default property's catalog. If unresolved, explain the limitation and end or use an independently configured contact fallback. | PROPERTY_CONTEXT_FAILED, optionally SESSION_ERROR; no ROUTING_STARTED until a valid snapshot exists. |
| STT disconnect | Stop interpreting partial transcripts as actionable input. Attempt bounded stream recovery, tell the user via an available channel, and ask them to repeat uncertain audio. Preserve finalized turns and committed actions. End explicitly if input cannot recover. | STT_FAILED and recovery/termination reason. |
| LLM timeout | Cancel stale generation and reject late proposals. Keep workflow and confirmed tool state. Use a deterministic clarification/failure message or an enabled handoff path; retry reasoning only within a defined budget. Never replay tools just because wording timed out. | LLM_FAILED with timeout, SESSION_ERROR when session recovery is affected. |
| TTS failure | Stop broken output; expose the confirmed result through future browser text/status where available and allow repeat/retry of presentation. Do not rerun the business action. End explicitly if no communication channel remains. | TTS_FAILED with response/generation reference; delivery status remains partial/failed. |
| Invalid tool arguments | Tool boundary rejects malformed, cross-property, stale-service or workflow-ineligible arguments before effects. Return structured validation errors; clarify missing fields or permit a bounded corrected proposal. | TOOL_STARTED then TOOL_FAILED with validation code and definite no-effect outcome. |
| Database failure | Do not announce success. Roll back a known failed local transaction. If commit acknowledgment is lost, mark the outcome unknown and query by operation identity before retrying. Block conflicting work until reconciled. | TOOL_FAILED or SESSION_ERROR with known/unknown outcome; preserve operation identity in surviving durable records where available. |
| Duplicate action | Same logical operation/key and matching arguments returns its existing result; concurrent attempts converge atomically. Conflicting arguments under the same key fail. A new fact updates the existing incident with a new update-operation identity. | TOOL_SUCCEEDED with replay indicator, or TOOL_FAILED for key conflict; no second incident. |
| Browser disconnect | Cancel playback and unnecessary generation; stop new requests. Preserve durable session/workflow/tool state, track in-flight writes and permit bounded reconnect/reconciliation if configured. On expiry, finalize the session; do not infer tool failure from lost transport. | SESSION_ERROR with disconnect reason, cancellation events as applicable, SESSION_ENDED on finalization. |
| Ambiguous routing | Abstain and ask a targeted clarification using only enabled services; never choose an unavailable service. After a bounded number of unsuccessful clarifications, offer enabled Human Handoff or explain the limitation. | ROUTING_FALLBACK with ambiguity evidence; ROUTING_FAILED reserved for technical/invalid-result failure. |

## Cross-cutting cases

- A property catalog load error or empty catalog is a context-gate failure, not permission to use a global service list. A disabled service discovered before a write requires refresh and a new eligible decision.
- Model output naming an ineligible service is rejected even when its stated confidence is high. Backend validation is independent of provider output quality.
- Barge-in is expected control flow, not a tool failure. Stop presentation promptly; retain committed results and operation references. A late provider chunk must not restart old speech.
- Unknown external PMS outcomes require reconciliation using the stable operation identity; blind retries can duplicate actions. A write must not start unless its logical identity can be tracked durably enough for the eventual recovery design.
- Handoff record creation failure leaves the agent responsible for communicating the failure. Successful record creation without destination acceptance is reported as a recorded request, not an accepted transfer.
- An unavailable urgent-service route uses configured emergency/contact guidance and eligible escalation options; it does not invent a service or claim an incident was created. Exact emergency language and destination rules remain unresolved.
- Observability sink failure must not reverse business success. Surface degraded telemetry through a surviving channel and use durable operation records for reconciliation. Log redacted error details rather than unrestricted resident data.

## Future verification scenarios

Exercise failures before a write, during an uncertain commit, after commit but before speech, and during interrupted playback. Verify that the Atlas demo yields one EM-1042 record plus its update, Palm never routes into Atlas-only service labels, and a handoff lists only confirmed completed actions. These checks belong to future tests/evaluations; Phase 0 contains no executable application tests.
