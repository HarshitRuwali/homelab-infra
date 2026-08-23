#!/usr/bin/env bash
# shellcheck shell=bash
# Secret resolution.
#
# Two backends:
#   file                 - secrets live in /etc/s3-backup on the server.
#   aws-secrets-manager  - only a narrowly-scoped bootstrap credential lives on
#                          the server; the restic password (and optionally the
#                          S3 keys) are fetched at run time.
#
# There is no way to read Secrets Manager without an AWS credential, so the
# bootstrap credential is unavoidable. What it buys is that the credential on
# disk can do exactly one thing - GetSecretValue on one secret ARN - so
# rotation and revocation happen centrally in AWS instead of by editing files
# on every host, and the restic password is no longer stored next to the data
# it protects.

# The AWS CLI runs in a container: nothing is installed on the host. `-e VAR`
# passes the value through from our environment rather than putting it in argv,
# so no secret is ever visible in `ps`.
# Must be called in the caller's shell, NOT from inside awscli_run: that runs
# in a command substitution, so an export there is discarded with the subshell
# and the credentials never reach restic or rclone.
_use_bootstrap_credentials() {
  if [[ -n "${BOOTSTRAP_AWS_PROFILE:-}" ]]; then
    export AWS_PROFILE="$BOOTSTRAP_AWS_PROFILE"
  else
    export AWS_ACCESS_KEY_ID="$BOOTSTRAP_AWS_ACCESS_KEY_ID"
    export AWS_SECRET_ACCESS_KEY="$BOOTSTRAP_AWS_SECRET_ACCESS_KEY"
  fi
}

awscli_run() {
  local args=()
  if [[ -n "${BOOTSTRAP_AWS_PROFILE:-}" ]]; then
    args+=(-e AWS_PROFILE -v "${BOOTSTRAP_AWS_DIR:-/root/.aws}:/root/.aws:ro")
  else
    args+=(-e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY)
  fi
  docker run --rm --network host \
    "${args[@]}" \
    -e "AWS_DEFAULT_REGION=${SECRET_REGION:-$AWS_DEFAULT_REGION}" \
    "$AWSCLI_IMAGE" "$@"
}

# Read one string field out of a JSON document, using jq from the runner image
# so that parsing a secret needs nothing installed on the host. The document
# arrives on stdin and is never in argv; only the field name is an argument.
_json_field() {
  printf '%s' "$1" | docker run --rm -i "$RUNNER_IMAGE" \
    jq -r --arg k "$2" '
      if type != "object" then error("secret is not a JSON object")
      elif (.[$k] | type) == "string" then .[$k]
      else "" end'
}

# The password is written to a tmpfs so it never reaches the disk, and is
# removed when the run ends. On tmpfs there is nothing for `shred` to overwrite;
# the pages are freed with the file.
write_runtime_password() {
  local pw="$1"
  install -d -m 0700 "$RUNTIME_SECRET_DIR"

  local fstype
  fstype="$(stat -f -c %T "$RUNTIME_SECRET_DIR" 2>/dev/null || echo unknown)"
  case "$fstype" in
    tmpfs|ramfs) ;;
    *) warn "RUNTIME_SECRET_DIR ($RUNTIME_SECRET_DIR) is on '$fstype', not tmpfs;" \
            "the restic password will touch the disk. Prefer a path under /run." ;;
  esac

  local f="${RUNTIME_SECRET_DIR}/restic-password"
  # Written verbatim. restic trims trailing newlines from a password file
  # itself (verified against restic 0.17.3), so a secret stored with or
  # without one opens the same repository - while a password that legitimately
  # contains a newline is preserved rather than silently truncated.
  ( umask 077; printf '%s' "$pw" > "$f" )
  chmod 0600 "$f"
  RESTIC_PASSWORD_FILE_HOST="$f"
  export RESTIC_PASSWORD_FILE_HOST
}

shred_runtime_secrets() {
  [[ -n "${RUNTIME_SECRET_DIR:-}" ]] || return 0
  rm -f "${RUNTIME_SECRET_DIR}/restic-password" 2>/dev/null || true
  rmdir "$RUNTIME_SECRET_DIR" 2>/dev/null || true
}

secrets_from_file() {
  [[ -r "$RESTIC_PASSWORD_FILE" ]] || die "restic password file unreadable: $RESTIC_PASSWORD_FILE"
  [[ -s "$RESTIC_PASSWORD_FILE" ]] || die "restic password file is empty: $RESTIC_PASSWORD_FILE"
  local pmode; pmode="$(stat -c %a "$RESTIC_PASSWORD_FILE")"
  [[ "$pmode" == "600" || "$pmode" == "400" ]] || warn "$RESTIC_PASSWORD_FILE is mode $pmode; should be 600"
  RESTIC_PASSWORD_FILE_HOST="$RESTIC_PASSWORD_FILE"
  export RESTIC_PASSWORD_FILE_HOST
}

secrets_from_aws() {
  docker image inspect "$AWSCLI_IMAGE" >/dev/null 2>&1 \
    || die "AWS CLI image '$AWSCLI_IMAGE' not present. Run: docker pull $AWSCLI_IMAGE"

  _use_bootstrap_credentials
  info "secrets: reading '$SECRET_ID' from AWS Secrets Manager"
  local payload
  payload="$(awscli_run secretsmanager get-secret-value \
                --secret-id "$SECRET_ID" \
                --query SecretString --output text 2>&1)" \
    || die "could not read secret '$SECRET_ID': ${payload}"
  [[ -n "$payload" && "$payload" != "None" ]] || die "secret '$SECRET_ID' has no SecretString"

  local pw="" akid="" asak=""
  if [[ "${payload:0:1}" == "{" ]]; then
    docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1 \
      || die "runner image '$RUNNER_IMAGE' is needed to parse a JSON secret. Run: docker/build.sh"
    pw="$(_json_field "$payload" "$SECRET_KEY_RESTIC_PASSWORD")" \
      || die "secret '$SECRET_ID' is not a valid JSON object"
    akid="$(_json_field "$payload" "$SECRET_KEY_AWS_ACCESS_KEY_ID")" || true
    asak="$(_json_field "$payload" "$SECRET_KEY_AWS_SECRET_ACCESS_KEY")" || true
  else
    # A plain-text secret is taken to be the restic password on its own.
    pw="$payload"
  fi
  unset payload

  [[ -n "$pw" ]] || die "secret '$SECRET_ID' has no '${SECRET_KEY_RESTIC_PASSWORD}' value"

  if [[ -n "$akid" && -n "$asak" ]]; then
    AWS_ACCESS_KEY_ID="$akid"
    AWS_SECRET_ACCESS_KEY="$asak"
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
    info "secrets: using S3 credentials from the secret"
  else
    # The runner image has no AWS profile or credential_process helper, so a
    # profile-based bootstrap cannot be reused for S3.
    [[ -z "${BOOTSTRAP_AWS_PROFILE:-}" ]] || die \
      "BOOTSTRAP_AWS_PROFILE is set, so the secret must supply '${SECRET_KEY_AWS_ACCESS_KEY_ID}' and '${SECRET_KEY_AWS_SECRET_ACCESS_KEY}' for S3"
    [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]] \
      || die "secret has no S3 credentials and no bootstrap key pair is configured"
    info "secrets: secret holds no S3 credentials; reusing the bootstrap key pair for S3"
  fi
  unset akid asak

  write_runtime_password "$pw"
  unset pw
}

resolve_secrets() {
  case "$SECRETS_BACKEND" in
    file)                secrets_from_file ;;
    aws-secrets-manager) secrets_from_aws ;;
    *) die "unknown SECRETS_BACKEND '$SECRETS_BACKEND'" ;;
  esac
}
