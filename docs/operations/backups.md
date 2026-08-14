# Backups

## What to back up

| Data | Location | Method |
|---|---|---|
| Qdrant vectors | `memory-lxc/data/qdrant/` | Copy directory or Qdrant snapshot API |
| PostgreSQL | `memory-lxc/data/postgres/` | `pg_dump` or copy directory |
| FastAPI logs | `fastapi-lxc/logs/` | Copy directory |
| `.env` files | repo root, `fastapi-lxc/`, `memory-lxc/` | Copy (contains secrets) |

## Qdrant

```bash
# Create a snapshot
curl -X POST http://localhost:6333/collections/memory/snapshots

# List snapshots
curl http://localhost:6333/collections/memory/snapshots
```

The path is **`/snapshots`**, plural. The singular form returns 404.

Snapshots are stored inside `memory-lxc/data/qdrant/snapshots/`.

## PostgreSQL

```bash
# Dump the database
docker exec open-memory-postgres pg_dump -U open_memory open_memory > backup.sql

# Restore
docker exec -i open-memory-postgres psql -U open_memory -d open_memory < backup.sql
```

## Full stack backup

```bash
tar czf open-memory-backup-$(date +%Y%m%d).tar.gz \
  memory-lxc/data/ \
  .env \
  fastapi-lxc/.env \
  fastapi-lxc/logs/
```

## Restore

```bash
docker compose down
tar xzf open-memory-backup-YYYYMMDD.tar.gz
docker compose up -d
```
