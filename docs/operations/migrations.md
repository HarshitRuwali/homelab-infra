# Migrations

The PostgreSQL schema is managed by Alembic.

## The container migrates itself

You usually do not run migrations by hand. The image's `CMD` is:

```sh
alembic upgrade head && uvicorn app.main:app ...
```

So `docker compose up -d --build` brings a fresh database to head before the
API accepts a request.

!!! warning "One instance at a time against a fresh database"
    Concurrent Alembic runs on the same database race. On a split deployment,
    let one API instance come up and migrate before starting the rest.

## Running migrations by hand

```bash
cd fastapi-lxc
uv run alembic upgrade head
```

Needed for local development outside Docker, or after pulling schema changes
into a checkout you run directly.

## Current migrations

| Revision | Description |
|---|---|
| `0001` | Initial schema: creates `memory_chunks`, `memory_files`, `entities`, `entity_mentions` |
| `0002` | Multi-agent scope: adds `agent_id`, `project`, `session_id` to chunks and files; widens the `memory_files` primary key to `(agent_id, project, file_path)` |
| `0003` | Adds the same scope to `entities`, with a unique `(agent_id, project, name)` |

All four tables exist from `0001`. `0003` **scopes** `entities`; it does not
create it.

Every change is additive: new columns carry server defaults, so pre-existing
rows backfill to the sentinel scope in place and no `chunk_id` is recomputed.

## Downgrades can lose data

`0002`'s `downgrade()` collapses `(agent_id, project, file_path)` back to a
single-column key. If two scopes track the same `file_path`, one of them has to
go — so the migration **refuses to run**, listing what would be lost:

```text
Refusing to downgrade 0002: 3 file_path(s) are tracked by more than one scope,
and the pre-0002 schema can only keep one row per path.
    2 scopes -> notes/plan.md
    ...
```

Override only if you accept the deletion:

```bash
ALLOW_LOSSY_DOWNGRADE=1 uv run alembic downgrade 0001
```

It keeps the `legacy` scope where present, otherwise the lexicographically
lowest `(agent_id, project)`.

## Creating a migration

```bash
cd fastapi-lxc
uv run alembic revision --autogenerate -m "description of change"
uv run alembic upgrade head
```

## Local development

When developing locally, run migrations after `uv sync`:

```bash
cd fastapi-lxc
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```
