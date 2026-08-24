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

| Scenario | What you need |
|---|---|
| One deleted photo | AWS console only |
| One deleted Nextcloud file | restic password |
| The HDD died | both |
| The whole server died | both, plus a fresh Docker host |

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
# What is in the repository, and when
s3-backup snapshots

docker run --rm -it \
  -e RESTIC_REPOSITORY -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
  -e RESTIC_PASSWORD_FILE=/run/secrets/pw \
  -v /etc/s3-backup/restic-password:/run/secrets/pw:ro \
  -v /var/cache/restic:/root/.cache/restic \
  -v /tmp/restore:/restore \
  s3-backup-runner:1.0.0 \
  restic restore <snapshot-id> \
    --include '*/files/Documents/thatfile.odt' \
    --target /restore
```

Copy the file back into place, `chown` it to the web server user, then:

```bash
docker exec -u www-data nextcloud php occ files:scan --path="alice/files/Documents"
```

`restic mount` is often easier for browsing, it exposes every snapshot as a
FUSE filesystem. It needs `--cap-add SYS_ADMIN --device /dev/fuse` on the
`docker run`.

---

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

Stop both stacks before restoring into their data directories.

### C2. Immich originals

```bash
docker run --rm \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
  -e RCLONE_CONFIG_S3_TYPE=s3 -e RCLONE_CONFIG_S3_PROVIDER=AWS \
  -e RCLONE_CONFIG_S3_ENV_AUTH=true -e RCLONE_CONFIG_S3_REGION=ap-south-1 \
  -v /mnt/hdd/immich:/mnt/hdd/immich \
  s3-backup-runner:1.0.0 \
  rclone copy s3:my-homelab-backup/immich /mnt/hdd/immich \
    --transfers 16 --fast-list --progress
```

`copy`, not `sync` - never point a `sync` at a half-restored directory.

Then fix ownership to whatever your Immich compose file runs as:

```bash
sudo chown -R 1000:1000 /mnt/hdd/immich
```

### C3. Immich database

```bash
# 1. Get the dump back
mkdir -p /tmp/dbrestore
docker run --rm \
  -e RESTIC_REPOSITORY -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
  -e RESTIC_PASSWORD_FILE=/run/secrets/pw \
  -v /etc/s3-backup/restic-password:/run/secrets/pw:ro \
  -v /tmp/dbrestore:/restore \
  s3-backup-runner:1.0.0 \
  restic restore latest --include '*/immich-db-*.sql.gz' --target /restore

# 2. Wipe the old database volume and start ONLY Postgres
cd /path/to/immich
docker compose down -v
docker compose pull
docker compose create
docker start immich_postgres
sleep 15

# 3. Load it
gunzip -c /tmp/dbrestore/var/lib/s3-backup/staging/immich-db-*.sql.gz \
  | sed "s/SELECT pg_catalog.set_config('search_path', '', false);/SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);/g" \
  | docker exec -i immich_postgres psql --username=postgres --dbname=postgres

# 4. Bring Immich up
docker compose up -d
```

Two details that will bite you if skipped:

- **`docker compose down -v` is required.** Restoring a `pg_dumpall` over an
  existing Immich database leaves a mix of old and new rows.
- **The `sed` is not optional.** It rewrites the dump's `search_path` reset so
  the vector extension resolves during restore. Without it the load fails
  partway through with confusing type errors.

The database image must be the **same version** you dumped from. Restoring a
Postgres 14 dump into 16 is a separate migration, not a restore.

### C4. Nextcloud files

```bash
docker run --rm \
  -e RESTIC_REPOSITORY -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
  -e RESTIC_PASSWORD_FILE=/run/secrets/pw \
  -v /etc/s3-backup/restic-password:/run/secrets/pw:ro \
  -v /mnt/hdd:/mnt/hdd \
  -v /var/cache/restic:/root/.cache/restic \
  s3-backup-runner:1.0.0 \
  restic restore latest --include '/mnt/hdd/nextcloud' --target /

sudo chown -R 33:33 /mnt/hdd/nextcloud   # 33 = www-data on the official image
```

`--target /` is correct: snapshots store absolute host paths, so this puts
everything back exactly where it came from.

### C5. Nextcloud database

```bash
# MySQL/MariaDB
docker compose up -d nextcloud-db
sleep 20
docker exec -i nextcloud-db mysql -u root -p"$PW" \
  -e "DROP DATABASE IF EXISTS nextcloud; CREATE DATABASE nextcloud
      CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
gunzip -c nextcloud-db-*.sql.gz | docker exec -i nextcloud-db mysql -u root -p"$PW"
```

### C6. Bring Nextcloud back and reconcile

```bash
docker compose up -d
docker exec -u www-data nextcloud php occ maintenance:mode --off
docker exec -u www-data nextcloud php occ maintenance:repair
docker exec -u www-data nextcloud php occ files:scan --all
docker exec -u www-data nextcloud php occ db:add-missing-indices
docker exec -u www-data nextcloud php occ maintenance:data-fingerprint
```

- `files:scan --all` closes the consistency window described in
  [architecture](architecture.md#consistency) - it reconciles what is on disk
  with what the database believes.
- `data-fingerprint` tells every synced client that the server was restored
  from backup, so they re-check rather than pushing local deletions up.
- Previews are excluded from the backup and will regenerate on demand.

### C7. Re-arm the backup

```bash
sudo s3-backup install-canaries   # the canaries were on the dead drive
sudo s3-backup preflight
```

---

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
