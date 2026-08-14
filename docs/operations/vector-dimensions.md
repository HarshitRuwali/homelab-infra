# Vector Dimensions

`VECTOR_DIM` declares the output dimension of your embedding model. It must
match both the model and the live Qdrant collection.

## Why it matters

Writing a 768-dim vector into a 1024-dim collection (or vice versa) corrupts
the index. The API validates this at startup and on every write:

1. **Startup**: `ensure_collection()` checks the live collection's vector size
   against `VECTOR_DIM`. A mismatch raises `VectorDimensionMismatch` and the
   container fails to start.
2. **Write time**: The `_embed()` helper validates the returned vector length.
   A mismatch returns HTTP 502.

## Checking current dimensions

```bash
curl http://localhost:8088/health | python3 -m json.tool
```

Look at `configured_vector_dim` vs `collection_vector_dim`. If they differ,
the health status is `"degraded"`.

## Common dimensions

| Model | Dimension |
|---|---|
| bge-large-en-v1.5 | 1024 |
| bge-base-en-v1.5 | 768 |
| all-mpnet-base-v2 | 768 |
| nomic-embed-text-v1.5 | 768 |

## Fixing a mismatch

If you changed your embedding model and the dimension no longer matches:

1. **If the collection is empty or you can lose its data:**
   Delete the Qdrant data and restart:
   ```bash
   docker compose down
   rm -rf memory-service/data/qdrant/*
   # Update VECTOR_DIM in .env
   docker compose up -d
   ```

2. **If you need to preserve existing data:**
   Create a new collection and migrate:
   ```bash
   # Update VECTOR_DIM in .env
   # Change QDRANT_COLLECTION to a new name
   docker compose up -d --build
   # Re-ingest your memories
   ```
