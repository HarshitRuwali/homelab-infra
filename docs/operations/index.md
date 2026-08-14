# Operations

Day-to-day tasks for running the Open Memory Stack.

## Starting the stack

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD
mkdir -p memory-lxc/data/{qdrant,postgres,redis} fastapi-lxc/logs
docker compose up -d --build
```

## Stopping the stack

```bash
docker compose down
```

This stops containers but preserves data.

!!! note "`docker compose down -v` does not delete your memories"
    `-v` removes *anonymous and named* Docker volumes. Every stateful path in
    this stack is a **bind mount** to `memory-lxc/data/`, which `-v` does not
    touch. There is nothing for it to reap.

    To actually wipe state, delete the directories:

    ```bash
    docker compose down
    rm -rf memory-lxc/data/qdrant memory-lxc/data/postgres memory-lxc/data/redis
    ```

    Read [Backups](backups.md) first.

## Viewing logs

```bash
docker compose logs -f fastapi
docker compose logs -f qdrant
docker compose logs -f postgres
```

## Updating

```bash
git pull
docker compose up -d --build
```

The FastAPI container rebuilds on each `--build`. Data containers (Qdrant,
PostgreSQL, Redis) are not rebuilt unless their image tag changes.

## Common tasks

- [Vector dimensions](vector-dimensions.md) -- checking and fixing dimension mismatch
- [Migrations](migrations.md) -- running Alembic migrations
- [Backups](backups.md) -- backing up and restoring data
- [Troubleshooting](troubleshooting.md) -- diagnosing common problems
