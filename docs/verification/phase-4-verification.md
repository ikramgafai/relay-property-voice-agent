# Phase 4 verification

Verified 2026-10-12. Phase 4 is complete: deterministic workflow processing and durable application
state. Phase 5 has not started. No operational action, voice integration, conversational generation
or multi-agent framework was added. Live-model semantic benchmarking remains the explicitly pending
Phase 3 verification item.

## Executed checks

| Check | Result |
| --- | --- |
| Complete pytest suite, including real PostgreSQL | **141 passed in 6.75s**, no failures/warnings/skips |
| Final deterministic suite with Docker networking disabled | **117 passed, 24 deselected in 2.57s** |
| PostgreSQL coverage within the complete suite | **24 integration tests**, including 5 new workflow/persistence tests |
| Earlier Phase 1–3 tests | All original 120 tests remain passing; 21 Phase 4 cases added |
| Alembic empty → head → base → head | Passed, with 0001 and 0002; teardown returns to base |
| Alembic model/schema parity | Passed: `No new upgrade operations detected.` |
| Ruff check | `All checks passed!` |
| Ruff format check | `98 files already formatted` |
| Strict mypy | `Success: no issues found in 78 source files` |

The final code was mounted read-only into the existing Python 3.12 Docker test image. The dedicated
`relay_test` PostgreSQL database uses the separate `compose.test.yml` project and ephemeral tmpfs.
Tests refuse a nonempty database or another database name before migration setup. All HTTP network
transports are blocked by the existing autouse test fixture; no model credentials or paid calls
were used. The final deterministic run also passed with Docker networking disabled.

Reproduction from the project directory:

```powershell
docker compose -f compose.test.yml up -d --wait postgres
docker compose -f compose.test.yml run --rm --no-deps --volume './backend:/app/backend:ro' --volume './evaluations:/app/evaluations:ro' tests pytest -q
docker run --rm --network none --volume "${PWD}/backend:/app/backend:ro" --volume "${PWD}/evaluations:/app/evaluations:ro" relay-phase2-tests-tests mypy
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
docker compose -f compose.test.yml down
```

If the test image is absent, build it using `docker compose -f compose.test.yml build tests`.
On a machine with working local Python, the equivalent checks remain `uv run pytest`,
`uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy`, with the isolated test
database configured for integration tests.

## ADR correspondence

The Phase 0–3 documents, all seven ADRs, PropertyContext, RoutingDecision, migration and
repository contracts were reviewed against the workflow design (conversation/workflow state,
registry, action boundary, preservation policy and persistence choice). No conflict required
replacing an accepted ADR.

- **ADR 001:** PropertyContext and immutable session identity gate workflow advancement. Existing
  catalog validation rejects cross-property and stale selections before workflow changes.
- **ADR 003:** ActionRequest declares required work. No executor, operational table, business
  record, external call or completed-action receipt exists in Phase 4.
- **ADR 004:** Persisted application state contains no audio/task/playback state. Cancelling a
  proposal does not claim to reverse an executed operation.
- **ADR 005:** The database still owns routable services. The capability registry is independent;
  unknown Pet Services routes successfully and returns controlled unsupported workflow status.
- **ADR 006:** The existing configured-evidence/model/backend-validation cascade is unchanged.
  Phase 4 consumes its real RoutingDecision and adds the downstream deterministic transition
  policy: new eligible routes can supersede previous workflows, preserve compatible fields, and
  invalidate unexecuted requests. It does not invent hazard keywords, model confidence thresholds,
  a global service enum, or an alternative classifier. Clarification/abstention remain safe results.
- **ADR 007:** Routing clarification remains backend-rendered in Phase 3. Workflow questions are
  structured intent/field specifications, not unconstrained model prose.

Persisting `sessions` and `workflow_runs` now provides restart continuity and stable proposal
identity. This realizes the corresponding Phase 0 entities. `messages` is unnecessary because typed
fields, provenance, transitions and the latest command receipt supply the required state. Migration
0002 adds only these two application tables; no future operational/event tables were introduced.

## Demonstrations exercised by tests

| Flow | Observed assertions |
| --- | --- |
| Non-emergency maintenance | Verbatim issue and known unit → ASK_USER for symptoms → WAIT_FOR_CONFIRMATION → confirmed CREATE_MAINTENANCE_REQUEST request; same identity on replay |
| Emergency maintenance | Collect location/hazard/severity → CREATE_EMERGENCY_INCIDENT request; known unit not requested again; zero completed actions |
| Leasing tour | ASK_USER → preferred time/contact → confirmation → SCHEDULE_LEASING_TOUR request |
| Leasing availability/pricing | COMPLETE with structured information intent, no invented stock/prices and no action |
| Human handoff | Reason plus prior business context → confirmation → CREATE_HUMAN_HANDOFF request; no transfer |
| Routing clarification and abstention | AWAIT_ROUTING; no new workflow, no action; active proposal stays unchanged when present |
| Database-only Pet Services | Insert service/association/scenario → fresh PropertyContext → “How do I register my dog?” routes Pet Services → UNSUPPORTED_WORKFLOW; routing file hashes unchanged |
| Editing or cancelling proposal | Old pending identity removed/replaced; confirmation is required again after edits; declined/cancelled state requests no action |
| New request | Fresh workflow ID, no inherited symptoms, old action disposition SUPERSEDED |

### Flagship PostgreSQL scenario

`test_flagship_ac_leak_emergency_handoff` uses the real seeded Atlas Residence, Ikram Gafai and
Unit 204 via the Phase 2 resolver. Routing decisions come through the existing router with a
deterministic provider double; this is a transition test, not a live semantic accuracy claim.

| Turn | Input and route | Workflow outcome |
| --- | --- | --- |
| 1 | “My AC isn't cooling.” → non_emergency_maintenance | ASK_USER for symptoms; unit 204 loaded from PropertyContext |
| 2 | Symptoms: warm air despite low thermostat | WAIT_FOR_CONFIRMATION with a maintenance proposal |
| 3 | “Actually water is pouring out of it.” → emergency_maintenance | Explicit transition; prior proposal superseded; issue/symptoms/unit preserved; ASK_USER for emergency details |
| 4 | Living-room AC location, water near electrical outlet, continuous flooding | CREATE_EMERGENCY_INCIDENT **request** |
| 5 | “I want to speak to someone.” → human_handoff | Explicit transition; incident request becomes SUPERSEDED; handoff confirmation |
| 6 | Deterministic confirmation | CREATE_HUMAN_HANDOFF **request**, carrying emergency service/hazard context |
| Replay 6 | Identical accepted command | Identical saved result/revision; no duplicate action/run |

The initial AC issue remains in field history with source turn 1, symptoms retain turn 2, and the
new leak report becomes the current issue with turn 3. Previous services are non-emergency then
emergency. Handoff business context stays emergency maintenance even though the active destination
service is human_handoff. The database contains one session and three workflow runs. All six
property-table row counts are unchanged, and all completed-action lists are empty. A separate
leasing → emergency test proves tour/contact fields are discarded and its pending proposal removed.

### Durable state and concurrency

`test_restart_reload_and_competing_transactions` commits an application turn, closes the engine,
constructs a fresh Database/repository/application, reloads exactly equal state, and replays the
same turn without another revision. Two independent REPEATABLE READ transactions then load one
revision; the winner commits and the stale writer receives StaleRevision. A separate stale
compare-and-swap attempt also fails without changing the winner's state.

`test_atomic_failure_does_not_leave_session_revision` attempts to reuse another session's workflow
ID. The savepoint rolls back the new session insert and leaves the original aggregate unchanged.
`test_information_completion_and_repository_ownership` verifies the completion event, preserves a
completed run's status during a later transition, and rejects foreign-property resident/run links.
The migration fixture checks table names, downgrade/upgrade reversibility and model parity.

## Acceptance checklist

All items below are satisfied by the implementation and passing suite.

| Area / criterion | Evidence |
| --- | --- |
| Explicit ConversationState | `workflows/schemas.py`; identity, turn, revision, current/previous service, active/run state |
| Explicit WorkflowState | Typed state, status, fields, history, confirmation, requests and timestamps |
| State not LLM history | No model history field or workflow provider dependency; deterministic typed UserTurn |
| Current/previous service information | Flagship asserts ordered history and separate handoff business service |
| Existing PropertyContext/RoutingDecision reused | Engine imports actual contracts and existing fingerprint/selection validation |
| Starts supported workflows | Tests cover all four capabilities through valid router decisions |
| Clarification/abstention safe | Both initial and active-workflow cases yield AWAIT_ROUTING without actions |
| Unsupported dynamic services safe | Database-only Pet Services and unknown configured capability return unsupported |
| Deterministic, no business effects | Stable UUID5 identities, explicit timestamps, copied state, no executor dependencies |
| Emergency workflow | Field collection and typed emergency request covered |
| Non-emergency workflow | Symptoms, known unit, proposal and confirmation covered |
| Leasing workflow | Tour, availability, pricing and unsupported scenario branches covered |
| Human handoff workflow | Typed handoff request preserves emergency business context |
| ASK_USER | Missing required field has structured question specification |
| WAIT_FOR_CONFIRMATION | Pending proposal stored; edits replace it; decline cancels |
| REQUEST_ACTION | Explicit true confirmation releases the same proposal identity |
| COMPLETE | Informational leasing intents and completion event covered |
| Typed ActionRequest | Discriminated payload union with action-kind consistency validation |
| No executed actions | No business tables/executor; completed_actions must be empty |
| Requests remain future-tool inputs | ACTION_REQUIRED stops at await_future_executor |
| Non-emergency → emergency | PostgreSQL flagship transition and preserved turn provenance |
| Emergency → handoff | Explicit second transition and emergency snapshot |
| Explicit transitions | From/to service/run, reason, turn/time, fields and superseded identities |
| Compatible fields retained | AC history, symptoms, unit; allowlisted handoff fields/history |
| Incompatible state removed | Leasing → emergency drops tour/contact; pending action superseded |
| Registry separate from routing taxonomy | Capability bindings only; no changes to production routing source |
| Session/property isolation | Atlas route on Palm, wrong session context, invalid IDs and foreign persisted links rejected |
| Focused durable schema | sessions/workflow_runs only; six existing property tables retained |
| Reversible migration | Empty → head → base → head plus teardown and schema parity |
| Reload works | New engine/repository/application reconstructs identical state |
| Stale updates rejected | Real competing transactions and stale CAS tested |
| Existing and new pytest green | 141 total, including all 120 earlier cases |
| PostgreSQL green | 24 integration cases in the full run |
| Ruff / formatting / mypy | All checks passed, 98 formatted files, 78 typed source files |
| No operational tools/PMS/notifications/transfer | No executor, network adapter, operational table or endpoint added |
| No voice/STT/TTS/final agent/framework | Reserved modules unchanged; workflow import-boundary test enforces separation |

Additional requested checks: collected-field source turns are asserted; known context is not asked
again; repeated turns preserve identity and request counts; caller input is not mutated; state
round-trips through JSON without ORM objects; cancellation and incompatible confirmation fail
safely. The architecture remains a deterministic component, not an LLM workflow prompt.

## Important files and remaining decisions

- `workflows/schemas.py`: typed aggregate, provenance, decisions, payloads and invariants.
- `workflows/engine.py`: route/context gate, turn ordering, replay, collection and confirmation.
- `workflows/definitions.py`, `registry.py`, `transitions.py`: implemented capabilities and preservation.
- `workflows/repository.py`, `application.py`: domain persistence boundary and event composition.
- `db/workflows.py`, `db/models.py`, migration `0002_workflow_state.py`: optimistic durable storage.
- `tests/unit/test_workflows.py`, `tests/integration/test_workflows.py`, support helper: behavior evidence.
- `docs/workflows.md`, README and architecture/data/event/state/scope docs: concrete Phase 4 contracts.

Remaining decisions are documented in workflows.md: operational executor/idempotency/result
contracts; authentication/session access; context refresh/catalog revisions; retention and bounded
history; natural-language field extraction; scheduling validation and richer leasing branches;
live-model semantic calibration. No such implementation is included here. Latest-turn replay is
supported; older turns fail closed rather than implementing a historical command ledger. Lifecycle
logs are observations inside the caller's transaction, not a durable event/commit ledger.

## Environment note

Windows Application Control blocks the local Python interpreter, so pytest/mypy ran in Docker.
Ruff's installed native executable ran locally. Docker initially failed on inaccessible stale
runtime sockets; the stale Docker Desktop runtime directory (two zero-byte sockets only) was set
aside and Docker Desktop was repaired. On reinspection its 28.5.1 engine was healthy and all
required checks then ran successfully. No factory reset was issued and no development volume was
deleted. The separate test containers are removed after verification.

## Resulting repository tree

Runtime environments, caches and bytecode are excluded. Source listing follows.

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
      api/
        __init__.py
        health.py
        router.py
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
        base.py
        models.py
        repository.py
        seed_data.py
        seed.py
        session.py
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
          protocol.py
        stt/
          __init__.py
        tts/
          __init__.py
      realtime/
        __init__.py
      routing/
        __init__.py
        configured.py
        model.py
        router.py
        schemas.py
        validation.py
      tools/
        __init__.py
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
    tests/
      __init__.py
      .gitkeep
      conftest.py
      integration/
        __init__.py
        conftest.py
        test_database_readiness.py
        test_property_domain.py
        test_routing_domain.py
        test_workflows.py
      support/
        __init__.py
        routing.py
        workflows.py
      test_config.py
      test_health.py
      test_observability.py
      test_property_contracts.py
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
    phase-1-verification.md
    phase-2-verification.md
    phase-3-verification.md
    phase-4-verification.md
    routing-benchmarks.md
    routing.md
    scope.md
    state-machine.md
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
