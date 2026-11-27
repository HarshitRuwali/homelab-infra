# Multi-stage build: dependencies are resolved in a throwaway stage so the final
# image carries only the Python runtime, the virtualenv and the app (no uv, no
# uv cache, no compiler toolchain: every dependency ships a prebuilt wheel).
# The container healthcheck lives in the Compose files.
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY . .

ENV PATH="/app/.venv/bin:$PATH"

# Run Alembic migrations then start Uvicorn from the venv.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host \"$APP_HOST\" --port \"$APP_PORT\" --workers \"$UVICORN_WORKERS\""]
