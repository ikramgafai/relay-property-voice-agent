# Speech input and output

LiveKit transports caller and agent audio. AssemblyAI transcribes. ElevenLabs synthesizes trusted
response text. Relay owns session and turn admission, property resolution, routing, workflows,
tools and the authoritative text response. No voice-agent framework, barge-in or telephony is
installed. Speech input arrived in Phase 8; speech output and guarded admission in Phase 9 (see
"Speech output" below).

## Boundaries and official API review

```text
Browser microphone → LiveKit room → LiveKitAudioSource → typed PCM AudioFrame
  → StreamingSTTProvider → provider-final text → RealtimeSession
  → existing TurnOrchestrator → verified backend result → AGENT_RESPONSE text
```

The media protocol and STT protocol contain no vendor SDK types. Routing, workflow and tool
contracts are unchanged. ADR 006 remains the existing cascade: trusted PropertyContext first,
configured evidence and continuation policy, constrained interpretation, catalog/transition
validation, then clarification or abstention when evidence is insufficient. Recognition is
input, never service or action authority. The workflow registry remains separate from the
database service catalog; an unimplemented dynamic service can still be routed safely.

Official APIs were reviewed on 2026-10-13 before implementation:

| Component | Selected version / reference |
| --- | --- |
| LiveKit Python RTC | Locked `livekit==1.1.20`; [AudioStream API](https://docs.livekit.io/reference/python/livekit/rtc/audio_stream.html). |
| LiveKit server API | Locked `livekit-api==1.2.1`, protocol dependency `1.1.27`. |
| Isolated LiveKit server | [v1.13.9](https://github.com/livekit/livekit/releases/tag/v1.13.9); [local server procedure](https://docs.livekit.io/transport/self-hosting/local/). |
| Browser demo | [livekit-client v2.22.4](https://github.com/livekit/client-sdk-js/releases/tag/v2.22.4), explicit CDN import. |
| AssemblyAI | [Streaming WebSocket API](https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket), `wss://streaming.assemblyai.com/v3/ws`, model `universal-3-6-pro`. |
| Streaming client | Existing locked `websockets==17.2`. [AssemblyAI Python SDK](https://github.com/AssemblyAI/assemblyai-python-sdk) 1.6.1 was inspected but is not installed: its asynchronous audio writer uses an unbounded queue. The documented raw WebSocket protocol keeps Relay's sends bounded. |

The provider choice implements the requested separation, not a quality benchmark. SDK and
provider knowledge stays inside adapters; another implementation can satisfy the same protocols.

## Admission and audio

Start the existing internal control WebSocket and send SESSION_START. Then POST an empty JSON
object to `/api/voice/{session_id}/token`. The session must be active and IDLE. The endpoint is
disabled in production and requires VOICE_ENABLED. This is a trusted internal demo, not public
authentication. Run one backend worker, as with Phase 7 connection ownership.

The backend derives room `relay-{session_id}` and participant `caller-{session_id}`. Tokens last
five minutes and permit only microphone publication in that room, with no subscription or data
publication. The backend token can subscribe but cannot publish. Token expiry limits admission,
not the lifetime of an already connected participant. Never expose server credentials.

Auto-subscription is disabled. Only the expected participant's microphone audio is selected;
foreign participants are ignored. A replacement authorized microphone replaces the prior
subscription. Client room metadata cannot choose a property, unit, resident or service. The
token endpoint rejects extra request fields and concurrent duplicate voice attachments.

AudioFrame declares signed little-endian PCM16, mono, sample rate and sequence. Default input
to STT is 16 kHz. LiveKit AudioStream performs resampling/channel conversion and yields 50 ms
frames with a capacity of 20. AssemblyAI packetizes into 50 ms chunks and rejects a format
mismatch. Relay performs no codec conversion in business code and retains no audio recording.
The bounded SDK media buffer is not a lossless recording guarantee under overload.

## Recognition, endpointing and identity

AssemblyAI Begin supplies stream identity. Turn events revise the whole current transcript;
`end_of_turn=true` alone creates a final. There is no Relay silence heuristic or promotion of a
partial on timeout. See [provider turn detection](https://www.assemblyai.com/docs/streaming/turn-detection).
Default provider silence settings are 800/2000 ms (raised from 128/1280 after live speech ended
turns on natural mid-sentence pauses) and remain configurable.

The provider proposes turn boundaries; Relay decides when a boundary becomes an application
turn. After a provider final, SpeechPipeline holds it provisionally for
`STT_FINAL_ADMISSION_GRACE_MS` (default 400 ms). If speech resumes inside that window
(provider-neutral `SPEECH_STARTED`, or the first `PARTIAL` of a newer provider turn), the final
stays provisional and the next provider final is coalesced with it: segments are joined with
one space, never rewritten, and the first segment's speech identity is the admitted identity.
The grace restarts after each final. A provisional final never reaches RealtimeSession, so it
cannot route, advance a workflow or execute a tool.

Admission fails closed whenever the utterance may be incomplete: the whole provisional
utterance is aborted, never admitted as a prefix, when resumed speech shows no provider
activity for 10 s (`CONTINUATION_TIMEOUT`), when it would exceed 8 segments or 8,000
characters (`ADMISSION_LIMIT_EXCEEDED`), when the STT provider fails (`PROVIDER_FAILURE`),
when input ends while the speaker had resumed (`INPUT_ENDED_DURING_CONTINUATION`), or when the
session ends (`SESSION_END`). The resumed provider turn is consumed too, so its tail never
becomes a turn of its own. Only a clean input end (media ended) with no continuation in
progress admits the provisional final through the normal path. Logs distinguish
`STT_FINAL_ADMITTED` from `STT_PROVISIONAL_ABORTED` and record counts, reasons and timings,
never transcript text.

SpeechStarted is retained until the next Turn supplies its turn_order; USER_SPEECH_STARTED then
carries the actual provider timestamp. This can delay event delivery but does not infer speech
onset from text arrival. USER_SPEECH_ENDED means provider-final turn endpoint, not a measured
acoustic stop. No invented confidence is attached.

Speech UUID is UUID5(provider stream UUID, turn_order). Application UUID is UUID5(conversation
UUID, speech UUID). An ordered-stream high-water mark suppresses duplicate finals and stale
partials using constant memory. Blank finals consume their identity without starting a turn.
Partials are ephemeral and cannot invoke the orchestrator. STT_FINAL contains normalized text,
speech/application IDs, provider/model, optional language and end_of_turn=true. Accepted finals
also include the original turn_number and expected_revision for exact durable replay.

The shared RealtimeSession input gate serializes text and speech admission. A final arriving
during another turn produces SESSION_BUSY; it is not queued or retried automatically. Speak
again after completion. A new stream has a new identity; repeating words is a new application
request, not transport replay. To recover an accepted request, retain its original ID, number,
revision and transcript and use the existing text replay protocol.

## Context and privacy

SpeechContext projects only the resolved property's name, enabled service display names and
scenario names: at most 50 unique terms, each at most 50 characters. It excludes resident/unit
records, contacts, arbitrary service configuration, hidden prompts and reasoning. Atlas and
Palm contexts are constructed independently. Keyterms bias transcription; they cannot authorize
routes or writes.

After the authoritative AGENT_RESPONSE is emitted, the exact response text feeds a separate
bounded context-update task. Provider limits restrict agent_context to its last 1,750 characters.
On reconnect, the latest persisted successful response text supplies initial context. No raw
database row is serialized. The provider manages finalized user transcript carryover within
its own stream. See [context carryover](https://www.assemblyai.com/docs/streaming/universal-3-5-pro/context-carryover)
and [configuration updates](https://www.assemblyai.com/docs/streaming/updating-configuration-mid-stream).

Audio and this allowlisted context leave the process for the configured STT provider. Response
text may itself contain user-facing information; this is not a claim of anonymous processing.
Default logs omit transcripts, audio, context contents, tokens and provider exception details.
WebSocket/LiveKit dependency logging is restricted to avoid debug handshake disclosure.

STT_LANGUAGE_CODES defaults to `["en"]`; a JSON array such as `["en","fr","ar"]` or an empty
array for automatic selection is supported by the configuration boundary. Language metadata is
reported only when supplied. No English, French, Arabic or code-switching accuracy is claimed.

## Lifecycle, failure and backpressure

SpeechPipeline owns audio, recognition and context tasks. Every task is retained, cancelled and
awaited. It owns provider and media cleanup on control/session shutdown, provider failure and
media disconnect. Track loss allows up to 30 seconds for a replacement; participant/room loss
ends the source. A connection is bounded to 10 seconds, provider opening to 15, frame/context
operations to 5, and resource cleanup to 5 seconds per resource. The provider sends Terminate,
waits at most 2 seconds for Termination, then closes the socket; teardown does not submit finals.

STT failure emits a sanitized STT_FAILED and requests reconnect/repetition. No partial becomes
a final. A fresh stream is required: there is no silent audio replay or recognition retry.
Media failure alone does not cancel an accepted application task. Control disconnect retains
Phase 7 cancellation/reconciliation semantics. Neither can undo a committed incident or receipt.
Reconnect reads durable state; temporary speech buffers never reconstruct business truth.

STT_PARTIAL is the only transient event. It is admitted only when the output queue is empty and
has capacity at least two; otherwise it is dropped, also when another emission holds the lock.
Thus at most one pending partial exists and it cannot fill the critical queue. All other events,
including STT_FINAL and business completion, retain bounded critical delivery with explicit
backpressure failure and replay. Sequence gaps are allowed. Defaults remain 32 events, one-second
enqueue, five-second socket send and two-second final drain. Recognition socket receive buffering
is bounded to 16 messages/64 KiB per message; context updates have four slots. Audio sends await
backpressure rather than accumulating an unbounded application queue.

Structured measurements include first-audio-to-first-partial, last-received-audio-to-final,
final-to-accepted-turn-start, stream duration, partial/final/duplicate/failure counts and partial
drops. Last audio includes silence and buffering, so this is not an acoustic endpoint latency
benchmark. No live numbers have been recorded. Existing turn/event logs provide session and turn
correlation; these measurements are diagnostics, not a new metrics service.

## Explicit live validation

Ordinary pytest makes no AssemblyAI calls. The isolated Compose suite runs an actual LiveKit
server, publishes PCM through WebRTC/Opus, resamples it and delivers frames to fake STT.
That verifies transport, not speech recognition quality or browser microphone permissions.

For provider smoke testing, supply ASSEMBLYAI_API_KEY privately and an approved short, single-turn
mono PCM16 WAV at STT_SAMPLE_RATE (maximum 60 seconds):

```text
uv run --locked relay-eval-stt-live --wav path/to/approved.wav --expected "My AC isn't cooling."
```

This explicit command incurs provider usage. It streams in real time, appends endpointing silence,
records expected/actual transcript, requested/provider-reported model, partial count and timings,
then closes the stream. Without a key it reports **LIVE PROVIDER VALIDATION PENDING**. The text
cases in evaluations/speech-cases.tsv are fixture specifications, not recorded audio or scores.

Live provider smoke, executed 2026-10-14: a real human recording of "My AC isn't cooling." (16 kHz
mono PCM16 WAV, 3.56 s, kept outside the repository) returned `LIVE_PROVIDER_VALIDATED`. Requested
and provider-reported model were both `universal-3-6-pro`. Partials were "My" and "My AC isn't
cooling."; the final was exactly "My AC isn't cooling.". First audio → first partial took about
1.6 s and first audio → final about 3.45 s, both measured with the CLI's real-time pacing.
Terminate was answered by Termination. Where application-control policy blocks uv's generated
script launchers, run the CLI as `uv run --locked python -m evaluations.run_stt_live …`.

Manual browser procedure (executed 2026-10-14; see [Phase 8.1 verification](verification/phase-8.1-verification.md)):

1. Start/migrate/seed Relay's development database. Run a reachable LiveKit server, using its
   official local procedure or separately configured service. The isolated test server publishes
   no host ports and is not the manual browser deployment.
2. Set VOICE_ENABLED=true, LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET and ASSEMBLYAI_API_KEY
   in the local ignored environment. Start `uv run relay` with one worker.
3. POST `{"resident":"Ikram"}` to `/api/demo/sessions`; retain the returned session UUID.
4. Run `uv run python -m http.server 5173 --directory frontend --bind 127.0.0.1` and open
   `http://localhost:5173/voice.html`. Enter the backend URL and session UUID.
5. Connect microphone. The page starts the control session, obtains the backend-scoped token,
   joins the room and publishes microphone audio. Grant browser permission.
6. Observe partial/final text, then Relay's text response and, with `TTS_ENABLED=true`, its voice.
   Use configured phrases or configure the existing routing model for broader language; STT does
   not replace routing interpretation. Use headphones so Relay's voice does not reach the
   microphone.
7. Disconnect and verify media/STT close logs. Reconnect to continue the durable conversation.

Live status (2026-10-14): real AssemblyAI streaming and a real browser microphone through a
local LiveKit server were validated. That covers single-turn smoke tests and the complete
nine-turn natural flagship with exactly one incident, the same incident updated, and one linked
handoff. LiveKit Cloud, multilingual quality, noise robustness and recognition/endpoint accuracy
remain unmeasured; single runs are not accuracy benchmarks. An always-on microphone also admits
ambient speech as application turns (safely abstained in the acceptance run). Local setup notes:
the backend and browser must both reach the LiveKit URL and its media ports. In the acceptance
setup LiveKit ran in Docker with `--dev --node-ip 127.0.0.1` and loopback-published 7880, 7881/tcp
and 7882/udp, and the backend ran on the host.
## Speech output (Phase 9)

```text
AgentTurnResult → AGENT_RESPONSE + TURN_COMPLETED (sent first) → spoken_text()
  → SpeechOutput → ElevenLabs HTTP streaming (eleven_flash_v2_5, pcm_24000)
  → PcmFramer (20 ms) → bounded queue (25 frames) → LiveKitAudioSink → relay-agent-audio → browser
```

- **Presentation only.** `RealtimeSession` asks an optional presentation to speak trusted text; it
  never sees providers, formats or credentials. Speech cannot route, advance workflows, call tools
  or change results. TTS or sink failure leaves the text and business result intact.
- **When Relay speaks.** Only first deliveries (`replayed=false`) after the text response was
  enqueued. Exact business replays are never re-spoken; a bounded history refuses re-presenting a
  turn whose speech already started.
- **Spoken references.** The renderer's terminal `Reference: <uuid>.` is spoken as "The reference
  is available in the chat."; the text keeps the UUID. No other text is rewritten.
- **Scheduling and buffering.** One active and one pending output; a newer response replaces a
  pending one, never the active one. The Python buffer holds 25 × 20 ms frames and the LiveKit
  `AudioSource` queue 200 ms; a full buffer pauses reading the TTS HTTP stream.
- **Transport.** The backend participant that receives the caller's microphone also publishes one
  `relay-agent-audio` track (microphone source), subscribable only by the caller. The browser
  attaches only that track from `relay-server-{session}` and offers an "Enable Relay audio" button
  when autoplay is blocked.
- **Events.** `AGENT_AUDIO_OUTPUT_STARTED` (first frame accepted by the local LiveKit source; not
  remote playback) and `AGENT_AUDIO_OUTPUT_ENDED` (status, safe code, accepted milliseconds). They
  carry no text, voice ID or provider detail.
- **Explicit smoke.** `uv run --locked python -m evaluations.run_tts_live` makes one real request and
  reports timing and PCM statistics without saving audio.

Barge-in is not implemented. Interruption must stop feeding frames and call `clear_queue()`; a task
blocked in `capture_frame()` must not be cancelled (see [roadmap](roadmap.md)).
