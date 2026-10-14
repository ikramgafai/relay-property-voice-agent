# Phase 8 verification — 2026-10-13

> **Addendum, 2026-10-14.** The pending live items below were later executed. Real AssemblyAI
> streaming passed on a recorded human WAV, and a real browser microphone through local LiveKit
> passed. The natural multi-turn flagship then exposed a pending-reply defect, which Phase 8.1
> fixed ([ADR 008](../decisions/008-pending-reply-interpretation.md)). Its full real-microphone
> acceptance is in [Phase 8.1 verification](phase-8.1-verification.md). The record below is kept
> as written on 2026-10-13.

Phase 8 implementation and deterministic verification are complete. **LIVE PROVIDER VALIDATION
PENDING**: no AssemblyAI API key or approved audio recording was available. No live recognition
accuracy, provider latency, multilingual quality, browser microphone or LiveKit Cloud result is
claimed. Phase 9 has not started.

## Executed checks

| Check | Actual result |
| --- | --- |
| Full isolated PostgreSQL + LiveKit pytest suite | **272 passed in 29.48 seconds**; all 248 previous tests plus 24 Phase 8 cases. |
| Strict mypy | **129 source files**, no issues. |
| Ruff | All checks passed. |
| Formatting | **158 files already formatted**. |
| Alembic parity | Full fixture performs migration round trips and `alembic check`: no new upgrade operations. Head remains 0004; no migration added. |
| Real local LiveKit | Actual room, authorized and foreign publishers, WebRTC/Opus PCM transport, 48 kHz → 16 kHz SDK conversion, track replacement and cleanup passed in the full suite. |
| Provider smoke command | No key: **LIVE PROVIDER VALIDATION PENDING**; no provider request or fabricated result. |

Four PyJWT warnings report the official isolated LiveKit development secret `secret` being shorter
than the recommended HMAC key length. This fixture is private to the test network. It is not a
production credential recommendation. PostgreSQL uses temporary test storage; no development
volume was deleted. Final Ruff and formatting checks passed after the documentation changes.

## Acceptance evidence

| Required criterion | Evidence / status |
| --- | --- |
| Media adapter, caller frames, explicit format | LiveKitAudioSource → AudioFrame PCM16 mono; actual media test passes. |
| Vendor-neutral media/STT contracts | Protocols contain typed Relay data; AST dependency-boundary test passes. |
| Owned media/provider lifecycle | Pipeline owns and awaits tasks; media/control/provider failure cases close both resources. |
| Real STT adapter + deterministic fake | Raw documented AssemblyAI v3 protocol; fake wire validates handshake, chunking, updates, malformed events and termination. FakeSTT drives application tests. Live provider remains pending. |
| Partial/final events | Typed STT_PARTIAL and STT_FINAL; partial does not call processor. Only provider-final endpoint submits. |
| Exactly one turn for duplicate final | Stable stream/turn-derived IDs, ordered high-water suppression; duplicate every flagship final, nine AgentTurn rows total. |
| Blank/failure safe handling | Blank final ignored; failures never promote partial text. |
| Trusted property/session media binding | Backend-derived room/participant, five-minute microphone-only token; extra authority fields rejected. Duplicate attachment denied. |
| Unexpected participant policy | Auto-subscribe disabled; only bound caller microphone accepted; foreign publisher included in real-room test. |
| Context isolation and privacy | Atlas/Palm vocabulary test, resident-name exclusion, allowlist projection; exact trusted response updates tested through text and speech paths. |
| Context is not business truth | Final text goes through unchanged property/catalog/workflow/tool validation. No speech-layer routing or action dispatch. |
| Transient partial pressure | 1,000 partials leave one pending and 999 dropped; critical event still fits. |
| Critical final/business delivery | Explicit full-queue STT_FINAL backpressure test; existing Phase 7 critical delivery tests remain green. |
| Failure and provider close | Scripted provider/media/control failures; adapter Terminate/Termination exchange tested. |
| Disconnect and durable state | PostgreSQL test disconnects media after incident commit while application task is held; committed incident remains, result completes, exact replay does not duplicate it. |
| Reconnect | Flagship replaces media stream and supplies latest persisted response; control reconnect exact-request replay tested. |
| Existing session/orchestrator reuse | SpeechPipeline stops at RealtimeSession.submit_speech; input gate shares text admission. |
| Same-session busy policy | Busy final emits SESSION_BUSY, no queued extra operation. |
| Different-session concurrency | Atlas held while Palm completes independently with isolated contexts. |
| Phase 7 text remains supported | Previous full suite passes; active voice also receives trusted context after a text turn. |
| Complete deterministic flagship | Nine finals: AC complaint/details → water emergency → confirmed creation → electrical hazard update → confirmed human handoff. One incident, same incident revision 2, one linked handoff. Responses remain text. |
| All earlier phases / PostgreSQL | 272 full-suite tests passed, including all previous 248. |
| No migration | Existing durable AgentTurn, workflows, operations and receipts suffice. Speech resources are ephemeral. |
| No forbidden scope | No TTS, response audio, barge-in, PSTN/SIP, new business tools or agent framework; Phase 9 not started. |
| Live validation honesty | Actual isolated LiveKit passed. AssemblyAI and manual browser/cloud smoke pending; instructions and explicit CLI supplied. |

The 30 requested minimum test concerns map to: `test_speech.py` (protocol, partial/final, duplicate,
blank, failure, cleanup, busy and pressure), `test_stt_adapter.py` (real adapter wire contract),
`test_livekit.py` (token and actual media boundary), and `integration/test_speech.py` (property
context, concurrent sessions, context updates, full flagship, post-commit disconnect and replay).
The existing 248 tests retain catalog isolation, dynamic service discovery, workflow/tool safety,
truthful result rendering and text transport regression coverage. Deterministic transcripts and
routing doubles demonstrate software behavior, not real speech or live model accuracy.

## Architectural correspondence

ADR 006 was read explicitly. This phase does not replace its cascade: property context gates
routing; configured cues and trusted continuation precede constrained model interpretation;
catalog and transition validation remain downstream. Safe clarification/abstention remains valid.
A Palm conversation never receives Atlas catalog authority merely because STT recognizes an
Atlas phrase. Dynamic configured services remain routable without a workflow registry entry.

ADR 002 is reflected by neutral media/STT protocols. ADR 004 is reflected by separately owned
media tasks and persistent business receipts: audio loss is not rollback. ADRs 001/003/005 remain
implemented by trusted context, backend-only writes and independently verified outcomes. LiveKit
and AssemblyAI are replaceable transport/recognition adapters, not orchestration frameworks.

## Important changes

- `realtime/media/`: neutral PCM contract and LiveKit room/track adapter.
- `providers/stt/`: neutral streaming protocol, typed context/events and AssemblyAI adapter.
- `realtime/speech.py`, `speech_context.py`: owned stream lifecycle, endpoint mapping, deduplication,
  property vocabulary and authoritative response updates.
- `realtime/session.py`, `schemas.py`, `queue.py`, `protocol.py`, `manager.py`: speech attachment,
  shared admission, typed events, replay metadata and transient partial policy.
- `api/voice.py`, API router, core settings/logging: internal admission, scoped tokens and safe config.
- `db/agent.py`: read latest saved response text for reconnect; no schema or business change.
- `compose.test.yml`, pyproject/lock: isolated local LiveKit and minimal locked dependencies.
- `tests/support/speech.py` and four speech/media test modules: deterministic and real transport checks.
- `evaluations/run_stt_live.py`, `speech-cases.tsv`: explicit paid-provider smoke and unrecorded cases.
- `frontend/voice.html`: small manual microphone/text observation surface, not a production UI.
- README and architecture/realtime/state/event docs; `speech.md` describes sources, bounds and setup.

Versions, official references, lifecycle limits and exact manual procedures are in [Speech](../speech.md).
The AssemblyAI Python SDK 1.6.1 was inspected but not installed because its asynchronous audio
writer queue is unbounded. The implementation uses the documented raw WebSocket API with bounded
sends instead. Selected model: `universal-3-6-pro`; actual live model output remains unobserved.

## Remaining decisions and validation

- Run paid provider smoke with an approved recording/key, then actual browser microphone smoke.
- Measure endpoint behavior and recognition quality on real English and optional French/Arabic,
  code-switching and noise recordings. Text fixtures cannot establish quality.
- Define production authentication, token authorization, distributed connection ownership,
  provider retention/data-region policy and session duration/usage budgets before public use.
- Tune bounded media-buffer overflow behavior and endpoint thresholds with actual audio evidence;
  bounded transport does not promise lossless capture under sustained overload.
- Define TTS/output task ownership and later interruption/delivery acknowledgments in future phases.
- External PMS/handoff delivery and live routing-model validation remain separate prior limitations.

## Repository tree

See the complete tree below, excluding caches, local environments and secrets.
```text
relay-property-voice-agent/
.dockerignore
.env.example
.github
  workflows
    .gitkeep
.gitignore
.python-version
alembic.ini
backend
  app
    __init__.py
    agent
      __init__.py
      errors.py
      interpretation.py
      orchestrator.py
      protocol.py
      reconciliation.py
      renderer.py
      response_plan.py
      schemas.py
    api
      __init__.py
      health.py
      router.py
      turns.py
      voice.py
    core
      __init__.py
      application.py
      config.py
      errors.py
      lifecycle.py
      logging.py
      middleware.py
      server.py
    db
      __init__.py
      agent_models.py
      agent.py
      base.py
      models.py
      operation_models.py
      operations.py
      repository.py
      seed_data.py
      seed.py
      session.py
      tool_store.py
      workflows.py
    main.py
    metrics
      __init__.py
    property
      __init__.py
      context.py
      errors.py
      repository.py
      schemas.py
    providers
      __init__.py
      embeddings
        __init__.py
      llm
        __init__.py
        openai_compatible.py
        phrasing.py
        protocol.py
      stt
        __init__.py
        assemblyai.py
        protocol.py
        schemas.py
      tts
        __init__.py
    realtime
      __init__.py
      errors.py
      manager.py
      media
        __init__.py
        livekit.py
        protocol.py
      protocol.py
      queue.py
      schemas.py
      session.py
      speech_context.py
      speech.py
      websocket.py
    routing
      __init__.py
      configured.py
      model.py
      router.py
      schemas.py
      validation.py
    tools
      __init__.py
      errors.py
      executor.py
      protocol.py
      registry.py
      schemas.py
      validation.py
    workflows
      __init__.py
      application.py
      definitions.py
      engine.py
      errors.py
      registry.py
      repository.py
      schemas.py
      transitions.py
  migrations
    env.py
    script.py.mako
    versions
      0001_property_domain.py
      0002_workflow_state.py
      0003_operational_tools.py
      0004_agent_turns.py
  tests
    __init__.py
    .gitkeep
    conftest.py
    integration
      __init__.py
      conftest.py
      test_agent.py
      test_database_readiness.py
      test_property_domain.py
      test_realtime.py
      test_routing_domain.py
      test_speech.py
      test_tools.py
      test_workflows.py
    support
      __init__.py
      realtime.py
      routing.py
      speech.py
      workflows.py
    test_agent_contracts.py
    test_config.py
    test_health.py
    test_livekit.py
    test_observability.py
    test_property_contracts.py
    test_realtime.py
    test_routing_adapter.py
    test_routing_evaluation.py
    test_routing.py
    test_speech.py
    test_stt_adapter.py
    unit
      test_workflows.py
compose.test.yml
docker-compose.yml
Dockerfile
docs
  architecture.md
  data-model.md
  decisions
    001-property-context-before-routing.md
    002-provider-abstractions.md
    003-tools-own-side-effects.md
    004-cancellable-presentation-state.md
    005-property-specific-services.md
    006-routing-cascade.md
    007-constrained-clarification.md
  event-model.md
  failure-modes.md
  orchestration.md
  phase-1-verification.md
  phase-2-verification.md
  phase-3-verification.md
  phase-4-verification.md
  phase-5-verification.md
  phase-6-verification.md
  phase-7-verification.md
  phase-8-verification.md
  realtime.md
  routing-benchmarks.md
  routing.md
  scope.md
  speech.md
  state-machine.md
  tools.md
  workflows.md
evaluations
  __init__.py
  .gitkeep
  cases.tsv
  catalogs.py
  offline_provider.py
  results
    offline.json
  run_routing.py
  run_stt_live.py
  speech-cases.tsv
frontend
  .gitkeep
  voice.html
pyproject.toml
README.md
uv.lock
```
