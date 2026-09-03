# Scheduling

Run the ingestion pipeline on a timer through `scripts/ingest_daily.sh`. That
wrapper, not the Python script, is the scheduling entry point.

## The wrapper

`scripts/ingest_daily.sh`:

- resolves `memory/` from its **own** path, so it works from any working
  directory and the checkout can live anywhere
- picks the app venv interpreter (`api-service/.venv/bin/python`), overridable
  with `OPEN_MEMORY_PYTHON`
- caps the run at **300 seconds** with `timeout`
- writes a timestamped log to `memory/logs/ingest_<date>_<time>.log`
- prints one summary line, and the JSON result on success, so a scheduler can
  deliver the output as a notification

Exit codes: `0` success, `1` failure, and a timeout is reported distinctly
(`timeout` returns `124` internally).

## Cron

```cron
0 1 * * * /path/to/homelab-infra/memory/scripts/ingest_daily.sh
```

No `cd` is needed: the script locates itself. Redirecting output is optional
since the script already logs to `memory/logs/`; the summary line on stdout is
what cron mails you.

Pick an hour when the embedding model is actually loaded. Every chunk is a
synchronous embed call, so ingestion against a sleeping model just burns the
timeout.

## systemd timer

```ini
# ~/.config/systemd/user/open-memory-ingest.service
[Unit]
Description=Open Memory Stack daily ingestion

[Service]
Type=oneshot
ExecStart=/path/to/homelab-infra/memory/scripts/ingest_daily.sh
```

```ini
# ~/.config/systemd/user/open-memory-ingest.timer
[Unit]
Description=Run Open Memory ingestion daily

[Timer]
OnCalendar=*-*-* 01:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now open-memory-ingest.timer
```

`Persistent=true` catches up a run missed while the machine was off.

## Failures

The pipeline aborts after **3 consecutive** store failures rather than grinding
through the whole set. Each failed store burns the server-side 30-second embed
timeout, so without the guard a dead embedding model costs
`len(chunks) × 30s` and blows the timeout instead of reporting. An aborted run
exits non-zero.

Check the newest log under `memory/logs/`, then verify:

1. The FastAPI service is up — `curl localhost:8080/health`
2. The embedding model is reachable at `AI_VM_HOST:EMBED_PORT`
3. `VECTOR_DIM` matches the live collection

!!! tip "Keep one copy of the script"
    If your scheduler needs the entry point somewhere else, make that file a
    one-line wrapper that `exec`s this one. Two copies of the real logic drift,
    and the stale copy is the one that runs at 01:00.
