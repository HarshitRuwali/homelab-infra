#!/usr/bin/env bash
# shellcheck shell=bash
# Refuse to run unless the world looks the way the backup assumes it does.

# The single most dangerous failure mode for a mirror-style backup: the HDD is
# not mounted, so every source directory is an empty stub on the root
# filesystem, and `rclone sync` faithfully deletes the entire cloud copy to
# match. Three independent guards below; any one of them stops the run.
check_source_is_real() {
  local path="$1" label="$2"

  [[ -d "$path" ]] || die "preflight: $label does not exist: $path"

  # Guard 1: the path must live on the HDD, not on the root filesystem.
  if [[ -n "${HDD_MOUNTPOINT:-}" ]]; then
    mountpoint -q "$HDD_MOUNTPOINT" \
      || die "preflight: HDD_MOUNTPOINT '$HDD_MOUNTPOINT' is NOT a mount point - the drive is not mounted. Refusing to run."
    local src_dev root_dev
    src_dev="$(stat -c %d "$path")"
    root_dev="$(stat -c %d /)"
    [[ "$src_dev" != "$root_dev" ]] \
      || die "preflight: $label ($path) is on the root filesystem, not the HDD. Refusing to run."
  fi

  # Guard 2: a canary file you create once by hand. It lives on the HDD, so it
  # vanishes the moment the mount is missing, even if the mount checks are off.
  if [[ -n "${CANARY_FILE:-}" ]]; then
    [[ -e "${path%/}/${CANARY_FILE}" ]] \
      || die "preflight: canary '${CANARY_FILE}' missing from $path. Either the drive is not mounted, or you have not run 's3-backup install-canaries'."
  fi

  # Guard 3: a source that is suddenly empty is never legitimate here.
  local entries
  entries="$(find "$path" -mindepth 1 -maxdepth 1 -printf . -quit 2>/dev/null | wc -c)"
  (( entries > 0 )) || die "preflight: $label ($path) is empty. Refusing to run."
}

preflight() {
  [[ "$(id -u)" == "0" ]] || die "must run as root (needs to read service data and write $STAGING_DIR)"

  command -v docker >/dev/null || die "docker not found on PATH"
  docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon"
  command -v flock >/dev/null || die "flock not found (install util-linux)"

  docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1 \
    || die "runner image '$RUNNER_IMAGE' not built. Run: docker/build.sh"

  # resolve_secrets runs before preflight and sets this to whichever file the
  # runner will actually be given - the configured one, or a tmpfs copy of the
  # value fetched from Secrets Manager.
  [[ -n "${RESTIC_PASSWORD_FILE_HOST:-}" ]] || die "secrets were not resolved before preflight"
  [[ -s "$RESTIC_PASSWORD_FILE_HOST" ]] || die "restic password is empty: $RESTIC_PASSWORD_FILE_HOST"

  mkdir -p "$STAGING_DIR" "$RESTIC_CACHE_DIR"
  chmod 700 "$STAGING_DIR"

  # Dumps land here before restic reads them; running out of room mid-dump
  # produces a truncated file that still looks plausible.
  local free_mb
  free_mb="$(df -Pm "$STAGING_DIR" | awk 'NR==2 {print $4}')"
  (( free_mb >= ${MIN_STAGING_FREE_MB:-5120} )) \
    || die "only ${free_mb}MB free at $STAGING_DIR, need ${MIN_STAGING_FREE_MB:-5120}MB"

  if [[ "$IMMICH_ENABLED" == "1" ]]; then
    check_source_is_real "$IMMICH_UPLOAD_LOCATION" "IMMICH_UPLOAD_LOCATION"
    container_running "$IMMICH_DB_CONTAINER" || die "Immich DB container '$IMMICH_DB_CONTAINER' not running"
  fi
  if [[ "$NEXTCLOUD_ENABLED" == "1" ]]; then
    check_source_is_real "$NEXTCLOUD_DATA_DIR" "NEXTCLOUD_DATA_DIR"
    container_running "$NEXTCLOUD_DB_CONTAINER" || die "Nextcloud DB container '$NEXTCLOUD_DB_CONTAINER' not running"
    container_running "$NEXTCLOUD_APP_CONTAINER" || die "Nextcloud app container '$NEXTCLOUD_APP_CONTAINER' not running"
  fi

  check_s3_reachable
  info "preflight: OK"
}

# Cheap end-to-end credential + bucket reachability test. Retried: an access
# key minted moments ago by s3-backup-setup-aws is a real, common case here -
# IAM access keys are eventually consistent, and using one within seconds of
# creation routinely fails with InvalidAccessKeyId until it propagates.
check_s3_reachable() {
  local attempt out rc delay="${S3_PREFLIGHT_RETRY_DELAY:-2}"
  for attempt in 1 2 3 4 5; do
    RUNNER_MOUNTS=()
    if out="$(run_in_runner rclone lsd "s3:${S3_BUCKET}" 2>&1)"; then
      return 0
    fi
    rc=$?
    # Only InvalidAccessKeyId is retried: a freshly minted IAM access key is
    # genuinely eventually consistent. SignatureDoesNotMatch (wrong secret) and
    # NoSuchBucket ("...does not exist") are permanent; retrying cannot fix
    # them, so they must not match this pattern.
    if (( attempt < 5 )) && printf '%s' "$out" | grep -qiE 'InvalidAccessKeyId'; then
      warn "preflight: S3 not reachable yet (attempt $attempt/5) - a freshly created access key can take a few seconds to propagate. Retrying in ${delay}s."
      sleep "$delay"
      delay=$(( delay * 2 ))
      continue
    fi
    break
  done

  err "preflight: cannot list s3://${S3_BUCKET} (rclone exit $rc)"
  printf '%s\n' "$out" | redact | tail -10 | sed 's/^/  /' >&2

  if printf '%s' "$out" | grep -qiE 'InvalidAccessKeyId'; then
    err "The access key does not exist yet from AWS's point of view. If you just ran s3-backup-setup-aws, this can take up to a minute to clear on its own; re-run 'sudo s3-backup preflight' shortly."
  elif printf '%s' "$out" | grep -qiE 'SignatureDoesNotMatch'; then
    err "The secret key does not match the access key ID. Check the value stored in Secrets Manager, or re-run s3-backup-setup-aws --apply."
  elif printf '%s' "$out" | grep -qiE 'NoSuchBucket'; then
    err "Bucket '${S3_BUCKET}' does not exist in this account/region. Check S3_BUCKET and AWS_DEFAULT_REGION in $CONFIG_FILE."
  elif printf '%s' "$out" | grep -qiE 'AccessDenied|Forbidden'; then
    err "Credentials are valid but lack permission on this bucket. Re-run s3-backup-setup-aws --apply to reattach the IAM policy."
  fi
  die "S3 preflight check failed"
}

install_canaries() {
  local p roots=()
  [[ "$IMMICH_ENABLED"    == "1" ]] && roots+=("${IMMICH_UPLOAD_LOCATION:-}")
  [[ "$NEXTCLOUD_ENABLED" == "1" ]] && roots+=("${NEXTCLOUD_DATA_DIR:-}")
  (( ${#roots[@]} )) || { warn "no enabled service to place a canary in"; return 0; }
  for p in "${roots[@]}"; do
    [[ -n "$p" && -d "$p" ]] || continue
    printf 'Created by s3-backup-automation on %s.\nDo not delete: its absence stops the backup from mistaking an unmounted drive for an empty one.\n' \
      "$(date -Is)" > "${p%/}/${CANARY_FILE:-.s3-backup-canary}"
    info "canary written to ${p%/}/${CANARY_FILE:-.s3-backup-canary}"
  done
}
