# Configuration

Everything is read from environment variables, or from a `.env` file next to
the process. `app/config.py` defines the settings model; there are no defaults
for anything that matters, so a missing value fails at startup rather than
silently picking `localhost`.

The full table lives in [Reference → Environment](../reference/environment.md).
This page covers the four decisions that actually shape a deployment.

## Where the models live

```bash
AI_VM_HOST=host.docker.internal
LLM_PORT=8080
EMBED_PORT=8081
```

The service builds two base URLs from these and proxies to them:

| Setting | Becomes | Used by |
|---|---|---|
| `AI_VM_HOST:EMBED_PORT` | `POST /embedding` | `/embed`, and every `/memory` write and search |
| `AI_VM_HOST:LLM_PORT` | `POST /v1/chat/completions` | `/llm/infer` |

`host.docker.internal` reaches the Docker host from inside a container. On
Linux this only resolves because the Compose file adds
`extra_hosts: host.docker.internal:host-gateway`. Use an IP or DNS name when
the models run elsewhere.

## `VECTOR_DIM`

The output dimension of your embedding model. This is validated against the
live Qdrant collection at startup, and the service **refuses to serve** on a
mismatch.

| Model | Dimension |
|---|---|
| `bge-large-en-v1.5` | 1024 |
| `bge-base-en-v1.5`, `all-mpnet-base-v2` | 768 |

`/memory/store` re-checks the vector it actually got back from the model, so a
model swapped underneath a running service is caught on the next write rather
than at the next restart. [Vector
dimensions](../operations/vector-dimensions.md) covers what to do when the two
disagree.

## Ports

```bash
APP_HOST=0.0.0.0
APP_PORT=8080        # what Uvicorn binds inside the container
FASTAPI_PORT=8088    # what the host publishes
UVICORN_WORKERS=2
```

`APP_PORT` is the container-internal port and rarely needs changing.
`FASTAPI_PORT` is the one you curl.

!!! note "Workers and the collection guard"
    `UVICORN_WORKERS` forks independent processes. Each one validates the
    collection and creates payload indexes on its own first request — the work
    is guarded per-process, not per-container, and is idempotent, so this
    costs a little duplicated effort at boot and nothing after.

## Logging

```bash
LOG_LEVEL=info
```

Logs go to stdout **and** to `logs/app.log`, rotated at midnight with seven
days kept. The root Compose file bind-mounts `api-service/logs` so they survive
a container rebuild.

Every request is logged twice — once on arrival, once on completion with a
status code and duration:

```text
2026-08-13 01:00:45 | INFO | app.main | Request started | client=172.18.0.1 | method=POST | path=/memory/store | query=
2026-08-13 01:00:45 | INFO | app.main | Response sent | client=172.18.0.1 | method=POST | path=/memory/store | status=201 | duration_ms=182.44
```

!!! warning "Request bodies are not logged, but memory text reaches disk anyway"
    The middleware logs metadata only. Chunk text still lands in PostgreSQL and
    Qdrant in the clear, and a stack trace on a failed write can include it.
    Treat the log directory and the data directories as equally sensitive.

## The database URL is derived, not configured

There is no `DATABASE_URL`. It is assembled from the five PostgreSQL settings:

```python
postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}
```

Alembic reads the same settings, which is why `alembic.ini` leaves
`sqlalchemy.url` empty. Setting it there overrides the environment and is a
good way to migrate the wrong database.
