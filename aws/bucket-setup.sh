#!/usr/bin/env bash
#
# bucket-setup.sh - create and harden the S3 bucket and the least-privilege
# IAM user the backup host will use.
#
# DRY RUN BY DEFAULT. Nothing is created until you pass --apply.
#
#   ./bucket-setup.sh --bucket my-homelab-backup --region ap-south-1
#   ./bucket-setup.sh --bucket my-homelab-backup --region ap-south-1 --apply
#
# Runs the AWS CLI in a container, so nothing is installed on the host.
# Credentials: this uses your ADMIN credentials, not the backup user's. Supply
# them with an existing ~/.aws profile (--profile) or in the environment.
#
set -euo pipefail

BUCKET=""; REGION=""; APPLY=0; IAM_USER="s3-backup-homelab"; PROFILE=""
AWSCLI_IMAGE="${AWSCLI_IMAGE:-amazon/aws-cli:2.17.0}"
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

while (( $# )); do
  case "$1" in
    --bucket)  BUCKET="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    --iam-user) IAM_USER="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --apply)   APPLY=1; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$BUCKET" ]] || { echo "--bucket is required" >&2; exit 1; }
[[ -n "$REGION" ]] || { echo "--region is required" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
for f in iam-policy.json bucket-policy-tls.json lifecycle.json; do
  sed "s/BUCKET_NAME/${BUCKET}/g" "$HERE/$f" > "$WORK/$f"
done

aws_env=()
[[ -n "$PROFILE" ]] && aws_env+=(-e "AWS_PROFILE=$PROFILE")
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
  [[ -n "${!v:-}" ]] && aws_env+=(-e "$v")
done

awscli() {
  docker run --rm -i \
    "${aws_env[@]}" \
    -e "AWS_DEFAULT_REGION=$REGION" \
    -v "$HOME/.aws:/root/.aws:ro" \
    -v "$WORK:/work:ro" \
    "$AWSCLI_IMAGE" "$@"
}

step() {
  local desc="$1"; shift
  if (( APPLY )); then
    printf '\n>> %s\n' "$desc"
    awscli "$@" || { echo "   FAILED: $desc" >&2; return 1; }
  else
    printf '\n[dry-run] %s\n   aws %s\n' "$desc" "$*"
  fi
}

echo "bucket : $BUCKET"
echo "region : $REGION"
echo "iam    : $IAM_USER"
(( APPLY )) || echo $'\nDRY RUN - showing what would be done. Re-run with --apply to execute.'

# --- read-only checks always run, even in dry-run -----------------------------
echo $'\n== identity =='
awscli sts get-caller-identity || { echo "cannot authenticate to AWS" >&2; exit 1; }

echo $'\n== does the bucket already exist? =='
if awscli s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  echo "yes - it exists and you can reach it. Creation will be skipped."
  BUCKET_EXISTS=1
else
  echo "no - it will be created."
  BUCKET_EXISTS=0
fi

# --- writes -------------------------------------------------------------------
if (( ! BUCKET_EXISTS )); then
  if [[ "$REGION" == "us-east-1" ]]; then
    step "create bucket" s3api create-bucket --bucket "$BUCKET"
  else
    step "create bucket" s3api create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=$REGION"
  fi
fi

step "block all public access" \
  s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

step "enable default encryption (SSE-S3)" \
  s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

# Versioning is the recovery path for "the backup itself was corrupted or
# maliciously deleted". Without it, a bad sync overwrites the only copy.
step "enable versioning" \
  s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

step "apply lifecycle rules (IA transition, graveyard expiry, MPU cleanup)" \
  s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration file:///work/lifecycle.json

step "deny non-TLS access" \
  s3api put-bucket-policy --bucket "$BUCKET" \
  --policy file:///work/bucket-policy-tls.json

step "create IAM user $IAM_USER" iam create-user --user-name "$IAM_USER"

step "attach least-privilege inline policy" \
  iam put-user-policy --user-name "$IAM_USER" \
  --policy-name "s3-backup-${BUCKET}" \
  --policy-document file:///work/iam-policy.json

if (( APPLY )); then
  cat <<'MSG'

>> create an access key for the backup host

    Run this yourself and paste the two values into /etc/s3-backup/backup.env.
    It is deliberately NOT run here so the secret never lands in this script's
    output, your shell history, or a log:

MSG
  echo "    aws iam create-access-key --user-name $IAM_USER"
  echo
  echo "Bucket setup complete."
else
  echo
  echo "Dry run complete. Nothing was changed. Re-run with --apply."
fi
