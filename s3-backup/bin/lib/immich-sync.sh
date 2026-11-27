#!/usr/bin/env bash
# shellcheck shell=bash
# Immich originals -> plain rclone mirror in S3, so photos stay browsable and
# restorable without any tooling beyond the AWS console.

rclone_run() {
  # Split across two statements deliberately - see the comment on restic_run
  # in restic-repo.sh for why `VAR=(...) cmd` on one line is broken here.
  RUNNER_MOUNTS=("${IMMICH_MOUNTS[@]:-}")
  run_in_runner rclone "$@"
}

immich_sync() {
  [[ "$IMMICH_ENABLED" == "1" ]] || return 0
  [[ -d "$IMMICH_UPLOAD_LOCATION" ]] || die "IMMICH_UPLOAD_LOCATION not found: $IMMICH_UPLOAD_LOCATION"

  local bw=() cmp=() dir src dst failed=0
  [[ -n "$RCLONE_BWLIMIT" ]] && bw=(--bwlimit "$RCLONE_BWLIMIT")

  # How rclone decides a file is unchanged. rclone's default on S3 (size plus
  # modtime) needs the object's mtime, which a listing does not return, so it
  # costs one HEAD request per object per run. Immich originals are written
  # once and never edited in place, so size alone is enough and needs no
  # per-object request. --checksum does not avoid the HEADs either: S3 ETags of
  # multipart uploads are not MD5, so rclone reads its own stored MD5 metadata,
  # again per object. See docs/operations.md, "Immich comparison".
  case "$RCLONE_COMPARE" in
    size-only) cmp=(--size-only) ;;
    checksum)  cmp=(--checksum) ;;
    modtime)   cmp=() ;;
  esac

  for dir in $IMMICH_SYNC_DIRS; do
    src="${IMMICH_UPLOAD_LOCATION%/}/${dir}"
    if [[ ! -d "$src" ]]; then
      info "immich: skipping '$dir' (not present)"
      continue
    fi
    dst="s3:${S3_BUCKET}/${IMMICH_PREFIX}/${dir}"

    # --backup-dir is the safety net that `sync` otherwise lacks: a file
    # deleted or corrupted on the HDD is moved aside in S3 under a dated
    # prefix instead of being destroyed. Lifecycle expires it after 90 days.
    info "immich: sync ${dir}/ -> ${dst}"
    if ! rclone_run sync "$src" "$dst" \
        --backup-dir "s3:${S3_BUCKET}/_deleted/${RUN_DATE}/${IMMICH_PREFIX}/${dir}" \
        --transfers "$RCLONE_TRANSFERS" \
        --checkers "$RCLONE_CHECKERS" \
        --fast-list \
        "${cmp[@]}" \
        --max-delete "$RCLONE_MAX_DELETE" \
        --retries 3 --low-level-retries 10 \
        --stats 5m --stats-one-line \
        --log-level INFO \
        "${bw[@]}" \
        ${DRY_RUN:+--dry-run}; then
      err "immich: sync of '$dir' FAILED"
      failed=1
    fi
  done

  (( failed == 0 )) || die "one or more Immich sync targets failed"
}

immich_remote_size() {
  [[ "$IMMICH_ENABLED" == "1" ]] || { echo "0 0"; return 0; }
  local json
  json="$(rclone_run size "s3:${S3_BUCKET}/${IMMICH_PREFIX}" --json --fast-list 2>/dev/null || echo '{}')"
  local bytes count
  bytes="$(printf '%s' "$json" | grep -o '"bytes":[0-9]*' | cut -d: -f2)"
  count="$(printf '%s' "$json" | grep -o '"count":[0-9]*' | cut -d: -f2)"
  echo "${bytes:-0} ${count:-0}"
}
