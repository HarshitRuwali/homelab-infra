# Immich + Nextcloud → S3 backup

Nightly off-site backup of the Immich and/or Nextcloud data on the HDD, so the
loss of that drive is an inconvenience rather than a catastrophe.

Two engines, chosen per data type:

| Data | Engine | Why |
|---|---|---|
| Immich Postgres, Nextcloud DB, Nextcloud `data/` + `config/` | **restic** | Encrypted, deduplicated, incremental. Nextcloud files change constantly; dedup means a nightly run uploads only the delta. Snapshots let you go back to *last Tuesday*, not just to *now*. |
| Immich originals (`library/`, `profile/`, `upload/`) | **rclone sync** | Photos are write-once and irreplaceable. A plain mirror means you can browse and download them from the S3 console with no tooling and no restic password, the restore path that still works when everything else has gone wrong. |

Either service can be switched off with `IMMICH_ENABLED=0` or
`NEXTCLOUD_ENABLED=0`; everything below applies to whichever you run.

## Read next

- **[Setup](setup.md)** - from nothing to a working nightly backup.
- **[Secrets](secrets.md)** - where credentials live, and the trade-offs.
- **[Architecture](architecture.md)** - what runs, in what order, and why.
- **[Restore](restore.md)** - the procedures. Read this *before* you need it.
- **[Operations](operations.md)** - daily running, monitoring, tuning.
- **[Costs](costs.md)** - what this will cost per month and how to change that.

## The two things that matter most

1. **Keep an offline copy of the restic password.** Losing it makes every
   restic backup permanently unreadable, and there is no recovery. This holds
   whether it lives in `/etc/s3-backup/restic-password` or in AWS Secrets
   Manager, a lost AWS account takes the password *and* the backups with it.
   Put it in a password manager today. See [Secrets](secrets.md).
2. **Run the restore drill.** `sudo s3-backup-restore-drill --deep` actually
   pulls the dumps out of S3 and loads them into throwaway databases. It runs
   monthly by itself, but run it once by hand after setup so you have seen it
   pass.
