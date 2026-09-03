# Storage model

Memory chunks are persisted in two stores. Qdrant holds the vectors and search
payload. PostgreSQL holds structured metadata and supports relational queries.

## Qdrant collection

| Field | Type | Indexed | Notes |
|---|---|---|---|
| `id` | UUID | (PK) | Deterministic from scope + path + index |
| `vector` | `float32[N]` | (vector) | N = `VECTOR_DIM`, cosine distance |
| `file_path` | string | keyword | Source file identifier |
| `chunk_index` | integer | no | Position within the file |
| `chunk_text` | string | no | Full text stored for retrieval |
| `type` | string | keyword | Category: `daily`, `project`, `decision`, etc. |
| `tags` | string[] | no | Free-form labels |
| `priority` | string | no | `high`, `medium`, `low` |
| `summary` | string | no | Short description |
| `updated_at` | timestamp | no | ISO 8601 string |
| `agent_id` | string | keyword | Owning agent |
| `project` | string | keyword | Owning project |
| `session_id` | string | keyword | Session marker (metadata only) |

The payload fields `agent_id`, `project`, `type`, `file_path`, and `session_id`
are created as keyword indexes on startup so filtered searches do not degrade to
full scans.

## PostgreSQL tables

### `memory_chunks`

One row per indexed chunk. Primary key is `chunk_id` (same UUID as the Qdrant
point). Columns mirror the Qdrant payload, plus `indexed_at` which records when
the row was first created.

### `memory_files`

One row per tracked file, per scope. Composite primary key is
`(agent_id, project, file_path)`. Tracks `chunk_count` so you can see how many
chunks a file has been split into.

### `entities` and `entity_mentions`

Placeholder tables for named-entity extraction (Phase 8 of the roadmap). Both
tables are scoped by `(agent_id, project)`. `entity_mentions` links entities to
chunks via foreign keys with `ON DELETE CASCADE`.

## Consistency model

The two stores are updated in the same request but **not** in a distributed
transaction. If the Qdrant upsert succeeds and the PostgreSQL upsert fails,
an orphaned Qdrant point exists until the next store of that chunk overwrites
it. In practice this is acceptable because:

1. PostgreSQL failures are rare (the connection pool health-checks on each use)
2. The next store of the same chunk re-syncs both stores
3. Search reads from Qdrant, so a transient DB failure does not block reads
