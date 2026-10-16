# Roadmap

## Phase 9 — speech output: COMPLETE / VALIDATED (v0.9.0)

Streaming TTS, LiveKit agent audio, browser playback, spoken-reference filtering, replay-safe
presentation and guarded speech admission are implemented and validated with a real microphone.
See [Phase 9 verification](verification/phase-9-verification.md).

## Phase 10 — barge-in / interruption: COMPLETE / VALIDATED (v0.10.0)

```text
Relay speaking → caller begins speaking → presentation interrupted (queued audio discarded,
  native LiveKit queue cleared, in-flight capture allowed to settle) → committed business state
  untouched → the caller's utterance becomes the next normal turn → speech output reusable
```

Validated with deterministic tests and two real-microphone gates, including a transactional run
in which a committed emergency incident survived the interruption of its spoken confirmation and
the interrupting hazard updated the same incident. See
[Phase 10 verification](verification/phase-10-verification.md).

## Later

- **Faster interruption (optional):** AssemblyAI reported speech about 1.4–1.5 s after onset in
  the successful live runs, and that provider latency dominates barge-in latency. Local
  voice-activity detection (in Relay or the browser) could react sooner at the cost of false
  barge-ins from noise or echo. It is not required by the v0.10.0 behavior.
- **Input admission hardening:** push-to-talk or voice-activity gating so open microphones do not
  turn background speech into application turns.
- **Measurement:** a paced live routing benchmark, a labelled reply-kind evaluation (ADR 008),
  end-to-end latency (provider final → text → first agent audio), recognition quality on recorded
  audio, repeated barge-in runs (one early run lost most of an interrupting sentence at the
  provider and has not recurred), and live observation of multi-final coalescing.
- **Deployment hardening:** authentication in front of demo, turn and voice endpoints; durable
  realtime ownership across workers; retention and access policy for transcripts; TTS provider
  retention settings where the plan allows.
