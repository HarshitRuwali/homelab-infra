# FastAPI LXC

FastAPI middleware for memory, embedding, and LLM proxy services.

## Configuration

Configuration is read from environment variables or `.env`. The defaults in `app/config.py` are local-development fallbacks; Docker Compose files pass explicit service hosts such as `postgres`, `qdrant`, and `redis`.

## Local setup

Use `uv` from this directory:

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

`uv sync` creates and manages the project `.venv` automatically.

## Dependencies

Add or update Python packages with `uv add` so `pyproject.toml` and `uv.lock` stay in sync.
