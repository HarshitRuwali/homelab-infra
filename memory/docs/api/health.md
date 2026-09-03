# Health endpoint

## `GET /health`

Liveness probe that checks PostgreSQL connectivity, Qdrant connectivity, and
vector dimension consistency.

**Response (200):**

| Field | Type | Description |
|---|---|---|
| `status` | string | `"ok"` or `"degraded"` |
| `postgres` | string | `"ok"` or error message |
| `qdrant` | string | `"ok"` or error message |
| `qdrant_collection` | string | Collection name |
| `configured_vector_dim` | integer | `VECTOR_DIM` from config |
| `collection_vector_dim` | integer | Live collection dimension |

**Example:**

```bash
curl http://localhost:8088/health | python3 -m json.tool
```

```json
{
  "status": "ok",
  "postgres": "ok",
  "qdrant": "ok",
  "qdrant_collection": "memory",
  "configured_vector_dim": 1024,
  "collection_vector_dim": 1024
}
```

When `configured_vector_dim` and `collection_vector_dim` differ, the status
is `"degraded"` and the `qdrant` field contains a dimension-mismatch error
message. The API refuses to serve memory writes in this state.
