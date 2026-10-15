# Phase 9 verification: speech output and guarded speech admission (2026-10-14 – 2026-10-15)

**Phase 9 is VALIDATED.** Relay now speaks: trusted responses are synthesized by ElevenLabs,
published on a LiveKit agent track and played in the browser, while text stays authoritative. Live
validation exposed a speech-input segmentation defect (one sentence split into two turns); it was
fixed with tuned provider endpointing plus a Relay-side guarded admission grace, and the final
9-turn real-microphone flagship passed.

One item is explicitly **not** live-validated: two provider finals coalescing into one application
turn. It is covered by deterministic tests; two controlled live attempts did not produce a provider
split (see "Guarded final admission").

## Delivered

| Step | Commit | Content |
| --- | --- | --- |
| 9B.1 | `e592ea7` | `LiveKitMediaSession` extracted; one backend participant shared by input and output |
| 9B.2 | `54dff90` | Provider-neutral TTS contracts (`StreamingTTSProvider`, `SpeechRequest`, `TTSFailure`), PCM framer, TTS settings |
| 9B.3 | `447d62d` | `SpeechOutput`: one active + one pending output, 25-frame (500 ms) bounded buffer, lifecycle records |
| 9B.4 | `ee9fcf0` | `LiveKitAudioSink` (24 kHz mono, 200 ms native queue), narrow grants, caller-only subscription |
| 9B.5 | `6533c0d` | ElevenLabs HTTP streaming adapter (raw httpx, `pcm_24000`, safe failures) |
| 9B.6 | `3c5a007` | Explicit live TTS smoke runner |
| 9B.7 | `14af68b` | Runtime wiring, spoken-reference filter, replay rule, audio lifecycle events, browser playback, frontend CI |
| A | `3cb5a64` | AssemblyAI endpointing 128/1280 → 800/2000 ms |
| B | `4c14a80` | Guarded final admission: 400 ms grace, coalescing, fail-closed aborts |

## Evidence levels

| Level | Result |
| --- | --- |
| Automated suite (PostgreSQL 17 + LiveKit server in Docker, fake providers) | 530 passed; strict mypy (150 files), Ruff and format clean; 12 browser audio tests |
| Real ElevenLabs streaming (smoke runner) | PASS: 86,936 PCM bytes, framed byte-exactly |
| Real LiveKit output (isolated server, caller client) | PASS: agent track received, ~1 s of tone delivered |
| Gate A: real microphone → real TTS → browser | PASS, human audibility confirmed |
| Final 9-turn flagship | PASS, all invariants held |
| Live provider-split coalescing | NOT YET OBSERVED |

## Real TTS (step 9B.6)

Four single requests were made, each deliberately without retry:

| Attempt | Result | Cause |
| --- | --- | --- |
| 1, 2 | `TTS_UNAVAILABLE` in < 1 s | API key lacked the Text to Speech permission |
| 3 | `TTS_TIMEOUT` (5 s, no bytes) | Account state before an upgrade |
| 4 | **SUCCESS** | — |

Successful request (`eleven_flash_v2_5`, `pcm_24000`, 30-character sentence): first audio byte
1,949 ms (cold connection and a newly designed voice), stream 2,021 ms, 86,936 bytes = 43,468
samples = 1.811 s at 24 kHz, 72 chunks (91–1,390 bytes), 90 full 20 ms frames + a 536-byte
remainder, framed bytes equal to input, RMS 6,457, peak 29,769. Raw PCM carries no channel
metadata; the 24 kHz mono interpretation framed exactly and produced a plausible duration.

## LiveKit output transport (step 9B.4)

The isolated-server test publishes deterministic PCM through `SpeechOutput` → `LiveKitAudioSink`
and receives it as a caller. Across six runs: 50 frames (1,000 ms) accepted locally; the caller
received a track named `relay-agent-audio` from `relay-server-{session}` with 24,480–24,960 voiced
samples at 24 kHz mono and RMS ≈ 5,200–5,500 (Opus is lossy, so evidence is energy and duration,
not byte equality); outbound bytes ≈ 13.7–14.0 k. A foreign participant with default grants never
received the track, and closing output left microphone input working on the shared room.

`AudioSource.capture_frame()` findings (SDK 1.1.20), relevant to Phase 10: capture blocks once the
200 ms native queue is full (≈ 20.3 ms per frame); cancelling a blocked capture returns
immediately, but the native source keeps the in-flight frame and rejects further captures with
`InvalidState` until it drains. `clear_queue()` releases a blocked capture in ≈ 15 ms and leaves
the source usable. The sink therefore marks itself unusable after an interrupted capture, and
interruption must use stop-feeding plus `clear_queue()`, not task cancellation.

## Gate A: minimal real end-to-end voice (session `97dd5978-…`)

"What services can you help me with?" → one final, one turn (`1da1b647-…`) → routed to General
Information (no workflow; 0 tool calls) → event order `AGENT_RESPONSE` (11), `TURN_COMPLETED` (12),
`AGENT_AUDIO_OUTPUT_STARTED` (14), `AGENT_AUDIO_OUTPUT_ENDED` (15). Speech output COMPLETED,
6,548 ms accepted; first TTS audio 362 ms and first frame accepted by LiveKit 365 ms after
scheduling. Browser timeline (local clock only): track subscribed +878 ms, attached +879 ms,
element playing +1,294 ms, server STARTED received +13,127 ms, agent speaking +13,378 ms, ENDED
received +19,472 ms. Human audibility confirmed. No key, voice ID or provider body appeared in the
backend log or lifecycle events.

## The Turn 7 defect and Step A (endpointing)

First flagship attempt (session `a8732757-…`, endpointing 128/1280 ms). The caller intended one
sentence, "Wait, the water is getting close through an electrical outlet.", with a natural pause:

- AssemblyAI emitted a final for "Wait, the water is getting close." (end of turn); about 1.0 s
  later it reported new speech and emitted a second final, "through an electrical outlet."
- Relay admitted every provider final immediately, so the session (idle by then) accepted both:
  two turns, two `UPDATE_EMERGENCY_INCIDENT` requests with distinct action IDs and hashes, incident
  revision 1 → 2 → 3, and a hazard field holding only the second fragment.
- Idempotency, routing and duplicate protection behaved correctly; the defect was provider
  endpointing plus immediate admission.

Step A raised AssemblyAI turn detection to `min_turn_silence=800`, `max_turn_silence=2000` (both
documented for `universal-3-6-pro`; `end_of_turn_confidence_threshold` is not documented for this
model and is not sent). Live check (session `1246f02c-…`): the same sentence with a ≈ 1 s pause
produced **one** provider final and one turn.

## Guarded final admission (Step B)

`SpeechPipeline` holds each provider final provisionally for `STT_FINAL_ADMISSION_GRACE_MS`
(400 ms). Resumed speech (provider-neutral `SPEECH_STARTED` or the first partial of a newer provider
turn) keeps it provisional and coalesces the next final (one-space join, first segment's identity).
A provisional final never reaches `RealtimeSession`. Admission fails closed when the utterance may
be incomplete: `CONTINUATION_TIMEOUT` (10 s with no continuation activity),
`ADMISSION_LIMIT_EXCEEDED` (8 segments / 8,000 characters), `PROVIDER_FAILURE`,
`INPUT_ENDED_DURING_CONTINUATION` and `SESSION_END` abort the whole utterance; only a clean input end
with no continuation admits it.

| Evidence | Status |
| --- | --- |
| Deterministic coalescing (18 admission tests) | PASSED |
| Fail-closed admission | PASSED |
| Live single-final grace path | PASSED (admitted after 388–418 ms, one turn each) |
| Live output/audibility regression | PASSED |
| Live 2 provider finals → 1 turn | NOT YET OBSERVED |

Two controlled live attempts with aggressive provider settings (sessions `767ea43f-…` at
128/300 ms and `baeefb9a-…` at 128/128 ms, ≈ 0.5 s pause) each produced a single provider final.
Both turns abstained with `MODEL_UNAVAILABLE`, later traced to an HTTP 402 from the routing provider
(account credit); a subsequent health check returned HTTP 200 and routed correctly.

## Final flagship (session `6c48c707-…`, 800/2000 ms endpointing, 400 ms grace)

Every turn: 1 provider final, 1 provisional, 1 admitted `STT_FINAL` (reason `GRACE`, 1 segment,
399–418 ms), `replayed=false`, speech output COMPLETED.

| # | Transcript | Outcome | Tool |
| --- | --- | --- | --- |
| 1 | My AC isn't cooling properly. | Non-Emergency Maintenance (configured); asks symptoms | — |
| 2 | My AC is only blowing warm air. | WORKFLOW_ANSWER; asks to confirm a request | — |
| 3 | Actually, water is spouting out of it. | Escalated to Emergency Maintenance `active_flooding` | — |
| 4 | Living room AC. | WORKFLOW_ANSWER; model view `non_emergency_maintenance`, route pinned to Emergency | — |
| 5 | No, nothing electrical. | WORKFLOW_ANSWER; asks severity | — |
| 6 | It is continuous. | WORKFLOW_ANSWER | `CREATE_EMERGENCY_INCIDENT` (rev 1) |
| 7 | Wait, the water is getting close through an electrical outlet. | Emergency `electrical_hazard` | `UPDATE_EMERGENCY_INCIDENT` (rev 2) |
| 8 | I want to speak with someone. | Human Handoff; asks to confirm | — |
| 9 | Yes, please. | Confirmed | `CREATE_HUMAN_HANDOFF` |

Turn 7 did not repeat the defect: one provider final, one provisional, no continuation, one admitted
final, one turn, one update. The incident's hazard field holds the full sentence.

### Database invariants

- Exactly 1 emergency incident (`a2d4777a-…`), OPEN, revision 2, updated in place by Turn 7.
- 0 maintenance requests; 1 human handoff (`d9f934d0-…`), REQUESTED, linked to the incident,
  `transfer_performed=false`.
- 3 tool calls, all SUCCEEDED, attempt 1, replay 0; 3 distinct action IDs and request hashes.
- All rows belong to Atlas Residence; 0 of 15 Palm Heights IDs in checkpoints or tool payloads.
- All effects present after disconnect.

### Speech, references and input

- 9 outputs scheduled, 9 COMPLETED (0 failed, cancelled or superseded); 0 stale frames; 1 brief
  underrun (Turn 5) that recovered.
- Reference-bearing texts (Turns 6, 7, 9) kept the full UUID; the spoken text produced by
  `spoken_text()` ends "The reference is available in the chat." with no UUID.
- Input totals: 27 partials, 9 provider finals, 9 admitted, 0 duplicates, 0 stale finals,
  0 aborts, 0 `SESSION_BUSY`, 0 STT failures, no background turns.
- Clean disconnect: 0 cancelled turns, 0 undelivered events, 0 active sessions.

## Latency observations (single runs, not benchmarks)

- Admission: provider endpointing waits ≥ 800 ms of silence, then the grace adds ≈ 400 ms.
- Final to turn start after admission: 20–30 ms.
- Model-routed turns: 1.3–2.6 s; configured or deterministic turns: 0.1–0.2 s.
- First TTS audio after scheduling: ≈ 240–253 ms warm, 650 ms for the first (cold) output.

## Failure semantics verified

TTS failure leaves text and business results intact; a sink failure disables output without
affecting input or text; exact business replays are never re-spoken; audio lifecycle events carry
only IDs, provider, model, status, safe code and accepted milliseconds; a provisional speech
boundary can never commit a business effect.

## Known limitations

- Live multi-final coalescing not yet observed.
- No barge-in: the caller can speak over Relay; that is Phase 10.
- Always-on microphone admits background speech; input admission hardening is later work.
- One backend process owns realtime connections; demo endpoints are unauthenticated.
- Response text is sent to ElevenLabs under standard retention.
- Browser `element_playing` is per track, not per utterance; per-utterance evidence is LiveKit
  active-speaker state and human listening.
