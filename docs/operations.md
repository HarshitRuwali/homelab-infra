# Operations

## Commands

| Command | Purpose |
|---|---|
| `s3-backup run` | Full backup. What the timer runs. |
| `s3-backup --dry-run run` | Full run that writes nothing. |
| `s3-backup preflight` | Safety and credential checks only. |
| `s3-backup snapshots` | List restic snapshots. |
| `s3-backup verify` | Full restic integrity check, downloads all data, costs egress. |
| `s3-backup install-canaries` | (Re)write the mount-detection markers. |
| `s3-backup-status` | Timer state, last run, snapshot list, mirror size, recent log. |
| `s3-backup-discover` | Print a config draft from the running containers. |
| `s3-backup-setup-aws` | Create/repair the bucket, IAM users, secret and keys, and write them into `backup.env`. Dry run unless `--apply`. |
| `s3-backup-restore-drill [--deep]` | Prove the backups restore. |

## Monitoring

Each run writes Prometheus metrics to `$METRICS_DIR/s3_backup.prom` (atomically,
so the collector never reads a half-written file). Point it at whatever
directory your Alloy `unix` exporter uses for the textfile collector.

| Metric | Meaning |
|---|---|
| `s3_backup_success` | 1 if the last run completed, 0 if it failed. |
| `s3_backup_last_success_timestamp_seconds` | Only updated on success. |
| `s3_backup_duration_seconds` | Wall clock of the last run. |
| `s3_backup_phase_success{phase=…}` | Per-phase: `dumps`, `restic`, `immich`, `retention`. |
| `s3_backup_immich_remote_bytes` / `_files` | Size and object count of the S3 mirror. |

The alert that matters is **staleness**, not failure, a backup that stops
running produces no failures at all:

```yaml
- alert: S3BackupStale
  expr: time() - s3_backup_last_success_timestamp_seconds > 172800
  for: 1h
  annotations:
    summary: "No successful Immich/Nextcloud backup in over 48h"

- alert: S3BackupFailing
  expr: s3_backup_success == 0
  for: 5m
```

Also alert on the metric being **absent** (`absent(s3_backup_success)`),
which catches the host going away entirely.

Set `HEALTHCHECK_URL` for an independent dead-man's switch (healthchecks.io or
similar). It pings `<url>` on success and `<url>/fail` on failure, so you get
told even when the whole monitoring stack is down.

## Retention

restic keeps 7 daily, 4 weekly, 6 monthly and 1 yearly snapshot by default.
`forget` runs nightly and is cheap; `prune` - which actually rewrites pack
files and reclaims space, runs only on `RESTIC_PRUNE_DAY` (Sunday), together
with a `--read-data-subset=1/52` check. Over a year that verifies the whole
repository without ever paying for a full download.

The Immich mirror has no snapshot history by design. Its protections are
`--backup-dir` (removed objects move to `_deleted/<date>/` for 90 days) and
bucket versioning.

## Tuning

| Setting | When to change it |
|---|---|
| `RCLONE_BWLIMIT` | The backup saturates your uplink. `"20M"` = 20 MiB/s. Supports a schedule: `"08:00,5M 23:00,off"`. |
| `RCLONE_TRANSFERS` | Raise for many small files, lower for a weak CPU or router. |
| `NEXTCLOUD_EXCLUDE_PREVIEWS` | Set to 0 only if regenerating previews after a restore is unacceptable. They are large and Nextcloud rebuilds them. |
| `IMMICH_SYNC_DIRS` | `thumbs` and `encoded-video` are excluded because Immich regenerates them. Add them only if you want a restore to be instantly fast rather than instantly complete. |
| `RESTIC_PRUNE_DAY` | Empty disables prune entirely (repo grows). |

## Routine tasks

**Rotate credentials.** All of it, S3 key, bootstrap key, restic password,
is in [Secrets](secrets.md#rotation). The one rule worth repeating here:
changing the stored restic password does not change the repository's password,
it just makes the repository unopenable. Use `restic key add` / `restic key
remove`.

**Upgrade restic or rclone.** Edit the defaults in `docker/build.sh`, rebuild,
bump `RUNNER_IMAGE` in `backup.env`. The image is pinned on purpose: an
unpinned backup tool is an unreviewed change to your recovery path.

**A run failed.** `journalctl -u s3-backup.service -e`. If it died mid-run,
confirm Nextcloud is not stuck in maintenance mode, the trap should have
cleared it, but verify with
`docker exec -u www-data nextcloud php occ maintenance:mode`.

**A stale restic lock** after a hard kill: `s3-backup snapshots` will complain;
clear it with `restic unlock` via the runner image.
