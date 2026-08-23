#!/usr/bin/env bash
#
# smoke-test.sh - drive the full backup flow against a mocked docker daemon.
#
# Verifies the orchestration, not restic/rclone themselves: phase ordering,
# maintenance-mode handling, dump verification, metrics output, and that every
# safety guard actually refuses to run. Needs root, so run it via
# tests/run.sh, which does it in a throwaway container.
#
set -uo pipefail

REPO="${REPO:-/opt/s3-backup}"
T="$(mktemp -d)"
export FAKE_DOCKER_LOG="$T/docker.log"
export FAKE_DOCKER_STATE="$T/state"
export PATH="$T/bin:$PATH"
mkdir -p "$T/bin" "$T/state"
cp "$REPO/tests/fake-docker" "$T/bin/docker"

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no()   { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
check() { if eval "$1"; then ok "$2"; else no "$2"; fi; }
check_eq() { # check_eq <actual> <expected> <label>
  if [[ "$1" == "$2" ]]; then ok "$3"; else no "$3 (want '"'"'$2'"'"', got '"'"'$1'"'"')"; fi
}

# --- fake HDD ---------------------------------------------------------------
HDD="$T/hdd"
mkdir -p "$HDD/immich/library/user1" "$HDD/immich/profile" \
         "$HDD/nextcloud/data/alice/files" "$HDD/nextcloud/config"
echo photo > "$HDD/immich/library/user1/IMG_0001.jpg"
echo doc   > "$HDD/nextcloud/data/alice/files/notes.txt"
echo cfg   > "$HDD/nextcloud/config/config.php"

cat > "$T/backup.env" <<EOF
S3_BUCKET="test-bucket"
AWS_DEFAULT_REGION="ap-south-1"
AWS_ACCESS_KEY_ID="AKIAFAKE"
AWS_SECRET_ACCESS_KEY="fakesecret"
IMMICH_ENABLED=1
IMMICH_DB_CONTAINER="immich_postgres"
IMMICH_UPLOAD_LOCATION="$HDD/immich"
IMMICH_SYNC_DIRS="library profile"
NEXTCLOUD_ENABLED=1
NEXTCLOUD_APP_CONTAINER="nextcloud"
NEXTCLOUD_DB_CONTAINER="nextcloud-db"
NEXTCLOUD_DB_ENGINE="mysql"
NEXTCLOUD_DATA_DIR="$HDD/nextcloud/data"
NEXTCLOUD_CONFIG_DIR="$HDD/nextcloud/config"
NEXTCLOUD_MAINTENANCE_MODE="dumps_only"
HDD_MOUNTPOINT=""
CANARY_FILE=".s3-backup-canary"
STAGING_DIR="$T/staging"
RESTIC_PASSWORD_FILE="$T/restic-password"
RESTIC_CACHE_DIR="$T/cache"
LOCK_FILE="$T/lock"
METRICS_DIR="$T/metrics"
MIN_STAGING_FREE_MB=1
RESTIC_PRUNE_DAY=""
EOF
chmod 600 "$T/backup.env"
printf 'fakepassword\n' > "$T/restic-password"; chmod 600 "$T/restic-password"
mkdir -p "$T/metrics"

BK="$REPO/bin/s3-backup"
run_backup() { "$BK" --config "$T/backup.env" "$@" >"$T/out.log" 2>&1; }

echo "== guard: refuses to run without canaries =="
run_backup preflight
check '[[ $? -ne 0 ]]' "preflight fails when the canary file is missing"
check 'grep -q "canary" "$T/out.log"' "  ...and says why"

echo
echo "== install-canaries =="
run_backup install-canaries
check '[[ -f "$HDD/immich/.s3-backup-canary" ]]'   "canary written to the Immich root"
check '[[ -f "$HDD/nextcloud/data/.s3-backup-canary" ]]' "canary written to the Nextcloud data root"

echo
echo "== guard: refuses to run on an empty source =="
EMPTY="$T/empty"; mkdir -p "$EMPTY"
sed "s|IMMICH_UPLOAD_LOCATION=.*|IMMICH_UPLOAD_LOCATION=\"$EMPTY\"|" "$T/backup.env" > "$T/empty.env"
chmod 600 "$T/empty.env"
"$BK" --config "$T/empty.env" preflight >"$T/out.log" 2>&1
check '[[ $? -ne 0 ]]' "preflight fails on an empty source directory"

echo
echo "== guard: refuses to run when a container is down =="
FAKE_CONTAINERS_RUNNING=false run_backup preflight
check '[[ $? -ne 0 ]]' "preflight fails when a service container is not running"

echo
echo "== preflight passes on a healthy system =="
run_backup preflight
check '[[ $? -eq 0 ]]' "preflight succeeds"

echo
echo "== full run =="
run_backup run
RC=$?
check '[[ $RC -eq 0 ]]' "s3-backup run exits 0"
[[ $RC -ne 0 ]] && { echo "--- output ---"; cat "$T/out.log"; }

check 'ls "$T"/staging/immich-db-*.sql.gz >/dev/null 2>&1'    "Immich dump written to staging"
check 'ls "$T"/staging/nextcloud-db-*.sql.gz >/dev/null 2>&1' "Nextcloud dump written to staging"
check 'gzip -t "$T"/staging/immich-db-*.sql.gz'               "Immich dump passes gzip integrity"
check 'zcat "$T"/staging/immich-db-*.sql.gz | tail -1 | grep -q "dump complete"' \
      "Immich dump has a completion trailer"

check '[[ -f "$FAKE_DOCKER_STATE/repo" ]]'       "restic repo was initialised on first use"
check '[[ -f "$FAKE_DOCKER_STATE/backup-ran" ]]' "restic backup ran"
check '[[ -f "$FAKE_DOCKER_STATE/forget-ran" ]]' "restic forget ran"
check '[[ -f "$FAKE_DOCKER_STATE/sync-ran" ]]'   "rclone sync ran"

echo
echo "== maintenance mode =="
check 'grep -q "maintenance:mode --on"  "$FAKE_DOCKER_LOG"' "Nextcloud was put into maintenance mode"
check 'grep -q "maintenance:mode --off" "$FAKE_DOCKER_LOG"' "Nextcloud was taken back out"
check '[[ "$(cat "$FAKE_DOCKER_STATE/maintenance")" == "off" ]]' "final maintenance state is off"
check '[[ $(grep -c "maintenance:mode --on" "$FAKE_DOCKER_LOG") -eq 1 ]]' "entered maintenance mode exactly once"

echo
echo "== dump correctness =="
check '! grep -q "docker exec -t" "$FAKE_DOCKER_LOG"' \
      "no dump used a TTY (which would corrupt the SQL with CRLF)"
check 'grep -q "single-transaction" "$FAKE_DOCKER_LOG"' \
      "mysqldump used --single-transaction"
check 'grep -q "pg_dumpall" "$FAKE_DOCKER_LOG"' \
      "Immich used pg_dumpall, not pg_dump"

echo
echo "== restic invocation =="
check 'grep -q -- "--host" "$FAKE_DOCKER_LOG"' \
      "restic was given an explicit --host (container hostnames are random)"
check 'grep -q -- "--max-delete" "$FAKE_DOCKER_LOG"' \
      "rclone sync was given --max-delete"
check 'grep -q -- "--backup-dir" "$FAKE_DOCKER_LOG"' \
      "rclone sync was given --backup-dir"
check '! grep -qE "\-v [^ ]*hdd[^ ]*:[^ ]*[^o]$" "$FAKE_DOCKER_LOG"' \
      "HDD paths were mounted into the runner read-only"

echo
echo "== metrics =="
M="$T/metrics/s3_backup.prom"
check '[[ -f "$M" ]]' "metrics file written"
check 'grep -q "^s3_backup_success 1" "$M"' "reports success"
for p in dumps restic immich retention; do
  check "grep -q 's3_backup_phase_success{phase=\"$p\"} 1' '$M'" "phase '$p' reported successful"
done
check 'grep -q "^s3_backup_last_success_timestamp_seconds" "$M"' "last-success timestamp present"
check 'grep -q "^s3_backup_immich_remote_bytes 567890123" "$M"' "mirror size recorded from rclone"

echo
echo "== failure path =="
rm -f "$M"
FAKE_IMAGE_MISSING=1 run_backup run
check '[[ $? -ne 0 ]]' "run fails when the runner image is missing"

echo
echo "== dry run writes nothing new =="
rm -rf "$T/staging"/*; : > "$FAKE_DOCKER_LOG"
run_backup --dry-run run
check 'grep -q -- "--dry-run" "$FAKE_DOCKER_LOG"' "--dry-run was propagated to restic and rclone"

echo
echo "== locking =="
( flock -n 9 || exit 1; "$BK" --config "$T/backup.env" run >"$T/lock.log" 2>&1 ) 9>"$T/lock"
check 'grep -q "another s3-backup run holds" "$T/lock.log"' "a second concurrent run is refused"

echo
echo "############ SECRETS_BACKEND=aws-secrets-manager ############"

# /run is tmpfs inside the container, matching the real deployment.
RTDIR="/run/s3-backup-test"
sed -e 's|^AWS_ACCESS_KEY_ID=.*|SECRETS_BACKEND="aws-secrets-manager"|' \
    -e 's|^AWS_SECRET_ACCESS_KEY=.*|SECRET_ID="homelab/s3-backup"|' \
    "$T/backup.env" > "$T/asm.env"
cat >> "$T/asm.env" <<EOF
BOOTSTRAP_AWS_ACCESS_KEY_ID="AKIABOOTSTRAP"
BOOTSTRAP_AWS_SECRET_ACCESS_KEY="bootstrap-secret"
RUNTIME_SECRET_DIR="$RTDIR"
AWSCLI_IMAGE="amazon/aws-cli:2.17.0"
EOF
chmod 600 "$T/asm.env"
rm -f "$T/restic-password"          # prove nothing falls back to the disk
rm -rf "$FAKE_DOCKER_STATE"; mkdir -p "$FAKE_DOCKER_STATE"
: > "$FAKE_DOCKER_LOG"

asm() { "$BK" --config "$T/asm.env" "$@" >"$T/out.log" 2>&1; }

echo
echo "== config validation =="
sed 's|^BOOTSTRAP_AWS_ACCESS_KEY_ID=.*|BOOTSTRAP_AWS_ACCESS_KEY_ID=""|' "$T/asm.env" > "$T/nobootstrap.env"
chmod 600 "$T/nobootstrap.env"
"$BK" --config "$T/nobootstrap.env" preflight >"$T/out.log" 2>&1
check '[[ $? -ne 0 ]]' "refuses to start with no bootstrap credential"
check 'grep -q "still needs some credential" "$T/out.log"' "  ...and explains why"

echo
echo "== full run against Secrets Manager =="
asm run
RC=$?
check '[[ $RC -eq 0 ]]' "run succeeds with the password fetched from Secrets Manager"
[[ $RC -ne 0 ]] && { echo "--- output ---"; cat "$T/out.log"; }

check '[[ ! -e "$T/restic-password" ]]' "no restic password file was created on disk"
check '[[ $(wc -l < "$FAKE_DOCKER_STATE/secret-fetches") -eq 1 ]]' "the secret was fetched exactly once"
check_eq "$(cat "$FAKE_DOCKER_STATE/secret-fetch-key" 2>/dev/null)" AKIABOOTSTRAP \
      "the fetch authenticated with the bootstrap key"
check_eq "$(cat "$FAKE_DOCKER_STATE/runner-key" 2>/dev/null)" AKIAOPERATIONAL \
      "S3 used the operational key from the secret, not the bootstrap key"

echo
echo "== the fetched password never persists =="
check '[[ ! -e "$RTDIR/restic-password" ]]' "tmpfs password file removed when the run ended"
check '[[ ! -d "$RTDIR" ]]'                 "tmpfs secret directory removed too"
check '! grep -q "s3cr3t-from-asm" "$T/out.log"'          "password never appears in the run log"
check '! grep -q "s3cr3t-from-asm" "$FAKE_DOCKER_LOG"'    "password never appears in a docker argument"
check '! grep -q "operational-secret" "$FAKE_DOCKER_LOG"' "S3 secret key never appears in a docker argument"
check '! grep -q "bootstrap-secret" "$FAKE_DOCKER_LOG"'   "bootstrap secret never appears in a docker argument"

echo
echo "== plain-text secret is taken as the password =="
rm -rf "$FAKE_DOCKER_STATE"; mkdir -p "$FAKE_DOCKER_STATE"
FAKE_SECRET_PAYLOAD="just-a-bare-password" asm run
check '[[ $? -eq 0 ]]' "a non-JSON secret is accepted as the restic password"
check_eq "$(cat "$FAKE_DOCKER_STATE/runner-key" 2>/dev/null)" AKIABOOTSTRAP \
      "with no keys in the secret, S3 falls back to the bootstrap credential"

echo
echo "== secret failures are fatal, not silent =="
rm -rf "$FAKE_DOCKER_STATE"; mkdir -p "$FAKE_DOCKER_STATE"
FAKE_SECRET_MISSING=1 asm run
check '[[ $? -ne 0 ]]' "run fails when the secret does not exist"
check 'grep -q "could not read secret" "$T/out.log"' "  ...with a clear message"
check '[[ ! -f "$FAKE_DOCKER_STATE/backup-ran" ]]' "  ...and no backup was attempted"

rm -rf "$T" "$RTDIR"
echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
