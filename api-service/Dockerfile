FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

# System deps for psycopg2-binary and asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Run Alembic migrations then start Uvicorn from the uv-managed venv.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host \"$APP_HOST\" --port \"$APP_PORT\" --workers \"$UVICORN_WORKERS\""]
