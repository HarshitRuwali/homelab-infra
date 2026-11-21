#!/usr/bin/env bash
# shellcheck shell=bash
# Database dumps. Every dump runs INSIDE the service's own DB container, using
# that container's own credentials from its environment. Two consequences:
#   1. No DB password ever lives in our config file.
#   2. The dump tool version always matches the server version exactly, which
#      matters for Immich's patched Postgres (VectorChord/pgvecto.rs).
#
# `docker exec` is deliberately called WITHOUT -t. A TTY translates LF to CRLF
# and silently corrupts the SQL stream; the dump only fails on restore, months
# later. Upstream docs show -t; do not copy that.
#
# Dumps are written UNCOMPRESSED (.sql). restic deduplicates by content-defined
# chunks and compresses what it stores (repository format v2, zstd), so a plain
# dump costs only the changed chunks per night, where a gzip stream changes end
# to end and is stored in full every time.

nextcloud_occ() {
  docker exec -u "$NEXTCLOUD_OCC_USER" "$NEXTCLOUD_APP_CONTAINER" \
    php "$NEXTCLOUD_OCC_PATH" "$@"
}

nextcloud_maintenance() {
  local state="$1"  # on|off
  [[ "${NEXTCLOUD_ENABLED}" == "1" ]] || return 0
  container_running "$NEXTCLOUD_APP_CONTAINER" || {
    warn "nextcloud container '$NEXTCLOUD_APP_CONTAINER' not running; skipping maintenance --$state"
    return 0
  }
  info "nextcloud: maintenance mode --$state"
  nextcloud_occ maintenance:mode "--$state" >/dev/null || warn "occ maintenance:mode --$state failed"
}

# verify_dump <file> <min_bytes>
verify_dump() {
  local f="$1" min="${2:-1024}" size
  # A dump that fails any check is removed: left in staging it would count
  # against STAGING_KEEP and push a good older dump out.
  [[ -s "$f" ]] || { rm -f "$f"; die "dump is empty: $f"; }
  size="$(stat -c %s "$f")"
  (( size >= min )) || { rm -f "$f"; die "dump suspiciously small ($size bytes, expected >= $min): $f"; }
  dump_integrity_ok "$f" || { rm -f "$f"; die "dump failed gzip integrity check: $f"; }
  dump_complete "$f" || { rm -f "$f"; die "dump has no completion trailer, it was truncated: $f"; }
  info "  verified $(basename "$f") ($(human "$size"))"
}

dump_immich_db() {
  [[ "${IMMICH_ENABLED}" == "1" ]] || return 0
  container_running "$IMMICH_DB_CONTAINER" \
    || die "Immich DB container '$IMMICH_DB_CONTAINER' is not running"

  local out="${STAGING_DIR}/immich-db-${RUN_STAMP}.sql"
  info "immich: pg_dumpall from $IMMICH_DB_CONTAINER"

  # pg_dumpall (not pg_dump): Immich needs roles and globals restored too.
  if ! docker exec "$IMMICH_DB_CONTAINER" sh -c '
        set -eu
        export PGPASSWORD="${POSTGRES_PASSWORD:-}"
        exec pg_dumpall --clean --if-exists --username="${POSTGRES_USER:-postgres}"
      ' > "$out.part"; then
    rm -f "$out.part"
    die "immich pg_dumpall failed"
  fi
  mv "$out.part" "$out"
  verify_dump "$out" 10240
}

dump_nextcloud_db() {
  [[ "${NEXTCLOUD_ENABLED}" == "1" ]] || return 0
  container_running "$NEXTCLOUD_DB_CONTAINER" \
    || die "Nextcloud DB container '$NEXTCLOUD_DB_CONTAINER' is not running"

  local out="${STAGING_DIR}/nextcloud-db-${RUN_STAMP}.sql"
  info "nextcloud: ${NEXTCLOUD_DB_ENGINE} dump from $NEXTCLOUD_DB_CONTAINER"

  local rc=0
  if [[ "$NEXTCLOUD_DB_ENGINE" == "mysql" ]]; then
    # --single-transaction gives a consistent InnoDB snapshot without locking
    # the whole database, so Nextcloud downtime stays at maintenance mode only.
    docker exec "$NEXTCLOUD_DB_CONTAINER" sh -c '
        set -eu
        exec mysqldump \
          --single-transaction --quick --no-tablespaces \
          --default-character-set=utf8mb4 \
          --routines --triggers --events \
          -u root -p"${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}" \
          --databases "${MYSQL_DATABASE:-${MARIADB_DATABASE:-nextcloud}}"
      ' 2> >(grep -v "Using a password on the command line" >&2) \
      > "$out.part" || rc=$?
  else
    docker exec "$NEXTCLOUD_DB_CONTAINER" sh -c '
        set -eu
        export PGPASSWORD="${POSTGRES_PASSWORD:-}"
        exec pg_dump --clean --if-exists --no-owner \
          -U "${POSTGRES_USER:-nextcloud}" -d "${POSTGRES_DB:-nextcloud}"
      ' > "$out.part" || rc=$?
  fi

  if (( rc != 0 )); then rm -f "$out.part"; die "nextcloud db dump failed (rc=$rc)"; fi
  mv "$out.part" "$out"
  verify_dump "$out" 10240

  # App list is not in the DB dump in a readable form; it makes a restore far
  # less guesswork. Best-effort only.
  nextcloud_occ app:list > "${STAGING_DIR}/nextcloud-apps-${RUN_STAMP}.txt" 2>/dev/null \
    || warn "could not capture 'occ app:list'"
}

prune_staging() {
  local svc keep="$STAGING_KEEP"
  # Plain .sql and legacy .sql.gz dumps age out together, newest N kept.
  for svc in immich-db nextcloud-db; do
    # shellcheck disable=SC2012
    find "$STAGING_DIR" -maxdepth 1 \( -name "${svc}-*.sql" -o -name "${svc}-*.sql.gz" \) -printf '%T@ %p\n' 2>/dev/null \
      | sort -rn | tail -n "+$((keep + 1))" | cut -d' ' -f2- \
      | while read -r old; do info "staging: removing old dump $(basename "$old")"; rm -f "$old"; done
  done
  # shellcheck disable=SC2012
  find "$STAGING_DIR" -maxdepth 1 -name 'nextcloud-apps-*.txt' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | tail -n "+$((keep + 1))" | cut -d' ' -f2- \
    | while read -r old; do info "staging: removing old dump $(basename "$old")"; rm -f "$old"; done
}
