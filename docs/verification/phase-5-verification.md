# Phase 5 verification

Verified 2026-10-12. Scope: trusted tools, local transactional operations, durable audit and
idempotency. Phase 6 has not started.

## Executed checks

| Check | Result |
| --- | --- |
| Full pytest suite with real PostgreSQL 17 | **163 passed in 13.29s**: all 141 existing tests plus 22 Phase 5 tests. |
| New Phase 5 PostgreSQL tests | 22 passing cases, including parameterized ownership and failure cases. |
| Initial deterministic suite without network/services | 117 passed, 24 integration tests deselected before new execution tests were added. |
| Ruff | All checks passed. |
| Ruff formatting | 112 files already formatted. |
| Strict mypy | Success: no issues found in 89 source files. |
| Migration | Empty → 0003 head → 0002 → head → base → head; fixture teardown returns to base. |
| Alembic check | No new upgrade operations detected. Thirteen domain tables match metadata. |

The tests use the existing isolated `compose.test.yml` project, PostgreSQL tmpfs and the
dedicated `relay_test` database, without a published database port or the development volume.
Windows Application Control prevents running local Python; Python checks ran in the existing
test image with current backend/evaluation source mounted read-only. Native Ruff ran locally.
No paid model calls or external PMS calls were made. This does not resolve Phase 3's pending
live-model semantic benchmark.

Commands used from the repository root:

```powershell
docker compose -f compose.test.yml up -d --wait postgres
docker compose -f compose.test.yml run --rm --no-deps --volume './backend:/app/backend:ro' --volume './evaluations:/app/evaluations:ro' tests pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
docker run --rm --network none --volume "${PWD}/backend:/app/backend:ro" --volume "${PWD}/evaluations:/app/evaluations:ro" relay-phase2-tests-tests mypy
docker compose -f compose.test.yml down
```

The session-scoped PostgreSQL fixture runs Alembic upgrade/downgrade/check before data tests.
It refuses a nonempty database or a database name other than relay_test. Operational tests use
real commits and independent sessions; they do not infer transaction guarantees from SQLite or
mock storage. Commit-failure seams inject errors around actual PostgreSQL transactions.

## Demonstrations and test evidence

All following tests are in `backend/tests/integration/test_tools.py`.

| Evidence | Demonstrated behavior |
| --- | --- |
| test_backend_flagship_create_retry_update_and_handoff | Atlas / Ikram / 204 moves from non-emergency to emergency; commits one incident; three retries return its same UUID; explicit update changes hazard to water near electrical outlet and revision to 2; update replay preserves revision/UUID; transition and trusted confirmation create a handoff referencing that incident and known context. One incident and one handoff remain. |
| test_confirmation_is_persisted_and_maintenance_commits | Before confirmation, no maintenance row; forged confirmation policy rejected. Trusted Phase 4 confirmation permits a committed OPEN maintenance row; audit attempt_count becomes 2. |
| test_concurrent_duplicate_execution_and_process_boundary | Two concurrent callers return the same incident UUID; exactly one is a replay; one incident and one logical tool call. A new Database/engine then replays the same result. This tests a fresh connection pool, not an operating-system restart. |
| test_payload_conflict_never_reuses_success | Reusing the action key with altered hazard data returns IDEMPOTENCY_CONFLICT, no success ID, and no second incident. Original request still replays. |
| test_tour_requires_explicit_aware_time | Confirmed free text fails validation with no tour. Explicit aware time plus new trusted confirmation commits one REQUESTED tour with the exact supplied time. |
| test_invalid_source_has_no_side_effect (7 cases) | Wrong property, Palm unit, nonexistent session/workflow, cancellation, supersession and disabled service all fail without an incident. |
| test_maintenance_rejects_palm_unit_and_blank_description | Atlas maintenance with Palm unit 318 fails property validation; whitespace-only description fails payload validation; neither creates a maintenance row. |
| test_database_rejects_foreign_unit_without_application_validation | A direct database update attempting to attach a Palm unit to an Atlas incident raises PostgreSQL IntegrityError; original ownership remains. |
| test_second_create_in_same_run_hits_real_database_constraint | A different create action for the same emergency run hits PostgreSQL uniqueness, produces BUSINESS_CONSTRAINT and a FAILED audit, and leaves exactly one incident. |
| test_rollback_and_commit_failure_never_report_success (2 cases) | Failure after an adapter flush rolls back the operation and persists a safe FAILED receipt; failure before commit rolls back the entire transaction. Neither returns an ID nor emits success. |
| test_success_is_not_visible_before_commit | Independent connection sees neither the flushed incident nor its receipt before commit, and no success log exists. After commit/verification both rows exist and success carries the UUID. |
| test_lost_commit_ack_reconciles_without_second_write | Error after a real commit is reconciled to the durable success; one incident remains. |
| test_unavailable_commit_verification_is_unknown | A committed operation with unavailable verification returns UNKNOWN_OUTCOME and no success ID. Later verification replays the real receipt without another incident. |
| test_invalid_payload_and_unregistered_action_fail_safely | Invalid typed payload and arbitrary action name fail safely with no incident. |
| test_update_rejects_foreign_incident_and_never_creates | An unknown incident target fails ownership validation; the real incident stays unchanged at revision 1. The SQL adapter additionally scopes lookup to property/session/workflow. |

The flagship checks persisted audit replay_count/attempt_count, incident revision, context
identity and JSON serialization without SQLAlchemy state. Successful ToolResults are checked
against actual committed rows, not merely mocked UUIDs.

## Architecture correspondence

ADR 006 was read and remains unchanged. Phase 5 preserves the property-context gate,
explicit/configured routing stages, constrained model stage, catalog membership validation,
clarification and abstention. It adds no classifier or service taxonomy. Existing routing
tests—including database-configured services and out-of-property proposal rejection—remain
green. The executor performs an additional current-context authorization check before writing;
it does not reroute requests.

ADR 003 becomes concrete: workflow/model declarations cannot perform effects or establish
success; a trusted backend transaction and verification do. ADRs 001/005 are enforced through
trusted property identity, fresh catalog eligibility and composite ownership constraints.
ADR 002 is reflected by the ORM-free operations protocol. ADR 004 is preserved because durable
operation receipts are independent of presentation cancellation and workflow supersession.

No architecture conflict required a replacement ADR. The existing ActionRequest UUID already
supports idempotency. Small backward-compatible extensions add an emergency update payload,
explicit incident target, aware tour time and source workflow reference in handoff context.
Service catalog, workflow registry and tool registry remain three distinct capabilities.

## Acceptance checklist

### Execution boundary

- [x] ActionRequest remains a request, not success.
- [x] ToolExecutor exists and owns execution coordination.
- [x] A static trusted tool registry exists.
- [x] Inputs are typed using the existing ActionRequest/payload contracts.
- [x] Outputs are typed discriminated operational results.
- [x] ToolResult is ORM-independent and JSON serializable.

### Operations

- [x] Maintenance requests can be committed.
- [x] Emergency incidents can be committed.
- [x] Emergency incidents can be updated in place.
- [x] Leasing tour requests can be committed with structured time.
- [x] Human handoff requests can be committed.

### Transactions

- [x] Successful ToolResult is returned only after commit and fresh verification.
- [x] Failed transactions never produce success.
- [x] Rollback leaves no partially committed local side effect.

### Idempotency

- [x] Stable action identity and a database-unique idempotency key exist.
- [x] Same request replays its persisted receipt.
- [x] Replay does not duplicate an operation.
- [x] Concurrent real PostgreSQL duplicates converge on one operation.
- [x] Different payload under the same key is rejected.

### Security and ownership

- [x] Trusted session/workflow state and exact declaration are validated.
- [x] Property isolation is enforced in application checks and composite database FKs.
- [x] Confirmation is validated from persisted state, never a caller-only flag.
- [x] Unsupported action names fail safely and cannot select arbitrary code.

### Emergency demo

- [x] One incident is created for the flagship flow.
- [x] Replays retain one incident and the same UUID.
- [x] New hazard information updates that same incident.
- [x] Update replay does not increment revision or duplicate hazard content.

### Handoff

- [x] One handoff record is created.
- [x] Known business context and real incident identity are retained.
- [x] No telephony transfer occurs or is claimed.

### Audit

- [x] Durable tool-call receipts exist with validated request and source identity.
- [x] Status distinguishes PENDING, SUCCEEDED, FAILED and UNKNOWN_OUTCOME.
- [x] Committed operation ID is persisted atomically with successful result.
- [x] Attempts, replay and safe failures are observable through audit fields and logs.

### Architecture

- [x] Operations adapter protocol is provider-neutral.
- [x] Local PostgreSQL adapter implements all five operations.
- [x] No external PMS is integrated.
- [x] Workflow code performs no direct operational writes.
- [x] Routing architecture is unchanged; dynamic services remain independent of registries.

### Quality

- [x] All Phase 1–4 tests remain green.
- [x] All Phase 5 tests pass.
- [x] Real PostgreSQL transaction/concurrency tests pass.
- [x] Migration round trips and Alembic check pass.
- [x] Ruff passes.
- [x] Formatting passes.
- [x] Strict mypy passes.

### Boundaries

- [x] No voice implementation.
- [x] No STT/TTS.
- [x] No realtime transport.
- [x] No final agent response generation or automatic end-to-end orchestrator.
- [x] No multi-agent framework.
- [x] Phase 6 has not started.

## Important changed files

- `tools/{schemas,protocol,registry,validation,executor,errors}.py`: ORM-free contracts,
  trusted dispatch, exact-source authorization, canonical hash, commit/verification coordination.
- `db/{operation_models,operations,tool_store}.py`: five persisted models, local adapter,
  locking, audit, savepoints, commit ownership and reconciliation.
- `db/models.py`, migration env and `0003_operational_tools.py`: supporting constraints,
  metadata registration and reversible explicit schema.
- `workflows/{schemas,definitions,engine,transitions}.py`: minimal compatible update/time/context
  extensions; no competing ActionRequest and no direct side effects.
- Integration fixture/schema expectation and `test_tools.py`: migrations and real execution tests.
- README, architecture, data model, tools documentation and this report: implemented boundaries
  and verification evidence. Earlier verification reports remain historical.

## Unresolved decisions and limits

- Future orchestration must consume verified receipts before updating workflow completion or
  speaking success. `completed_actions` stays empty in Phase 5.
- Authentication/authorization at a public transport boundary, emergency dispatch/contact
  policies, human acknowledgment and delivery are not implemented by this internal component.
- External PMS needs provider idempotency and reconciliation; PostgreSQL atomicity cannot cover
  an external side effect. Unknown external outcomes must not be blindly retried.
- Tour availability, timezone interpretation of natural language, calendar booking and final
  confirmation remain future concerns. Current tools require an explicit aware timestamp.
- Audit retention/redaction and an append-only attempt history need an operational policy.
  One logical row retains the latest attempt/result and counters. Database outages cannot promise
  durable failure logging; malformed ownership attempts cannot corrupt another action's receipt.
- Session locking deliberately serializes writes within one conversation. Throughput/fairness,
  lock timeouts and future long-running external operations need separate production design.
- Live-model routing calibration remains unverified; deterministic coverage is not a claim of
  live classification accuracy. No Phase 3 routing thresholds were changed.

## Resulting repository tree

Generated inventory follows; runtime environments, caches, secrets and Git internals are excluded.
```text
relay-property-voice-agent/
  .dockerignore
  .env.example
  .github/workflows/.gitkeep
  .gitignore
  .python-version
  alembic.ini
  backend/app/__init__.py
  backend/app/agent/__init__.py
  backend/app/api/__init__.py
  backend/app/api/health.py
  backend/app/api/router.py
  backend/app/core/__init__.py
  backend/app/core/application.py
  backend/app/core/config.py
  backend/app/core/errors.py
  backend/app/core/lifecycle.py
  backend/app/core/logging.py
  backend/app/core/middleware.py
  backend/app/core/server.py
  backend/app/db/__init__.py
  backend/app/db/base.py
  backend/app/db/models.py
  backend/app/db/operation_models.py
  backend/app/db/operations.py
  backend/app/db/repository.py
  backend/app/db/seed_data.py
  backend/app/db/seed.py
  backend/app/db/session.py
  backend/app/db/tool_store.py
  backend/app/db/workflows.py
  backend/app/main.py
  backend/app/metrics/__init__.py
  backend/app/property/__init__.py
  backend/app/property/context.py
  backend/app/property/errors.py
  backend/app/property/repository.py
  backend/app/property/schemas.py
  backend/app/providers/__init__.py
  backend/app/providers/embeddings/__init__.py
  backend/app/providers/llm/__init__.py
  backend/app/providers/llm/openai_compatible.py
  backend/app/providers/llm/protocol.py
  backend/app/providers/stt/__init__.py
  backend/app/providers/tts/__init__.py
  backend/app/realtime/__init__.py
  backend/app/routing/__init__.py
  backend/app/routing/configured.py
  backend/app/routing/model.py
  backend/app/routing/router.py
  backend/app/routing/schemas.py
  backend/app/routing/validation.py
  backend/app/tools/__init__.py
  backend/app/tools/errors.py
  backend/app/tools/executor.py
  backend/app/tools/protocol.py
  backend/app/tools/registry.py
  backend/app/tools/schemas.py
  backend/app/tools/validation.py
  backend/app/workflows/__init__.py
  backend/app/workflows/application.py
  backend/app/workflows/definitions.py
  backend/app/workflows/engine.py
  backend/app/workflows/errors.py
  backend/app/workflows/registry.py
  backend/app/workflows/repository.py
  backend/app/workflows/schemas.py
  backend/app/workflows/transitions.py
  backend/migrations/env.py
  backend/migrations/script.py.mako
  backend/migrations/versions/0001_property_domain.py
  backend/migrations/versions/0002_workflow_state.py
  backend/migrations/versions/0003_operational_tools.py
  backend/tests/__init__.py
  backend/tests/.gitkeep
  backend/tests/conftest.py
  backend/tests/integration/__init__.py
  backend/tests/integration/conftest.py
  backend/tests/integration/test_database_readiness.py
  backend/tests/integration/test_property_domain.py
  backend/tests/integration/test_routing_domain.py
  backend/tests/integration/test_tools.py
  backend/tests/integration/test_workflows.py
  backend/tests/support/__init__.py
  backend/tests/support/routing.py
  backend/tests/support/workflows.py
  backend/tests/test_config.py
  backend/tests/test_health.py
  backend/tests/test_observability.py
  backend/tests/test_property_contracts.py
  backend/tests/test_routing_adapter.py
  backend/tests/test_routing_evaluation.py
  backend/tests/test_routing.py
  backend/tests/unit/test_workflows.py
  compose.test.yml
  docker-compose.yml
  Dockerfile
  docs/architecture.md
  docs/data-model.md
  docs/decisions/001-property-context-before-routing.md
  docs/decisions/002-provider-abstractions.md
  docs/decisions/003-tools-own-side-effects.md
  docs/decisions/004-cancellable-presentation-state.md
  docs/decisions/005-property-specific-services.md
  docs/decisions/006-routing-cascade.md
  docs/decisions/007-constrained-clarification.md
  docs/event-model.md
  docs/failure-modes.md
  docs/phase-1-verification.md
  docs/phase-2-verification.md
  docs/phase-3-verification.md
  docs/phase-4-verification.md
  docs/phase-5-verification.md
  docs/routing-benchmarks.md
  docs/routing.md
  docs/scope.md
  docs/state-machine.md
  docs/tools.md
  docs/workflows.md
  evaluations/__init__.py
  evaluations/.gitkeep
  evaluations/cases.tsv
  evaluations/catalogs.py
  evaluations/offline_provider.py
  evaluations/results/offline.json
  evaluations/run_routing.py
  frontend/.gitkeep
  pyproject.toml
  README.md
  uv.lock
```
