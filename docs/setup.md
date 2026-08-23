# Setup

Everything below runs **on the server that has the HDD**.

## 0. Prerequisites

- Docker, and Immich + Nextcloud already running under it.
- An AWS account with admin credentials available for the one-time setup.
- Root on the server.

## 1. Copy the repository over

```bash
scp -r s3-backup-automation/ youruser@server:/tmp/
ssh youruser@server
sudo /tmp/s3-backup-automation/install.sh --secrets aws
```

The installer copies to `/opt/s3-backup`, symlinks the commands into
`/usr/local/bin`, builds the pinned runner image, pulls the AWS CLI image, and
enables the timers. Use `--secrets file` instead to keep the restic password on
disk; the installer then generates one, and you must copy it off the machine
immediately.

## 2. Create the bucket and the IAM user

Dry run first — the script changes nothing until you pass `--apply`:

```bash
cd /opt/s3-backup/aws
./bucket-setup.sh --bucket my-homelab-backup --region ap-south-1
./bucket-setup.sh --bucket my-homelab-backup --region ap-south-1 --apply
```

This creates the bucket and applies: all public access blocked, SSE-S3
encryption, versioning, the lifecycle rules from `lifecycle.json`, a policy
denying non-TLS access, and an IAM user with the least-privilege policy from
`iam-policy.json`.

Then create the access key yourself (deliberately not done by the script, so
the secret never enters a log):

```bash
aws iam create-access-key --user-name s3-backup-homelab
```

Pick a bucket region close to the server; it is where your egress bill comes
from during a restore.

## 2b. Create the secret and the bootstrap user

Dry run first, as before:

```bash
cd /opt/s3-backup/aws
./secret-setup.sh --secret-id homelab/s3-backup --region ap-south-1 --generate
./secret-setup.sh --secret-id homelab/s3-backup --region ap-south-1 --generate --apply
aws iam create-access-key --user-name s3-backup-bootstrap
```

This generates a restic password that exists only in Secrets Manager, and
creates an IAM user that can read that one secret and nothing else.

> **Save the restic password offline anyway.** Read
> [Secrets](secrets.md#the-trade-off-you-are-accepting) before deciding this
> step is optional — losing the AWS account loses the backups *and* the key.

Optionally fold the S3 access key into the same secret so it can be rotated
centrally:

```bash
printf '%s' 'THE-SECRET-KEY' > /tmp/s3key && chmod 600 /tmp/s3key
./secret-setup.sh --secret-id homelab/s3-backup --region ap-south-1 \
    --restic-password-file /dev/null --s3-access-key-id AKIA... \
    --s3-secret-key-file /tmp/s3key --apply
shred -u /tmp/s3key
```

## 3. Write the config

`s3-backup-discover` reads your running containers and prints a filled-in
draft — container names, the HDD mount point, Immich's upload location,
Nextcloud's data and config paths, and the database engine:

```bash
sudo s3-backup-discover
```

Review it, then write it out and add the values from steps 2 and 2b:

```bash
sudo s3-backup-discover | sudo tee /etc/s3-backup/backup.env >/dev/null
sudo chmod 600 /etc/s3-backup/backup.env
sudo nano /etc/s3-backup/backup.env
```

Every option is documented in `config/backup.env.example`.

## 4. Place the canaries

These marker files are how the backup tells "the drive is empty" apart from
"the drive is not mounted":

```bash
sudo s3-backup install-canaries
```

## 5. Verify before touching S3

```bash
sudo s3-backup preflight      # safety checks and credential test only
sudo s3-backup --dry-run run  # full run, writes nothing
```

Fix anything that fails here. `preflight` is intentionally strict.

## 6. Seed

The first run uploads everything. For 100 GB–1 TB on a home connection this is
hours to days, so run it detached:

```bash
tmux new -s backup
sudo systemctl start s3-backup.service
journalctl -u s3-backup.service -f
```

It is resumable: restic and rclone both pick up where they left off, so an
interrupted seed costs you only the in-flight file. If the upload saturates
your connection, set `RCLONE_BWLIMIT="20M"` in `backup.env`.

After the seed, the timer takes over at 01:30 nightly (±30 min jitter, chosen
to finish before the fleet's 04:00 container-update window).

## 7. Prove it works

Do not skip this. It is the only step that tells you the previous six worked:

```bash
sudo s3-backup-restore-drill --deep
```

This pulls the dumps back out of S3, verifies them, loads them into throwaway
database containers built from the same images as production, and queries
them. It runs monthly on its own timer thereafter.
