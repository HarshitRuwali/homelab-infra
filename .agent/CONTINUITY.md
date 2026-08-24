# CONTINUITY, s3-backup-automation

Canonical briefing. Facts only.

## [PLANS]

- 2026-08-19T08:50Z [USER] Goal: daily backup of Immich + Nextcloud data from an
  attached HDD to S3, so a HDD failure is survivable.
- 2026-08-19T09:05Z [ASSUMPTION] Deliverable is a deploy-ready repo in
  `/home/harshit/s3-backup-automation`; the user installs it on the server.
  Remaining work is entirely on the server: bucket creation, config, seed run,
  restore drill.

## [DECISIONS]

- 2026-08-19T08:52Z [USER] Tiered engines: restic for databases + Nextcloud
  data; rclone `sync` for Immich originals so photos stay browsable in S3.
- 2026-08-19T08:52Z [USER] No AWS resources exist yet; repo ships bucket/IAM/
  lifecycle setup as a dry-run-by-default script (now `bin/s3-backup-setup-aws`).
- 2026-08-19T08:52Z [USER] Data volume 100 GB – 1 TB. Seed must be resumable.
- 2026-08-19T09:00Z [ASSUMPTION] Storage classes: Immich → `GLACIER_IR` at
  upload (write-once data, 90-day minimum never triggers); restic data →
  `STANDARD_IA` via lifecycle at day 30 (prune can delete packs earlier than
  IA's 30-day minimum, so writing straight to IA risks early-delete fees).
  Deep Archive rejected: 12–48h restores break the browser-only recovery path.
- 2026-08-19T09:00Z [CODE] Dumps execute via `docker exec` inside the service's
  own DB container. Keeps DB passwords out of `backup.env` and guarantees the
  dump tool matches the server version (matters for Immich's VectorChord PG).
- 2026-08-19T09:00Z [CODE] Container-first: `restic`/`rclone` ship in a pinned
  image (`docker/Dockerfile`), nothing installed on the host.
- 2026-08-19T09:10Z [CODE] Timer at 01:30 ±30m jitter, chosen to complete
  before the fleet's existing 04:00 docker-update window.

## [DISCOVERIES]

- 2026-08-19T08:47Z [TOOL] `ubuntu-dev` (this VM) does NOT host Immich or
  Nextcloud, and has no HDD: `lsblk` shows only a 100G QEMU disk. Its
  containers are Superset, FastAPI, superset-mcp.
- 2026-08-19T08:49Z [TOOL] Target server `100.83.72.78` is unreachable from
  `ubuntu-dev`: absent from `tailscale status` peers, no ICMP, port 22 closed.
  Hence the build-and-hand-off approach and `s3-backup-discover`.
- 2026-08-19T08:48Z [TOOL] Fleet doc `monitorting-stack/docs/fleet/
  container-updates.md` names `immich_postgres`
  (`ghcr.io/immich-app/postgres:14-vectorchord`), `immich_redis`
  (`valkey/valkey:9`), `mysql:8.0`. Immich PG is v14 with VectorChord →
  restore requires the same image; defaults assume MySQL for Nextcloud.
- 2026-08-19T09:00Z [CODE] `docker exec -t` injects CRLF into a piped SQL dump
  and silently corrupts it. Upstream Immich docs use `-t`; this repo does not.
- 2026-08-19T09:00Z [CODE] restic run inside a container gets a random
  hostname, so `--host` must be passed explicitly or nightly `forget`
  retention never matches and snapshots accumulate forever.
- 2026-08-19T09:05Z [CODE] Primary catastrophic failure mode identified: an
  unmounted HDD presents empty source dirs and `rclone sync` deletes the cloud
  copy to match. Four guards implemented (mountpoint + device check, canary
  file, empty-dir refusal, `--max-delete`), plus `--backup-dir` and versioning.

## [PROGRESS]

- 2026-08-19T09:45Z [USER] Requested credentials move to AWS Secrets Manager,
  read at run time, rather than sitting in files on the server.
- 2026-08-19T09:55Z [CODE] Added `SECRETS_BACKEND` (`file` |
  `aws-secrets-manager`) in `bin/lib/secrets.sh`,
  `aws/bootstrap-iam-policy.json`, `docs/secrets.md`. Default remains `file`
  for backward compatibility; `install.sh --secrets aws` selects the other.

- 2026-08-19T09:15Z [CODE] Repo complete: orchestrator + 6 libs, discover /
  status / restore-drill tools, pinned runner image, 2 systemd timers, AWS
  setup + policies, 6 docs pages. `bash -n` clean on all scripts.

## [DECISIONS, secrets]

- 2026-08-19T09:50Z [ASSUMPTION] A bootstrap credential on disk is unavoidable:
  reading Secrets Manager requires an AWS credential, so it cannot itself be
  fetched. Scoped to `GetSecretValue` on one ARN with an explicit Deny on all
  other Secrets Manager actions. The gain is central revocation/rotation and
  keeping the restic password off the machine - NOT protection against a
  stolen disk, which still reaches the secret in one hop. Stated plainly in
  `docs/secrets.md` rather than oversold.
- 2026-08-19T09:50Z [ASSUMPTION] Restic password written to `/run/s3-backup`
  (tmpfs) for the life of a run, never to disk; removed by an EXIT trap and by
  systemd `RuntimeDirectory=`. Secrets reach containers via env passthrough
  and stdin only, never argv, so nothing is visible in `ps`.
- 2026-08-19T09:52Z [CODE] JSON parsed with `jq` from the runner image, not
  python3 on the host - keeps the "nothing installed on the host" property.
  Superseded an initial python3 implementation.
- 2026-08-19T09:55Z [ASSUMPTION] Trade-off accepted and documented: the restic
  password now lives in the same AWS account as the restic repo, so one
  account compromise yields ciphertext AND key. Mitigations offered: keep an
  offline copy regardless, and optionally a customer-managed KMS key.

## [DECISIONS - setup flow]

- 2026-08-23T09:10Z [USER] Reported the setup flow was inconsistent: README,
  install.sh output and docs/setup.md disagreed on ordering, and "then add
  keys" had no well-defined meaning after the Secrets Manager change.
- 2026-08-23T09:15Z [CODE] Root cause was structural, not editorial: setup was
  split across `aws/bucket-setup.sh` + `aws/secret-setup.sh` + a manual
  backup.env edit, so no surface owned the sequence. Consolidated into a single
  `bin/s3-backup-setup-aws`; both old scripts deleted.
- 2026-08-23T09:15Z [CODE] Superseded the earlier decision to have the user run
  `aws iam create-access-key` by hand (taken so secrets stayed out of logs).
  The script now captures the key from the API and writes it into backup.env at
  0600 without printing it - same secrecy property, no manual paste.
- 2026-08-23T09:16Z [CODE] `install.sh` now seeds `/etc/s3-backup/backup.env`
  by running discover, so the only blank values are the ones setup-aws fills.
- 2026-08-23T09:20Z [ASSUMPTION] Canonical order, enforced by test: install.sh
  -> s3-backup-setup-aws -> install-canaries -> preflight -> systemctl start
  -> restore-drill. Each surface has a distinct job: README = orientation,
  installer output = next actions, docs/setup.md = canonical walkthrough.
- 2026-08-23T09:20Z [CODE] An existing secret NEVER has its restic password
  replaced by setup-aws; it is reused. Replacing it would orphan the
  repository, and rotating it is `restic key add`.

## [DISCOVERIES - first real-server run]

- 2026-08-23T10:42Z [USER] `sudo s3-backup-setup-aws --profile default` failed
  on `cloud-services` with "The config profile (default) could not be found".
- 2026-08-23T10:45Z [CODE] Two defects, both mine:
  (a) `awscli()` mounted `$HOME/.aws`, but under sudo `$HOME` is `/root`, so it
      mounted `/root/.aws` instead of the invoking user's. `docker -v` then
      CREATED that path as an empty dir, turning "no config" into the
      misleading "profile not found". Now resolved via `SUDO_USER`, overridable
      with `--aws-config-dir`, mounted only when it already exists.
  (b) `HAVE_ENV_CREDS` was computed AFTER sourcing backup.env, so it picked up
      `AWS_ACCESS_KEY_ID` = the backup host's least-privilege S3 key and would
      have attempted bucket/IAM creation with it. Admin credentials are now
      snapshotted before the config is sourced.
- 2026-08-23T10:50Z [TOOL] The existing setup-aws tests had been passing
  *because of* defect (b): they supplied no admin credentials and silently used
  the stale key from backup.env. They now pass admin credentials explicitly.
  Evidence that a green suite can encode the bug it should catch.
- 2026-08-23T10:52Z [TOOL] Both fixes mutation-tested: reverting the env
  snapshot produces 19 failures, reintroducing the sudo `$HOME` bug produces 6.

## [DISCOVERIES - Immich-only deployment]

- 2026-08-24T07:01Z [USER] `sudo s3-backup install-canaries` died with
  "NEXTCLOUD_DB_ENGINE must be 'mysql' or 'postgres', got 'UNKNOWN'" on a host
  that runs Immich but no Nextcloud.
- 2026-08-24T07:05Z [CODE] `load_config` validated Nextcloud settings
  unconditionally, so a disabled service could still block every command.
  Validation is now gated on `NEXTCLOUD_ENABLED` / `IMMICH_ENABLED`, discover
  emits a valid placeholder rather than `UNKNOWN` when nothing is found, and
  enabling neither service is refused. Mutation-tested: 6 failures on revert.
- 2026-08-24T07:20Z [CODE] README claimed "81 assertions" against an actual
  111. Counts restated in prose cannot be kept true; removed them and added a
  consistency check forbidding them. First regex missed a comma-terminated
  case and was widened after the mutation test failed to trip it.
- 2026-08-24T07:25Z [CODE] `aws/bucket-setup.sh` and `aws/secret-setup.sh`
  reinstated as tombstones: executable stubs naming `s3-backup-setup-aws` and
  exiting 1. The user re-ran the removed path from scrollback and got only
  "command not found". Both rules added to `~/.claude/CLAUDE.md`.

## [OUTCOMES]

- 2026-08-23T09:35Z [TOOL] Added `tests/docs-consistency-test.sh` (8 checks:
  unknown commands, unknown flags, missing files, broken links/anchors,
  undocumented config keys, step ordering). Mutation-tested: all six drift
  classes are caught. Also added a global rule in `~/.claude/CLAUDE.md`
  ("No drift between docs, scripts, and output") per [USER] request.
- 2026-08-23T09:36Z [DISCOVERIES] The em dash sweep (global writing rule)
  silently broke a markdown anchor, `restore.md#a-recover-a-single-immich-photo--no-tooling`,
  because the heading gained a literal hyphen. Caught by the new consistency
  test, not by review. Heading changed to use parentheses instead.
- 2026-08-19T10:00Z [TOOL] Verified on `ubuntu-dev`: shellcheck clean
  (0 warnings, `-S warning`, SC1091/SC2034/SC2016 excluded as cross-file
  false positives); `tests/run.sh` 56/56 and `tests/secret-parse-test.sh`
  16/16 passing;
  runner image builds and yields restic 0.17.3 + rclone 1.68.2; every restic
  and rclone flag used by the code confirmed present in those versions.
- 2026-08-19T10:00Z [DISCOVERIES] The test suites caught four real defects:
  (a) `rclone_run`/`restic_run` crashed under `set -u` on an unset mounts
  array, hit by `s3-backup-status` every invocation; (b) `DUMP_FILES` written
  and never read; (c) `awscli_run` exported the bootstrap credentials inside a
  command substitution, so they were discarded with the subshell and any
  secret lacking S3 keys left restic/rclone with empty credentials;
  (d) `write_runtime_password` used `${pw%%$'\n'*}`, truncating a password at
  its first newline instead of stripping trailing ones. All fixed.
- 2026-08-19T09:58Z [TOOL] Verified against restic 0.17.3 directly: restic
  trims trailing newlines from a password file, so a secret stored with or
  without one opens the same repo. Password is now written verbatim.
- 2026-08-19T09:30Z [ASSUMPTION] NOT verified against real infrastructure - no
  HDD, no Immich/Nextcloud, no AWS credentials exist on `ubuntu-dev`.
  UNCONFIRMED until the server run: actual container names, HDD mount path,
  Nextcloud DB engine, Immich upload location. `s3-backup-discover` exists to
  resolve all four on the server.
- 2026-08-19T09:30Z [PLANS] Next actions are all on server `100.83.72.78`:
  install.sh --secrets aws -> s3-backup-setup-aws --apply -> install-canaries
  -> preflight -> --dry-run run -> seed -> restore-drill --deep.
