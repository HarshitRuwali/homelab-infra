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
echo "############ Immich only (no Nextcloud) ############"
# Reproduces the real-server failure: s3-backup-discover finds no Nextcloud,
# writes NEXTCLOUD_DB_ENGINE="UNKNOWN", and every command then refused to run
# even though Nextcloud was disabled.
NOCLOUD="$T/nonextcloud.env"
sed -e 's|^NEXTCLOUD_ENABLED=.*|NEXTCLOUD_ENABLED=0|' \
    -e 's|^NEXTCLOUD_DB_ENGINE=.*|NEXTCLOUD_DB_ENGINE="UNKNOWN"|' \
    "$T/backup.env" > "$NOCLOUD"
chmod 600 "$NOCLOUD"
rm -rf "$FAKE_DOCKER_STATE"; mkdir -p "$FAKE_DOCKER_STATE"
: > "$FAKE_DOCKER_LOG"
rm -f "$T"/staging/*.sql.gz   # earlier suites left dumps here
nc_off() { "$BK" --config "$NOCLOUD" "$@" >"$T/nc.log" 2>&1; }

nc_off preflight
check '[[ $? -eq 0 ]]' "preflight accepts NEXTCLOUD_DB_ENGINE=UNKNOWN when Nextcloud is disabled"
check '! grep -q "must be .mysql. or .postgres" "$T/nc.log"' "  ...and does not complain about the engine"

nc_off run
RC=$?
check '[[ $RC -eq 0 ]]' "a full run succeeds with Immich only"
[[ $RC -ne 0 ]] && { echo "--- output ---"; cat "$T/nc.log"; }
check 'ls "$T"/staging/immich-db-*.sql.gz >/dev/null 2>&1' "Immich database still dumped"
check '! ls "$T"/staging/nextcloud-db-*.sql.gz >/dev/null 2>&1' "no Nextcloud dump attempted"
check '! grep -q "maintenance:mode" "$FAKE_DOCKER_LOG"' "Nextcloud maintenance mode never touched"
check '! grep -q "mysqldump" "$FAKE_DOCKER_LOG"' "mysqldump never run"
check '[[ -f "$FAKE_DOCKER_STATE/sync-ran" ]]' "Immich mirror still synced"

nc_off install-canaries
check '[[ $? -eq 0 ]]' "install-canaries works with Nextcloud disabled"
check '[[ -f "$HDD/immich/.s3-backup-canary" ]]' "  ...and marks the Immich root"
rm -f "$HDD/nextcloud/data/.s3-backup-canary"
nc_off install-canaries
check '[[ ! -f "$HDD/nextcloud/data/.s3-backup-canary" ]]' \
      "  ...and does not mark a disabled service"

# The disabled-service check above deleted the Nextcloud canary; put it back
# before any configuration that needs it.
"$BK" --config "$T/backup.env" install-canaries >/dev/null 2>&1

echo
echo "== the reverse, and the degenerate case =="
sed 's|^IMMICH_ENABLED=.*|IMMICH_ENABLED=0|' "$T/backup.env" > "$T/nc-only.env"
chmod 600 "$T/nc-only.env"
"$BK" --config "$T/nc-only.env" preflight >"$T/nc.log" 2>&1
check '[[ $? -eq 0 ]]' "Nextcloud-only is also a valid configuration"

sed -e 's|^IMMICH_ENABLED=.*|IMMICH_ENABLED=0|' -e 's|^NEXTCLOUD_ENABLED=.*|NEXTCLOUD_ENABLED=0|' \
    "$T/backup.env" > "$T/neither.env"
chmod 600 "$T/neither.env"
"$BK" --config "$T/neither.env" preflight >"$T/nc.log" 2>&1
check '[[ $? -ne 0 ]]' "disabling both services is rejected"
check 'grep -q "nothing to back up" "$T/nc.log"' "  ...with a message saying why"

# restore the canary the other suites rely on
"$BK" --config "$T/backup.env" install-canaries >/dev/null 2>&1
rm -rf "$FAKE_DOCKER_STATE"; mkdir -p "$FAKE_DOCKER_STATE"

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

echo
echo "############ s3-backup-setup-aws ############"

SETUP="$REPO/bin/s3-backup-setup-aws"
CFG="$T/setup.env"
# Admin credentials come from the environment. They must be supplied explicitly:
# the key inside backup.env belongs to the backup host and is deliberately
# ignored, so these tests would otherwise fail for the right reason.
admin() { AWS_ACCESS_KEY_ID=AKIAADMIN AWS_SECRET_ACCESS_KEY=adminsecret HOME=/root "$SETUP" "$@"; }
rm -rf "$FAKE_DOCKER_STATE"; mkdir -p "$FAKE_DOCKER_STATE"
cat > "$CFG" <<'EOF'
SECRETS_BACKEND="aws-secrets-manager"
S3_BUCKET=""
AWS_DEFAULT_REGION=""
SECRET_ID=""
AWS_ACCESS_KEY_ID="stale-value-that-must-be-cleared"
AWS_SECRET_ACCESS_KEY="stale-secret"
BOOTSTRAP_AWS_ACCESS_KEY_ID=""
BOOTSTRAP_AWS_SECRET_ACCESS_KEY=""
NEXTCLOUD_DB_ENGINE="mysql"
EOF
chmod 600 "$CFG"
cp "$CFG" "$T/setup.env.orig"

echo
echo "== admin credential discovery (the sudo \$HOME trap) =="
# Reproduces: `sudo s3-backup-setup-aws --profile default` failing with
# "The config profile (default) could not be found" because $HOME is root's.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

rm -rf /root/.aws
HOME=/root "$SETUP" --bucket b1 --region r1 --config "$CFG" >"$T/cred.log" 2>&1
check '[[ $? -ne 0 ]]'                              "fails when there are no admin credentials at all"
check 'grep -q "looked for a config directory at: /root/.aws" "$T/cred.log"' \
      "  ...and says exactly which path it tried"
check '[[ ! -d /root/.aws ]]' \
      "  ...and does not let docker fabricate an empty /root/.aws"

# A real user whose home holds the credentials, reached through SUDO_USER.
id testadmin >/dev/null 2>&1 || useradd -m testadmin >/dev/null 2>&1
ADMIN_HOME="$(getent passwd testadmin | cut -d: -f6)"
mkdir -p "$ADMIN_HOME/.aws"
printf '[default]\naws_access_key_id = AKIAADMIN\n' > "$ADMIN_HOME/.aws/credentials"
printf '[profile other]\nregion = eu-west-1\n'      > "$ADMIN_HOME/.aws/config"

: > "$FAKE_DOCKER_LOG"
HOME=/root SUDO_USER=testadmin "$SETUP" --bucket b1 --region r1 --config "$CFG" >"$T/cred.log" 2>&1
check '[[ $? -eq 0 ]]' "finds credentials in the sudo-invoking user's home"
check "grep -q \"$ADMIN_HOME/.aws:/root/.aws:ro\" \"$FAKE_DOCKER_LOG\"" \
      "  ...and mounts that directory, not root's"
check "grep -q \"aws credentials : $ADMIN_HOME/.aws\" \"$T/cred.log\"" \
      "  ...and reports which one it chose"
# backup.env defines AWS_ACCESS_KEY_ID for the backup host: a least-privilege
# key that cannot create buckets or IAM users. It must never be used as admin.
check_eq "$(cat "$FAKE_DOCKER_STATE/admin-key" 2>/dev/null)" none \
      "  ...and never authenticates with the low-privilege key from backup.env"

HOME=/root SUDO_USER=testadmin "$SETUP" --bucket b1 --region r1 --config "$CFG" \
  --profile nosuchprofile >"$T/cred.log" 2>&1
check '[[ $? -ne 0 ]]' "rejects a profile that is not defined"
check 'grep -q "profiles found: default other" "$T/cred.log"' \
      "  ...and lists the profiles that do exist"

HOME=/root SUDO_USER=testadmin "$SETUP" --bucket b1 --region r1 --config "$CFG" \
  --profile default >"$T/cred.log" 2>&1
check '[[ $? -eq 0 ]]' "accepts a profile that is defined in credentials"

: > "$FAKE_DOCKER_LOG"
mkdir -p "$T/elsewhere/.aws"; printf '[default]\n' > "$T/elsewhere/.aws/credentials"
HOME=/root "$SETUP" --bucket b1 --region r1 --config "$CFG" \
  --aws-config-dir "$T/elsewhere/.aws" >"$T/cred.log" 2>&1
check '[[ $? -eq 0 ]]' "--aws-config-dir overrides discovery"
check "grep -q \"$T/elsewhere/.aws:/root/.aws:ro\" \"$FAKE_DOCKER_LOG\"" \
      "  ...and is what gets mounted"

# Environment credentials alone are enough, with no config directory anywhere.
: > "$FAKE_DOCKER_LOG"
HOME=/root AWS_ACCESS_KEY_ID=AKIAENV AWS_SECRET_ACCESS_KEY=envsecret \
  "$SETUP" --bucket b1 --region r1 --config "$CFG" >"$T/cred.log" 2>&1
check '[[ $? -eq 0 ]]' "environment credentials work with no ~/.aws at all"
check 'grep -q "aws credentials : environment" "$T/cred.log"' "  ...and are reported as such"
check '! grep -q "/root/.aws:ro" "$FAKE_DOCKER_LOG"' "  ...with no credential directory mounted"

rm -rf "$FAKE_DOCKER_STATE"; mkdir -p "$FAKE_DOCKER_STATE"
cp "$T/setup.env.orig" "$CFG"; chmod 600 "$CFG"

echo
echo "== dry run changes nothing =="
admin --bucket b1 --region ap-south-1 --secret-id homelab/s3-backup --config "$CFG" >"$T/setup.log" 2>&1
check '[[ $? -eq 0 ]]' "dry run exits 0"
check 'diff -q "$CFG" "$T/setup.env.orig" >/dev/null' "config untouched by the dry run"
check '[[ ! -f "$FAKE_DOCKER_STATE/bucket" ]]' "no bucket created by the dry run"
check '[[ ! -f "$FAKE_DOCKER_STATE/secret" ]]' "no secret created by the dry run"
check 'grep -q "DRY RUN" "$T/setup.log"' "says it is a dry run"

echo
echo "== apply creates everything and writes the config =="
admin --bucket b1 --region ap-south-1 --secret-id homelab/s3-backup --config "$CFG" --apply >"$T/setup.log" 2>&1
RC=$?
check '[[ $RC -eq 0 ]]' "apply exits 0"
[[ $RC -ne 0 ]] && { echo "--- output ---"; cat "$T/setup.log"; }
check 'grep -q "^S3_BUCKET=\"b1\"" "$CFG"'                     "S3_BUCKET written"
check 'grep -q "^AWS_DEFAULT_REGION=\"ap-south-1\"" "$CFG"'    "AWS_DEFAULT_REGION written"
check 'grep -q "^SECRET_ID=\"homelab/s3-backup\"" "$CFG"'      "SECRET_ID written"
check 'grep -qE "^BOOTSTRAP_AWS_ACCESS_KEY_ID=\"AKIAFAKEKEY" "$CFG"' "bootstrap key written"
check 'grep -qE "^BOOTSTRAP_AWS_SECRET_ACCESS_KEY=\"fake-secret" "$CFG"' "bootstrap secret written"
check 'grep -q "^AWS_ACCESS_KEY_ID=\"\"$" "$CFG"'              "stale on-disk S3 key cleared"
check 'grep -q "^AWS_SECRET_ACCESS_KEY=\"\"$" "$CFG"'          "stale on-disk S3 secret cleared"
check '[[ "$(stat -c %a "$CFG")" == "600" ]]'                    "config still mode 0600"
check '[[ -f "$FAKE_DOCKER_STATE/secret" ]]'                     "secret created"
check 'grep -q "^create-secret$" "$FAKE_DOCKER_STATE/secret-writes"' "created rather than overwrote"
check_eq "$(cat "$FAKE_DOCKER_STATE/admin-key" 2>/dev/null)" AKIAADMIN \
      "authenticated with the admin key, not the backup host's key"

echo
echo "== the rewrite does not corrupt the config =="
check '[[ $(grep -c "^S3_BUCKET=" "$CFG") -eq 1 ]]'  "no duplicate S3_BUCKET line"
check '[[ $(grep -c "^SECRET_ID=" "$CFG") -eq 1 ]]'  "no duplicate SECRET_ID line"
check 'grep -q "^NEXTCLOUD_DB_ENGINE=\"mysql\"" "$CFG"' "unrelated settings preserved"
check 'bash -n "$CFG"'                                "config is still valid shell"

echo
echo "== nothing sensitive is printed =="
check '! grep -q "fake-secret-value" "$T/setup.log"' "no access key secret in the output"

echo
echo "== re-running is safe and preserves the restic password =="
admin --bucket b1 --region ap-south-1 --secret-id homelab/s3-backup --config "$CFG" --apply >"$T/setup2.log" 2>&1
check '[[ $? -eq 0 ]]' "second apply exits 0"
check 'grep -q "already exists" "$T/setup2.log"' "reports existing resources instead of recreating them"
check 'grep -q "reusing the restic password" "$T/setup2.log"' "reuses the stored restic password"
check '[[ $(grep -c "^create-secret$" "$FAKE_DOCKER_STATE/secret-writes") -eq 1 ]]' \
      "secret was never re-created"

rm -rf "$T" "$RTDIR"
echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
