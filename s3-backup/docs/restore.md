# Restore

Read this before you need it. Restores go wrong when they are being invented
under pressure.

You need two things: **an AWS credential** and **the restic password**. With
`SECRETS_BACKEND=aws-secrets-manager` the password is in Secrets Manager, so
the AWS credential gets you both, see
[Secrets: disaster recovery](secrets.md#disaster-recovery) for the one command
that retrieves it. If the only copy of either was on the failed machine, stop
and read [Secrets](secrets.md) now, while you still have a working system.

## What you need per scenario

| Scenario | What you need | Command |
|---|---|---|
| One deleted photo | AWS console only | (browser) |
| One deleted Nextcloud file | restic password | `s3-backup-restore files --include ...` |
| The HDD died | both | `s3-backup-restore immich\|nextcloud\|db` |
| The whole server died | both, plus a fresh Docker host | as above, after reinstalling |

`s3-backup-restore` never writes over live data unless you pass `--in-place`,
and refuses even then while the service containers are running. Start with
`s3-backup-restore list`.

---

## A. Recover a single Immich photo, no tooling

Because Immich originals are a plain mirror, this needs nothing but a browser.

1. S3 console → your bucket → `immich/library/<user-id>/…`
2. Find the file and download it.
3. If it is in `GLACIER_IR`, download works immediately, no restore job.

If Immich deleted it and you want it back *as an Immich asset*, upload the
file to Immich again rather than dropping it into `library/`; Immich's
database will not know about a file placed there behind its back.

Files removed from the HDD in the last 90 days are under
`_deleted/<date>/immich/library/…`.

## B. Recover a single Nextcloud file

```bash
s3-backup-restore list                       # what snapshots exist, and when
s3-backup-restore files --include '*/files/Documents/thatfile.odt'
```

It restores into a fresh timestamped directory under
`/var/lib/s3-backup/restore` and prints what it recovered. Nothing live is
touched, so this is safe to run on a working system.

Add `--snapshot ID` to read an older snapshot than the latest, and `--target
DIR` to choose where it lands.

Copy the file back yourself, deliberately, then let Nextcloud notice it:

```bash
sudo cp <restored-file> /path/in/nextcloud/data/alice/files/Documents/
sudo chown 33:33 /path/in/nextcloud/data/alice/files/Documents/thatfile.odt
docker exec -u www-data nextcloud php occ files:scan --path="alice/files/Documents"
```

`files` deliberately has no `--in-place`: a restore pattern can match anything,
and silently overwriting arbitrary live paths is not a thing this should do
for you.

## C. The HDD died, full restore

### C1. Prepare the new drive

Mount it at the same path as before. **The old paths must be recreated
exactly**, otherwise the config, the canaries and the restic snapshot paths
all stop lining up.

```bash
sudo mkdir -p /mnt/hdd
sudo mount /dev/sdX1 /mnt/hdd
mountpoint /mnt/hdd    # must succeed before anything else
```

**Stop both stacks before restoring.** `--in-place` refuses to run while the
service containers are up, because restoring underneath a running service
produces a corrupt mixture of old and new:

```bash
cd /path/to/immich && docker compose down
cd /path/to/nextcloud && docker compose down
```

### C2. Files

```bash
sudo s3-backup-restore immich --in-place
sudo s3-backup-restore nextcloud --in-place
```

Each asks you to type `RESTORE` before overwriting live paths; `--yes` skips
that when scripting. Without `--in-place` they restore into
`/var/lib/s3-backup/restore/<timestamp>-<service>/` instead, which is the safer
choice if you want to inspect before committing.

Immich uses `rclone copy`, never `sync`, so a half-restored target is never
truncated to match the mirror.

Fix ownership afterwards, which the command prints for you:

```bash
sudo chown -R 1000:1000 /mnt/hdd/immich        # match your Immich compose
sudo chown -R 33:33 /mnt/hdd/nextcloud/data    # 33 = www-data
```

### C3. Databases

```bash
sudo s3-backup-restore db immich
sudo s3-backup-restore db nextcloud
```

This restores the newest dump, verifies it (gzip integrity **and** the
completion trailer, which is what catches a dump truncated mid-write), and
prints the exact load commands for your containers and file paths.

**Loading is deliberately not automated.** For Immich it requires destroying
the Postgres volume first, and a script that gets that wrong is unrecoverable.
Run the printed commands yourself. Two details they include, both of which
will bite you if skipped:

- **`docker compose down -v` is required.** Restoring a `pg_dumpall` over an
  existing Immich database leaves a mix of old and new rows.
- **The `sed` rewriting `search_path` is not optional.** Without it the load
  fails partway through with confusing type errors, because the vector
  extension does not resolve.

The database image must be the **same version** you dumped from. Restoring a
Postgres 14 dump into 16 is a migration, not a restore.

### C4. Bring Nextcloud back and reconcile

```bash
docker compose up -d
docker exec -u www-data nextcloud php occ maintenance:mode --off
docker exec -u www-data nextcloud php occ maintenance:repair
docker exec -u www-data nextcloud php occ files:scan --all
docker exec -u www-data nextcloud php occ db:add-missing-indices
docker exec -u www-data nextcloud php occ maintenance:data-fingerprint
```

- `files:scan --all` closes the consistency window described in
  [architecture](architecture.md#consistency): it reconciles what is on disk
  with what the database believes.
- `data-fingerprint` tells every synced client the server was restored from
  backup, so they re-check rather than pushing local deletions up.
- Previews are excluded from the backup and regenerate on demand.

### C5. Re-arm the backup

```bash
sudo s3-backup install-canaries   # the canaries were on the dead drive
sudo s3-backup preflight
```

## D. The whole server died

1. New Docker host, redeploy whichever compose stacks you back up (from
   your own configuration management - **this backup does not contain your
   compose files**; see [gaps](#what-this-does-not-cover)).
2. Reinstall this tool: `sudo ./install.sh`.
3. Restore the restic password before running anything:
   - `SECRETS_BACKEND=aws-secrets-manager`: nothing to do beyond putting the
     bootstrap key back into `backup.env` - the password comes from AWS. Verify
     with `sudo s3-backup snapshots` before going further.
   - `SECRETS_BACKEND=file`: put `/etc/s3-backup/restic-password` back from
     your password manager **before** running `install.sh`, so it does not
     generate a new one.
4. Write `/etc/s3-backup/backup.env` with the same bucket, region and paths.
5. Follow C1–C7.

## What this does not cover

Deliberate gaps, so you know where the edges are:

- **Compose files, `.env` files and reverse-proxy config** are not backed up,
  they usually live outside the HDD. Keep them in git. If they live on the
  HDD, add their directory to the restic paths.
- **Immich `thumbs/` and `encoded-video/`** are excluded; Immich regenerates
  them. A restore is complete immediately but slow to browse until it catches
  up.
- **Nextcloud previews** are excluded for the same reason.
- **A single S3 region.** This protects against the HDD failing, not against
  losing the AWS account. If that is in scope, enable cross-region replication
  on the bucket.
