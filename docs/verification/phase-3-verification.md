# Phase 3 verification — property-scoped service routing

Verified 2026-10-11. Phase 3 implementation and deterministic verification are complete. **Live model evaluation remains pending because no endpoint/model/credentials were configured.** This is not a claim of external semantic benchmarking. Phase 4 has not started.

## Architecture review

The design was checked against the architecture/scope/data/event/state/failure documents, the six existing ADRs, both prior verification reports, and the actual PropertyContext schemas, resolver, repository, seeds and test infrastructure.

ADR 006's operative decision is: after the property-context gate, use explicit/configured cues first, constrained model interpretation when unresolved, and validate all proposed services against the enabled catalog. Clarify/abstain on insufficient evidence, preserve decision evidence/source/context identity, and leave business effects to their owning layers. There was no material conflict with the Phase 3 scope. Workflow continuation/escalation rules are not implemented in this stateless phase.

Evidence design: conservative whole-utterance equality over supplied effective text, unique service/scenario only, no keyword dictionary, priority tie-break or embeddings. The model protocol is paired with backend catalog-membership validation. PropertyContextResolver is reused unchanged; its repository projection now adds existing scenario UUIDs. ADRs 001–006 remain unchanged. ADR 007 records the new, narrower decision to render clarification questions from validated service choices.

## Exact checks

| Check | Final result |
| --- | --- |
| Dependency sync / lockfile | `uv sync` succeeded; 40 locked packages. HTTPX moved from dev-only to runtime dependencies. |
| Full pytest with PostgreSQL | **120 passed in 6.56s**, no warnings/skips. Python 3.12.15, pytest 9.1.1, PostgreSQL 17.11. |
| Normal suite with Docker networking disabled | **101 passed, 19 deselected in 2.37s** (`-m 'not integration'`). |
| PostgreSQL coverage | All 19 integration cases passed within the full run, including the three new Phase 3 cases. |
| Migration safeguards | Empty → upgrade → downgrade → upgrade, six domain tables, and `alembic check`: **No new upgrade operations detected.** No migration changes. |
| Ruff | **All checks passed!** |
| Ruff format | **83 files already formatted**. |
| Strict mypy | **Success: no issues found in 65 source files**. Includes evaluations. |
| Offline evaluation | **146 cases / 10 configurations**, completed with Docker `--network none`; actual artifact saved. |
| Runtime image and packaged CLI | `docker compose build backend` succeeded; non-root runtime CLI completed the same 146 cases with `--network none`, no source mounts or dev dependencies. Outcome counts matched the saved report. |
| Live evaluation | **PENDING** — routing URL/name/key absent and no project `.env`; no live request attempted. |
| Test environment cleanup | Isolated Compose test containers/network removed with `down`; development volume untouched. |
| Final inspection | Both Compose `ps -a` listings empty; all local Markdown file links resolve; production routing/provider source has no demo keys or ORM imports. |

The local uv bootstrap is still `..\.tools\bin\uv.exe`, with project UV_CACHE_DIR. Windows Application Control blocked the local Python executable (OS error 4551), so pytest/mypy ran in the existing Docker test image. Ruff ran locally. No lint/type rules, database isolation checks or migration safeguards were weakened. Initial validation found one inferred-list type error and a deprecated Pydantic test access; both were corrected before the final checks.

Commands used for the final source, from the project root (PowerShell):

```powershell
# Existing image was built using compose.test.yml; mounted paths select final workspace source.
docker compose -f compose.test.yml up -d --wait postgres
docker compose -f compose.test.yml run --rm --no-deps --volume './backend:/app/backend:ro' --volume './evaluations:/app/evaluations:ro' tests pytest -q -s

docker run --rm --network none --volume "${PWD}/backend:/app/backend:ro" --volume "${PWD}/evaluations:/app/evaluations:ro" relay-phase2-tests-tests pytest -q -m 'not integration'
docker run --rm --network none --volume "${PWD}/backend:/app/backend:ro" --volume "${PWD}/evaluations:/app/evaluations:ro" relay-phase2-tests-tests mypy
docker run --rm --network none --volume "${PWD}/backend:/app/backend:ro" --volume "${PWD}/evaluations:/app/evaluations" relay-phase2-tests-tests relay-eval-routing --offline --output /app/evaluations/results/offline.json

uv run --locked ruff check .
uv run --locked ruff format --check .
docker compose -f compose.test.yml down
```

The portable clean-build full-suite command remains `docker compose -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests`. The test project retains its established `relay-phase2-tests` name to preserve the existing isolated environment; it now runs the complete Phase 3 suite.

## Demonstrated catalogs and outcomes

The same resolver and router consume these actual PostgreSQL demo catalogs:

| Property | Enabled keys |
| --- | --- |
| Atlas Residence | emergency_maintenance, emergency_non_maintenance, general_information, human_handoff, leasing, non_emergency_maintenance, renewal, resident_support |
| Palm Heights | general_information, human_handoff, leasing, maintenance, parking, security |

| Demonstration | Observed result and evidence |
| --- | --- |
| Strong configured match | `My AC isn't cooling properly.` → Atlas non_emergency_maintenance / ac_not_cooling, CONFIGURED_EVIDENCE. Model never called. |
| Independent later direct turn | `Water is pouring across my kitchen floor.` → emergency_maintenance / active_flooding. No sticky previous route. |
| Required shorter AC/leak utterances | PostgreSQL integration test routes `My AC isn't cooling.` and `Actually water is pouring out of it.` independently to their valid services with scripted model proposals. This proves routing/validation plumbing, not live interpretation quality. |
| Atlas gas interpretation | `I smell gas in my apartment.` with scripted provider → valid Atlas emergency_maintenance / gas_smell. |
| Palm receives same Atlas UUID | ABSTAINED / MODEL_CONTRACT_VIOLATION / invalid_service. No selected service. |
| Overlapping configuration | Lofts `I need account help.` matches Resident Desk and Billing Desk; invokes model and returns clarification. |
| Grounded clarification | `Is your request about Resident Desk or Billing Desk?` rendered from validated current IDs; arbitrary question fields rejected. |
| Insufficient information | Generic clarification returns `What would you like help with at this property?`, with no selected service. |
| Out-of-scope/unavailable | Valid model abstention is preserved; the offline dataset also rejects Palm renewal and Cedar maintenance requests. |
| Invalid model output | Invented/foreign services, wrong/inactive scenarios and unavailable clarification choices safely abstain. |
| Timeout/failure | Deadline cancels the awaited fake; result is MODEL_UNAVAILABLE with no route; exceptions are sanitized. Caller cancellation propagates. |
| Real adapter contract | MockTransport verifies strict JSON schema request, response parsing, refusal/truncation/tool-call rejection, transport errors and zero retries. No live API. |

### Database-only dynamic services

The PostgreSQL integration test parametrizes Pet Services and Package Lockers. It first resolves Atlas without each service and observes abstention. It then inserts a Service, enabled PropertyService and active scenario/example through SQLAlchemy, resolves a fresh context with the unchanged PropertyContextResolver, and routes the example directly. Pet Services also receives policy and fees scenarios.

Observed printed proof:

```text
Database-only pet_services: How do I register my dog? -> pet_services; routing source unchanged
Database-only package_lockers: Where is my locker code? -> package_lockers; routing source unchanged
```

The test compares a hash of all routing source files before and after insertion, verifies actual service/scenario UUIDs in the result, and additionally accepts a scripted model route to the new service. After deactivating its scenario, the same scenario ID is rejected; after disabling the association, its service ID is rejected. All writes roll back in the isolated test database. Only the two existing polished demo properties remain in seed data.

## Evaluation results and limits

See [benchmarks](../routing-benchmarks.md) and [the measured JSON artifact](../../evaluations/results/offline.json). Actual offline results: route correctness 72/90, clarification correctness 12/21, abstention correctness 33/35, overall correctness 117/146. Zero accepted invalid service IDs, invalid scenario IDs, cross-property leaks or confident wrong routes. Direct evidence routes: 68; model-double fallback: 72; model-double routes: 4; clarifications: 24; abstentions: 50. Total p50/p95: 0.443/1.723 ms. All are offline measurements.

The offline lexical double is intentionally imperfect and independent of authored expectations. Its 29 misses are documented, including hazard paraphrases and human requests. The deterministic suite separately tests all required valid/invalid model outcomes. No live accuracy or live latency has been invented. Synthetic Cedar models the post-filter catalog with Parking unavailable; actual disabled-row behavior is established by PostgreSQL tests.

## Explicit acceptance checklist

| Criterion | Status / evidence |
| --- | --- |
| Existing PropertyContextResolver reused | PASS — resolver unchanged; scenario ID added only to projection/schema. |
| Router consumes PropertyContext | PASS — RoutingRequest contract; no ORM imports in routing. |
| No global service taxonomy | PASS — keys are data; production algorithms import no demo list. |
| Cannot select absent service | PASS — common backend ID lookup; foreign/invented UUID tests. |
| Side-effect-free routing | PASS — no DB, PMS, operational tool or workflow calls. |
| ADR 006 cascade respected | PASS — configured → model when unresolved → validation → typed outcome. |
| Generic configured evidence exists | PASS — same normalized comparison over supplied entries. |
| Evidence data-driven | PASS — names, effective descriptions and scenario examples. |
| Active scenarios contribute | PASS — direct example tests and database proofs. |
| Disabled services excluded | PASS — fresh resolver omits disabled association; model ID rejected. |
| Inactive scenarios excluded | PASS — fresh resolver omits scenario; direct evidence absent and model ID rejected. |
| Ambiguous evidence falls through | PASS — cross-service and same-service scenario overlap tests. |
| No service-specific keyword dictionary | PASS — production source inspection and dynamic insertion. |
| Provider-neutral interface | PASS — RoutingModelProvider async protocol. |
| Real configurable adapter | PASS — HTTPX strict structured-output adapter, transport contract tests. Live interoperability pending. |
| Deterministic fake provider | PASS — scripted test provider and independent evaluation lexical double. |
| Structured model output | PASS — Pydantic ModelRoutingResponse; strict schema and extra-field rejection. |
| Only current catalog sent to model | PASS — minimal projection and Palm payload assertions. |
| Model can route, clarify or abstain | PASS — all three exercised. |
| Backend rejects invalid selection | PASS — foreign service/scenario and invalid clarification tests. |
| Timeout/failure safe | PASS — bounded deadline, no guessed route, no retry loop. |
| ROUTED exists | PASS — selected service required, canonical snapshot metadata. |
| CLARIFICATION_REQUIRED exists | PASS — question required, no selected service. |
| ABSTAINED exists | PASS — empty/out-of-scope/failure cases, no selection. |
| Method/evidence/reason represented | PASS — generic enums, typed evidence, reason codes and concise explanations. |
| Service property-scoped | PASS — active snapshot membership validated for both stages. |
| Optional scenario validated | PASS — identity and selected-service relationship checked. |
| Atlas/Palm isolation passes | PASS — actual PostgreSQL catalogs and adversarial provider. |
| Palm cannot return Atlas-only service | PASS — explicit rejection plus dataset cross-property cases. |
| Evaluation leakage zero | PASS — 0/146 accepted cross-property routes. |
| Invalid service selections zero | PASS — 0/146; rejected adversarial attempts tested separately. |
| New service needs no routing-source changes | PASS — source hashes unchanged across database inserts. |
| Pet Services demonstration | PASS — requested dog-registration utterance routes to inserted association/scenario. |
| Database-only custom behavior | PASS — Pet Services and Package Lockers, both direct and model paths. |
| Ambiguous inputs can clarify | PASS — overlap and weak-evidence tests. |
| Unavailable/out-of-scope can abstain | PASS — dataset and scripted contract tests. |
| No forced classification | PASS — clarification/abstention retain null selection. |
| Reproducible evaluation harness | PASS — explicit CLI, dataset hash, observable per-case report. |
| Demo and synthetic configurations | PASS — 10 configurations, only two database demo properties. |
| Approximately 120–250 curated cases | PASS — 146 authored cases, 90 route / 21 clarify / 35 abstain. |
| Actual metrics reported | PASS — JSON report and benchmark document; offline clearly labeled. |
| Confident wrong routes tracked | PASS — empty actual list; deliberate wrong-route scorer test. |
| Configured-evidence rate | PASS — 68/146 = 46.58%. |
| Model fallback rate | PASS — 72/146 = 49.32%, offline double. |
| p50/p95 latency | PASS — monotonic measured durations; offline only. |
| No fabricated metrics | PASS — report from actual execution; live pending explicitly. |
| Existing Phase 1/2 tests green | PASS — all previous 54 included in full 120. |
| New Phase 3 tests green | PASS — 66 new test cases. |
| PostgreSQL integration green | PASS — 19 integration cases, real migrations and rollback. |
| Ruff | PASS — all checks passed. |
| Formatting | PASS — 83 files already formatted. |
| mypy | PASS — strict checks, 65 source files. |
| No voice | PASS — realtime remains reserved. |
| No STT/TTS | PASS — provider packages remain empty markers. |
| No workflows | PASS — workflow package remains reserved. |
| No business side effects | PASS — routing only reads values, interprets, validates and logs. |
| No PMS integration | PASS — no PMS adapter added. |
| No operational human handoff | PASS — service choice only, no contact/delivery. |
| No multi-agent framework | PASS — no orchestration dependency or implementation. |
| No frontend feature implementation | PASS — frontend remains reserved. |

## Important files changed

| File(s) | Purpose |
| --- | --- |
| `backend/app/routing/schemas.py` | Request, decision, method/reason and evidence types. |
| `routing/configured.py` | Generic conservative direct-match policy. |
| `routing/model.py` | Minimal provider request and strict interpretation response. |
| `routing/validation.py` | Service/scenario membership, clarification rendering, catalog fingerprint. |
| `routing/router.py` | Stateless cascade, deadline, safe failures, timings and logs. |
| `providers/llm/protocol.py`, `openai_compatible.py` | Neutral seam, normalized errors and one real HTTP adapter. |
| `property/schemas.py`, `db/repository.py` | Actual scenario IDs in the existing ORM-free projection. |
| `core/config.py`, `.env.example` | Validated opt-in routing settings and redacted API key. |
| `evaluations/` | Ten contexts, 146 authored cases, independent offline double, CLI/scorer and actual report. |
| `backend/tests/test_routing*.py`, `tests/support/routing.py` | Deterministic routing, failure, adapter and metric tests. |
| `tests/integration/test_routing_domain.py` | Real database-only service routing, filtering and Atlas/Palm proof. |
| `tests/conftest.py` | Block real HTTP transports during all standard tests. |
| `pyproject.toml`, `uv.lock`, `Dockerfile`, `.dockerignore` | Runtime HTTPX, evaluation entry point/package/assets, strict evaluation typing and image packaging. |
| README, architecture/scope/data-model docs | Current phase, commands, boundaries and scenario projection update. |
| `docs/routing.md`, `routing-benchmarks.md`, ADR 007 | Routing policy, measured results/limits, grounded clarification decision. |

## Remaining decisions

The configured live provider/model and its measured quality/latency remain unverified. Future work must establish semantic acceptance targets and held-out multilingual/adversarial coverage; durable catalog revision/reload policy; workflow continuation, transitions and emergency escalation; scenario-specific/localized clarification and session budgets; operational retention/telemetry; voice transports/providers; tool idempotency/reconciliation; and handoff destinations/acknowledgment. No embeddings or vector store were added. None of these authorize Phase 4 here.

No commits, pushes or remote deployments were made. The existing development database was not altered by Phase 3 verification.

## Resulting repository tree

Generated environments, caches, bytecode and Git internals are omitted. The tree below includes the saved measured artifact.

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
    migrations/
      env.py
      script.py.mako
      versions/
        0001_property_domain.py
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
      support/
        __init__.py
        routing.py
      test_config.py
      test_health.py
      test_observability.py
      test_property_contracts.py
      test_routing_adapter.py
      test_routing_evaluation.py
      test_routing.py
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
    routing-benchmarks.md
    routing.md
    scope.md
    state-machine.md
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
