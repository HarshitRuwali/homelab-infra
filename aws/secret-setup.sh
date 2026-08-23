#!/usr/bin/env bash
#
# secret-setup.sh - put the restic password (and optionally the S3 keys) into
# AWS Secrets Manager, and create a bootstrap IAM user that can read that one
# secret and nothing else.
#
# DRY RUN BY DEFAULT. Nothing is created until you pass --apply.
#
#   ./secret-setup.sh --secret-id homelab/s3-backup --region ap-south-1 --generate
#   ./secret-setup.sh --secret-id homelab/s3-backup --region ap-south-1 \
#       --restic-password-file /etc/s3-backup/restic-password --apply
#
# Secret values are only ever read from files or generated here - never from
# the command line, so nothing sensitive lands in `ps` or your shell history.
# This script does not print any secret value.
#
set -euo pipefail

SECRET_ID=""; REGION=""; APPLY=0; GENERATE=0
PW_FILE=""; S3_KEY_ID=""; S3_SECRET_FILE=""; KMS_KEY=""
BOOTSTRAP_USER="s3-backup-bootstrap"; PROFILE=""
AWSCLI_IMAGE="${AWSCLI_IMAGE:-amazon/aws-cli:2.17.0}"
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

while (( $# )); do
  case "$1" in
    --secret-id)            SECRET_ID="$2"; shift 2 ;;
    --region)               REGION="$2"; shift 2 ;;
    --generate)             GENERATE=1; shift ;;
    --restic-password-file) PW_FILE="$2"; shift 2 ;;
    --s3-access-key-id)     S3_KEY_ID="$2"; shift 2 ;;
    --s3-secret-key-file)   S3_SECRET_FILE="$2"; shift 2 ;;
    --kms-key-id)           KMS_KEY="$2"; shift 2 ;;
    --bootstrap-user)       BOOTSTRAP_USER="$2"; shift 2 ;;
    --profile)              PROFILE="$2"; shift 2 ;;
    --apply)                APPLY=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$SECRET_ID" ]] || { echo "--secret-id is required" >&2; exit 1; }
[[ -n "$REGION"    ]] || { echo "--region is required" >&2; exit 1; }
if (( GENERATE )) && [[ -n "$PW_FILE" ]]; then
  echo "--generate and --restic-password-file are mutually exclusive" >&2; exit 1
fi
if (( ! GENERATE )) && [[ -z "$PW_FILE" ]]; then
  echo "pass --generate (new repo) or --restic-password-file (existing repo)" >&2; exit 1
fi

WORK="$(mktemp -d)"; chmod 700 "$WORK"; trap 'rm -rf "$WORK"' EXIT

aws_env=()
[[ -n "$PROFILE" ]] && aws_env+=(-e "AWS_PROFILE=$PROFILE")
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
  [[ -n "${!v:-}" ]] && aws_env+=(-e "$v")
done
awscli() {
  docker run --rm -i "${aws_env[@]}" -e "AWS_DEFAULT_REGION=$REGION" \
    -v "$HOME/.aws:/root/.aws:ro" -v "$WORK:/work:ro" "$AWSCLI_IMAGE" "$@"
}

# --- build the secret payload ------------------------------------------------
if (( GENERATE )); then
  RESTIC_PW="$(head -c 32 /dev/urandom | base64)"
  echo "A new restic password was generated. It will exist ONLY in Secrets Manager."
else
  [[ -r "$PW_FILE" ]] || { echo "cannot read $PW_FILE" >&2; exit 1; }
  # restic ignores a trailing newline in a password file; strip it so the
  # secret and the file are the same password.
  RESTIC_PW="$(printf '%s' "$(cat "$PW_FILE")")"
  [[ -n "$RESTIC_PW" ]] || { echo "$PW_FILE is empty" >&2; exit 1; }
  echo "Migrating the existing restic password from $PW_FILE (value not shown)."
fi

S3_SECRET=""
if [[ -n "$S3_SECRET_FILE" ]]; then
  [[ -r "$S3_SECRET_FILE" ]] || { echo "cannot read $S3_SECRET_FILE" >&2; exit 1; }
  S3_SECRET="$(printf '%s' "$(cat "$S3_SECRET_FILE")")"
  [[ -n "$S3_KEY_ID" ]] || { echo "--s3-secret-key-file needs --s3-access-key-id" >&2; exit 1; }
fi

# Values are passed through the environment, never argv.
OUT="$WORK/secret.json" RESTIC_PW="$RESTIC_PW" S3_KEY_ID="$S3_KEY_ID" S3_SECRET="$S3_SECRET" \
python3 -c '
import json, os
doc = {"restic_password": os.environ["RESTIC_PW"]}
if os.environ.get("S3_KEY_ID") and os.environ.get("S3_SECRET"):
    doc["aws_access_key_id"] = os.environ["S3_KEY_ID"]
    doc["aws_secret_access_key"] = os.environ["S3_SECRET"]
with open(os.environ["OUT"], "w") as fh:
    json.dump(doc, fh)
'
chmod 600 "$WORK/secret.json"
unset RESTIC_PW S3_SECRET

echo
echo "secret id       : $SECRET_ID"
echo "region          : $REGION"
echo "bootstrap user  : $BOOTSTRAP_USER"
echo "contains S3 keys: $([[ -n "$S3_KEY_ID" ]] && echo yes || echo 'no (S3 keys stay in backup.env)')"
(( APPLY )) || echo $'\nDRY RUN - showing what would be done. Re-run with --apply to execute.'

echo $'\n== identity =='
ACCOUNT="$(awscli sts get-caller-identity --query Account --output text)" \
  || { echo "cannot authenticate to AWS" >&2; exit 1; }
echo "account: $ACCOUNT"

SECRET_ARN="arn:aws:secretsmanager:${REGION}:${ACCOUNT}:secret:${SECRET_ID}-*"
sed "s|SECRET_ARN|${SECRET_ARN}|g" "$HERE/bootstrap-iam-policy.json" > "$WORK/bootstrap-policy.json"
if [[ -n "$KMS_KEY" ]]; then
  python3 - "$WORK/bootstrap-policy.json" "$KMS_KEY" "$REGION" "$ACCOUNT" <<'PYK'
import json, sys
path, key, region, account = sys.argv[1:5]
doc = json.load(open(path))
arn = key if key.startswith("arn:") else f"arn:aws:kms:{region}:{account}:key/{key}"
doc["Statement"].insert(1, {
    "Sid": "DecryptTheSecret", "Effect": "Allow",
    "Action": ["kms:Decrypt"], "Resource": arn})
json.dump(doc, open(path, "w"), indent=2)
PYK
fi

echo $'\n== does the secret already exist? =='
if awscli secretsmanager describe-secret --secret-id "$SECRET_ID" >/dev/null 2>&1; then
  echo "yes - it will be UPDATED to a new version."
  EXISTS=1
else
  echo "no - it will be created."
  EXISTS=0
fi

if (( ! APPLY )); then
  cat <<MSG

[dry-run] would run:
  aws secretsmanager $( ((EXISTS)) && echo put-secret-value || echo create-secret ) \\
      --secret-id $SECRET_ID --secret-string file:///work/secret.json${KMS_KEY:+ --kms-key-id $KMS_KEY}
  aws iam create-user --user-name $BOOTSTRAP_USER
  aws iam put-user-policy --user-name $BOOTSTRAP_USER \\
      --policy-name read-$SECRET_ID --policy-document file:///work/bootstrap-policy.json

bootstrap policy grants GetSecretValue on:
  $SECRET_ARN
and explicitly denies every other Secrets Manager action.

Dry run complete. Nothing was changed. Re-run with --apply.
MSG
  exit 0
fi

echo $'\n>> writing the secret'
if (( EXISTS )); then
  awscli secretsmanager put-secret-value --secret-id "$SECRET_ID" \
    --secret-string "file:///work/secret.json" >/dev/null
else
  awscli secretsmanager create-secret --name "$SECRET_ID" \
    --description "restic password for the Immich/Nextcloud S3 backup" \
    ${KMS_KEY:+--kms-key-id "$KMS_KEY"} \
    --secret-string "file:///work/secret.json" >/dev/null
fi
echo "   done (value not shown)"

echo $'\n>> creating the bootstrap IAM user'
awscli iam create-user --user-name "$BOOTSTRAP_USER" >/dev/null 2>&1 \
  || echo "   user already exists, continuing"
awscli iam put-user-policy --user-name "$BOOTSTRAP_USER" \
  --policy-name "read-$(echo "$SECRET_ID" | tr '/' '-')" \
  --policy-document "file:///work/bootstrap-policy.json"
echo "   policy attached: GetSecretValue on $SECRET_ARN, everything else denied"

cat <<MSG

Create the bootstrap access key yourself, so the secret never lands in this
script's output or your shell history:

    aws iam create-access-key --user-name $BOOTSTRAP_USER

Put the two values into /etc/s3-backup/backup.env as:

    SECRETS_BACKEND="aws-secrets-manager"
    SECRET_ID="$SECRET_ID"
    BOOTSTRAP_AWS_ACCESS_KEY_ID="..."
    BOOTSTRAP_AWS_SECRET_ACCESS_KEY="..."

Then verify and remove the old on-disk password:

    sudo s3-backup preflight
    sudo s3-backup snapshots
    sudo shred -u /etc/s3-backup/restic-password

Keep an offline copy of the restic password anyway. See docs/secrets.md.
MSG
