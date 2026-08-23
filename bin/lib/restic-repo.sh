#!/usr/bin/env bash
# shellcheck shell=bash
# restic side: DB dumps + Nextcloud data/config -> encrypted, deduplicated,
# snapshotted repository in S3.

restic_run() {
  RUNNER_MOUNTS=("${RESTIC_MOUNTS[@]:-}") run_in_runner restic "$@"
}

restic_ensure_repo() {
  if restic_run cat config >/dev/null 2>&1; then
    info "restic: repository present at ${RESTIC_REPOSITORY}"
    return 0
  fi
  info "restic: initialising new repository at ${RESTIC_REPOSITORY}"
  restic_run init || die "restic init failed"
  warn "A NEW restic repository was created. Back up ${RESTIC_PASSWORD_FILE} somewhere"
  warn "off this machine NOW - without it these backups are unrecoverable."
}

restic_backup() {
  local paths=() excludes=()

  [[ -d "$STAGING_DIR" ]] && paths+=("$STAGING_DIR")

  if [[ "$NEXTCLOUD_ENABLED" == "1" ]]; then
    [[ -d "$NEXTCLOUD_DATA_DIR" ]] || die "NEXTCLOUD_DATA_DIR not found: $NEXTCLOUD_DATA_DIR"
    paths+=("$NEXTCLOUD_DATA_DIR")
    [[ -d "${NEXTCLOUD_CONFIG_DIR:-}" ]] && paths+=("$NEXTCLOUD_CONFIG_DIR")

    # Regenerable or transient Nextcloud state.
    [[ "$NEXTCLOUD_EXCLUDE_PREVIEWS" == "1" ]] && excludes+=(--exclude 'appdata_*/preview')
    excludes+=(
      --exclude '*/cache'
      --exclude '*/uploads'
      --exclude 'updater-*/backups'
      --exclude 'nextcloud.log*'
    )
  fi

  (( ${#paths[@]} )) || { warn "restic: nothing to back up"; return 0; }

  # --host is mandatory here: inside the runner container the hostname is a
  # random container ID, so without this every night would create a snapshot
  # under a new host and `forget` retention would never match anything.
  info "restic: backing up ${#paths[@]} path(s)"
  restic_run backup \
    --host "$RESTIC_HOST" \
    --tag s3-backup-automation \
    --tag "$RUN_DATE" \
    --exclude-caches \
    "${excludes[@]}" \
    ${DRY_RUN:+--dry-run} \
    "${paths[@]}" || die "restic backup failed"
}

restic_retention() {
  local prune=0 today
  today="$(date +%u)"
  [[ -n "$RESTIC_PRUNE_DAY" && "$today" == "$RESTIC_PRUNE_DAY" ]] && prune=1

  local prune_flag=(); (( prune )) && prune_flag=(--prune)
  info "restic: forget (keep ${RESTIC_KEEP_DAILY}d/${RESTIC_KEEP_WEEKLY}w/${RESTIC_KEEP_MONTHLY}m/${RESTIC_KEEP_YEARLY}y)$([[ $prune == 1 ]] && echo ' + prune')"
  restic_run forget \
    --host "$RESTIC_HOST" \
    --group-by host,tags \
    --tag s3-backup-automation \
    --keep-daily "$RESTIC_KEEP_DAILY" \
    --keep-weekly "$RESTIC_KEEP_WEEKLY" \
    --keep-monthly "$RESTIC_KEEP_MONTHLY" \
    --keep-yearly "$RESTIC_KEEP_YEARLY" \
    "${prune_flag[@]}" \
    ${DRY_RUN:+--dry-run} || die "restic forget failed"

  if (( prune )) && [[ -z "${DRY_RUN:-}" ]]; then
    # Reads ~1/52 of the pack files, so the whole repo is verified over a year
    # without a full download every week.
    info "restic: integrity check (1/52 of data)"
    restic_run check --read-data-subset=1/52 || warn "restic check reported problems - INVESTIGATE"
  fi
}

restic_stats() {
  restic_run snapshots --host "$RESTIC_HOST" --latest 1 --json 2>/dev/null || echo '[]'
}
