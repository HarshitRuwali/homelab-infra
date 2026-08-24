# Setup

Six steps. Everything runs **on the server that has the HDD**.

## 0. Prerequisites

- Docker, with Immich and/or Nextcloud already running under it. Either one
  alone is fine; the other is switched off and its settings ignored.
- AWS admin credentials available on that server for step 2, as an `~/.aws`
  profile belonging to your normal user, or in the environment. They are used
  once and never stored.
- Root.

## 1. Install

```bash
ssh youruser@server
git clone https://github.com/HarshitRuwali/s3-backup-automation.git
cd s3-backup-automation
sudo ./install.sh --secrets aws
```

Keep the checkout: it is how you update later. If the server cannot reach
GitHub, `scp -r s3-backup-automation/ youruser@server:~/` instead and run
`sudo ./install.sh --secrets aws` from that directory.

This copies to `/opt/s3-backup`, symlinks the commands into `/usr/local/bin`,
builds the pinned runner image, pulls the AWS CLI image, enables the timers,
and writes `/etc/s3-backup/backup.env` by inspecting your running containers.

Use `--secrets file` instead to keep the restic password on disk rather than in
Secrets Manager. The installer then generates one, and you must copy it off the
machine immediately. The trade-offs are in [Secrets](secrets.md).

`git pull` alone changes nothing that runs: the commands in `/usr/local/bin`
execute from `/opt/s3-backup`, and only `install.sh` writes there. After every
pull, from the checkout:

```bash
sudo ./install.sh --check          # stale or up to date; changes nothing
sudo ./install.sh --secrets aws    # deploy
```

Full detail in [Operations: updating](operations.md#updating).

### Check what it guessed

```bash
sudo nano /etc/s3-backup/backup.env
```

The AWS values are deliberately blank; step 2 fills them in. What to verify:

| Setting | Should be | Checked when |
|---|---|---|
| `IMMICH_ENABLED`, `NEXTCLOUD_ENABLED` | 1 for what you actually run | always |
| `HDD_MOUNTPOINT` | the HDD's mount point, not `/` | always |
| `IMMICH_DB_CONTAINER` | your Immich Postgres container | `IMMICH_ENABLED=1` |
| `IMMICH_UPLOAD_LOCATION` | the directory containing `library/`, `upload/`, `profile/` | `IMMICH_ENABLED=1` |
| `NEXTCLOUD_APP_CONTAINER`, `NEXTCLOUD_DB_CONTAINER` | your Nextcloud containers | `NEXTCLOUD_ENABLED=1` |
| `NEXTCLOUD_DATA_DIR`, `NEXTCLOUD_CONFIG_DIR` | Nextcloud's data and config directories | `NEXTCLOUD_ENABLED=1` |
| `NEXTCLOUD_DB_ENGINE` | `mysql` or `postgres` | `NEXTCLOUD_ENABLED=1` |

A setting for a disabled service is ignored and never validated, so leaving
`UNKNOWN` or a wrong path there is harmless until you switch it on.

`s3-backup-discover` prints the same draft on demand if you want to compare.

Discover sets `IMMICH_ENABLED` and `NEXTCLOUD_ENABLED` from what it actually
found. Set them by hand if you add a service later. At least one must be
enabled.

## 2. Create everything in AWS

One command. Dry run first: it prints a plan and changes nothing.

```bash
sudo s3-backup-setup-aws --bucket my-homelab-backup --region ap-south-1
sudo s3-backup-setup-aws --bucket my-homelab-backup --region ap-south-1 --apply
```

It creates, skipping whatever already exists:

1. the bucket, with public access blocked, SSE-S3, versioning, the lifecycle
   rules from `aws/lifecycle.json`, and a policy denying non-TLS access;
2. an IAM user for the backup host, restricted to that one bucket;
3. a Secrets Manager secret holding the restic password and the S3 key;
4. a bootstrap IAM user that can read that one secret and nothing else;
5. the access keys, written straight into `backup.env` at mode 0600.

**No secret is printed and there is nothing to paste.** Access key values go
from the AWS API into the config file without passing through your terminal.

Re-running it is safe. An existing secret keeps its restic password: replacing
that would leave the repository unopenable, and changing it is
[`restic key add`](secrets.md#rotation), not this script's job.

Pick a bucket region close to the server; it is where your egress bill comes
from during a restore.

### Where it looks for your admin credentials

Under `sudo`, `$HOME` is root's, so the command reads `~/.aws` belonging to the
user who invoked sudo (via `SUDO_USER`), not `/root/.aws`. It prints which one
it chose in the header. Override with `--aws-config-dir /path/to/.aws`, or
export `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` instead.

Credentials in `backup.env` are deliberately ignored here: that key belongs to
the backup host and cannot create buckets or IAM users.

If `--profile NAME` is not defined in your config, the command says so and
lists the profiles that are, rather than failing inside the AWS CLI.

> **Save the restic password offline now.** It exists only in AWS. Losing the
> account loses the backups and the key to them. The command is in
> [Secrets](secrets.md#disaster-recovery), and the reasoning is in
> [the trade-off](secrets.md#the-trade-off-you-are-accepting).

Migrating an existing repository from `SECRETS_BACKEND=file`? Pass
`--restic-password-file /etc/s3-backup/restic-password` so the current password
moves into the secret unchanged, then follow
[Secrets: migrating](secrets.md#migrating-from-file).

## 3. Mark the drive

These files are how the backup tells "the drive is empty" apart from "the drive
is not mounted". Without them it refuses to run.

```bash
sudo s3-backup install-canaries
```

## 4. Verify, without touching S3

```bash
sudo s3-backup preflight      # safety checks, secret fetch, credential test
sudo s3-backup --dry-run run  # the full run, writing nothing
```

Fix anything that fails here. `preflight` is strict on purpose: it is the last
thing standing between an unmounted HDD and an `rclone sync` that mirrors the
resulting emptiness over your only cloud copy.

## 5. Seed

The first run uploads everything. For 100 GB to 1 TB on a home connection that
is hours to days, so run it detached:

```bash
tmux new -s backup
sudo systemctl start s3-backup.service
journalctl -u s3-backup.service -f
```

It is resumable. restic and rclone both continue where they stopped, so an
interrupted seed costs only the in-flight file. If it saturates your uplink,
set `RCLONE_BWLIMIT="20M"` in `backup.env`.

After this the timer takes over at 01:30 nightly, with up to 30 minutes of
jitter, chosen to finish before the fleet's 04:00 container-update window.

## 6. Prove it restores

Do not skip this. It is the only step that tells you the previous five worked.

```bash
sudo s3-backup-restore-drill --deep
```

It pulls the dumps back out of S3, verifies them, loads them into throwaway
database containers built from the same images as production, and queries them.
It runs monthly on its own timer from here on.

## Where things ended up

| | |
|---|---|
| Commands | `/usr/local/bin/s3-backup*` |
| Code, docs, policies | `/opt/s3-backup` (what actually runs) |
| Which build is deployed | `/opt/s3-backup/.installed`, or `s3-backup --version` |
| Config | `/etc/s3-backup/backup.env` (0600) |
| Database dumps awaiting upload | `/var/lib/s3-backup/staging` |
| restic cache | `/var/cache/restic` |
| Restic password during a run | `/run/s3-backup` (tmpfs, removed afterwards) |
| Logs | `journalctl -u s3-backup.service` |
| Metrics | `$METRICS_DIR/s3_backup.prom` |
