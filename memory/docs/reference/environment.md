# Environment Variables

Complete list of environment variables used by the all-in-one Docker Compose
stack and the FastAPI application.

## PostgreSQL

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_USER` | `open_memory` | Database user |
| `POSTGRES_PASSWORD` | *(required)* | Database password |
| `POSTGRES_DB` | `open_memory` | Database name |
| `POSTGRES_HOST` | `postgres` | Host (use `postgres` in all-in-one, IP for split) |
| `POSTGRES_PORT` | `5432` | Database port |
| `POSTGRES_HOST_PORT` | `5432` | Host-mapped port |

## Qdrant

| Variable | Default | Description |
|---|---|---|
| `QDRANT_HOST` | `qdrant` | Host (use `qdrant` in all-in-one, IP for split) |
| `QDRANT_PORT` | `6333` | Qdrant HTTP port |
| `QDRANT_HTTP_PORT` | `6333` | Host-mapped HTTP port |
| `QDRANT_GRPC_PORT` | `6334` | Host-mapped gRPC port |
| `QDRANT_COLLECTION` | `memory` | Collection name |
| `VECTOR_DIM` | `1024` | Embedding vector dimension |

## Redis

| Variable | Default | Description |
|---|---|---|
| `REDIS_HOST` | `redis` | Host |
| `REDIS_PORT` | `6379` | Port |
| `REDIS_HOST_PORT` | `6379` | Host-mapped port |

## LLM and embedding services

| Variable | Default | Description |
|---|---|---|
| `AI_VM_HOST` | `host.docker.internal` | Host running LLM and embed models |
| `LLM_PORT` | `8080` | LLM service port |
| `EMBED_PORT` | `8081` | Embedding model port |

## FastAPI

| Variable | Default | Description |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | Bind address |
| `APP_PORT` | `8080` | Internal listen port |
| `FASTAPI_PORT` | `8088` | Host-mapped port |
| `UVICORN_WORKERS` | `2` | Worker count |
| `LOG_LEVEL` | `info` | Log level (`debug`, `info`, `warning`, `error`) |

## MCP Server

| Variable | Default | Description |
|---|---|---|
| `OPEN_MEMORY_API_URL` | `http://localhost:8080` | FastAPI base URL |
| `OPEN_MEMORY_AGENT_ID` | hostname | Owning agent |
| `OPEN_MEMORY_PROJECT` | `default` | Owning project |
| `OPEN_MEMORY_SESSION_ID` | *(none)* | Session marker |
| `OPEN_MEMORY_TIMEOUT` | `60` | HTTP timeout (seconds) |
