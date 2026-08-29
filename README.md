# s3-backup-automation

Nightly off-site backup of the **Immich** and **Nextcloud** data on the HDD to
**Amazon S3**, so a drive failure costs you a weekend rather than a decade of
photos.

```
restic  ->  Immich DB + Nextcloud DB + Nextcloud data/config   (encrypted, deduped, snapshots)
rclone  ->  Immich originals                                    (plain browsable mirror)
```

## Install

On the server that has the HDD:

```bash
sudo ./install.sh --secrets aws
```

That deploys to `/opt/s3-backup`, builds the pinned runner image, writes
`/etc/s3-backup/backup.env` from whichever of Immich and Nextcloud you run,
and enables the timers. It then prints the three commands that finish the job,
the first of which, `s3-backup-setup-aws`, creates the bucket, IAM users,
secret and access keys and writes them into the config itself. There is nothing
to copy by hand.

After any `git pull`, re-run the installer. Pulling updates this checkout; the
commands in `/usr/local/bin` run from `/opt/s3-backup`, which only `install.sh`
writes. Every command detects this automatically and warns if you forget, so
it fails loudly instead of silently running last month's code. It rebuilds the
image only when the Dockerfile changed, so redeploying is quick.

```bash
sudo ./install.sh --check          # is the deployed copy stale?
sudo ./install.sh --secrets aws    # bring it up to date
```

Step-by-step, with the reasoning: **[docs/setup.md](docs/setup.md)**.

## Layout

```
bin/
  s3-backup                  orchestrator (run / preflight / snapshots / verify / install-canaries)
  s3-backup-setup-aws        creates bucket, IAM users, secret and keys; writes the config
  s3-backup-discover         reads your containers and prints a backup.env draft
  s3-backup-status           timer state, last run, snapshots, mirror size
  s3-backup-restore          get data back: files, Immich originals, Nextcloud, DB dumps
  s3-backup-restore-drill    restores from S3 into throwaway DBs and queries them
  lib/                       common, secrets, preflight, dumps, restic-repo, immich-sync, metrics
config/backup.env.example    every option, documented
docker/                      pinned restic + rclone + jq runner image
systemd/                     nightly backup timer, monthly restore-drill timer
aws/                         IAM policies, lifecycle rules, TLS-only bucket policy
docs/                        setup, architecture, secrets, restore, operations, costs
tests/                       smoke test (mocked docker) + secret-parser test (real image)
```

## Tests

```bash
./tests/run.sh                     # orchestrator, mocked docker, throwaway container
./tests/secret-parse-test.sh       # secret parsing, against the real runner image
./tests/docs-consistency-test.sh   # the docs still match the code
./tests/deploy-test.sh             # install.sh --check, and the automatic warning on every command
```

The suite mocks `docker` and runs the real orchestrator end to end. It asserts
the phase ordering, that maintenance mode is entered exactly once and always
cleared, that dumps are verified, that every safety guard refuses to run, that
metrics are written on both success and failure, that concurrent runs are
locked out, that `s3-backup-setup-aws` writes the config without corrupting it
or printing a key, and that the fetched password never reaches the disk, never
appears in a log or a `docker` argument, and is gone when the run ends.

The third suite is what stops the docs drifting away from the code: it checks
that every command and flag shown to a user exists, that referenced files and
links resolve, that every config key with a default is documented, and that the
installer's output and `docs/setup.md` agree on the order of the steps.

## Design notes

- **Nothing is installed on the host.** `restic` and `rclone` come from a
  pinned container image; the AWS CLI is only used through `amazon/aws-cli`
  during one-time setup. The host needs docker, bash and flock.
- **No database passwords in the config.** Dumps run inside the Immich and
  Nextcloud database containers and use those containers' own environment.
- **Credentials can live in AWS Secrets Manager**, fetched per run onto tmpfs
  and removed afterwards. The only thing left on disk is a bootstrap key that
  can read one secret ARN and nothing else. See [docs/secrets.md](docs/secrets.md).
- **Four independent guards** stop a run when the HDD is not mounted, because
  the worst outcome available to a mirror backup is faithfully replicating an
  empty disk over your only cloud copy.
- **`rclone --backup-dir` plus bucket versioning** mean no sync can destroy
  data; removed objects move to `_deleted/<date>/` for 90 days.
- **The restore is tested, monthly, automatically** -
  `s3-backup-restore-drill --deep` loads the dumps into scratch containers
  built from the production images and checks that the tables have rows.

## Restoring

```bash
s3-backup-restore list                                  # what exists
s3-backup-restore files --include '*/Documents/x.odt'   # one file back
s3-backup-restore immich --in-place                     # full recovery
s3-backup-restore db immich                             # fetch + verify a dump
```

Restores default to a fresh directory under `/var/lib/s3-backup/restore`, so
they never overwrite live data by accident. `--in-place` does, and refuses
while the service containers are running. Loading a database dump stays manual
on purpose: for Immich it requires destroying the Postgres volume first, so the
command restores and verifies the dump and prints the exact commands rather
than running them. Full procedures in [docs/restore.md](docs/restore.md).

## The two things that will actually lose your data

1. **Losing the restic password.** There is no recovery, and moving it into
   Secrets Manager does not change that, since losing the AWS account loses the
   password and the backups together. Keep an offline copy.
2. **Never testing a restore.** Run the drill.

Costs about **$3.50/month for 600 GB** - see [docs/costs.md](docs/costs.md).
