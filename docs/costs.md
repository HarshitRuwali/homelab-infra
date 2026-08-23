# Costs

List prices below are AWS `us-east-1` and are **approximate** — they vary by
region (`ap-south-1` runs slightly higher) and change over time. Check the
[S3 pricing page](https://aws.amazon.com/s3/pricing/) before committing.

| Storage class | Per GB-month | Used for |
|---|---|---|
| Standard | $0.023 | restic metadata, first 30 days of restic data |
| Standard-IA | $0.0125 | restic data after 30 days (lifecycle) |
| Glacier Instant Retrieval | $0.004 | Immich originals (set at upload) |

## Worked example: 600 GB

Assume 500 GB of Immich originals and 100 GB of Nextcloud data.

| Item | Monthly |
|---|---|
| Immich 500 GB in `GLACIER_IR` | $2.00 |
| restic ~100 GB in `STANDARD_IA` (after the first month) | $1.25 |
| Requests (nightly incrementals) | ~$0.20 |
| **Total** | **≈ $3.50/month** |

The same 600 GB left entirely in S3 Standard would be about $13.80/month. Most
of the saving comes from `GLACIER_IR`, which is why it is the default for
Immich.

First-month cost is a little higher: the seed's `PUT` requests are billed at
$0.02 per 1,000 for `GLACIER_IR`, so 100,000 photos costs about $2 once.

**Upload bandwidth is free.** AWS does not charge for data in.

## Why these storage classes

**Immich → `GLACIER_IR`.** Originals are written once and never modified, so
the 90-day minimum storage duration never triggers an early-deletion charge.
Retrieval is instant — no restore job, no waiting — which keeps [scenario
A](restore.md#a-recover-a-single-immich-photo--no-tooling) a browser-only
operation. Objects under 128 KB are billed as 128 KB; photos are far larger,
so this is noise.

**restic → Standard, then Standard-IA at 30 days.** Writing restic data
straight to IA looks cheaper, but `prune` can delete a pack file within days
of writing it, and IA bills a 30-day minimum regardless. Transitioning at day
30 gets essentially all of the saving with no early-deletion exposure.
`restic/index/`, `restic/snapshots/` and `restic/locks/` stay in Standard —
they are read on every single run, and IA charges per retrieval.

**Deep Archive is deliberately not used.** At $0.00099/GB it is four times
cheaper again, but restores take 12–48 hours and Immich originals would stop
being directly downloadable. For a backup whose main job is surviving a drive
failure, that trade is bad: you want the restore to be boring.

## What restores cost

This is the number people forget.

| Action | Cost |
|---|---|
| Retrieving 500 GB from `GLACIER_IR` | $15.00 ($0.03/GB) |
| Egress of 500 GB to your house | ~$45.00 ($0.09/GB, first 100 GB/month free) |
| **Full 600 GB disaster restore** | **roughly $50–70, once** |

Worth knowing, not worth optimising for. If you are restoring, that money is
the cheapest part of the day.

`s3-backup verify` (full `restic check --read-data`) downloads the entire
restic repository and therefore costs full egress. The scheduled weekly check
reads only 1/52 of the data — a few cents — and covers the whole repository
over a year.

## Reducing cost further

- `IMMICH_SYNC_DIRS`: dropping `upload` (the staging area for in-flight
  uploads) saves a little and risks nothing once assets are in `library`.
- Shorten `RESTIC_KEEP_MONTHLY` / `RESTIC_KEEP_YEARLY` if you do not need to
  reach back a year.
- Raise `NoncurrentVersionExpiration` in `aws/lifecycle.json` from 30 days if
  versioning is accumulating more than you expect.
- Set a **billing alarm**. It is the only control that tells you when a
  misconfiguration is quietly costing money.
