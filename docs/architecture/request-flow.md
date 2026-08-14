# Request Flow

Every write and read path touches both Qdrant and PostgreSQL. This page walks
through what happens inside a single request.

## Store a memory chunk

```text
Client ── POST /memory/store ──▶ FastAPI
                                       │
                                       ├─ 1. Normalize agent_id, project (defaults to "legacy"/"default")
                                       │
                                       ├─ 2. Derive chunk_id — sha256 of
                                       │      "{agent_id}:{project}:{file_path}:{chunk_index}",
                                       │      or "{file_path}:{chunk_index}" for the legacy scope
                                       │
                                       ├─ 3. POST chunk_text to embedding model
                                       │      │
                                       │      └─▶ returns float32[VECTOR_DIM] vector
                                       │
                                       ├─ 4. Upsert Qdrant point (id=chunk_id, vector, payload)
                                       │
                                       └─ 5. Upsert PostgreSQL row (chunk_id PK, payload columns)
```

If step 3 fails (embedding model unreachable), the request returns **502** and
neither step 4 nor 5 executes. Steps 4 and 5 run in the same async context; a
failure in step 5 rolls back the database session, but the Qdrant point may
already exist. In practice this means a transient DB error leaves an orphaned
Qdrant point that will be overwritten on the next store of the same chunk.

## Search memory

```text
Client ── POST /memory/search ──▶ FastAPI
                                      │
                                      ├─ 1. POST query text to embedding model
                                      │      │
                                      │      └─▶ returns float32[VECTOR_DIM] vector
                                      │
                                      ├─ 2. Build Qdrant filter from scope + type constraints
                                      │
                                      └─ 3. Query Qdrant (cosine similarity, top-k)
                                             │
                                             └─▶ returns ranked hits with payload
```

Search reads from Qdrant only. PostgreSQL is not queried during search; the
Qdrant payload carries all fields returned to the client.

## Update a chunk

```text
Client ── POST /memory/update ──▶ FastAPI
                                      │
                                      ├─ 1. Look up existing chunk in PostgreSQL (by derived chunk_id)
                                      │
                                      ├─ 2. Merge: omitted fields keep stored values
                                      │
                                      └─ 3. Delegate to store_memory() (re-embeds, upserts both stores)
```

If the chunk does not exist and `chunk_text` is omitted, the endpoint returns **404**.

## Delete chunks

```text
Client ── DELETE /memory/delete ──▶ FastAPI
                                       │
                                       ├─ 1. Query PostgreSQL for chunk_ids matching (file_path, scope)
                                       │
                                       ├─ 2. Delete Qdrant points by payload filter
                                       │
                                       └─ 3. Delete PostgreSQL rows (cascade removes entity_mentions)
```

Delete is scoped by default. Setting `all_scopes: true` removes the scope guard
and deletes the file path across every agent and project.
