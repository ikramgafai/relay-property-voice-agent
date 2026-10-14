# Roadmap

Nothing in this document is implemented. It records the next planned work and the constraints
that work must preserve.

## Streaming TTS and LiveKit agent output

```text
AgentTurnResult → trusted response text → StreamingTTSProvider → provider adapter
  → PCM audio → LiveKit agent output track
```

Planned shape: a vendor-neutral TTS protocol with a real adapter and a deterministic fake,
explicit PCM output format, owned and bounded synthesis/publication tasks, and a scoped LiveKit
output track. The first candidate provider is ElevenLabs Flash v2.5, subject to a review of the
current official streaming API, PCM formats, sample rates and completion/error semantics.

Today the backend token can subscribe but not publish, and the caller token can publish its
microphone but not subscribe. Agent audio requires revising exactly those grants, not
broadening them.

Constraints:

- TTS is presentation. It cannot establish business truth, reroute, advance a workflow or
  call ToolExecutor.
- Only trusted Relay response text is synthesized. FAILED stays a failure and UNKNOWN_OUTCOME
  stays uncertain when spoken.
- TTS failure cannot undo a committed operation. Trusted text remains observable when audio
  fails.
- Synthesized, queued, transported and played audio are distinct states. No playback
  acknowledgment is invented.
- Audio buffering is bounded.
- Business-turn replay does not replay speech. Presentation gets its own explicit repeat
  policy; a replayed AgentTurnResult is not permission to speak again.
- Presentation resources carry an output identity so they can be cancelled later.
- No audio buffers or task handles are persisted without a demonstrated durable requirement.

## Barge-in and cancellation

```text
agent speaking → caller starts speaking → cancel synthesis/publication
  → drop unplayed audio → process the new final turn
```

Cancellation stops presentation and unneeded computation; it is not a rollback. Committed
operations are preserved, late chunks from a cancelled generation are rejected by output
identity, and the start of the interrupting utterance is retained while cancellation runs.
The architecture's [barge-in section](architecture.md#barge-in-and-committed-effects) describes
the target semantics.

## Measurement

- A paced or paid live routing benchmark on the 146-case dataset plus a held-out set, with
  attention to hazard and human-request misses.
- A labelled reply-kind evaluation for ADR 008 (answer versus new request versus ambiguous).
- End-to-end latency: provider final → response text, and later → first agent audio.
- Recognition and endpointing quality on a recorded audio corpus, including noise.

## Deployment hardening

- Authentication and authorization in front of the demo session, turn and voice endpoints.
- Durable realtime ownership across multiple workers (today one process owns connections).
- Input admission policy (push-to-talk or voice-activity gating) so ambient speech does not
  become application turns.
- Retention, access and encryption policy for transcripts and operational records.
