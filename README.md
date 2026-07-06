# Open Memory Stack

Open Memory Stack is a self-hosted memory, retrieval, and LLM middleware stack for AI applications. It gives your app a durable memory backend, semantic search, and a simple FastAPI boundary for storing, searching, and updating context.

## Why I Built This

Most AI apps are useful only for the length of a single chat or request. I wanted a small, self-hosted memory layer that can persist context, search it semantically, and make it available to local or remote LLMs through a simple API.

The goal is to keep memory infrastructure understandable and portable: PostgreSQL for structured metadata, Qdrant for vector search, Redis for fast state, and FastAPI as the service boundary. It should be easy to run on a homelab, a single server, or any Docker-friendly environment.

## What Runs

- PostgreSQL: structured memory metadata
- Qdrant: vector storage and semantic search
- Redis: cache and retry/state layer
- FastAPI: memory CRUD, semantic search, LLM proxying, and embedding proxying

## Prerequisites

- Docker and Docker Compose
- A running LLM service on port `8080`
- A running embedding service on port `8081`
- `uv` if you want to develop the FastAPI app locally

## Quick Start: One Machine

Use the root Compose file when you want PostgreSQL, Qdrant, Redis, and the FastAPI middleware on the same machine.

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD.
# If your LLM or embedding services run elsewhere, update AI_VM_HOST.
mkdir -p memory-lxc/data/qdrant memory-lxc/data/postgres memory-lxc/data/redis fastapi-lxc/logs
docker compose up -d --build
docker compose logs -f
```

By default, the API listens on:

```text
http://localhost:8088
```

Change `FASTAPI_PORT` in `.env` if you want another host port. The FastAPI container still listens on port `8080` internally.

## Configuration

The root `.env.example` contains every configurable value used by the all-in-one Compose file and FastAPI settings:

- PostgreSQL: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_HOST_PORT`
- Qdrant: `QDRANT_HOST`, `QDRANT_PORT`, `QDRANT_HTTP_PORT`, `QDRANT_GRPC_PORT`, `QDRANT_COLLECTION`, `VECTOR_DIM`
- Redis: `REDIS_HOST`, `REDIS_PORT`, `REDIS_HOST_PORT`
- LLM services: `AI_VM_HOST`, `LLM_PORT`, `EMBED_PORT`
- FastAPI: `APP_HOST`, `APP_PORT`, `FASTAPI_PORT`, `UVICORN_WORKERS`, `LOG_LEVEL`

Use `AI_VM_HOST=host.docker.internal` when your LLM and embedding services run on the same host as Docker. Set it to an IP address or DNS name when they run on another machine.

## Data Persistence

Memory service data is stored on the local filesystem under:

```text
memory-lxc/data/
```

The root Compose file bind-mounts these folders:

- `memory-lxc/data/postgres`
- `memory-lxc/data/qdrant`
- `memory-lxc/data/redis`

Because these are bind mounts, `docker compose down -v` removes containers but does not delete the local data files. To wipe memory state, stop the stack and delete the relevant folders under `memory-lxc/data/`.

## Split-Machine Deployment

You can also run the memory services and FastAPI service separately. This is useful when PostgreSQL, Qdrant, and Redis live on one host, while the API runs on another.

Start memory services:

```bash
cd memory-lxc
cp .env.example .env
# Fill in POSTGRES_PASSWORD.
mkdir -p data/qdrant data/postgres data/redis
docker compose up -d
docker compose logs -f
```

Then start FastAPI on the API host:

```bash
cd fastapi-lxc
cp .env.example .env
# Set POSTGRES_HOST, QDRANT_HOST, REDIS_HOST, and AI_VM_HOST.
docker compose up -d --build
docker compose logs -f
```

The split FastAPI Compose file exposes the API on port `8080`.

## FastAPI Local Development

`fastapi-lxc` uses `uv` for dependency and virtualenv management.

```bash
cd fastapi-lxc
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

`uv sync` creates and maintains `fastapi-lxc/.venv` automatically. Add dependencies with:

```bash
uv add <package>
```

Commit both `pyproject.toml` and `uv.lock` when dependencies change.

## Useful Endpoints

- `GET /health`
- `POST /memory/store`
- `POST /memory/search`
- `POST /memory/update`
- `DELETE /memory/delete`
- `POST /llm/infer`
- `POST /embed`

## Repository Layout

```text
.
├── docker-compose.yml        # All-in-one local stack
├── .env.example              # Root Compose environment template
├── memory-lxc/               # PostgreSQL, Qdrant, Redis Compose stack
└── fastapi-lxc/              # FastAPI app, Dockerfile, uv project files
```

## Notes

- Secrets live in local `.env` files and should not be committed.
- Runtime data under `memory-lxc/data/` is ignored by git.
- FastAPI dependencies are locked in `fastapi-lxc/uv.lock`.
- FastAPI logs are written under `fastapi-lxc/logs` when the Compose stack is running.
