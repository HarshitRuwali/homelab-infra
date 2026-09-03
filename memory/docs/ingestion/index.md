# Ingestion

The daily ingestion pipeline collects context from local sources, chunks it,
and stores it in the memory layer via `POST /memory/store`. It runs as a
standalone Python script or a cron job.

## What it does

Each run:

1. Collects data from configured sources
2. Splits large text into chunks (max 500 characters to stay under the
   embedding model's token limit)
3. Posts each chunk to `/memory/store` with appropriate type and tags
4. Aborts after 3 consecutive failures to avoid hanging when the backend is
   down

## Running manually

The wrapper handles the interpreter, the timeout and the logging, and works
from any directory:

```bash
./scripts/ingest_daily.sh
```

To run the script directly, use an interpreter that has `httpx` — the app venv
is the easy one. The system `python3` will fail with `ModuleNotFoundError`:

```bash
./api-service/.venv/bin/python scripts/ingest_daily_data.py
```

Output is a JSON summary on the last line:

```json
{"stored": 87, "failed": 0, "total": 87}
```

## Configuration

The script has no configuration file — the constants at the top of
`scripts/ingest_daily_data.py` are the knobs:

- `MEMORY_API_URL` — FastAPI endpoint (default `http://localhost:8080`; the
  all-in-one Compose stack publishes **8088**, so edit this or set
  `FASTAPI_PORT=8080`)
- `MAX_CHUNK_CHARS` — chunk size limit (default 500)
- `MAX_CONSECUTIVE_FAILURES` — abort threshold (default 3)
- Source paths (`HERMES_HOME`, `OBSIDIAN_HOME`, `PROJECTS_HOME`)

!!! note "The collectors are shaped around one person's setup"
    The bundled sources read `~/.hermes/memories/`, an Obsidian vault at
    `~/obsidian-self/`, and a hard-coded list of project directories. Treat
    them as a worked example rather than a general-purpose importer — see
    [Extending the pipeline](extending.md) for the collector interface.

!!! warning "Ingestion writes to the unscoped `legacy` corpus"
    The script sends no `agent_id` or `project`, so everything it stores lands
    in the sentinel scope `("legacy", "default")`. That is deliberate — it is a
    shared corpus every agent can read — but it means two machines running this
    pipeline against one service will overwrite each other's chunks for the
    same `file_path`. See [Memory scoping](../architecture/scoping.md).
