# Roadmap

## Phase 9 — speech output: COMPLETE / VALIDATED (v0.9.0)

Streaming TTS, LiveKit agent audio, browser playback, spoken-reference filtering, replay-safe
presentation and guarded speech admission are implemented and validated with a real microphone.
See [Phase 9 verification](verification/phase-9-verification.md). Live coalescing of two provider
finals into one turn is covered by deterministic tests but has not yet been observed live.

## Phase 10 — barge-in / interruption (next)

```text
Relay speaking → caller begins speaking → stop feeding new speech audio
  → clear queued LiveKit audio → process the new user turn
```

Invariants:

- Already committed business effects remain committed. Stopping speech is not a rollback.
- Interrupt by stopping the frame feed and calling `clear_queue()` on the LiveKit `AudioSource`.
  Do **not** rely on cancelling a task blocked in `capture_frame()`: the native source keeps the
  in-flight frame and rejects captures with `InvalidState` until it drains (Phase 9 finding).
- Late frames from a cancelled presentation are rejected by `speech_output_id`.
- The start of the interrupting utterance is retained while presentation stops.
- Interruption must not be confused with business replay; the interrupted turn's text stays
  delivered.

## Later

- **Input admission hardening:** push-to-talk or voice-activity gating so open microphones do not
  turn background speech into application turns.
- **Measurement:** a paced live routing benchmark, a labelled reply-kind evaluation (ADR 008),
  end-to-end latency (provider final → text → first agent audio), recognition quality on recorded
  audio, and live observation of multi-final coalescing.
- **Deployment hardening:** authentication in front of demo, turn and voice endpoints; durable
  realtime ownership across workers; retention and access policy for transcripts; TTS provider
  retention settings where the plan allows.
