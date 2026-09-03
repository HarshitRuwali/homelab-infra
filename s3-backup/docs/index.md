# S3 Backup Automation

Nightly off-site backup of the Immich and Nextcloud data on the HDD, so the
loss of that drive is an inconvenience rather than a catastrophe. Two engines,
chosen per data type, and a restore that is drilled every month rather than
assumed.

## What it does

<div class="grid cards" markdown>

- :material-database-lock: **Snapshots the databases**

    restic takes encrypted, deduplicated, incremental snapshots of the Immich
    and Nextcloud databases and of Nextcloud's `data/` and `config/`. You can
    go back to *last Tuesday*, not just to *now*.

- :material-image-multiple-outline: **Mirrors the photos**

    Immich originals go up as a plain rclone mirror, so you can browse and
    download them from the S3 console with no tooling and no restic password.

- :material-shield-alert-outline: **Refuses to run blind**

    Four independent guards stop a run when the HDD is not mounted, because the
    worst outcome available to a mirror is faithfully replicating an empty disk
    over your only cloud copy.

- :material-backup-restore: **Proves it can restore**

    A monthly drill pulls the dumps out of S3, loads them into throwaway
    databases built from the production images, and checks the tables have rows.

</div>

## The two engines

| Data | Engine | Why |
|---|---|---|
| Immich Postgres, Nextcloud DB, Nextcloud `data/` + `config/` | **restic** | Encrypted, deduplicated, incremental. Nextcloud files change constantly; dedup means a nightly run uploads only the delta. Snapshots let you go back to *last Tuesday*, not just to *now*. |
| Immich originals (`library/`, `profile/`, `upload/`) | **rclone sync** | Photos are write-once and irreplaceable. A plain mirror means you can browse and download them from the S3 console with no tooling and no restic password, the restore path that still works when everything else has gone wrong. |

Either service can be switched off with `IMMICH_ENABLED=0` or
`NEXTCLOUD_ENABLED=0`; everything here applies to whichever you run.

## Start here

!!! tip "Going from nothing to a working nightly backup?"
    **[Setup](setup.md)** is the whole path in six numbered steps, with the
    reasoning for each: [Install](setup.md#1-install) →
    [Create everything in AWS](setup.md#2-create-everything-in-aws) →
    [Prove it restores](setup.md#6-prove-it-restores).

| If you want to… | Go to |
|---|---|
| Get it running tonight | [Setup](setup.md) |
| Know where credentials live | [Secrets](secrets.md) |
| Understand what runs, in what order | [Architecture](architecture.md) |
| Get data back | [Restore](restore.md) |
| Run it day to day | [Operations](operations.md) |
| Know what this costs | [Costs](costs.md) |
| Build these docs | [Building the docs](tooling.md) |

## The one-command version

```bash
sudo ./install.sh --secrets aws
```

That deploys to `/opt/s3-backup`, builds the pinned runner image, writes
`/etc/s3-backup/backup.env` from whichever of Immich and Nextcloud you run, and
enables the timers. It then prints the three commands that finish the job.

!!! warning "Installing is not the same as being backed up"
    The installer configures the machinery. Nothing has reached S3 until
    `s3-backup-setup-aws` has created the bucket and keys and a first run has
    completed. [Setup](setup.md) walks the remaining steps in order.

## The two things that matter most

1. **Keep an offline copy of the restic password.** Losing it makes every
   restic backup permanently unreadable, and there is no recovery. This holds
   whether it lives in `/etc/s3-backup/restic-password` or in AWS Secrets
   Manager: a lost AWS account takes the password *and* the backups with it.
   Put it in a password manager today. See [Secrets](secrets.md).
2. **Run the restore drill.** `sudo s3-backup-restore-drill --deep` actually
   pulls the dumps out of S3 and loads them into throwaway databases. It runs
   monthly by itself, but run it once by hand after setup so you have seen it
   pass.

## Reading these docs offline

```bash
python3 -m venv .venv
.venv/bin/pip install -r docs/requirements.txt
.venv/bin/mkdocs serve    # live preview on http://127.0.0.1:8000
.venv/bin/mkdocs build    # render the static site into ./site
```

See [Building the docs](tooling.md).

## Design commitments

These are the decisions everything else follows from. Each one exists because
the alternative is a backup that quietly is not one.

**Nothing is installed on the host.** restic and rclone come from a pinned
container image, and the AWS CLI is only used through `amazon/aws-cli` during
one-time setup. The host needs docker, bash and flock. An upgrade to the host's
package set cannot change what the backup does.

**No database passwords in the config.** Dumps run inside the Immich and
Nextcloud database containers and use those containers' own environment, so
there is no second copy of a credential to rotate or leak.

**A sync can never destroy data.** `rclone --backup-dir` plus bucket versioning
means removed objects move to `_deleted/<date>/` for 90 days rather than
vanishing. See [Architecture](architecture.md#safety-guards).

**An unmounted drive is a hard stop, not a small backup.** The failure mode
that matters is not "the run errored", it is "the run succeeded against an
empty mountpoint and mirrored that over the only copy". Hence four independent
guards rather than one.

**The restore is tested by the machine, monthly.** A backup nobody has restored
is a hypothesis. `s3-backup-restore-drill --deep` turns it into a fact on a
timer. See [Restore](restore.md).
