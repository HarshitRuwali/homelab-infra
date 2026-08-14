# Architecture

Open Memory Stack is a three-tier system: a FastAPI middleware layer proxies requests
to an embedding model and an LLM, while a PostgreSQL + Qdrant pair provides durable,
semantically-searchable memory.

## Components

| Layer | Technology | Role |
|---|---|---|
| **API** | FastAPI (Uvicorn) | Request routing, embedding proxy, LLM proxy, memory CRUD |
| **Vector DB** | Qdrant | Embedding storage, cosine-similarity search, payload filtering |
| **Relational DB** | PostgreSQL 16 | Chunk metadata, file tracking, named entities |
| **Cache** | Redis 7.2 | Reserved for retry queues and embedding cache (future) |
| **Inference** | llama.cpp / external | LLM inference and embedding generation |

## Design principles

- **Dual-write, not two-phase**: Every `POST /memory/store` upserts to both Qdrant
  and PostgreSQL in the same request, but they are **not** one transaction. A
  PostgreSQL failure rolls back the session and leaves the Qdrant point in place,
  to be overwritten on the next store of that chunk. See
  [Storage model](storage.md#consistency-model).
- **Scoped identity**: A chunk's ID encodes its `(agent_id, project)` scope, so
  multiple agents can store the same file path without collision.
- **Fail loudly on mismatch**: A `VECTOR_DIM` mismatch with the live Qdrant
  collection refuses to start instead of writing corrupt vectors.
- **Backward-compatible scoping**: Pre-scoping data uses a sentinel scope
  `("legacy", "default")` and retains its original chunk-ID formula.

## How it fits together

```text
Client (curl, MCP agent, script)
         │
         │  HTTP
         ▼
  ┌──────────────┐
  │   FastAPI    │  ── proxy ──▶  Embedding model  (AI_VM_HOST:EMBED_PORT)
  │  :8080       │  ── proxy ──▶  LLM              (AI_VM_HOST:LLM_PORT)
  └──┬──────┬────┘
     │      │
     │      │  upsert / query
     ▼      ▼
  Qdrant  PostgreSQL
  :6333   :5432
```

See [Request flow](request-flow.md) for a step-by-step walkthrough of a store
and a search request.
