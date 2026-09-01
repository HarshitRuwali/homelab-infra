# Troubleshooting

## The API container won't start

Check the logs:

```bash
docker compose logs fastapi
```

Common causes:

- **Vector dimension mismatch**: `VECTOR_DIM` does not match the live Qdrant
  collection. Fix `VECTOR_DIM` in `.env` or clear Qdrant data.
- **PostgreSQL unreachable**: Verify `POSTGRES_HOST` and `POSTGRES_PORT` in
  `.env`. In the all-in-one stack, `POSTGRES_HOST` should be `postgres`.
- **Embedding model unreachable**: The API does not fail startup if the embed
  model is down, but every write will return 502. Verify `AI_VM_HOST` and
  `EMBED_PORT`.

## `GET /health` returns `"degraded"`

The health endpoint reports per-service status. Check which service is
failing:

```bash
curl http://localhost:8088/health | python3 -m json.tool
```

- `postgres: "error: ..."` -- PostgreSQL connection issue
- `qdrant: "error: ..."` -- Qdrant connection issue
- `qdrant: "error: dimension mismatch..."` -- `VECTOR_DIM` mismatch

## Writes return 502

A 502 means the embedding model rejected the request. Check:

1. The embedding model is running and reachable at `AI_VM_HOST:EMBED_PORT`
2. The model's output dimension matches `VECTOR_DIM`
3. The chunk text is not longer than the model's context window
   (bge-large-en-v1.5 rejects inputs over 512 tokens)

## Search returns empty results

- Verify chunks were stored successfully (check `POST /memory/store` responses)
- The query may not be semantically similar to any stored chunk
- If you set scope filters (`agent_id`, `project`), try removing them

## Qdrant data grows unexpectedly

Qdrant stores the full `chunk_text` in the payload. Large chunks mean large
storage. The ingestion pipeline limits chunks to 500 characters, but direct API
writes have no such limit.

## Redis is running but not used

Redis is provisioned by both Compose files but no code path connects to it yet.
It is reserved for caching and retry queues. You can remove the Redis service
from your Compose file without affecting functionality.
