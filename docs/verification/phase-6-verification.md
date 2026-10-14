# Phase 6 verification

Verified 2026-10-12. Implemented text-turn orchestration, response planning/rendering, trusted
completion reconciliation and an internal/demo HTTP interface. Phase 7 has not started.

## Executed checks

| Check | Observed result |
| --- | --- |
| Complete pytest suite with PostgreSQL 17 | **209 passed in 21.81s**. |
| Regression coverage | All **163 Phase 1–5 tests** remain green. |
| New Phase 6 coverage | **46 tests**: 20 PostgreSQL integration cases and 26 contract/rendering cases. |
| Strict mypy | Success: no issues found in **104 source files**. |
| Ruff | All checks passed. |
| Formatting | All files passed the formatting check. |
| Alembic parity | No new upgrade operations detected. |
| Migration round trips | Empty → 0004 head → 0003 → head → 0002 → head → base → head. Fixture teardown returns to base. |

Python runs in the existing Docker test image because Windows Application Control blocks local
Python. Current backend/evaluation sources are mounted read-only. Native Ruff runs locally.
The PostgreSQL fixture uses the isolated relay-phase2-tests project, dedicated relay_test database
and tmpfs storage; it refuses a nonempty starting database. Development volumes are untouched.
No live external model calls, external PMS calls, audio or voice providers were used.

Commands from the repository root:

```powershell
docker compose -f compose.test.yml up -d --wait postgres
docker compose -f compose.test.yml run --rm --no-deps --volume './backend:/app/backend:ro' --volume './evaluations:/app/evaluations:ro' tests pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
docker run --rm --network none --volume "${PWD}/backend:/app/backend:ro" --volume "${PWD}/evaluations:/app/evaluations:ro" relay-phase2-tests-tests mypy
docker compose -f compose.test.yml down
```

Migration upgrade/downgrade and metadata parity are executed by the real PostgreSQL fixture,
before integration scenarios. Fourteen domain tables are present at head. No historical migration
was edited. The Phase 5 downgrade check remains present alongside the new Phase 6 round trip.

## End-to-end evidence

The flagship runs the actual ServiceRouter, WorkflowEngine, ToolExecutor and PostgreSQL adapters
through TurnOrchestrator. Ambiguous/free-form routing uses a deterministic provider stub: this
proves orchestration and backend invariants, not live-model semantic quality. Standard tests block
live HTTP transports. The optional phrasing adapter is tested using an HTTP mock.

`test_flagship_text_and_durable_replay` demonstrates:

1. “My AC isn't cooling.” routes to Non-Emergency Maintenance and asks for symptoms.
2. Reported warm-air details produce a maintenance confirmation proposal, without execution.
3. “Actually water is pouring out of it.” transitions to Emergency Maintenance and collects
   location, hazard and severity as text replies.
4. ToolExecutor commits an incident; only then does the response contain its real UUID and a
   success clause. The database has exactly one incident.
5. Replaying that complete turn through a fresh orchestrator/store returns identical saved
   response text and plan, with no route/model work and no second incident.
6. “Wait, the water is getting close to an electrical outlet.” changes the emergency scenario
   and updates the **same incident UUID** to revision 2. It does not start another incident.
7. “I want to speak with someone.” transitions to Human Handoff and requests confirmation.
8. “Yes, please.” creates one handoff linked to that real incident. The response explicitly says
   no live transfer occurred. A durable structured summary includes known identity, reported
   facts and verified operations. Three completion records exist: create, update and handoff.
9. The older emergency-create turn still replays after these later transitions.

## Test-to-requirement mapping

Integration tests live in `backend/tests/integration/test_agent.py`.

| Tests | Verified behavior |
| --- | --- |
| test_flagship_text_and_durable_replay | Complete flagship flow, question/proposal/tool sequencing, actual IDs, same-incident update, handoff, completion records, durable historical response replay. |
| test_confirmation_outcomes (4 cases) | “go ahead” and “yes” authorize through persisted confirmation and create maintenance; rejection cancels without a row; “maybe” asks again without a row. |
| test_stale_yes_cannot_confirm_changed_proposal | Explicit symptom changes retire the prior proposal; a yes carrying the previous revision is rejected; current confirmation can execute. |
| test_uncertain_routing_has_no_workflow_or_tool (2 cases) | CLARIFICATION_REQUIRED and ABSTAINED return normally with no workflow invocation or tool calls. |
| test_simultaneous_turns_reject_busy_without_corruption | Two different concurrent turns for one session yield one accepted turn and one SESSION_BUSY; one workflow and one turn progression remain. |
| test_non_success_never_completes_or_claims_success (2 cases) | FAILED and UNKNOWN_OUTCOME never populate completion records, operation facts or success wording; uncertainty remains explicitly unresolved and replayable. |
| test_crash_after_tool_commit_recovers_receipt | Interruption after a real tool commit resumes by receipt verification and reconciles success without a second incident/tool execution. |
| test_crash_before_dispatch_remains_unknown_without_retry | Dispatch checkpoint followed by interruption before the call's effect yields uncertainty on recovery, not another dispatch. |
| test_unverified_success_receipt_cannot_supply_fake_reference | An invented executor success/UUID with no matching durable receipt becomes UNKNOWN; the fake UUID never enters plan or response. |
| test_dynamic_database_service_is_safe_without_workflow | Database-only Pet Services plus a fresh context routes “How do I register my dog?” through configured evidence, returns UNSUPPORTED_WORKFLOW safely, and executes no tool. No routing-source modifications are made for this service. |
| test_internal_http_turn_and_injection_rejection | Actual ASGI POST returns AgentTurnResult; exact replay is durable; changed contents conflict; missing sessions fail; property/unit/action/result/confirmation injection returns 422. Backend-created Alex session resolves Palm. Production setting disables endpoints. |
| test_unknown_outcome_is_not_automatically_dispatched_again | A later relevant turn performs reconciliation only; executor call count stays one and no incident appears. |
| test_explicit_iso_tour_time_flows_through_text | A timezone-aware ISO time and contact text plus confirmation commit a REQUESTED tour; wording does not claim a confirmed calendar booking. |
| test_prior_assistant_text_does_not_establish_business_truth | A prior transcript claiming a fake created ticket cannot make the next turn complete or assert a maintenance operation. |
| test_palm_cannot_accept_atlas_route_through_orchestrator | A Palm session rejects an Atlas-only emergency proposal, selects no foreign service and invokes no workflow/tool. |

Contract/rendering tests live in `backend/tests/test_agent_contracts.py`.

| Tests | Verified behavior |
| --- | --- |
| test_confirmation_is_conservative (7 cases) | Positive/negative phrases are explicit; ambiguous/mixed-consent language is UNCLEAR. |
| test_transport_rejects_authority_injection (8 cases) | Typed input forbids property, resident, unit, workflow, tool result, action, confirmation and PropertyContext authority. |
| test_action_request_and_workflow_decision_are_not_success | A real REQUEST_ACTION decision without a receipt produces PENDING; a successful plan without operation evidence fails validation. |
| test_optional_phrasing_is_bounded_and_falls_back (4 cases) | Valid approved indices select backend wording; invalid index and provider failure return deterministic text with no secret exception content. |
| test_model_cannot_rewrite_critical_status (3 cases) | Failure, pending and unknown clauses bypass the provider entirely. |
| test_success_plan_rejects_missing_or_extraneous_reference | An injected reference outside the typed operation-fact contract is rejected. |
| test_approved_model_response_uses_mocked_provider_transport | Optional provider adapter consumes a structured index under an explicitly configured model; no live request. |
| test_success_reference_and_clause_are_deterministic | Successful wording and exact UUID come from backend values; no model call is made. |

Phase 1–5 regression tests continue to prove transaction/commit failures, concurrent action
idempotency, canonical hash conflicts, unit/property database constraints, rollback, seeds,
routing isolation, and workflow transition/replay behavior. New orchestration status-failure
tests use deterministic executor outcomes; real database/commit-failure evidence remains in the
unchanged Phase 5 execution tests.

## Architecture correspondence and compatibility decisions

ADR 006 was read explicitly. It allows configured evidence and continuation/transition policy
before constrained model interpretation. Phase 6 introduces an internal current-prompt hint
for conservative confirmation or explicit `Answer:` continuation; configured evidence still
takes precedence and every selected ID passes current catalog validation. Clarification and
abstention remain terminal routing outcomes for that turn. No service taxonomy, property-name
classification branch or independent orchestrator classifier is introduced.

The database service catalog, workflow registry and tool registry remain separate. Pet Services
requires neither a new workflow nor an arbitrary executable function. Palm never receives an
Atlas-only service. ADRs 001/005 remain enforced by fresh trusted context and source validation;
ADR 003 remains enforced by ToolExecutor-only side effects and durable receipt verification.
ADR 002 is reflected by storage, confirmation and rendering protocols. ADR 004 remains intact:
completed operations are durable facts independent of future presentation cancellation.

Phase 4's empty completion list was deliberately extended to typed receipt metadata, with
validator checks and COMPLETED request disposition. No competing ActionRequest, UserTurn,
PropertyContext, RoutingDecision or ToolResult contract was created. AgentTurnRequest wraps
transport inputs; the interpreter builds the existing internal UserTurn. A committed emergency
can reopen for a verified-identity update, including a refined hazard scenario. Existing
unexecuted scenario transitions remain unchanged.

A single new agent_turns table is justified because Phase 4 stores only the latest workflow
decision and Phase 5 stores operations, neither a full text response nor historical turn replay.
It does not duplicate the full conversation aggregate or require reconstructing business state
from text. PostgreSQL's transaction advisory guard is used because a session row lock held across
ToolExecutor's separate transaction would deadlock. Existing row locks/CAS revisions still govern
state and operation writes. No new distributed lock service is introduced.

## Acceptance checklist

### Orchestrator

- [x] TurnOrchestrator exists.
- [x] It coordinates existing Phase 2–5 components through their authoritative boundaries.
- [x] It does not duplicate catalog, routing, workflow business rules or operational adapters.
- [x] Complete text turns and the full flagship conversation work.

### Truth boundary

- [x] ActionRequest cannot establish success.
- [x] WorkflowDecision cannot establish success.
- [x] Only a verified successful ToolResult enables a success plan/response.
- [x] UNKNOWN_OUTCOME preserves uncertainty and does not automatically redispatch.
- [x] FAILED cannot be rendered as successful completion.

### Workflow reconciliation

- [x] Successful tools populate typed completion metadata.
- [x] Failed tools do not mark the action completed.
- [x] Unknown outcomes remain unresolved in durable state.
- [x] Action, execution, operation identity, type and completion turn are retained.

### Response plan

- [x] Typed ResponsePlan exists.
- [x] It contains only application-authorized context, questions and operation facts.
- [x] Critical operation status is explicit.
- [x] No hidden chain-of-thought or provider reasoning tokens are stored.

### Rendering

- [x] Deterministic renderer handles every response intent.
- [x] Optional model-assisted renderer uses a provider-neutral protocol.
- [x] Renderer has no database dependency/access.
- [x] Model failure or invalid output falls back deterministically.
- [x] Critical success/failure/unknown facts cannot be freely generated by the model.

### Confirmation

- [x] Confirm, reject and unclear are supported.
- [x] Confirmation applies only to the current persisted proposal and acknowledged revision.
- [x] Superseded/stale proposals cannot be confirmed.

### Replay

- [x] Exact turn replay is idempotent and returns saved text/plan.
- [x] Phase 5 action replay remains idempotent.
- [x] Repeated turns do not duplicate operations or invoke response models again.

### API

- [x] Minimal internal text-turn interface exists.
- [x] Client cannot inject PropertyContext, ActionRequest, ToolResult or confirmation authority.
- [x] Session ownership resolves from backend state.

### Flagship flow

- [x] AC non-emergency flow progresses.
- [x] Escalation to emergency transitions correctly.
- [x] One emergency incident is created.
- [x] The same incident is updated, including across a hazard-scenario refinement.
- [x] One human handoff request is created.
- [x] No duplicate incidents occur on replay/recovery.
- [x] Responses reflect verified backend truth and do not claim a live transfer.

### Property isolation

- [x] Session remains property scoped.
- [x] Turn payload cannot switch property/unit.
- [x] Cross-property injection and Atlas-only proposals under Palm are rejected.

### Quality

- [x] All Phase 1–5 tests remain green.
- [x] All new Phase 6 tests pass.
- [x] Real PostgreSQL integration tests pass.
- [x] Focused migration round trips and Alembic metadata parity pass.
- [x] Ruff passes.
- [x] Formatting passes.
- [x] Strict mypy passes.

### Boundaries

- [x] No voice.
- [x] No STT/TTS.
- [x] No realtime audio.
- [x] No barge-in.
- [x] No multi-agent framework.
- [x] Phase 7 has not started.

## Important changed files

- `agent/schemas.py`, `protocol.py`, `errors.py`: text transport, turn result/checkpoint, plan/fact
  contracts and ORM-free storage/rendering interfaces.
- `agent/orchestrator.py`: sequencing, durable replay, dispatch checkpoint and independent receipt
  verification; no operational SQL.
- `agent/interpretation.py`, `reconciliation.py`: conservative confirmation/field replies and
  receipt-to-workflow completion translation.
- `agent/response_plan.py`, `renderer.py`: authorized plans, structured handoff summary,
  deterministic critical clauses and optional bounded phrasing.
- `db/agent.py`, `agent_models.py`, migration `0004_agent_turns.py`: turn audit, checkpoints,
  per-session guard, atomic final reconciliation/replay and demo session creation.
- `api/turns.py`, root router and core lifecycle/config/application: internal POST API, composition,
  optional model setting and configured-origin POST support.
- `providers/llm/phrasing.py`: compatible structured-index provider, with no free-text output path.
- `routing/schemas.py`, `router.py`: validated internal continuation hints consistent with ADR 006.
- `workflows/schemas.py`, `engine.py`: typed completed/unresolved metadata, session active flag,
  and same-incident emergency update continuation.
- Integration fixture/schema expectation, `test_agent.py`, `test_agent_contracts.py`: new migration
  parity and the 46 tests above. Existing quality settings remain strict.
- README, architecture/data model/workflow/tool notes, orchestration guide and this report.

## Remaining architectural decisions and practical limits

- Transport authentication/authorization is absent by request. Production APP_ENV disables these
  demo endpoints; that guard is not a substitute for authentication on an exposed deployment.
- The demo session path supports known seeded residents. Prospect and arbitrary identity creation,
  session lifecycle endpoints and an administrative recovery interface remain future work.
- The interpreter is intentionally small. Offline answers can use `Answer:`; broader free-form
  understanding needs configured routing evidence or an explicitly configured routing provider.
  A live routing benchmark remains pending. No live quality metrics are fabricated.
- Optional model rendering chooses approved variants only. Free-form response generation and
  prose summaries require a separately justified safety design; neither is needed for this phase.
- Crash recovery favors uncertainty over possible duplicate dispatch. An abandoned incomplete
  turn blocks subsequent different turns; operator-driven reconciliation/abandonment policy remains
  to be designed. Exact completed turn replay retains the historical outcome even if a later turn
  reconciles an uncertain operation.
- Transcript/audit retention, redaction, access restrictions, request budgets and session guard
  throughput/fairness need production decisions. A 64-bit guard collision can conservatively
  reject concurrent unrelated turns; it cannot authorize shared state.
- Tour rows are requests, not confirmed bookings. Handoff rows are requests, not accepted live
  transfers. Emergency dispatch and approved safety/contact instructions still need policy.
- External PMS idempotency/reconciliation remains outside local PostgreSQL atomicity. No remote
  effect or new operational tool is introduced.

## Resulting repository inventory

Source inventory follows. Git internals, environments, caches, bytecode and secret environment
files are excluded. Earlier phase reports remain historical evidence.
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
  backend/app/agent/errors.py
  backend/app/agent/interpretation.py
  backend/app/agent/orchestrator.py
  backend/app/agent/protocol.py
  backend/app/agent/reconciliation.py
  backend/app/agent/renderer.py
  backend/app/agent/response_plan.py
  backend/app/agent/schemas.py
  backend/app/api/__init__.py
  backend/app/api/health.py
  backend/app/api/router.py
  backend/app/api/turns.py
  backend/app/core/__init__.py
  backend/app/core/application.py
  backend/app/core/config.py
  backend/app/core/errors.py
  backend/app/core/lifecycle.py
  backend/app/core/logging.py
  backend/app/core/middleware.py
  backend/app/core/server.py
  backend/app/db/__init__.py
  backend/app/db/agent_models.py
  backend/app/db/agent.py
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
  backend/app/providers/llm/phrasing.py
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
  backend/migrations/versions/0004_agent_turns.py
  backend/tests/__init__.py
  backend/tests/.gitkeep
  backend/tests/conftest.py
  backend/tests/integration/__init__.py
  backend/tests/integration/conftest.py
  backend/tests/integration/test_agent.py
  backend/tests/integration/test_database_readiness.py
  backend/tests/integration/test_property_domain.py
  backend/tests/integration/test_routing_domain.py
  backend/tests/integration/test_tools.py
  backend/tests/integration/test_workflows.py
  backend/tests/support/__init__.py
  backend/tests/support/routing.py
  backend/tests/support/workflows.py
  backend/tests/test_agent_contracts.py
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
  docs/orchestration.md
  docs/phase-1-verification.md
  docs/phase-2-verification.md
  docs/phase-3-verification.md
  docs/phase-4-verification.md
  docs/phase-5-verification.md
  docs/phase-6-verification.md
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
