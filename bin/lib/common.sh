#!/usr/bin/env bash
# shellcheck shell=bash
# Shared helpers: logging, config loading, container-runner invocation.

CONFIG_FILE="${CONFIG_FILE:-/etc/s3-backup/backup.env}"

_ts() { date -Is; }
log()  { printf '%s [%-5s] %s\n' "$(_ts)" "$1" "${*:2}" >&2; }
info() { log INFO "$@"; }
warn() { log WARN "$@"; }
err()  { log ERROR "$@"; }
die()  { err "$@"; exit 1; }

# Redact anything secret-looking before it can reach a log line.
redact() { sed -E 's/(AWS_SECRET_ACCESS_KEY|PASSWORD|password|token)=[^ ]*/\1=<redacted>/g'; }

load_config() {
  check_deployment_freshness

  [[ -r "$CONFIG_FILE" ]] || die "config not readable: $CONFIG_FILE (copy config/backup.env.example)"
  local mode
  mode="$(stat -c '%a' "$CONFIG_FILE")"
  [[ "$mode" == "600" || "$mode" == "400" ]] || warn "$CONFIG_FILE is mode $mode; should be 600 (it holds AWS keys)"

  # Export everything the file sets, so the runner container inherits it.
  set -a
  # shellcheck source=/dev/null
  source "$CONFIG_FILE"
  set +a

  : "${S3_BUCKET:?S3_BUCKET must be set}"
  : "${AWS_DEFAULT_REGION:?AWS_DEFAULT_REGION must be set}"

  # Defaults for anything the operator left out.
  : "${RESTIC_PREFIX:=restic}"
  : "${RESTIC_PASSWORD_FILE:=/etc/s3-backup/restic-password}"
  : "${RESTIC_CACHE_DIR:=/var/cache/restic}"
  : "${RESTIC_KEEP_DAILY:=7}"; : "${RESTIC_KEEP_WEEKLY:=4}"
  : "${RESTIC_KEEP_MONTHLY:=6}"; : "${RESTIC_KEEP_YEARLY:=1}"
  : "${RESTIC_PRUNE_DAY:=7}"
  : "${IMMICH_ENABLED:=1}"; : "${NEXTCLOUD_ENABLED:=1}"
  : "${IMMICH_PREFIX:=immich}"
  : "${IMMICH_SYNC_DIRS:=library profile upload backups}"
  : "${IMMICH_S3_STORAGE_CLASS:=GLACIER_IR}"
  : "${NEXTCLOUD_DB_ENGINE:=mysql}"
  : "${NEXTCLOUD_OCC_USER:=www-data}"; : "${NEXTCLOUD_OCC_PATH:=occ}"
  : "${NEXTCLOUD_MAINTENANCE_MODE:=dumps_only}"
  : "${NEXTCLOUD_EXCLUDE_PREVIEWS:=1}"
  : "${STAGING_DIR:=/var/lib/s3-backup/staging}"; : "${STAGING_KEEP:=2}"
  : "${RUNNER_IMAGE:=s3-backup-runner:1.0.0}"
  : "${LOCK_FILE:=/var/lock/s3-backup.lock}"
  : "${RCLONE_TRANSFERS:=8}"; : "${RCLONE_CHECKERS:=16}"; : "${RCLONE_BWLIMIT:=}"
  : "${S3_ENDPOINT:=}"; : "${S3_PROVIDER:=AWS}"
  : "${METRICS_DIR:=}"; : "${HEALTHCHECK_URL:=}"
  : "${SECRETS_BACKEND:=file}"
  : "${SECRET_ID:=}"; : "${SECRET_REGION:=}"
  : "${SECRET_KEY_RESTIC_PASSWORD:=restic_password}"
  : "${SECRET_KEY_AWS_ACCESS_KEY_ID:=aws_access_key_id}"
  : "${SECRET_KEY_AWS_SECRET_ACCESS_KEY:=aws_secret_access_key}"
  : "${BOOTSTRAP_AWS_PROFILE:=}"; : "${BOOTSTRAP_AWS_DIR:=/root/.aws}"
  : "${BOOTSTRAP_AWS_ACCESS_KEY_ID:=}"; : "${BOOTSTRAP_AWS_SECRET_ACCESS_KEY:=}"
  : "${AWSCLI_IMAGE:=amazon/aws-cli:2.17.0}"
  : "${RUNTIME_SECRET_DIR:=/run/s3-backup}"
  : "${AWS_ACCESS_KEY_ID:=}"; : "${AWS_SECRET_ACCESS_KEY:=}"
  : "${HDD_MOUNTPOINT:=}"; : "${CANARY_FILE:=.s3-backup-canary}"
  : "${RCLONE_MAX_DELETE:=1000}"; : "${MIN_STAGING_FREE_MB:=5120}"

  # Where the credentials come from decides what must already be set here.
  case "$SECRETS_BACKEND" in
    file)
      : "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID must be set when SECRETS_BACKEND=file}"
      : "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY must be set when SECRETS_BACKEND=file}"
      ;;
    aws-secrets-manager)
      : "${SECRET_ID:?SECRET_ID must be set when SECRETS_BACKEND=aws-secrets-manager}"
      [[ -n "$BOOTSTRAP_AWS_PROFILE" || -n "$BOOTSTRAP_AWS_ACCESS_KEY_ID" ]] \
        || die "set BOOTSTRAP_AWS_PROFILE or BOOTSTRAP_AWS_ACCESS_KEY_ID: reading a secret still needs some credential"
      ;;
    *) die "SECRETS_BACKEND must be 'file' or 'aws-secrets-manager', got '$SECRETS_BACKEND'" ;;
  esac

  # Both services are optional, but backing up neither is certainly a mistake.
  [[ "$IMMICH_ENABLED" == "1" || "$NEXTCLOUD_ENABLED" == "1" ]] \
    || die "both IMMICH_ENABLED and NEXTCLOUD_ENABLED are 0: there is nothing to back up"

  # Only validate a service's settings when that service is switched on.
  # s3-backup-discover writes UNKNOWN for a database it could not identify;
  # that is fine as long as the service is disabled.
  if [[ "$NEXTCLOUD_ENABLED" == "1" ]]; then
    : "${NEXTCLOUD_APP_CONTAINER:?NEXTCLOUD_APP_CONTAINER must be set when NEXTCLOUD_ENABLED=1}"
    : "${NEXTCLOUD_DB_CONTAINER:?NEXTCLOUD_DB_CONTAINER must be set when NEXTCLOUD_ENABLED=1}"
    : "${NEXTCLOUD_DATA_DIR:?NEXTCLOUD_DATA_DIR must be set when NEXTCLOUD_ENABLED=1}"
    case "$NEXTCLOUD_DB_ENGINE" in
      mysql|postgres) ;;
      *) die "NEXTCLOUD_DB_ENGINE must be 'mysql' or 'postgres', got '$NEXTCLOUD_DB_ENGINE'.
   If you do not run Nextcloud, set NEXTCLOUD_ENABLED=0 in $CONFIG_FILE." ;;
    esac
    case "$NEXTCLOUD_MAINTENANCE_MODE" in
      dumps_only|full_run) ;;
      *) die "NEXTCLOUD_MAINTENANCE_MODE must be 'dumps_only' or 'full_run'" ;;
    esac
  fi

  if [[ "$IMMICH_ENABLED" == "1" ]]; then
    : "${IMMICH_DB_CONTAINER:?IMMICH_DB_CONTAINER must be set when IMMICH_ENABLED=1}"
    : "${IMMICH_UPLOAD_LOCATION:?IMMICH_UPLOAD_LOCATION must be set when IMMICH_ENABLED=1}"
  fi

  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION

  if [[ -n "$S3_ENDPOINT" ]]; then
    RESTIC_REPOSITORY="s3:${S3_ENDPOINT%/}/${S3_BUCKET}/${RESTIC_PREFIX}"
  else
    RESTIC_REPOSITORY="s3:s3.${AWS_DEFAULT_REGION}.amazonaws.com/${S3_BUCKET}/${RESTIC_PREFIX}"
  fi
  export RESTIC_REPOSITORY
}

container_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" == "true" ]]
}

# Run a command inside the pinned runner image. Host paths are bind-mounted at
# the SAME path inside the container, so restic snapshot paths match the host
# and a restore reads naturally.
#
# Usage: RUNNER_MOUNTS=(-v /a:/a:ro) run_in_runner restic backup /a
run_in_runner() {
  local mounts=()
  local m
  for m in "${RUNNER_MOUNTS[@]:-}"; do [[ -n "$m" ]] && mounts+=("$m"); done

  docker run --rm \
    --name "s3-backup-runner-$$" \
    --network host \
    -e RESTIC_REPOSITORY \
    -e RESTIC_PASSWORD_FILE=/run/secrets/restic-password \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
    -e RCLONE_CONFIG_S3_TYPE=s3 \
    -e RCLONE_CONFIG_S3_PROVIDER="$S3_PROVIDER" \
    -e RCLONE_CONFIG_S3_ENV_AUTH=true \
    -e RCLONE_CONFIG_S3_REGION="$AWS_DEFAULT_REGION" \
    -e RCLONE_CONFIG_S3_ENDPOINT="$S3_ENDPOINT" \
    -e RCLONE_CONFIG_S3_STORAGE_CLASS="$IMMICH_S3_STORAGE_CLASS" \
    -e RCLONE_CONFIG_S3_NO_CHECK_BUCKET=true \
    -v "$RESTIC_PASSWORD_FILE_HOST:/run/secrets/restic-password:ro" \
    -v "$RESTIC_CACHE_DIR:/root/.cache/restic" \
    "${mounts[@]}" \
    "$RUNNER_IMAGE" "$@"
}

# Content fingerprint of a deployed or source tree. Only the functional
# directories: a docs edit should not make a working install look stale.
fingerprint_tree() {
  local root="$1"
  ( cd "$root" 2>/dev/null || return 1
    find bin docker aws systemd -type f -print0 2>/dev/null \
      | LC_ALL=C sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -c1-12 )
}

# Warn - never block - when the deployed tree under /opt no longer matches
# the checkout it was installed from. `git pull` fast-forwards the checkout;
# it does not touch /opt, and nothing else makes that visible. This is the
# generalized form of `install.sh --check`, run automatically on every
# command instead of only when someone remembers to ask.
check_deployment_freshness() {
  local self_lib self_prefix installed_file source_dir installed_fp current_fp secrets_hint
  self_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"      # .../bin/lib
  self_prefix="$(cd "$self_lib/../.." && pwd)"                  # install root

  installed_file="$self_prefix/.installed"
  [[ -r "$installed_file" ]] || return 0   # not a real install (e.g. dev checkout, tests)

  source_dir="$(sed -n 's/^source_dir=//p' "$installed_file")"
  installed_fp="$(sed -n 's/^version=//p' "$installed_file")"
  secrets_hint="$(sed -n 's/^secrets=//p' "$installed_file")"
  [[ -n "$source_dir" && -d "$source_dir" ]] || return 0

  current_fp="$(fingerprint_tree "$source_dir" 2>/dev/null)" || return 0
  [[ -n "$current_fp" && -n "$installed_fp" ]] || return 0

  if [[ "$current_fp" != "$installed_fp" ]]; then
    warn "the deployed copy in $self_prefix is out of date with $source_dir - you likely ran 'git pull' without redeploying. Re-run: sudo $source_dir/install.sh${secrets_hint:+ --secrets $secrets_hint}"
  fi
}

human() { numfmt --to=iec-i --suffix=B "${1:-0}" 2>/dev/null || echo "${1:-0}"; }
