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
sudo ./install.sh --secrets aws                      # deploy, build image, enable timers
sudo aws/bucket-setup.sh --bucket B --region R       # dry run
sudo aws/bucket-setup.sh --bucket B --region R --apply
sudo aws/secret-setup.sh --secret-id homelab/s3-backup --region R --generate --apply
sudo s3-backup-discover | sudo tee /etc/s3-backup/backup.env   # then add keys
sudo s3-backup install-canaries
sudo s3-backup preflight
sudo s3-backup --dry-run run
sudo systemctl start s3-backup.service               # seed, in tmux
sudo s3-backup-restore-drill --deep                  # prove it
```

Full walkthrough: **[docs/setup.md](docs/setup.md)**.

## Layout

```
bin/
  s3-backup                  main orchestrator (run / preflight / snapshots / verify)
  s3-backup-discover         generates backup.env from your running containers
  s3-backup-status           timer state, last run, snapshots, mirror size
  s3-backup-restore-drill    restores from S3 into throwaway DBs and queries them
config/backup.env.example    every option, documented
docker/                      pinned restic+rclone runner image
systemd/                     nightly backup timer, monthly drill timer
  lib/                       common, secrets, preflight, dumps, restic-repo, immich-sync, metrics
aws/                         bucket + secret setup, IAM policies, lifecycle, TLS-only policy
docs/                        setup, architecture, secrets, restore, operations, costs
tests/                       smoke test (mocked docker) + secret-parser test (real image)
```

## Tests

```bash
./tests/run.sh                 # 56 assertions, throwaway container, no host changes
./tests/secret-parse-test.sh   # 16 assertions against the real runner image
```

The suite mocks `docker` and runs the real orchestrator end to end. It asserts
the phase ordering, that maintenance mode is entered exactly once and always
cleared, that dumps are verified, that every safety guard refuses to run, that
metrics are written on both success and failure, that concurrent runs are
locked out, and — for the Secrets Manager backend — that the fetched password
never reaches the disk, never appears in a log or a `docker` argument, and is
gone when the run ends.

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
- **The restore is tested, monthly, automatically** —
  `s3-backup-restore-drill --deep` loads the dumps into scratch containers
  built from the production images and checks that the tables have rows.

## The two things that will actually lose your data

1. **Losing the restic password.** There is no recovery — and moving it into
   Secrets Manager does not change that, since losing the AWS account loses the
   password and the backups together. Keep an offline copy.
2. **Never testing a restore.** Run the drill.

Costs about **$3.50/month for 600 GB** — see [docs/costs.md](docs/costs.md).
