# FastAPI service

Memory CRUD, semantic search, and embedding / LLM proxying for the
[Open Memory Stack](../README.md).

Full documentation: <https://harshitruwali.github.io/homelab-infra/memory/>
Source under [`../docs/`](../docs/index.md).

## Local development

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

`uv sync` creates and manages `.venv` automatically. Add packages with `uv add`
so `pyproject.toml` and `uv.lock` stay in sync.

Configuration is read from environment variables or `.env`. The Compose files
pass explicit service hosts (`postgres`, `qdrant`, `redis`); the defaults in
`app/config.py` are local-development fallbacks.

## Layout

| Path | Contents |
|---|---|
| `app/routers/` | `/health`, `/embed`, `/memory/*`, `/llm/infer` |
| `app/scope.py` | multi-agent scope constants and normalisation |
| `app/qdrant_store.py` | collection creation, dimension validation, payload indexes |
| `app/models.py` | SQLAlchemy models |
| `alembic/versions/` | schema migrations, head is `0003` |

## Two things worth knowing before you change anything

### The chunk-ID scheme is conditional on scope

Every chunk is owned by `(agent_id, project)`, folded into `chunk_id` — but
**only for non-legacy scopes**:

| Scope | `chunk_id` hash input |
|---|---|
| `("legacy", "default")` — the sentinel | `"{file_path}:{chunk_index}"` (the original formula) |
| anything else | `"{agent_id}:{project}:{file_path}:{chunk_index}"` |

That special case is load-bearing, not an oversight. It keeps every
pre-multi-agent chunk ID byte-identical, so Qdrant points stay bound to their
Postgres rows and nothing needed re-embedding. Do not "simplify" it.

Details, including the additive migration and the Qdrant payload backfill:
[Memory scoping](../docs/architecture/scoping.md).

### `VECTOR_DIM` must match the live collection

`ensure_collection()` validates the collection's vector size at startup and
**refuses to start** on a mismatch (`VectorDimensionMismatch`). `/health`
reports `configured_vector_dim` against `collection_vector_dim`, and
`/memory/store` rejects any embedding whose length disagrees.

> **Warning:** the two `.env` files in this repo describe **different
> deployments** — `../.env` is the all-in-one stack with its own datastores,
> this directory's `.env` is an API pointed at datastores that already exist
> elsewhere. Do not copy values between them.

See [Vector dimensions](../docs/operations/vector-dimensions.md).

## Migrations

The container runs `alembic upgrade head` before Uvicorn starts, so a fresh
database migrates itself. Run it by hand only for local development.

`0002`'s downgrade is lossy when two scopes share a `file_path` and refuses to
run without `ALLOW_LOSSY_DOWNGRADE=1`. See
[Migrations](../docs/operations/migrations.md).
