# Database Schema

## `memory_chunks`

Primary table. One row per indexed chunk.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `chunk_id` | TEXT | PK | Deterministic UUID from scope + path + index |
| `file_path` | TEXT | NOT NULL, indexed | Source file identifier |
| `chunk_index` | INTEGER | NOT NULL | Position within file |
| `chunk_text` | TEXT | | Stored text |
| `type` | VARCHAR(64) | indexed | Category label |
| `tags` | TEXT[] | | Free-form tags |
| `priority` | VARCHAR(16) | | Priority level |
| `summary` | TEXT | | Short description |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default now() | Last updated |
| `indexed_at` | TIMESTAMPTZ | NOT NULL, default now() | First indexed |
| `agent_id` | TEXT | NOT NULL, indexed, default `"legacy"` | Owning agent |
| `project` | TEXT | NOT NULL, indexed, default `"default"` | Owning project |
| `session_id` | TEXT | | Session marker |

Composite index: `(agent_id, project, file_path)`.

## `memory_files`

Tracks files per scope.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `agent_id` | TEXT | PK part | Owning agent |
| `project` | TEXT | PK part | Owning project |
| `file_path` | TEXT | PK part, indexed | File path |
| `type` | VARCHAR(64) | | Category label |
| `tags` | TEXT[] | | Tags |
| `priority` | VARCHAR(16) | | Priority |
| `updated_at` | TIMESTAMPTZ | | Last updated |
| `chunk_count` | INTEGER | | Number of chunks |

## `entities`

Named entities (Phase 8, placeholder).

| Column | Type | Constraints | Description |
|---|---|---|---|
| `entity_id` | SERIAL | PK | Auto-increment |
| `name` | TEXT | NOT NULL | Entity name |
| `type` | VARCHAR(64) | | Entity type |
| `first_seen` | TIMESTAMPTZ | | First appearance |
| `last_seen` | TIMESTAMPTZ | | Last appearance |
| `agent_id` | TEXT | NOT NULL | Owning agent |
| `project` | TEXT | NOT NULL | Owning project |

Unique constraint: `(agent_id, project, name)`.

## `entity_mentions`

Links entities to chunks.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `chunk_id` | TEXT | PK, FK to `memory_chunks` | Chunk reference |
| `entity_id` | INTEGER | PK, FK to `entities` | Entity reference |

Both foreign keys use `ON DELETE CASCADE`.
