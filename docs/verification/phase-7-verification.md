# Phase 7 verification

Verified 2026-10-12. Phase 7 implements the realtime session core, typed text events and internal
WebSocket transport. Phase 8 has not started.

## Executed checks

| Check | Observed result |
| --- | --- |
| Full pytest suite with PostgreSQL 17 | **248 passed in 23.58s**. |
| Earlier phases | All **209 Phase 1–6 tests** remain green. |
| Phase 7 additions | **39 tests: 10 PostgreSQL integration cases and 29 protocol/lifecycle cases**. |
| Strict mypy | No issues in **114 source files**. |
| Ruff | All checks passed. |
| Formatting | **140 files already formatted** in the local check. |
| Locked dependency build | Docker test image rebuilt successfully; `websockets==17.2` added for Uvicorn WebSocket support. |
| Alembic parity | No new upgrade operations detected. |
| Migrations | Empty → 0004 head → 0003 → head → 0002 → head → base → head; fixture teardown returns to base. |
| Schema | Same 14 domain tables. No realtime session/event table or new migration. |

Python verification ran in Docker because Windows Application Control blocks local Python.
Current backend/evaluation sources were mounted read-only. The isolated relay-phase2-tests
project uses the dedicated relay_test database and tmpfs storage. Tests refuse a nonempty
starting database; development volumes are untouched. Native Ruff ran locally.

```powershell
docker compose -f compose.test.yml build tests
docker compose -f compose.test.yml up -d --wait postgres
docker compose -f compose.test.yml run --rm --no-deps --volume './backend:/app/backend:ro' --volume './evaluations:/app/evaluations:ro' tests pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
docker run --rm --network none --volume "${PWD}/backend:/app/backend:ro" --volume "${PWD}/evaluations:/app/evaluations:ro" relay-phase2-tests-tests mypy
docker compose -f compose.test.yml down
```

## Verification method and limits

The async WebSocket driver invokes the **actual FastAPI ASGI WebSocket route**, supplying
connect/receive/disconnect messages and validating outbound frames through ServerEvent. It runs
on the same loop as PostgreSQL and test pause events. This tests endpoint admission, framing,
validation, task ownership and cleanup without thread/sleep race assumptions. It does not claim
a browser, TCP load test or public production deployment.

Integration tests use actual TurnOrchestrator, ServiceRouter, WorkflowEngine, ToolExecutor and
PostgreSQL stores. Ambiguous routing uses the existing deterministic provider seam; there are no
live model calls or claims about live classification coverage. External HTTP is blocked by the
test fixture. No audio/STT/TTS/PMS service is involved.

## Flagship demonstration

`backend/tests/integration/test_realtime.py::test_flagship_and_durable_replay` verifies:

1. SESSION_START binds the durable conversation and returns revision/next-turn metadata.
2. “My AC isn't cooling.” produces a maintenance question through the existing orchestrator.
3. Warm-air details produce a confirmation proposal, without creating an operation.
4. “Actually water is pouring out of it.” switches to Emergency Maintenance. Location, hazard
   and severity replies lead to one committed emergency incident and a truthful success response.
5. “Wait, the water is getting close to an electrical outlet.” updates the **same incident UUID**
   to revision 2. Database hazard details contain the electrical-outlet report.
6. Human Handoff plus confirmation creates **one handoff linked to that incident**. The response
   explicitly says no live transfer occurred.
7. Replaying the original create turn returns identical saved text with `replayed=true`, including
   after disconnect/reconnect. Counts remain **one incident, one handoff**.
8. Reusing the turn ID with changed text yields TURN_CONFLICT, no successful result and IDLE.

Every received frame is schema-validated. Event IDs are unique and sequence numbers strictly
increase. The first successful turn has this observed/asserted protocol structure:

```text
1 SESSION_STARTED
2 SESSION_STATE_CHANGED(IDLE)
3 SESSION_STATE_CHANGED(PROCESSING_TURN)
4 TURN_STARTED       [application turn UUID]
5 AGENT_RESPONSE     [same turn UUID; authoritative result text/intent]
6 TURN_COMPLETED     [same turn UUID; revision/status/replay flag]
7 SESSION_STATE_CHANGED(IDLE)
```

Later turns continue the sequence. Reconnect starts at 1 with new connection/realtime UUIDs.
A conflict produces PROCESSING_TURN → TURN_STARTED → ERROR → IDLE. Critical output delivery
failure produces explicit close/replay policy rather than pretending the turn failed to commit.

## Cancellation and disconnect evidence

`test_cancel_disconnect_and_recovery` has six synchronized PostgreSQL cases: cancel and disconnect
at each of three seams: before orchestration, during routing after turn checkpoint, and after
ToolExecutor has returned a committed/verified result but before orchestration receives it.
Each case checks zero manager entries and no remaining named realtime tasks after close, then
reconnects and submits the exact original request. A second replay returns saved text without
another operation. Post-commit cases preserve exactly one incident; earlier seams have none.

The cancel variants also reject concurrent text with SESSION_BUSY and a wrong cancellation ID
with TURN_NOT_ACTIVE before accepting the correct ID. TURN_CANCELLED always requires reconciliation;
it never claims rollback. `test_cancel_after_result_is_controlled` pauses completion delivery after
the durable result exists and verifies TURN_ALREADY_COMPLETED while preserving the incident.

The two-durable-session test pauses A before processing and completes B while A remains paused.
Duplicate binding is tested both after connection and while trusted lookup is pending; the first
connection retains working heartbeat. Idle disconnect, explicit session end, manager termination,
shutdown, transport failure and handler cancellation all exercise cleanup. Handler cancellation
awaits a deliberately paused cleanup without injecting another cancellation into that cleanup.

## Required test mapping

Paths: integration cases are in `backend/tests/integration/test_realtime.py`; protocol/lifecycle
cases are in `backend/tests/test_realtime.py`; shared driver is `backend/tests/support/realtime.py`.

| # | Required proof | Evidence |
| --- | --- | --- |
| 1 | Valid durable session binds | Flagship and bind/reconnect integration tests. |
| 2 | Missing session rejected | Random absent UUID → SESSION_NOT_FOUND and close. |
| 3 | Duplicate connection rejected | Duplicate integration and pending-bind reservation tests. |
| 4 | Reconnect succeeds | Bind/reconnect and all six cancellation recovery cases. |
| 5 | SESSION_START works | Handshake and IDLE assertions. Repeated start controlled. |
| 6 | Text invokes existing orchestrator | All PostgreSQL realtime turns. |
| 7 | Response uses AgentTurnResult | Integration intent/text/revision and identical durable replay checks. |
| 8 | Ordered sequence | Driver asserts strictly increasing sequence on every received event. |
| 9 | Unique event IDs | Driver rejects duplicate outbound UUIDs. |
| 10 | Idempotent same-turn replay | Flagship and cancellation recovery assert saved response/counts. |
| 11 | Changed same-ID payload rejected | Flagship → TURN_CONFLICT and IDLE. |
| 12 | Same-session concurrent turn rejected | Paused integration turn → SESSION_BUSY. |
| 13 | Independent session concurrency | Real durable A paused while B completes. |
| 14 | Safe task cancellation | Synchronized before/routing/committed seams. |
| 15 | Missing turn cancellation controlled | Idle/wrong-ID cancellation → TURN_NOT_ACTIVE. |
| 16 | Cancellation preserves commit | Exactly one incident after post-commit cancellation and replay. |
| 17 | Disconnect cleans task | Each processing-disconnect case asserts no realtime tasks. |
| 18 | Disconnect removes manager entry | Integration and protocol teardown asserts active count zero. |
| 19 | Commit survives disconnect | Post-commit disconnect/reconnect/receipt recovery. |
| 20 | Heartbeat | PING/PONG after validation errors and duplicate rejection. |
| 21 | Invalid payload safe | Malformed JSON, blank text, missing/mismatched IDs, binary, oversize. |
| 22 | Unknown event safe | CLIENT_AUDIO → UNSUPPORTED_EVENT, heartbeat still works. |
| 23 | Authority injection rejected | property/resident/unit/routing/workflow/action/result fields forbidden. |
| 24 | Bounded queue | Capacity-one overflow preserves queued event, stops explicitly; stalled transport closes 1013. Error-report overflow also owned/cleaned. |
| 25 | Session end cleans | ENDING, ENDED, SESSION_ENDED, close, zero manager/tasks. |
| 26 | No ORM protocol leakage | Schema assertions, typed payloads and import-boundary test. |
| 27 | No domain logic in handler | Import-boundary test plus delegation through TurnProcessor. |
| 28 | Production disabled | Production rejects upgrade with 1008 and creates no manager entry. |
| 29 | Phase 1–6 regression | All 209 earlier tests included in the green full suite. |
| 30 | Complete realtime flagship | Maintenance → emergency create → same-incident update → handoff and replay. |

## Explicit acceptance checklist

| Area | Verified criteria |
| --- | --- |
| Realtime session | RealtimeSession and RealtimeSessionManager exist. Ephemeral execution state is separate from durable state. One-connection policy is explicit and tested. |
| WebSocket | Internal `/api/realtime/{session_id}` works. Discriminated payloads are strongly typed. Authority fields are rejected. Production safeguard is tested and demo-only deployment documented. |
| Events | Typed envelope, event UUID, monotonic per-connection sequence and application turn correlation exist. Malformed events fail with safe codes. |
| Turn execution | Existing TurnOrchestrator reused. No duplicate routing/workflow/tool implementation. Successful text turns verified over the endpoint. |
| Concurrency | One active turn enforced; different durable sessions run independently; simultaneous duplicate binding prevented locally. |
| Cancellation | Every owned task is cancellable/awaited. Semantics documented before/during/after commit. Committed operations survive; no rollback-after-commit claim. |
| Disconnect | Turn and IO tasks cleaned, manager membership removed, committed truth preserved, reconnect recovers through existing checkpoints/receipts/result replay. |
| Backpressure | Queue capacity and waits bounded. All Phase 7 events critical. Failure explicit with close/log/replay policy; no silent normal-path dropping. |
| Flagship | Complete text flow; one emergency incident; same UUID updated; one linked handoff; event order/IDs/sequences asserted. |
| Quality | Earlier tests, Phase 7 tests and PostgreSQL integration green. Ruff, formatting and strict mypy green. Migration parity/round trips green with no schema changes. |
| Boundaries | No STT, TTS, audio, LiveKit requirement, VAD, barge-in, new business tools or multi-agent framework. Phase 8 not started. |

## Important changed files

| Files | Change |
| --- | --- |
| `backend/app/realtime/schemas.py` | Versioned envelopes, discriminated payloads, execution states and safe codes. |
| `realtime/protocol.py`, `errors.py`, `queue.py` | Processor/lookup boundaries, safe application error mapping, bounded queue. |
| `realtime/session.py` | Owned turn lifecycle, state/events, cancellation and authoritative result forwarding. |
| `realtime/manager.py` | Protected bind reservations, membership, lookup, termination, shutdown and duplicate metrics. |
| `realtime/websocket.py` | Internal endpoint, frame validation, tracked IO tasks, draining and explicit closes. |
| `backend/app/core/lifecycle.py`, `api/router.py` | Compose the existing orchestrator/session lookup and register transport; shutdown ordering. |
| Three realtime test/driver files above | Actual ASGI WebSocket protocol tests, real PostgreSQL flagship and synchronized recovery. |
| `pyproject.toml`, `uv.lock` | Explicit WebSocket server implementation dependency. |
| `README.md`, architecture/state/event docs | Concrete Phase 7 status and relationship to conceptual Phase 0 voice model. |
| `docs/realtime.md`, this report | Protocol/ownership/recovery contract and full acceptance evidence. |

`events.py` is unnecessary: payloads and envelopes are together in `schemas.py`, preventing
two protocol authorities. Existing business modules and historical migrations are unchanged.

## Remaining architectural decisions

- Multi-worker/host connection leases and failover; Phase 7 explicitly uses one process.
- Public authentication, authorization, origin policy, rate limits, ingress frame limits and
  deployment hardening; the current endpoint remains an internal demonstration.
- Client acknowledgements/resume offsets or replay retention policies, if future requirements
  need more than durable application-turn replay. No exactly-once transport delivery is claimed.
- Future media transport, final-transcript identity, STT/TTS ownership, playback acknowledgement,
  presentation cancellation and droppable partial/audio policy. No provider integration begun.
- Live model routing/phrasing evaluation, external PMS reconciliation and actual handoff delivery
  remain separate earlier-phase limitations; the realtime tests do not imply they are solved.

No unresolved decision blocks the specified single-process, text-only Phase 7 acceptance scope.

## Repository tree

Source/configuration inventory follows. Local Python installs, virtual environments, caches,
secrets and Git internals are excluded.

```text
relay-property-voice-agent/
  .dockerignore
  .env.example
  .github/
    workflows/
      .gitkeep
  .gitignore
  .python-version
  alembic.ini
  backend/
    app/
      __init__.py
      agent/
        __init__.py
        errors.py
        interpretation.py
        orchestrator.py
        protocol.py
        reconciliation.py
        renderer.py
        response_plan.py
        schemas.py
      api/
        __init__.py
        health.py
        router.py
        turns.py
      core/
        __init__.py
        application.py
        config.py
        errors.py
        lifecycle.py
        logging.py
        middleware.py
        server.py
      db/
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
      metrics/
        __init__.py
      property/
        __init__.py
        context.py
        errors.py
        repository.py
        schemas.py
      providers/
        __init__.py
        embeddings/
          __init__.py
        llm/
          __init__.py
          openai_compatible.py
          phrasing.py
          protocol.py
        stt/
          __init__.py
        tts/
          __init__.py
      realtime/
        __init__.py
        errors.py
        manager.py
        protocol.py
        queue.py
        schemas.py
        session.py
        websocket.py
      routing/
        __init__.py
        configured.py
        model.py
        router.py
        schemas.py
        validation.py
      tools/
        __init__.py
        errors.py
        executor.py
        protocol.py
        registry.py
        schemas.py
        validation.py
      workflows/
        __init__.py
        application.py
        definitions.py
        engine.py
        errors.py
        registry.py
        repository.py
        schemas.py
        transitions.py
    migrations/
      env.py
      script.py.mako
      versions/
        0001_property_domain.py
        0002_workflow_state.py
        0003_operational_tools.py
        0004_agent_turns.py
    tests/
      __init__.py
      .gitkeep
      conftest.py
      integration/
        __init__.py
        conftest.py
        test_agent.py
        test_database_readiness.py
        test_property_domain.py
        test_realtime.py
        test_routing_domain.py
        test_tools.py
        test_workflows.py
      support/
        __init__.py
        realtime.py
        routing.py
        workflows.py
      test_agent_contracts.py
      test_config.py
      test_health.py
      test_observability.py
      test_property_contracts.py
      test_realtime.py
      test_routing_adapter.py
      test_routing_evaluation.py
      test_routing.py
      unit/
        test_workflows.py
  compose.test.yml
  docker-compose.yml
  Dockerfile
  docs/
    architecture.md
    data-model.md
    decisions/
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
    realtime.md
    routing-benchmarks.md
    routing.md
    scope.md
    state-machine.md
    tools.md
    workflows.md
  evaluations/
    __init__.py
    .gitkeep
    cases.tsv
    catalogs.py
    offline_provider.py
    results/
      offline.json
    run_routing.py
  frontend/
    .gitkeep
  pyproject.toml
  README.md
  uv.lock
```
