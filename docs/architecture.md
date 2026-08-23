# Architecture

## What runs where

Nothing backup-related is installed on the host. Per the container-first
policy, `restic` and `rclone` live in a pinned image (`s3-backup-runner`), and
the AWS CLI is only ever used through `amazon/aws-cli` during one-time bucket
setup. The host needs `docker`, `bash` and `flock` - all already present on
Ubuntu.

```
host: /opt/s3-backup/bin/s3-backup   (bash, root)
  |
  |-- docker exec ---> immich_postgres   pg_dumpall  ---> staging/*.sql.gz
  |-- docker exec ---> nextcloud-db      mysqldump   ---> staging/*.sql.gz
  |-- docker exec ---> nextcloud         occ maintenance:mode
  |
  '-- docker run ----> s3-backup-runner
                         |-- restic backup  staging/ + nextcloud data/config  --> s3://BUCKET/restic/
                         '-- rclone sync    immich library/profile/upload     --> s3://BUCKET/immich/
```

### Why dumps run inside the database containers

`docker exec <db-container> pg_dumpall …` rather than a client in the runner
image. Two reasons:

1. **No credentials in our config.** The dump command reads
   `$POSTGRES_PASSWORD` / `$MYSQL_ROOT_PASSWORD` from the database container's
   own environment. `/etc/s3-backup/backup.env` contains no database password
   at all, so rotating a DB password never touches the backup config.
2. **Exact version match.** Immich runs a patched Postgres
   (VectorChord/pgvecto.rs). A dump taken by a mismatched client version is a
   restore-time failure you discover months later.

`docker exec` is called **without `-t`**. A TTY translates LF to CRLF and
silently corrupts the SQL stream. Upstream Immich docs show `-t`; do not copy
that.

## Where credentials come from

With `SECRETS_BACKEND=aws-secrets-manager`, the only credential on disk is a
bootstrap key scoped to `GetSecretValue` on one secret ARN. Before preflight,
`s3-backup` fetches the secret through a pinned `amazon/aws-cli` container,
parses it with `jq` from the runner image, so no JSON parser is needed on the
host, and writes the restic password to `/run/s3-backup`, a tmpfs.

Secret values are passed to containers by environment *passthrough*
(`docker run -e VAR`, no value in argv) and by stdin, never as command
arguments, so nothing sensitive is visible in `ps`. The tmpfs copy is removed
by an `EXIT` trap, and systemd's `RuntimeDirectory=s3-backup` deletes it even
if the process is `SIGKILL`ed.

Full detail, including what this does and does not protect against, is in
[Secrets](secrets.md).

## Run order

| # | Phase | What happens |
|---|---|---|
| 0 | preflight | Safety guards (below). Aborts before anything is touched. |
| 1 | dumps | Nextcloud → maintenance mode on; dump Nextcloud DB; dump Immich DB; verify both; maintenance mode off. |
| 2 | restic | Snapshot `staging/` + Nextcloud `data/` and `config/`. |
| 3 | immich | `rclone sync` each Immich subdirectory to S3. |
| 4 | retention | `restic forget`; on `RESTIC_PRUNE_DAY` also `prune` and a 1/52 integrity check. |

Nextcloud is in maintenance mode only for phase 1, seconds to a couple of
minutes. Set `NEXTCLOUD_MAINTENANCE_MODE=full_run` to hold it for the whole
run instead; see [the consistency trade-off](#consistency).

## Safety guards

The dangerous failure mode for any mirror-style backup is: **the HDD is not
mounted**, so every source directory is an empty stub on the root filesystem,
and `rclone sync` faithfully deletes the entire cloud copy to match. Four
independent guards, any one of which stops the run:

1. `HDD_MOUNTPOINT` must actually be a mount point (`mountpoint -q`), and each
   source directory must be on a different device than `/`.
2. A canary file (`.s3-backup-canary`) must exist in each source root. It
   lives on the HDD, so it disappears the instant the mount does.
3. A source directory that is empty is rejected outright.
4. `rclone --max-delete` aborts a sync that wants to remove more than
   `RCLONE_MAX_DELETE` objects.

On top of that, `rclone --backup-dir` means a `sync` never destroys anything:
removed and overwritten objects are moved to `s3://BUCKET/_deleted/<date>/`
and expire 90 days later. Bucket versioning is a second layer under that.

## Consistency

`NEXTCLOUD_MAINTENANCE_MODE=dumps_only` (the default) keeps downtime to the
length of the database dump. The trade-off: a file uploaded *after* the DB
dump but *before* the file sync lands in S3 without its database row. On
restore, `occ files:scan --all` reconciles the file tree with the database and
picks it up. That is a normal, supported Nextcloud operation.

`full_run` eliminates the window by holding maintenance mode across the whole
backup, at the cost of Nextcloud being unavailable for the duration.

Immich has the same window and needs no remediation: assets are written once
and never mutated, so a mid-run asset is simply included in tomorrow's run.

## Locking and failure handling

A `flock` on `LOCK_FILE` means one run at a time. An `EXIT` trap guarantees
Nextcloud is taken **out** of maintenance mode even if the run dies, and
writes the metrics file either way, a failed run reports
`s3_backup_success 0` rather than going quiet.
