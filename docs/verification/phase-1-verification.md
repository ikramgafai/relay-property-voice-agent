# Phase 1 verification and handoff

Phase 1 backend foundation is implemented and verified. Phase 2 has not started. No Relay domain feature, voice transport or provider is implemented.

## Exact final check results

Commands ran from the project root. This workspace lacked uv, so uv 0.12.24 was bootstrapped into the ignored sibling `.tools/bin/uv.exe`; the commands below used `..\.tools\bin\uv.exe` in place of `uv`. Python 3.12.15 was downloaded into the ignored project `.python/` directory. `UV_CACHE_DIR` was set to the project `.uv-cache/` during local verification. No global Python installation was changed.

| Check | Final result |
| --- | --- |
| `uv sync` | Succeeded; generated `uv.lock`, installed 33 packages including the editable project and development tools. |
| `uv run --locked pytest` | Exit 0: **37 passed in 0.24s**, Python 3.12.15, pytest 9.1.1. |
| `uv run --locked ruff check .` | Exit 0: **All checks passed!** Ruff 0.16.10. |
| `uv run --locked ruff format --check .` | Exit 0: **43 files already formatted**. |
| `uv run --locked mypy` | Exit 0: **Success: no issues found in 30 source files**. mypy 1.20.2. |
| `docker compose build backend` | Exit 0; final image built with Python 3.12.15 and 18 runtime packages, no development dependencies. |
| `docker compose up -d --wait --wait-timeout 120` | Exit 0; backend and postgres both healthy. |
| Live `GET /health?probe=omit-query` | HTTP 200, `{"status":"ok"}`, supplied `X-Request-ID: phase1-final-check` preserved. |
| Live `GET /ready` | HTTP 200, `{"status":"ready"}`. |
| `docker compose exec -T postgres pg_isready -U relay -d relay` | Exit 0: `/var/run/postgresql:5432 - accepting connections`. |
| PostgreSQL SQL inspection | PostgreSQL **17.11**; **0** tables in the public schema. |
| Backend runtime inspection | Python 3.12.15; non-root UID/GID 10001 (`relay`). |
| Live log inspection | JSON startup and correlated request-completion events present; query string and duplicate Uvicorn access stream absent. |
| `docker compose stop` | Both services stopped cleanly; JSON `application_stopped` and Uvicorn shutdown completion confirmed. |
| `docker compose down` / `docker compose ps -a` | Exit 0; project containers/network removed; no containers remain. Database volume retained. |
| Scope inspection | Every domain/provider Python file is still an empty `__init__.py`; no scattered `os.getenv` / `os.environ` usage in application code. |

Initial tests identified capture-fixture issues and an optional-path typing error; both were fixed before the final successful suite. Live verification found duplicate Uvicorn access logs after factory initialization; logging configuration now keeps that stream disabled, with a regression assertion and a rebuilt/live-verified image.

Windows rejected the original PostgreSQL host binding on port 5432. PostgreSQL now remains on the Compose internal network, reachable by the backend as `postgres:5432` and inspectable with `docker compose exec`. The backend alone publishes loopback port 8000. No database host port is needed by Phase 1.

Docker Desktop was started for verification and remains available. The built image, ignored local tooling/environment/cache, and empty named database volume remain. No commits, pushes or remote deployments were made.

## Important files

| File | Change and purpose |
| --- | --- |
| `backend/app/main.py` | Small `create_app() -> FastAPI` composition root. |
| `backend/app/api/health.py` | Typed liveness/readiness responses and startup-only readiness behavior. |
| `backend/app/api/router.py` | Modular root router including infrastructure probes only. |
| `backend/app/core/config.py` | Centralized Pydantic Settings, environments, URL/port/origin validation, redacted database URL, fail-fast loading. |
| `backend/app/core/errors.py` | Minimal RelayError/ConfigurationError hierarchy and centralized generic HTTP error responses. |
| `backend/app/core/logging.py` | JSON structlog/stdlib pipeline; explicit filtering of duplicate/full-URL request logs. |
| `backend/app/core/middleware.py` | Pure ASGI request IDs, monotonic duration, response status/completion logging, context isolation. |
| `backend/app/core/application.py` | Small FastAPI subclass placing observation and CORS outside framework error handling. |
| `backend/app/core/lifecycle.py` | Lifespan startup/shutdown logs and readiness flag. |
| `backend/app/core/server.py` | Portable settings-aware Uvicorn entry point with development-only reload. |
| `backend/tests/conftest.py` | Environment isolation and lifespan-aware async HTTPX fixtures. |
| `backend/tests/test_health.py` | Factory, response schemas/payloads, readiness lifecycle, IDs, CORS and framework errors. |
| `backend/tests/test_config.py` | Invalid settings, supported environments, precedence, secrets and CORS validation. |
| `backend/tests/test_observability.py` | Real JSON formatting, lifecycle/request logs, concurrent ID isolation and error propagation. |
| `pyproject.toml` | Python 3.12 package, actual runtime/dev dependencies, CLI entry point, pytest/Ruff/mypy/uv configuration. |
| `uv.lock` | Resolved cross-platform dependency versions and hashes. |
| `.python-version` | Python 3.12 selection. |
| `.env.example` | Safe local-only placeholders for all supported settings. |
| `Dockerfile` | Locked multi-stage build; runtime runs as non-root without dev tooling. |
| `.dockerignore` | Allowlisted build context excluding environment files and local artifacts. |
| `docker-compose.yml` | Backend/PostgreSQL 17, both healthchecks, internal database network and persistent named volume. |
| `.gitignore` | Ignores optional project-local managed Python and uv cache in addition to existing exclusions. |
| Workspace parent `.gitignore` | Ignores bootstrap `.tools/`; no nested Git repository created. |
| `README.md` | Current phase, portable install/run/check/Compose commands, configuration and HTTP behavior. |
| `docs/architecture.md` | Records frozen Phase 1 choices and infrastructure implementation without changing domain boundaries. |
| `docs/scope.md` | Clarifies Phase 1 authorization while preserving future product scope. |
| `docs/phase-1-verification.md` | This evidence, file summary, tree and acceptance checklist. |

The six accepted ADRs and the data/event/state/failure documents remain unchanged.

## Acceptance criteria

| Criterion | Status / evidence |
| --- | --- |
| Python 3.12 configured | PASS — project range, version file, local and container runtime. |
| `create_app()` exists | PASS — factory tested and used by Uvicorn. |
| FastAPI lifespan exists | PASS — startup/readiness/shutdown exercised. |
| Uvicorn starts application | PASS — live container server answered probes. |
| Pydantic Settings centralized | PASS — `core/config.py`; entry point/factory use the same loader. |
| Environments validated | PASS — development/test/production accepted, invalid environment and production debug rejected. |
| No scattered getenv | PASS — source inspection. |
| No real secrets committed | PASS — only deliberate development placeholders added; no real credentials or commits created; environment files ignored and excluded from Docker. |
| `/health` works | PASS — HTTPX and live HTTP 200 with correct body. |
| `/ready` works | PASS — HTTPX and live HTTP 200; 503 outside lifespan. |
| Explicit response models | PASS — HealthResponse/ReadyResponse and OpenAPI assertions. |
| Modular API routing | PASS — API router and separate health module. |
| structlog configured | PASS — actual JSON formatter and live logs checked. |
| Every HTTP request has an ID | PASS — generated/preserved IDs, invalid/duplicate replacement and concurrent isolation tested. |
| X-Request-ID in responses | PASS — success, preflight, framework errors and unexpected 500 tests; live echo checked. |
| Request duration measured | PASS — `perf_counter`, milliseconds in JSON completion logs. |
| Startup/shutdown logged | PASS — tests and live lifecycle logs. |
| Minimal error hierarchy | PASS — RelayError and ConfigurationError only. |
| Centralized application error handling | PASS — registered handlers in `core/errors.py`. |
| Unexpected errors not swallowed | PASS — traceback logging, generic client response, exception propagates to ASGI server. |
| pytest passes | PASS — 37/37. |
| Ruff passes | PASS — exit 0. |
| Formatting check passes | PASS — exit 0. |
| mypy passes | PASS — exit 0, 30 source files. |
| Docker image builds | PASS — final image built successfully. |
| PostgreSQL starts | PASS — PostgreSQL 17.11 running via Compose. |
| PostgreSQL healthcheck succeeds | PASS — healthy container and pg_isready. |
| Backend starts through Compose | PASS — healthy container and live probes. |
| Phase 0 architecture intact | PASS — boundaries preserved; existing ADRs unchanged. |
| No Relay business functionality | PASS — reserved packages empty; no domain tables/repositories/migrations or frontend functionality. |
| No premature provider selection/implementation | PASS — STT/TTS/LLM/embeddings/voice transport remain undecided and unimplemented. |

## Remaining architectural questions

No unresolved question blocks this foundation. Future phases must resolve voice/browser transport; STT/TTS/LLM/embedding providers and interfaces; repository/transaction/idempotency/reconciliation design; property resolution and configuration versioning; routing calibration; workflow/emergency policy; handoff destination/acknowledgment; and retention, reconnect, operational timeouts and evaluation targets. PostgreSQL, Python/FastAPI and the future SQLAlchemy async/asyncpg/Alembic stack are already frozen and should not be treated as open choices.

## Resulting repository tree

Generated local environments/caches and `__pycache__` are omitted. Empty package markers remain in all reserved modules.

```text
relay-property-voice-agent/
├── .github/
│   └── workflows/.gitkeep
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
│   │   ├── agent/__init__.py
│   │   ├── db/__init__.py
│   │   ├── metrics/__init__.py
│   │   ├── property/__init__.py
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
│   └── tests/
│       ├── .gitkeep
│       ├── conftest.py
│       ├── test_config.py
│       ├── test_health.py
│       └── test_observability.py
├── docs/
│   ├── architecture.md
│   ├── data-model.md
│   ├── event-model.md
│   ├── failure-modes.md
│   ├── phase-1-verification.md
│   ├── scope.md
│   ├── state-machine.md
│   └── decisions/
│       ├── 001-property-context-before-routing.md
│       ├── 002-provider-abstractions.md
│       ├── 003-tools-own-side-effects.md
│       ├── 004-cancellable-presentation-state.md
│       ├── 005-property-specific-services.md
│       └── 006-routing-cascade.md
├── evaluations/.gitkeep
├── frontend/.gitkeep
├── .dockerignore
├── .env.example
├── .gitignore
├── .python-version
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── README.md
└── uv.lock
```

Stop at Phase 1. Phase 2 requires a separate instruction.
