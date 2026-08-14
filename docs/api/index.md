# API Reference

The FastAPI service exposes a REST API with auto-generated Swagger and Redoc
interfaces at `/docs` and `/redoc` respectively.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check for PostgreSQL and Qdrant |
| `POST` | `/embed` | Generate an embedding vector |
| `POST` | `/memory/store` | Store or upsert a memory chunk |
| `POST` | `/memory/search` | Semantic search across stored chunks |
| `POST` | `/memory/update` | Update an existing chunk |
| `DELETE` | `/memory/delete` | Delete chunks by file path |
| `POST` | `/llm/infer` | Proxy chat completion to the LLM |

## Authentication

The API currently has no built-in authentication. It is designed to run behind
a reverse proxy or inside a private network segment. Service-to-service JWT auth
is planned for the split-deployment path.

## Error responses

| Status | Meaning |
|---|---|
| `422` | Request body failed validation (missing field, `top_k` out of the 1–20 range, negative `chunk_index`) |
| `404` | Chunk not found — `/memory/update` only, and only when `chunk_text` is also omitted |
| `502` | Backend error: the embedding model or LLM is unreachable, returned an error, or returned a vector whose length disagrees with `VECTOR_DIM` |

All errors return a JSON body with a `detail` field.

!!! note "502 means the model, not the memory stack"
    Every `/memory` write and search embeds through
    `AI_VM_HOST:EMBED_PORT` first. A 502 on `/memory/store` almost always means
    that service, not PostgreSQL or Qdrant — which is why `/health` can be
    green while writes fail. See
    [Troubleshooting](../operations/troubleshooting.md).

## Base URL

In the all-in-one Docker Compose stack the API is on `http://localhost:8088`.
In a split deployment or local development it defaults to `http://localhost:8080`.
