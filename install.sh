#!/usr/bin/env bash
#
# install.sh - deploy s3-backup-automation onto the server that has the HDD.
#
#   sudo ./install.sh                  # secrets on disk (default)
#   sudo ./install.sh --secrets aws    # secrets in AWS Secrets Manager
#   sudo ./install.sh --check          # is the deployed copy up to date?
#
# Re-run it after every `git pull`: pulling updates this checkout, not the
# copy under /opt that actually runs. The image is only rebuilt when the
# Dockerfile changed, so a reinstall is quick.
#
# Idempotent. It will never overwrite an existing backup.env or an existing
# restic password file.
#
set -euo pipefail

SECRETS="file"; CHECK=0; SKIP_BUILD=0
while (( $# )); do
  case "$1" in
    --secrets) SECRETS="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done
case "$SECRETS" in file|aws) ;; *) echo "--secrets must be 'file' or 'aws'" >&2; exit 1 ;; esac

PREFIX="${PREFIX:-/opt/s3-backup}"
ETC="${ETC:-/etc/s3-backup}"
SRC="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

# shellcheck source=bin/lib/common.sh
source "$SRC/bin/lib/common.sh"

SRC_FP="$(fingerprint_tree "$SRC")"
COMMIT="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"

if (( CHECK )); then
  if [[ ! -r "$PREFIX/.installed" ]]; then
    echo "not installed at $PREFIX"; exit 1
  fi
  dep_fp="$(fingerprint_tree "$PREFIX")"
  rec="$(sed -n 's/^version=//p' "$PREFIX/.installed")"
  echo "source   : $SRC_FP  (commit $COMMIT)"
  echo "deployed : $dep_fp  ($(sed -n 's/^installed=//p' "$PREFIX/.installed"))"
  if [[ "$SRC_FP" == "$dep_fp" ]]; then
    echo "up to date"; exit 0
  fi
  echo
  echo "DEPLOYED COPY IS STALE. The commands under /usr/local/bin run from"
  echo "$PREFIX, not from this checkout. Re-run:  sudo ./install.sh${SECRETS:+ --secrets $SECRETS}"
  [[ "$rec" != "$dep_fp" ]] && echo "(files under $PREFIX were also edited by hand since install)"
  exit 1
fi

[[ "$(id -u)" == "0" ]] || { echo "run as root: sudo ./install.sh" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/6  installing to $PREFIX"
install -d -m 0755 "$PREFIX"
rm -rf "${PREFIX:?}/bin" "${PREFIX:?}/docker" "${PREFIX:?}/aws" "${PREFIX:?}/docs" "${PREFIX:?}/systemd"
cp -r "$SRC/bin" "$SRC/docker" "$SRC/aws" "$SRC/docs" "$SRC/systemd" "$PREFIX/"
chmod 0755 "$PREFIX"/bin/s3-backup*
for b in s3-backup s3-backup-status s3-backup-discover s3-backup-restore-drill s3-backup-setup-aws; do
  ln -sf "$PREFIX/bin/$b" "/usr/local/bin/$b"
done

say "2/6  creating $ETC"
install -d -m 0700 "$ETC"
if [[ -f "$ETC/backup.env" ]]; then
  echo "     backup.env already exists - left untouched"
else
  # Fill it in from the running containers so the only values still missing are
  # the ones s3-backup-setup-aws creates.
  if "$PREFIX/bin/s3-backup-discover" > "$ETC/backup.env.tmp" 2>/dev/null \
     && [[ -s "$ETC/backup.env.tmp" ]]; then
    mv "$ETC/backup.env.tmp" "$ETC/backup.env"
    echo "     wrote $ETC/backup.env from the running Immich/Nextcloud containers"
  else
    rm -f "$ETC/backup.env.tmp"
    install -m 0600 "$SRC/config/backup.env.example" "$ETC/backup.env"
    echo "     could not inspect the containers; wrote the template instead"
  fi
  chmod 0600 "$ETC/backup.env"
  sed -i "s|^SECRETS_BACKEND=.*|SECRETS_BACKEND=\"$([[ "$SECRETS" == aws ]] && echo aws-secrets-manager || echo file)\"|" \
    "$ETC/backup.env"
  echo "     review it: the container names and HDD paths are guesses"
fi

say "3/6  restic repository password"
if [[ "$SECRETS" == "aws" ]]; then
  cat <<'MSG'
     Skipped: SECRETS_BACKEND=aws-secrets-manager keeps the restic password in
     AWS Secrets Manager and writes it to tmpfs only for the duration of a run.
     s3-backup-setup-aws creates it in step 1 below.
MSG
elif [[ -s "$ETC/restic-password" ]]; then
  echo "     existing password file kept (never regenerate: it would orphan the repo)"
else
  ( umask 077; head -c 32 /dev/urandom | base64 > "$ETC/restic-password" )
  chmod 0600 "$ETC/restic-password"
  cat <<'WARN'
     A new random restic password was generated at /etc/s3-backup/restic-password

     >>> COPY IT SOMEWHERE OFF THIS MACHINE NOW. <<<
     A password manager, a printout, another machine - anywhere but this HDD.
     If this file is lost, every restic backup in S3 is permanently unreadable.
     There is no recovery path. This is the whole point of the encryption.
WARN
fi

say "4/6  runtime directories"
install -d -m 0700 /var/lib/s3-backup/staging
install -d -m 0700 /var/cache/restic

say "5/6  runner image"
# Rebuild only when the image is missing or its definition changed, so
# reinstalling after a pull stays cheap.
DOCKERFILE_FP="$(sha256sum "$SRC/docker/Dockerfile" "$SRC/docker/build.sh" | sha256sum | cut -c1-12)"
PREV_DOCKERFILE_FP="$(sed -n 's/^dockerfile=//p' "$PREFIX/.installed" 2>/dev/null || true)"
RUNNER_IMAGE_NAME="${RUNNER_IMAGE:-s3-backup-runner:1.0.0}"
if (( SKIP_BUILD )); then
  echo "     skipped (--skip-build)"
elif docker image inspect "$RUNNER_IMAGE_NAME" >/dev/null 2>&1 \
     && [[ "$DOCKERFILE_FP" == "$PREV_DOCKERFILE_FP" ]]; then
  echo "     unchanged, keeping $RUNNER_IMAGE_NAME"
else
  "$PREFIX/docker/build.sh"
fi
if [[ "$SECRETS" == "aws" ]]; then
  AWSCLI_IMAGE="${AWSCLI_IMAGE:-amazon/aws-cli:2.17.0}"
  echo "     pulling $AWSCLI_IMAGE (used to read the secret at run time)"
  docker pull -q "$AWSCLI_IMAGE"
fi

say "6/6  installing systemd units"
install -m 0644 "$SRC"/systemd/*.service "$SRC"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now s3-backup.timer s3-backup-drill.timer
systemctl list-timers 's3-backup*' --no-pager

# Written last, so a stamp always means a completed install.
printf 'version=%s\ncommit=%s\ndockerfile=%s\ninstalled=%s\nsource_dir=%s\nsecrets=%s\n' \
  "$SRC_FP" "$COMMIT" "$DOCKERFILE_FP" "$(date -Is)" "$SRC" "$SECRETS" > "$PREFIX/.installed"
chmod 0644 "$PREFIX/.installed"

if [[ "$SECRETS" == aws ]]; then
  CREATES="the bucket, the IAM users, the Secrets Manager secret and the access keys"
else
  CREATES="the bucket, the IAM user and its access key"
fi

cat <<NEXT

Installed. Three commands left.

  1. Create everything in AWS and finish the config.
     Dry run first; it prints a plan and changes nothing:

       sudo s3-backup-setup-aws --bucket YOUR-BUCKET --region YOUR-REGION
       sudo s3-backup-setup-aws --bucket YOUR-BUCKET --region YOUR-REGION --apply

     It creates ${CREATES},
     and writes them into $ETC/backup.env itself. Nothing to copy by hand.

  2. Mark the drive and check everything, still without touching S3:

       sudo s3-backup install-canaries
       sudo s3-backup preflight
       sudo s3-backup --dry-run run

  3. Seed the first backup. It uploads everything, so run it in tmux:

       sudo systemctl start s3-backup.service
       journalctl -u s3-backup.service -f

Then prove it restores - do this once by hand, it also runs monthly:

       sudo s3-backup-restore-drill --deep

Before step 1, open $ETC/backup.env and check the container names
and HDD paths. Full walkthrough: $PREFIX/docs/setup.md
NEXT
