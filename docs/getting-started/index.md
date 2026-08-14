# Getting started

The stack runs in two shapes. Pick one before copying any `.env` file, because
the two configurations are not interchangeable.

| Shape | When | Guide |
|---|---|---|
| **All-in-one** | One machine, its own PostgreSQL, Qdrant and Redis containers | [One machine](one-machine.md) |
| **Split** | Datastores on one host, the API on another | [Split deployment](split-deployment.md) |

!!! danger "Do not copy values between the two `.env` files"
    The root `.env` describes the all-in-one stack and its *own* local
    containers. `fastapi-lxc/.env` describes an API pointed at datastores that
    already exist somewhere else. Copying `VECTOR_DIM` or a host name from one
    into the other is the fastest way to a
    [dimension mismatch](../operations/vector-dimensions.md).

## Prerequisites

- Docker and Docker Compose
- An **embedding** service reachable over HTTP, exposing `POST /embedding`
- An **LLM** service exposing an OpenAI-compatible `POST /v1/chat/completions`,
  if you want `/llm/infer`
- `uv`, only if you want to develop the FastAPI app outside a container

The stack computes no embeddings of its own. It proxies to whatever you run at
`AI_VM_HOST:EMBED_PORT` — llama.cpp's `llama-server`, LM Studio, or anything
speaking the same shape.

!!! note "Which embedding model decides `VECTOR_DIM`"
    The model's output dimension is the truth; `VECTOR_DIM` only declares it.
    `bge-large-en-v1.5` is 1024, `bge-base` and `all-mpnet` are 768. Declaring
    the wrong number fails at startup rather than corrupting data.

## What comes up

| Service | Purpose | Default host port |
|---|---|---|
| FastAPI | memory CRUD, semantic search, LLM and embed proxying | `8088` (all-in-one) |
| Qdrant | vector storage and search | `6333` HTTP, `6334` gRPC |
| PostgreSQL | chunk metadata, file tracking, entities | `5432` |
| Redis | provisioned, not yet used — see below | `6379` |

The FastAPI container always listens on `8080` internally. `FASTAPI_PORT` maps
it to the host.

!!! note "Redis is running but idle"
    `REDIS_HOST` and `REDIS_PORT` are read into the settings model and the
    container is started by both Compose files, but **no code path opens a Redis
    connection**. It is reserved for the caching and retry layer described in
    the [Roadmap](../roadmap.md), not currently on the request path. You can
    remove the service from your Compose file today without affecting anything.

## Next

1. [One machine](one-machine.md) — bring the whole stack up with Compose
2. [Configuration](configuration.md) — what every variable does
3. [Your first memory](first-memory.md) — store a chunk and search it back
4. [Agents](../agents/index.md) — wire it into a coding agent over MCP
