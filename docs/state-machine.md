# Session state machine

Phase 7 implements the text-only subset below. The broader voice execution state machine later
in this document remains conceptual. Durable workflow/tool state is independent and survives
execution interruption or termination.

## Implemented Phase 7 subset

CONNECTED → IDLE → PROCESSING_TURN → IDLE. All live states can enter ENDING → ENDED on
disconnect, client end, shutdown or fatal transport/session failure. Recoverable turn errors
and accepted task cancellation return to IDLE. Each connection is a new ephemeral lifecycle.

IDLE is text-input readiness, not audio LISTENING. PROCESSING_TURN wraps the authoritative
TurnOrchestrator without inventing voice or tool timing events. No SPEAKING, INTERRUPTED or
HANDED_OFF is emitted: a local handoff request does not prove a live transfer. Future states
can refine this enum without moving business state into the realtime session.

SESSION_END ends the connection only. Reconnect binds the existing active durable conversation
and loads its revision/turn number; exact turn replay retains the original request fields.
The connection sequence resets and old transport events never restore business truth.
See [Realtime](realtime.md) for task ownership, cancellation, backpressure and safe close behavior.

Phase 4 implements the separate application/workflow state in [Workflows](workflows.md): ACTIVE,
WAITING_FOR_USER, WAITING_FOR_CONFIRMATION, ACTION_REQUIRED, COMPLETED, CANCELLED and ESCALATED.
ACTION_REQUIRED is a request, never evidence of execution. None of the realtime states below
were implemented by Phase 4.

## States

| State | Meaning |
| --- | --- |
| CONNECTED | Transport accepted; initialize context and listening resources. |
| LISTENING | Capture user audio/await a final transcript; property resolution may still be pending. |
| THINKING | Resolve/clarify context, interpret a final turn, route when eligible, and prepare the next action. |
| EXECUTING_TOOL | Supervise a validated backend action or reconcile its outcome. |
| SPEAKING | Deliver a response; generation and playback can overlap. |
| INTERRUPTED | Cancel obsolete presentation and switch input ownership to the new turn. |
| HANDED_OFF | Handoff destination has acknowledged ownership, or a configured handoff completion policy has explicitly completed. |
| ENDING | Stop accepting new work, close transport/presentation and preserve outstanding operation identities. |
| ENDED | Terminal session state; no new turns or session actions. |

## Valid transitions

Unlisted transitions are invalid. Repeated partial transcript/provider events need not change state.

| From | To | Trigger/guard |
| --- | --- | --- |
| CONNECTED | LISTENING | Input resources ready; routing still gated independently on property context. |
| CONNECTED | ENDING | Setup failure or disconnect. |
| LISTENING | THINKING | Final transcript or explicit context selection ready to process. |
| LISTENING | ENDING | User ends session, disconnect expires, or unrecoverable input failure. |
| THINKING | EXECUTING_TOOL | Workflow authorizes an action; context, service and arguments pass backend checks. |
| THINKING | SPEAKING | Clarification, informational response, fallback or confirmed result requires no new tool. |
| THINKING | LISTENING | New speech supersedes uncommitted reasoning or bounded recovery awaits new input. |
| THINKING | ENDING | End request, fatal failure or unrecoverable disconnect. |
| EXECUTING_TOOL | THINKING | Outcome known; prepare next step or reason about explicit failure. |
| EXECUTING_TOOL | SPEAKING | Confirmed result or truthful failure/pending explanation ready. |
| EXECUTING_TOOL | LISTENING | New input is accepted while the durable operation remains tracked independently. |
| EXECUTING_TOOL | HANDED_OFF | Handoff creation and the configured acknowledgment/completion condition both confirmed. |
| EXECUTING_TOOL | ENDING | Session ends; in-flight writes remain tracked for reconciliation. |
| SPEAKING | LISTENING | Playback drains or speech fails and input remains available. TTS_COMPLETED alone is insufficient if audio is queued. |
| SPEAKING | INTERRUPTED | New user speech detected; stop obsolete output. |
| SPEAKING | HANDED_OFF | Final handoff presentation ends and the completion condition is confirmed. |
| SPEAKING | ENDING | Final farewell playback completes, termination request, fatal failure or disconnect. |
| INTERRUPTED | LISTENING | Cancellation requested/obsolete output invalidated; preserve incoming audio and committed state. |
| INTERRUPTED | ENDING | User/session terminates during interruption recovery. |
| HANDED_OFF | ENDING | Agent yields ownership and closes its part of the session. |
| ENDING | ENDED | Local cleanup complete; pending operation references durably retained. |

Reconnect within an eventual bounded grace period keeps the same session identity only after context and operation-state reconciliation; it does not resurrect ENDED. After termination, any new session is a new lifecycle. Grace-period duration and resume transport protocol remain open.

## Normal turn lifecycle

```text
CONNECTED → LISTENING → THINKING → EXECUTING_TOOL → SPEAKING → LISTENING
```

PROPERTY_CONTEXT_LOADED must precede ROUTING_STARTED. Context failure may produce a clarification while THINKING, but it cannot produce a service route or service write. Turns without actions skip EXECUTING_TOOL. Multi-step turns can return from EXECUTING_TOOL to THINKING before another authorized action. Completion events describe business results separately from session execution transitions.

## Interruption lifecycle

```text
SPEAKING → INTERRUPTED → LISTENING → THINKING
```

USER_SPEECH_STARTED triggers USER_INTERRUPTED during SPEAKING. Cancel TTS/playback and unnecessary model generation, invalidate late output by generation ID, and preserve the incoming speech segment. Do not wait for a remote cancellation acknowledgment before suppressing stale playback. A completed generation may have nothing left to cancel even though its queued audio must be stopped.

Committed EM-1042 remains in application state. The next finalized utterance can authorize an update to that incident. If speech arrives during EXECUTING_TOOL, listen while tracking the operation separately; do not cancel a write merely because the primary session state changes. Conflicting subsequent writes wait for a confirmed/reconciled prior outcome.

## Handoff lifecycle

```text
LISTENING → THINKING → EXECUTING_TOOL → SPEAKING → HANDED_OFF → ENDING → ENDED
```

This path assumes the configured destination acknowledgment/completion condition has been met before entering HANDED_OFF. The handoff tool first commits structured context and emits HANDOFF_CREATED/TOOL_SUCCEEDED. Until completion is confirmed, the session can return to LISTENING, explain that a request was recorded, or explicitly end without claiming a live transfer. Record creation alone does not force HANDED_OFF. Failed handoff attempts return to a truthful failure/fallback response and preserve the existing incident.

## Termination lifecycle

Every nonterminal state can reach ENDING through the transitions above. ENDING stops accepting turns, suppresses queued speech, requests cancellable-task cleanup, and persists end reason and unresolved operation references. ENDED emits SESSION_ENDED once logically; telemetry retries reuse its event identity. Outstanding business operations may be reconciled after the live session ends and do not reopen it. A dropped browser cannot erase an already committed write.

## Phase 8 speech input and execution state

The concrete execution states remain CONNECTED, IDLE, PROCESSING_TURN, ENDING and ENDED.
IDLE is application-input availability, not a claim that an audio track or provider is ready.
A provider-final transcript uses the same transition as a text turn. Partials and provider
speech-start/end signals do not execute business transitions. Busy finals receive SESSION_BUSY.

Media/STT lifecycle is a separately owned attachment. Media loss closes recognition while an
accepted application turn may finish; control-session end closes the attachment and applies
existing turn cancellation/reconciliation. Reconnect uses durable state. No SPEAKING, playback
or barge-in state is implemented. See [Speech](speech.md) for endpoint and cleanup semantics.
