#!/usr/bin/env bash
# shellcheck shell=bash
# Prometheus textfile metrics, so an absent or failing backup shows up in the
# Grafana alerting you already run instead of in nobody's inbox.

write_metrics() {
  local success="$1" duration="$2"
  [[ -n "${METRICS_DIR:-}" && -d "$METRICS_DIR" ]] || return 0

  local out="${METRICS_DIR%/}/s3_backup.prom"
  local tmp="${out}.$$"
  local immich_bytes=0 immich_files=0
  read -r immich_bytes immich_files <<<"${IMMICH_REMOTE_STATS:-0 0}"

  {
    echo '# HELP s3_backup_success Whether the last backup run completed (1) or failed (0).'
    echo '# TYPE s3_backup_success gauge'
    echo "s3_backup_success ${success}"
    echo '# HELP s3_backup_last_run_timestamp_seconds Unix time the last run finished.'
    echo '# TYPE s3_backup_last_run_timestamp_seconds gauge'
    echo "s3_backup_last_run_timestamp_seconds $(date +%s)"
    if [[ "$success" == "1" ]]; then
      echo '# HELP s3_backup_last_success_timestamp_seconds Unix time of the last SUCCESSFUL run.'
      echo '# TYPE s3_backup_last_success_timestamp_seconds gauge'
      echo "s3_backup_last_success_timestamp_seconds $(date +%s)"
    fi
    echo '# HELP s3_backup_duration_seconds Wall-clock duration of the last run.'
    echo '# TYPE s3_backup_duration_seconds gauge'
    echo "s3_backup_duration_seconds ${duration}"
    echo '# HELP s3_backup_phase_success Per-phase result of the last run.'
    echo '# TYPE s3_backup_phase_success gauge'
    local phase
    for phase in "${!PHASE_RESULT[@]}"; do
      echo "s3_backup_phase_success{phase=\"${phase}\"} ${PHASE_RESULT[$phase]}"
    done
    echo '# HELP s3_backup_immich_remote_bytes Size of the Immich mirror in S3.'
    echo '# TYPE s3_backup_immich_remote_bytes gauge'
    echo "s3_backup_immich_remote_bytes ${immich_bytes}"
    echo '# HELP s3_backup_immich_remote_files Object count of the Immich mirror in S3.'
    echo '# TYPE s3_backup_immich_remote_files gauge'
    echo "s3_backup_immich_remote_files ${immich_files}"
  } > "$tmp"

  chmod 644 "$tmp"
  mv -f "$tmp" "$out"   # atomic: the collector never reads a half-written file
}

ping_healthcheck() {
  local success="$1"
  [[ -n "${HEALTHCHECK_URL:-}" ]] || return 0
  local url="$HEALTHCHECK_URL"
  [[ "$success" == "1" ]] || url="${url%/}/fail"
  curl -fsS -m 15 --retry 3 -o /dev/null "$url" 2>/dev/null \
    || warn "healthcheck ping failed"
}
