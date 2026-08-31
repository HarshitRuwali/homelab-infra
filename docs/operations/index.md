# Operations

Day-to-day tasks for running the Open Memory Stack.

## Starting the stack

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD
mkdir -p memory-service/data/{qdrant,postgres,redis} api-service/logs
docker compose up -d --build
```

## Stopping the stack

```bash
docker compose down
```

This stops containers but preserves data.

!!! note "`docker compose down -v` does not delete your memories"
    `-v` removes *anonymous and named* Docker volumes. Every stateful path in
    this stack is a **bind mount** to `memory-service/data/`, which `-v` does not
    touch. There is nothing for it to reap.

    To actually wipe state, delete the directories:

    ```bash
    docker compose down
    rm -rf memory-service/data/qdrant memory-service/data/postgres memory-service/data/redis
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

### One-time step when upgrading past the `*-lxc` directory rename

`fastapi-lxc/` became `api-service/` and `memory-lxc/` became `memory-service/`.
The Compose bind mounts moved with them, so a plain `git pull && docker compose
up -d` mounts **empty** directories: PostgreSQL runs `initdb` into a fresh data
directory and Qdrant starts with no collections. The API comes up healthy and
reports zero memories while the real data still sits in `memory-lxc/data/`.

Stop the stack from the old checkout *before* pulling, then move the state:

```bash
docker compose down                 # all-in-one stack
mv memory-lxc/data memory-service/data
mv fastapi-lxc/logs api-service/logs
rmdir memory-lxc fastapi-lxc
docker compose up -d --build
```

For a [split deployment](../getting-started/split-deployment.md) the two
service directories are also the Compose *project* names, because neither
`memory-service/docker-compose.yml` nor `api-service/docker-compose.yml` sets
`name:`. Run `docker compose down` inside `memory-lxc/` and `fastapi-lxc/`
before renaming; otherwise the old containers stay running under the old
project, and `docker compose up -d` from the new directory aborts with
`container name "qdrant" is already in use`.

Old directories left in place are no longer covered by `.gitignore`, so
`git status` will show the stale PostgreSQL data as untracked until you remove
them.

## Common tasks

- [Vector dimensions](vector-dimensions.md) -- checking and fixing dimension mismatch
- [Migrations](migrations.md) -- running Alembic migrations
- [Backups](backups.md) -- backing up and restoring data
- [Troubleshooting](troubleshooting.md) -- diagnosing common problems
