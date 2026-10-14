# Phase 2 verification — PostgreSQL, property domain and context

Verified 2026-10-09. Phase 2 is complete. Phase 3 has not started. No routing, AI, voice, workflow, tool, PMS or handoff execution was implemented.

## Architecture review and implementation decisions

README, all architecture/scope/data/event/state/failure documents, ADRs 001–006, Phase 1 verification, and the existing source tree were reviewed before implementation. ServiceScenario ownership already belonged to PropertyService in Phase 0. There is no material conflict and no reason to supersede an accepted ADR or create ADR 007. Existing ADRs remain unchanged.

Persistence lives under `db/`; ORM-independent Pydantic contracts, repository protocol and resolver live under `property/`. Placing the read protocol with its domain consumer concretizes the requested separation without importing SQLAlchemy into the resolver. No speculative CRUD/context API endpoints were added.

Resident/unit integrity is validated before repository writes and backed by a composite foreign key, not a trigger. Property-local IANA timezones use zoneinfo/tzdata. UUID4s are Python-generated. All six tables have timezone-aware timestamps; SQLAlchemy writes maintain updated_at. Read sessions use repeatable-read transactions to keep multi-query context resolution coherent.

The property/unit resolver intentionally leaves resident unset rather than choosing an occupant. A resident can have a null unit. Missing/inactive records raise PropertyContextError. The context can accurately represent an empty enabled catalog; future routing still must reject it. Durable configuration revision tokens remain future design rather than fabricated fields.

## Exact verification results

Local commands used the workspace's existing `..\.tools\bin\uv.exe` (uv 0.12.24), project `.uv-cache/`, and Python 3.12.15. Docker tests used Python 3.12.15 and real PostgreSQL 17.11. The locked database dependencies are SQLAlchemy 2.0.54, asyncpg 0.32.0 and Alembic 1.20.0; tzdata is 2026.5.

| Check | Result |
| --- | --- |
| `uv sync` | Exit 0; updated lockfile to 40 packages. |
| Windows `uv run --locked pytest -m "not integration"` | Exit 0; **38 passed, 16 deselected in 20.70s**. Preserves all 37 Phase 1 cases plus timezone contract validation. |
| Full PostgreSQL suite: `docker compose -f compose.test.yml run --rm --no-deps tests pytest -q -s` | Exit 0; **54 passed in 3.02s**, no skips/deselections. Includes real credential rejection and unavailable DB checks. |
| `uv run --locked ruff check .` | Exit 0; **All checks passed!** |
| `uv run --locked ruff format --check .` | Exit 0; **62 files already formatted**. |
| `uv run --locked mypy` | Exit 0; **Success: no issues found in 48 source files**. |
| Test and runtime Docker builds | Both successful; runtime includes Alembic migration files and seed entry point, without test/development dependencies. |
| Source boundary inspection | Reserved agent/realtime/routing/providers/workflows/tools/metrics packages remain empty markers; property contracts/resolver contain no ORM/provider imports. |

The asyncpg exception import has one localized `import-untyped` annotation because that driver distribution lacks type metadata. Strict checking remains enabled for application, migration and test code; no global ignore_missing_imports was introduced.

The isolated test project has a separate name/network and PostgreSQL tmpfs storage, no published DB port, and no development volume. The fixture requires an explicitly supplied database named `relay_test` with no existing domain tables. It never falls back to the development DATABASE_URL. Per-test transactions roll back their data.

## Migration evidence

The integration fixture ran real Alembic commands against empty PostgreSQL:

1. `command.upgrade(config, "head")` — six domain tables present.
2. `command.downgrade(config, "base")` — no domain tables remain.
3. `command.upgrade(config, "head")` — the same six tables restored.
4. `command.check(config)` — **No new upgrade operations detected.**

Foreign-key, composite ownership, natural-key uniqueness and JSONB-backed context behavior were tested against these migrated tables, not mocks or create_all. The fixture also downgraded its own isolated schema after the suite.

The development database was inspected before migration and had no public tables. It was upgraded only, not downgraded or reset:

```text
docker compose run --rm --no-deps backend alembic upgrade head
docker compose run --rm --no-deps backend alembic current
0001_property_domain (head)

docker compose run --rm --no-deps backend alembic check
No new upgrade operations detected.
```

Only properties, services, property_services, service_scenarios, units and residents were created, plus Alembic version bookkeeping. No future session, workflow, tool, incident, handoff or event tables exist.

## Seed evidence

Two successful development runs of `docker compose run --rm --no-deps backend relay-seed` printed identical counts:

| Table | First seed | Second seed |
| --- | ---: | ---: |
| properties | 2 | 2 |
| services | 11 | 11 |
| property_services | 14 | 14 |
| service_scenarios | 33 | 33 |
| units | 2 | 2 |
| residents | 2 | 2 |

Integration tests also compared every entity ID before/after the second seed and confirmed preservation of a customized property name. Seeds use insert-on-conflict by natural keys, never deletion. Resident external IDs are `relay-demo-ikram` and `relay-demo-alex`, unique within their property. Existing rows are not overwritten, including custom service configuration. Seeds are intended to run serially in one transaction.

## Live context evidence

The runtime container queried seeded resident UUIDs and called PropertyContextResolver through SQLAlchemyPropertyRepository in a database transaction. Only Pydantic values were printed.

| Resident | Property | Unit | Timezone | Enabled services | Active scenarios |
| --- | --- | --- | --- | ---: | ---: |
| Ikram Gafai | Atlas Residence | 204 | America/New_York | 8 | 25 |
| Alex Morgan | Palm Heights | 318 | America/Los_Angeles | 6 | 8 |

Atlas returned:

```text
emergency_maintenance
emergency_non_maintenance
general_information
human_handoff
leasing
non_emergency_maintenance
renewal
resident_support
```

Palm returned:

```text
general_information
human_handoff
leasing
maintenance
parking
security
```

The same resolver produced both catalogs. Palm does not expose Atlas-only emergency categories, renewal or resident_support. Tests verify disabled service exclusion, active/inactive scenario filtering, different examples for shared leasing service scenarios, property isolation, JSON serialization after ORM detachment, copied configuration and effective overrides. A new database-only `package_lockers` service/association/scenario appeared in context without changing domain source. Cross-property create/reassignment attempts were rejected by the repository, and a direct SQL bypass was rejected by PostgreSQL.

## Live HTTP, outage recovery and cleanup

Both development services became healthy. Observed HTTP results:

| Condition | Endpoint | Status and body |
| --- | --- | --- |
| PostgreSQL healthy | `/health` | 200 `{"status":"ok"}` |
| PostgreSQL healthy | `/ready` | 200 `{"status":"ready","database":"ok"}` |
| PostgreSQL deliberately stopped | `/health` | 200 `{"status":"ok"}` |
| PostgreSQL deliberately stopped | `/ready` | 503 `{"status":"not_ready","database":"unavailable"}` |
| PostgreSQL restarted and healthy | `/ready` | 200 `{"status":"ready","database":"ok"}` |

The engine recovered without restarting the backend. Probes use a two-second deadline, sanitize operational errors and never return credentials/exception details. PostgreSQL `pg_isready` reported **accepting connections**. JSON logs retained request correlation and application shutdown events. Authentication rejection was also tested against real PostgreSQL, returning controlled 503.

Both projects were stopped and removed with `docker compose down` and `docker compose -f compose.test.yml down`; final `ps -a` listings were empty. The seeded development volume is retained. No development data volume was destroyed, and PostgreSQL host port behavior is unchanged. There were no commits, pushes or external deployments.

## Important file changes

| File | Responsibility/change |
| --- | --- |
| `backend/app/db/base.py` | Typed declarative base, naming conventions, UUID4 and UTC timestamp fields. |
| `backend/app/db/models.py` | Six typed SQLAlchemy models, FKs, uniqueness, JSONB and ownership constraint. |
| `backend/app/db/session.py` | Async engine/session ownership, transaction scope, connection probe, cleanup and FastAPI session dependency. |
| `backend/app/db/repository.py` | Scoped reads, active scenario loading, Pydantic conversion and validated resident/unit writes. |
| `backend/app/db/seed_data.py` | Small demo-only service/scenario data; not a runtime taxonomy. |
| `backend/app/db/seed.py` | Atomic insert-missing seed command and row-count reporting. |
| `backend/app/property/schemas.py` | ORM-free PropertyContext, summaries, service/scenario values and timezone/ownership validation. |
| `backend/app/property/repository.py` | ORM-free repository read protocol. |
| `backend/app/property/context.py` | Both requested resolution paths, with missing/inactive checks. |
| `backend/app/property/errors.py` | Explicit context-resolution failure. |
| `backend/app/core/lifecycle.py` | Lifespan-owned database engine and shutdown disposal. |
| `backend/app/api/health.py` | Database-aware ready response; liveness unchanged. |
| `alembic.ini`, `backend/migrations/env.py` | Centralized URL loading, model registration, async online/offline migrations and supplied test connections. |
| `backend/migrations/script.py.mako` | Typed future migration template. |
| `backend/migrations/versions/0001_property_domain.py` | Explicit reversible six-table migration, independent of future model changes. |
| `backend/tests/conftest.py`, `test_health.py` | Preserve infrastructure tests, adjust readiness contract and use availability seam in unit tests only. |
| `backend/tests/test_property_contracts.py` | Valid and invalid IANA timezone validation. |
| `backend/tests/integration/conftest.py` | Isolated PostgreSQL migration cycle, model parity and transactional test fixtures. |
| `backend/tests/integration/test_property_domain.py` | Seed/catalog/context/integrity/dynamic-service/constraint tests. |
| `backend/tests/integration/test_database_readiness.py` | Real healthy/unavailable/auth-failed readiness and transaction rollback. |
| Test package `__init__.py` files | Unambiguous Python/mypy package identities. |
| `pyproject.toml`, `uv.lock` | Selected database dependencies, timezone data, seed CLI, integration marker and migration type checking. |
| `Dockerfile`, `.dockerignore` | Runtime migration assets and separate full-suite test image stage. |
| `compose.test.yml` | Dedicated temporary PostgreSQL integration-test environment with no DB host port. |
| `README.md` | Exact setup/migration/seed/full-test commands, resolver usage and readiness behavior. |
| `docs/architecture.md`, `docs/data-model.md`, `docs/scope.md` | Concrete Phase 2 boundaries and schema, preserving future design. |
| `docs/phase-2-verification.md` | This report. |

`docker-compose.yml` remains unchanged: PostgreSQL stays internal, and the existing development volume is reused. Accepted ADRs, event/state/failure documents and Phase 1 verification remain unchanged.

## Explicit acceptance checklist

| Criterion | Status/evidence |
| --- | --- |
| Async SQLAlchemy infrastructure exists | PASS — Database, async_sessionmaker, transactional sessions and dependency. |
| PostgreSQL connectivity managed cleanly | PASS — lifespan disposal, bounded probe, rollback test and live recovery. |
| Alembic configured | PASS — async environment, registered metadata and packaged migration assets. |
| Initial migration works forward/backward | PASS — empty → head → base → head on real PostgreSQL. |
| Property exists | PASS — UUID, unique indexed slug, timezone, active and timestamps. |
| Service exists | PASS — dynamic unique key and canonical description/name. |
| PropertyService exists | PASS — unique property/service association, enablement, overrides, priority and JSONB. |
| ServiceScenario exists | PASS — property-service FK, unique scoped key, examples and active flag. |
| Unit exists | PASS — property FK, unique scoped number, active and optional external ID. |
| Resident exists | PASS — property, optional matching unit, identity/contact/role/language fields and timestamps. |
| Services data-driven | PASS — newly inserted service appears without source changes. |
| Atlas/Palm different catalogs | PASS — live 8 versus 6 catalogs shown above. |
| Disabled services excluded | PASS — integration context test. |
| Scenarios property-service-specific | PASS — schema and different shared-service examples. |
| Complete PropertyContext | PASS — property, optional resident/unit, enabled services and active scenarios. |
| ORM does not leak into context | PASS — Pydantic-only contract, no ORM imports and detached JSON round trip. |
| Ikram resolves correctly | PASS — Atlas Residence/unit 204, tests and live runtime. |
| Alex resolves correctly | PASS — Palm Heights/unit 318, tests and live runtime. |
| Cross-property relationships rejected | PASS — repository create/reassignment and direct SQL constraint tests. |
| Atlas seeded | PASS — property/resident/unit/catalog/scenarios verified. |
| Palm seeded | PASS — property/resident/unit/catalog/scenarios verified. |
| Seeds idempotent | PASS — all six counts and all IDs unchanged; custom name preserved. |
| `/health` stays liveness-only | PASS — 200 during actual DB outage. |
| `/ready` checks PostgreSQL | PASS — real SELECT 1, healthy 200. |
| DB unavailable produces controlled response | PASS — actual outage/auth rejection, sanitized 503 and recovery. |
| Existing tests pass | PASS — all meaningful Phase 1 cases retained and passing. |
| New Phase 2 tests pass | PASS — full 54-case suite with real PostgreSQL. |
| Ruff passes | PASS — exit 0. |
| Formatting passes | PASS — exit 0. |
| mypy passes | PASS — exit 0, strict application/migration/test checks. |
| No routing | PASS — no classification, embeddings, thresholds, similarity or router implementation. |
| No LLM | PASS — no model/provider implementation. |
| No voice | PASS — no voice/audio/transport implementation. |
| No workflows/tools | PASS — no operational side-effect functions, PMS or handoff execution. |
| Phase 0 respected | PASS — property-first, data-driven services, scoped scenarios, no ADR rewrite. |

## Unresolved architectural decisions

Future work still needs durable property configuration versioning/reload rules, caller/phone matching and ambiguity handling, write-operation idempotency/reconciliation for operational tools, voice transport and providers, routing calibration, workflow/emergency policy and handoff delivery, retention/reconnect policies and evaluation targets. Multi-unit/history modeling, administrative CRUD and deletion policy also remain deferred. These do not block Phase 2 and do not authorize Phase 3.

## Repository tree

Generated environments, caches and bytecode are omitted. Reserved module directories contain only their existing empty package markers.

```text
relay-property-voice-agent/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── health.py
│   │   │   └── router.py
│   │   ├── core/
│   │   │   ├── __init__.py
│   │   │   ├── application.py
│   │   │   ├── config.py
│   │   │   ├── errors.py
│   │   │   ├── lifecycle.py
│   │   │   ├── logging.py
│   │   │   ├── middleware.py
│   │   │   └── server.py
│   │   ├── db/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── models.py
│   │   │   ├── repository.py
│   │   │   ├── seed.py
│   │   │   ├── seed_data.py
│   │   │   └── session.py
│   │   ├── property/
│   │   │   ├── __init__.py
│   │   │   ├── context.py
│   │   │   ├── errors.py
│   │   │   ├── repository.py
│   │   │   └── schemas.py
│   │   ├── agent/__init__.py
│   │   ├── metrics/__init__.py
│   │   ├── providers/
│   │   │   ├── __init__.py
│   │   │   ├── embeddings/__init__.py
│   │   │   ├── llm/__init__.py
│   │   │   ├── stt/__init__.py
│   │   │   └── tts/__init__.py
│   │   ├── realtime/__init__.py
│   │   ├── routing/__init__.py
│   │   ├── tools/__init__.py
│   │   └── workflows/__init__.py
│   ├── migrations/
│   │   ├── env.py
│   │   ├── script.py.mako
│   │   └── versions/0001_property_domain.py
│   └── tests/
│       ├── .gitkeep
│       ├── __init__.py
│       ├── conftest.py
│       ├── test_config.py
│       ├── test_health.py
│       ├── test_observability.py
│       ├── test_property_contracts.py
│       └── integration/
│           ├── __init__.py
│           ├── conftest.py
│           ├── test_database_readiness.py
│           └── test_property_domain.py
├── docs/
│   ├── architecture.md
│   ├── data-model.md
│   ├── event-model.md
│   ├── failure-modes.md
│   ├── phase-1-verification.md
│   ├── phase-2-verification.md
│   ├── scope.md
│   ├── state-machine.md
│   └── decisions/
│       ├── 001-property-context-before-routing.md
│       ├── 002-provider-abstractions.md
│       ├── 003-tools-own-side-effects.md
│       ├── 004-cancellable-presentation-state.md
│       ├── 005-property-specific-services.md
│       └── 006-routing-cascade.md
├── .github/workflows/.gitkeep
├── evaluations/.gitkeep
├── frontend/.gitkeep
├── .dockerignore
├── .env.example
├── .gitignore
├── .python-version
├── alembic.ini
├── compose.test.yml
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── README.md
└── uv.lock
```

Stop at Phase 2.
