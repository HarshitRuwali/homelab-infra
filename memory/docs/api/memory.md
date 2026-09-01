# Memory Endpoints

## `POST /memory/store`

Embed and upsert a single memory chunk into both Qdrant and PostgreSQL.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `file_path` | string | yes | -- | Canonical path identifier |
| `chunk_index` | integer | yes | -- | Position within file (>= 0) |
| `chunk_text` | string | yes | -- | Text to embed and store |
| `type` | string | no | null | Category label |
| `tags` | string[] | no | `[]` | Free-form tags |
| `priority` | string | no | null | `high`, `medium`, or `low` |
| `summary` | string | no | null | Short description |
| `updated_at` | datetime | no | now | ISO 8601 timestamp |
| `agent_id` | string | no | `"legacy"` | Owning agent |
| `project` | string | no | `"default"` | Owning project |
| `session_id` | string | no | null | Session marker (metadata only) |

**Response (201):**

| Field | Type | Description |
|---|---|---|
| `chunk_id` | string | Generated UUID |
| `upserted` | boolean | Always `true` |
| `agent_id` | string | Normalized agent ID |
| `project` | string | Normalized project |
| `session_id` | string | Echoed session ID |

**Example:**

```bash
curl -X POST http://localhost:8088/memory/store \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "notes/2026-08-13.md",
    "chunk_index": 0,
    "chunk_text": "Deployed open-memory-stack to production.",
    "type": "daily",
    "tags": ["deploy", "ops"],
    "priority": "high",
    "agent_id": "opencode",
    "project": "open-memory-stack"
  }'
```

---

## `POST /memory/search`

Return the top-k most semantically similar chunks to a natural-language query.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | string | yes | -- | Search query text |
| `top_k` | integer | no | 5 | Results to return (1-20) |
| `filters` | object | no | `{}` | Raw Qdrant payload filters |
| `agent_id` | string \| string[] | no | all | Restrict by agent |
| `project` | string \| string[] | no | all | Restrict by project |
| `session_id` | string \| string[] | no | all | Restrict by session |
| `type` | string \| string[] | no | all | Restrict by type |

**Response (200):**

| Field | Type | Description |
|---|---|---|
| `chunks` | MemoryChunkResult[] | Ranked results |
| `total` | integer | Number of chunks returned |

Each `MemoryChunkResult` contains: `chunk_id`, `file_path`, `chunk_index`,
`chunk_text`, `score`, `type`, `tags`, `priority`, `summary`, `updated_at`,
`agent_id`, `project`, `session_id`.

**Example:**

```bash
curl -X POST http://localhost:8088/memory/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "recent deployments",
    "top_k": 3,
    "type": "daily"
  }'
```

---

## `POST /memory/update`

Re-embed and overwrite an existing chunk. Omitted fields keep their stored
values.

**Request body:** Same fields as `store`, all optional except `file_path` and
`chunk_index`. If `chunk_text` is omitted and the chunk exists, the stored text
is re-embedded.

**Response (200):** Same shape as `store`.

**Returns 404** if the chunk does not exist and `chunk_text` is not provided.

---

## `DELETE /memory/delete`

Delete chunks for a file path within a scope.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `file_path` | string | yes | -- | Path to delete |
| `agent_id` | string | no | `"legacy"` | Scope guard |
| `project` | string | no | `"default"` | Scope guard |
| `all_scopes` | boolean | no | `false` | Delete across all scopes |

**Response (200):**

| Field | Type | Description |
|---|---|---|
| `file_path` | string | Echoed path |
| `deleted_chunks` | integer | Rows removed |
| `agent_id` | string | Scope used (null if all_scopes) |
| `project` | string | Scope used (null if all_scopes) |
| `all_scopes` | boolean | Echoed flag |
