# Development

All commands run from the repository root. Requirements: [uv](https://docs.astral.sh/uv/getting-started/installation/)
0.12.24 or newer, and Docker with Compose (Docker Desktop's Linux engine on Windows/macOS).
Python 3.12 is pinned; uv downloads it when absent. Commands work in PowerShell and POSIX
shells without environment activation.

## Configuration

Settings are validated by `backend/app/core/config.py`. Process environment overrides `.env`,
which overrides defaults. Unknown `.env` keys and invalid values fail startup. Copy
`.env.example` to `.env` for local values; `.env` is git-ignored and excluded from the Docker
build context.

| Setting | Default / rule |
| --- | --- |
| `APP_NAME` | `Relay`; nonblank |
| `APP_ENV` | `development`; one of `development`, `test`, `production` |
| `DEBUG` | `false`; forbidden in production |
| `LOG_LEVEL` | `INFO` |
| `HOST` / `PORT` | `127.0.0.1` / `8000` |
| `DATABASE_URL` | `postgresql+asyncpg://…`; stored as a redacted secret |
| `CORS_ORIGINS` | JSON array, default `["http://localhost:5173"]`; explicit origins only, `[]` disables |
| `CORS_ALLOW_CREDENTIALS` | `false`; wildcard origins are rejected |
| `ROUTING_MODEL_BASE_URL`, `ROUTING_MODEL_NAME` | Unset. Set both to enable the model stage; remote endpoints must use HTTPS |
| `ROUTING_MODEL_API_KEY` | Optional (unauthenticated local endpoints are allowed) |
| `ROUTING_MODEL_TIMEOUT_SECONDS`, `ROUTING_MODEL_MAX_COMPLETION_TOKENS` | `10`, `1200` |
| `RESPONSE_MODEL_ENABLED` | `false`; lets the model choose among approved non-operational phrasings |
| `VOICE_ENABLED` | `false`; enables the speech token endpoint |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Required when voice is enabled |
| `ASSEMBLYAI_API_KEY` | Required when voice is enabled |
| `STT_MODEL`, `STT_SAMPLE_RATE`, `STT_LANGUAGE_CODES` | `universal-3-6-pro`, `16000`, `["en"]` |
| `STT_MIN_TURN_SILENCE`, `STT_MAX_TURN_SILENCE` | `800`, `2000` ms (provider endpointing) |
| `STT_FINAL_ADMISSION_GRACE_MS` | `400` ms; Relay-side grace before a provider final becomes a turn; `0` admits immediately |

The model adapter speaks the OpenAI-compatible Chat Completions API with strict JSON-schema
output; any provider supporting that works. No provider or model is selected by default.

Test-only variables (`TEST_DATABASE_URL`, `TEST_LIVEKIT_URL`) are supplied by
`compose.test.yml`; they are not application settings.

## Tests and static checks

The full suite needs PostgreSQL and a LiveKit server. `compose.test.yml` runs both in a
separate Compose project with tmpfs storage and no published ports:

```text
docker compose -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f compose.test.yml down
```

The migration fixture verifies empty → head → previous → head → base → head and
`alembic check`. Tests refuse a database not named `relay_test`. Domain tests roll back; workflow
and tool tests commit real rows and remove the records they own. Ordinary tests never call paid
providers: an autouse guard blocks live HTTP, and model/STT behavior uses scripted doubles.

Without the Compose services, `uv run --locked pytest` skips integration tests. That is not the
full suite.

```text
uv sync --locked
uv run --locked pytest -m "not integration"
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

## Development database and backend

```text
docker compose build backend
docker compose up -d --wait postgres
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend relay-seed
docker compose up -d --wait backend
curl http://127.0.0.1:8000/ready
docker compose down
```

The backend publishes port 8000 on loopback; PostgreSQL stays on the Compose network. The
named volume survives `down`. Migrations and seeds are explicit commands, never startup side
effects. Seeds use natural keys and conflict handling, so repeated runs preserve IDs and local
customizations. A clean seed creates 2 properties, 11 services, 14 property-service
associations, 33 scenarios, 2 units and 2 residents.

`alembic downgrade base` drops all domain tables. Round trips belong in the isolated test
database only.

## Text API

`POST /api/demo/sessions` with `{"resident": "Ikram"}` (Atlas Residence) or `{"resident": "Alex"}`
(Palm Heights) returns the backend-resolved identity, `session_id` and `revision`. Submit turns
to `POST /api/sessions/{session_id}/turns` with a new UUID `turn_id`, sequential `turn_number`,
the current `expected_revision` and `utterance`. Retries reuse the exact request. Clients cannot
supply property, unit, action, result or confirmation fields. These endpoints are demo-only and
disabled when `APP_ENV=production`. See [orchestration](orchestration.md) for examples.

Without a routing model, configured service phrases, confirmations, `answer:` replies and
"cancel" work; other free-form language abstains or receives a clarification.

## Voice demo (local LiveKit)

The voice path needs a LiveKit server reachable from both the browser and the backend, an
AssemblyAI key, and a backend running on the host with the voice settings. Run one backend
worker; connection ownership is per process.

1. Start LiveKit in development mode. `--dev` uses LiveKit's documented development key pair
   (`devkey` / `secret`); never use it outside local development.

   ```text
   docker run -d --name relay-dev-livekit -p 127.0.0.1:7880:7880 -p 127.0.0.1:7881:7881 -p 127.0.0.1:7882:7882/udp livekit/livekit-server:v1.13.9 --dev --bind 0.0.0.0 --node-ip 127.0.0.1
   ```

2. Publish the development PostgreSQL to the host with a git-ignored
   `docker-compose.override.yml`, then migrate and seed as above:

   ```yaml
   services:
     postgres:
       ports:
         - "127.0.0.1:15432:5432"
   ```

3. In `.env`, set `DATABASE_URL=postgresql+asyncpg://relay:relay_dev_only@127.0.0.1:15432/relay`,
   `VOICE_ENABLED=true`, `LIVEKIT_URL=ws://127.0.0.1:7880`, the LiveKit key pair,
   `ASSEMBLYAI_API_KEY`, and optionally the routing model settings. Stop the Compose backend so
   port 8000 is free, then run `uv run relay`.
4. Create a session with `POST /api/demo/sessions`.
5. Serve the page and open `http://localhost:5173/voice.html` (this origin matches the default
   CORS setting):

   ```text
   uv run python -m http.server 5173 --directory frontend --bind 127.0.0.1
   ```

6. Enter the backend URL and session UUID and connect. The page opens the control WebSocket,
   requests a scoped LiveKit token and publishes the microphone. Partial transcripts display;
   only provider-final transcripts become turns. Disconnect when finished: an open microphone
   turns background speech into turns.

## Evaluations

```text
uv run --locked relay-eval-routing --offline --output evaluations/results/offline.json
uv run --locked relay-eval-routing --live --output evaluations/results/live.json
uv run --locked relay-eval-stt-live --wav path/to/recording.wav --expected "My AC isn't cooling."
```

The offline run uses a deterministic lexical double and makes no network calls; its report is
committed. Live commands call external providers and may incur charges; live reports are
git-ignored. The STT check takes a short single-turn mono PCM16 WAV at `STT_SAMPLE_RATE`
(maximum 60 seconds). See [routing benchmarks](routing-benchmarks.md) and [speech](speech.md).

## Platform notes

- Use `127.0.0.1` rather than `localhost` in `DATABASE_URL` where IPv6 resolution of
  `localhost` is slow or unrouted; asyncpg otherwise waits for the connect timeout.
- Pick a free host port for PostgreSQL if a native server already uses 5432. On Windows,
  Hyper-V may reserve port ranges; `netsh interface ipv4 show excludedportrange protocol=tcp`
  lists them.
- Where application-control policy blocks uv's generated script launchers on Windows, run the
  module form instead: `uv run python -m evaluations.run_stt_live …` or
  `uv run python -c "from app.core.server import main; main()"`.

## HTTP behavior

- `GET /health` is liveness only. `GET /ready` returns 200 when the application has started and
  PostgreSQL answers `SELECT 1` within two seconds, otherwise 503 without exception details. It
  does not check migration revision or seed state.
- `X-Request-ID` is preserved when it matches `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`; otherwise a
  UUID is generated. Every response carries it.
- Logs are JSON on stdout. Request logs include request ID, method, path without query string,
  status and duration; bodies, headers, settings, transcripts, audio and tokens are not logged.
- Relay errors and unexpected exceptions return a generic 500 payload; tracebacks stay in logs.
