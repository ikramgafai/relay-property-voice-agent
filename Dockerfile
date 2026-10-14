FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY backend/app ./backend/app
COPY evaluations ./evaluations
RUN uv sync --locked --no-dev --no-editable

FROM builder AS test
RUN uv sync --locked
COPY alembic.ini ./
COPY backend/migrations ./backend/migrations
COPY backend/tests ./backend/tests
ENV PATH="/app/.venv/bin:$PATH"
CMD ["pytest", "-v"]

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOST=0.0.0.0 PORT=8000
RUN groupadd --gid 10001 relay && useradd --uid 10001 --gid relay --no-create-home relay
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY backend/migrations ./backend/migrations
USER relay
EXPOSE 8000
CMD ["relay"]
