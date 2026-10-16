# Phase 10 verification: barge-in and transaction-safe interruption (2026-10-15 – 2026-10-16)

**Phase 10 is VALIDATED.** A caller who starts speaking while Relay talks now stops Relay's voice,
and the new utterance becomes the next normal turn. Interruption changes presentation state only:
in the transactional gate, a committed emergency incident survived the interruption of its spoken
confirmation and the interrupting hazard updated the same incident.

The first real-microphone attempt failed and is recorded below. It led to STT timing
instrumentation, which showed that barge-in latency is dominated by the provider's speech
detection rather than by Relay.

## Delivered

| Step | Commit | Content |
| --- | --- | --- |
| 2A | `5750abd` | Cooperative `SpeechOutput.interrupt()`, generation fencing, `USER_INTERRUPTED` |
| 2B | `8cba99c` | `RealtimeSession.user_speech_started()` policy; one speech start per provider turn |
| 3A | `9099913` | STT speech-start timing instrumentation (counts and timings only) |

## Design and deterministic validation

**Contract.** `SpeechPresentation.interrupt() -> bool` is synchronous and non-blocking. It returns
true when it newly targets an active or pending output. Affected outputs end `CANCELLED` with the
new code `USER_INTERRUPTED`; there is no new status or event type. Session teardown keeps using
`close()` (`SESSION_ENDED`).

**Cooperative settlement.** For the active output the supervisor cancels the TTS producer,
discards the Python-side frames, calls `MediaAudioSink.clear()` (LiveKit `clear_queue()`), waits
for an in-flight capture to return on its own, clears again to drop that last frame, and finishes
the record exactly once. The consumer checks for interruption after each queue read, so it never
starts another capture. An in-flight `capture_frame()` is never cancelled: Phase 9 showed that a
cancelled capture leaves the native source rejecting captures until its frame drains.

**Generation fencing.** Each presented job gets a generation. An interrupt affects only
generations that exist when it is called. The next output starts only after the interrupted one
has settled, and a native clear owed by an idle interrupt runs before any later output. A
pending output that existed at the interrupt ends at once and never reaches the TTS provider;
`SUPERSEDED` stays reserved for replacement by a newer response.

**Producer ownership.** An interrupted run refuses new frames even from a provider that ignores
cancellation. A producer slower than 2 s to stop is retired: it stays tracked, cannot enqueue, does
not delay the next output, and is cancelled by `close()`.

**Clear failures.** A failed clear is logged and settlement continues. If the in-flight capture
then times out or fails, the existing sink rules apply and output becomes unavailable
(`SPEECH_OUTPUT_TRANSPORT_DEGRADED`); the record still ends `USER_INTERRUPTED`.

**Speech-start wiring.** `SPEECH_STARTED` is the primary trigger and the first `PARTIAL` of a newer
provider turn the fallback. A repeat, late or stale signal for an already signalled or finalized
provider turn is ignored, so each provider turn produces at most one barge-in request; FINAL never
triggers one. `RealtimeSession.user_speech_started()` calls `interrupt()` before emitting
`USER_SPEECH_STARTED`, always when a presentation is attached (an idle output may owe a native
clear), isolates `interrupt()` exceptions, and never cancels `active_task`, calls `cancel()` or
changes the binding or results. Guarded final admission is unchanged.

| Deterministic evidence | Status |
| --- | --- |
| Blocked capture: `interrupt()` returns at once; capture not cancelled; clear, capture returns, second clear; one terminal `USER_INTERRUPTED`; sink usable; next output completes with no old-ID frames | PASSED |
| Fence race: B presented while A settles stays pending; both of A's clears precede B's first capture | PASSED |
| Pending-only, during synthesis, queued frames after synthesis, repeated interrupt, natural completion wins, interrupt wins, interrupt/close in both orders, idle clear, output after an idle interrupt | PASSED |
| Clear failure with and without a blocked capture; slow producer retired and unable to enqueue; no task leaks after close | PASSED |
| One barge-in per provider turn (SPEECH_STARTED + partials; partial fallback; late SPEECH_STARTED; next provider turn) | PASSED |
| Active business task never cancelled; failing `interrupt()` isolated; interrupt requested before a blocked outbound event; interrupting final admitted and next response spoken | PASSED |
| Mutation checks: cancelling the consumer, removing the enqueue refusal, removing dedupe, removing the partial fallback and emitting before interrupting each fail the suite | PASSED |

## Automated results

| Check | Result |
| --- | --- |
| Compose suite (PostgreSQL 17 + LiveKit server, fake providers) | **569 passed** (530 at v0.9.0; +17 in 2A, +16 in 2B, +6 in 3A) |
| Browser audio tests | 12/12 |
| Strict mypy | clean (150 files) |
| Ruff lint and format | clean |
| `git diff --check` | clean |

## STT timing instrumentation (3A)

Added after the failed Gate A. All logs carry counts and timings only, never transcript text:
`ASSEMBLYAI_RAW_SPEECH_STARTED` (provider `audio_timestamp_ms`, audio sent so far),
`ASSEMBLYAI_TURN_RECEIVED` (turn order, character/word counts, first/last word offsets, whether a
speech start is held), `ASSEMBLYAI_SPEECH_START_RELEASED` (hold duration) and
`STT_PROVIDER_EVENT_RECEIVED` (each provider event with admission and dedupe state). Adapter and
pipeline behavior are unchanged. `held_speech_start_ms` slightly underestimates the hold because
its clock starts after the raw-arrival log line is written; time since stream open gives the true
figure.

## Gate A, first attempt: FAIL (session `f18665f8-b8be-4658-97be-581c2f678f3a`)

Script: ask "What services can you help me with?", then interrupt with "Actually, tell me about
leasing instead."

- **Human report:** the old voice did not stop promptly, and the transcript held only "Instead.".
- **Provider:** the only final for the interrupting turn was "Instead."; Relay forwards finals
  verbatim, and no final was dropped, duplicated or aborted. The interruption signal arrived
  4.58 s after the output started, together with the first partial of the new provider turn.
- **Output mechanics worked:** output `c1ac088f-…` (4,800 ms accepted) ended `CANCELLED` /
  `USER_INTERRUPTED` 17 ms after the request, with 25 frames discarded, 2 clears, 0 clear failures
  and output still available. The next output completed on the same sink.
- **Classification at the time:** multiple factors (late speech detection and provider transcript
  loss). The run had no raw speech-start timing, which motivated step 3A.

## Gate A, instrumented rerun: PASS (session `f28431cc-abae-43d1-b144-320df797ec8a`)

- **Human report:** quick audible cutoff; the browser showed the complete "Actually, tell me about
  leasing instead."; the next response was heard.
- **Speech start:** provider `audio_timestamp_ms` 11,520 with 13,000 ms of audio already sent, so
  the provider reported speech about 1,480 ms after the onset it describes. The raw SpeechStarted
  arrived 7 ms before the first Turn, and Relay's adapter added at most about 8 ms.
- **Interruption:** release to interrupt request about 2 ms; request to terminal `USER_INTERRUPTED`
  about 9 ms. Output `fb74ce1c-…` (5,320 ms accepted, 266 frames): 25 frames discarded, 2 clears,
  0 failures, no producer timeout, output available.
- **Transcript:** partial word counts 1 → 1 → 4 → 6, final 6 words / 40 characters, matching the
  browser; one provider turn, one barge-in request (SPEECH_STARTED as primary trigger).
- **Next output** `f6a1bb83-…` COMPLETED (277 frames, 5,526 ms, 0 stale, 0 underruns) on the same
  sink; the turn routed to Leasing.
- **Conclusion:** the adapter's hold is not a meaningful latency source; provider speech detection
  (which in this setup coincides with first-word recognition) dominates. Emitting the raw
  SpeechStarted earlier would save at most about 8 ms.

The first attempt's transcript loss did not recur. A single pass does not establish how often it
happens.

## Gate B, transactional hero: PASS (session `0bc0f967-5bbe-4834-8523-6f13a58288ff`)

| # | Caller | Outcome |
| --- | --- | --- |
| 1 | My AC isn't cooling properly. | Non-Emergency Maintenance (configured); asks symptoms |
| 2 | My AC is only blowing warm air. | WORKFLOW_ANSWER; asks to confirm a request |
| 3 | Actually, water is pouring out of it. | Escalated to Emergency Maintenance `active_flooding` |
| 4 | Living room AC. | WORKFLOW_ANSWER; stayed on Emergency |
| 5 | No, nothing electrical. | WORKFLOW_ANSWER; asks severity |
| 6 | It is continuous. | `CREATE_EMERGENCY_INCIDENT` |
| 7 | *(interrupting the confirmation)* Wait, the water is getting close to an electrical outlet. | Emergency `electrical_hazard`; `UPDATE_EMERGENCY_INCIDENT` |

**Create before interruption.** Action `6e3d1259-3814-574d-acee-43d295877609` SUCCEEDED (attempts
1, replays 0) and created incident `dc04a2d1-70e8-4e97-8faa-18d2992c47c6` at revision 1. The
incident row existed about 2.86 s before the barge-in, and the authoritative Turn-6 result was
persisted, then `AGENT_RESPONSE` and `TURN_COMPLETED` were sent before speech began.

**Interrupted confirmation.** Output `85f8cb89-8cdc-49bd-9f50-2c1b4d78196c`, previously
`OUTPUTTING`, 2,740 ms accepted (137 frames): 25 frames discarded, 2 clears, 0 failures, no
producer timeout, terminal `CANCELLED` / `USER_INTERRUPTED` about 13 ms after the interrupt
request. No capture was cancelled and the transport was not degraded.

**Interrupting turn.** Provider lag about 1,418 ms; hold about 1 ms; partial word counts
1 → 2 → 6 → 9, final 10 words / 57 characters; admitted after the grace (about 402 ms) as the
exact sentence. No duplicate or stale suppression, no abort, no `SESSION_BUSY`.

**Update in place.** Action `4278d8aa-918b-5c2a-a53a-6b8225ff3b34` SUCCEEDED (attempts 1,
replays 0) on the same incident, revision 1 → 2. `hazard_detail` changed from "No, nothing
electrical." to the new sentence; `location_detail` ("Living room AC.") and `severity_detail`
("It is continuous.") were preserved; status stayed OPEN.

**Database invariants.**

- Exactly one emergency incident in the session; 0 maintenance requests, 0 handoffs, 0 leasing
  tours.
- Exactly two tool executions with distinct action IDs, request hashes and idempotency keys; no
  `UNKNOWN_OUTCOME` and no reconciliation retry.
- No rollback: the incident kept its create and moved forward to revision 2; the create receipt
  is unchanged; Turn 6 is persisted and COMPLETED with its original text; no turn was cancelled;
  the emergency workflow completed with both actions recorded.
- Every row belongs to Atlas Residence (property `ee29db28-…`, resident `1dc89ac6-…`, unit
  `00ab9e88-…`); none of the 15 Palm Heights identifiers appears in checkpoints, workflow state,
  tool payloads or the incident.

**Next output.** `d222df3c-c8d2-4311-a546-4a629a84ca74` COMPLETED (286 frames, about 5,712 ms,
0 stale, 0 underruns) on the same `SpeechOutput` and LiveKit sink.

**Input health.** 7 provider finals → 7 provisional → 7 admitted (all `GRACE`, single segment);
24 partials; 0 duplicate or stale finals; 0 aborts; 0 `SESSION_BUSY`; 0 cancelled turns; 0 event
errors; no background turns. Each provider turn produced exactly one barge-in request; six found
the previous output already completed and only cleared the sink. No live multi-final coalescing
occurred.

**Human evidence.** Confirmation voice stopped quickly: YES. Full electrical-outlet sentence
preserved in the browser: YES. Updated response heard: YES.

## Latency observations (single runs, not benchmarks)

- Provider speech detection: about 1.4–1.5 s after the provider-reported onset in both
  successful runs; it dominates barge-in latency.
- Relay adapter hold: ≤ about 8 ms. Signal to interrupt request: about 1–2 ms.
- Interrupt request to `USER_INTERRUPTED`: 9–17 ms across the three live interruptions.

## Known limitations

- Barge-in reacts only as fast as the STT provider reports speech; local voice-activity detection
  is not implemented.
- The first Gate A attempt lost most of the interrupting sentence at the provider; it did not
  recur, but its cause was not isolated and its frequency is unknown.
- Live coalescing of two provider finals into one turn is still not observed.
- An always-on microphone admits background speech; Relay's own voice reaching the microphone
  (no headphones) could interrupt it.
- Validated on a local stack; demo endpoints are unauthenticated and realtime ownership is
  single-process. Not production-ready.
