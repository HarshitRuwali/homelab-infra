#!/usr/bin/env bash
#
# secret-parse-test.sh - exercise the real secret parser against the real
# runner image. The smoke suite mocks docker, so its jq is a stand-in; this is
# what proves the actual jq filter handles awkward values correctly.
#
#   ./tests/secret-parse-test.sh          (needs the runner image built)
#
set -uo pipefail
LIB="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../bin/lib" && pwd)"
source "$LIB/common.sh"
source "$LIB/secrets.sh"

RUNNER_IMAGE="${RUNNER_IMAGE:-s3-backup-runner:1.0.0}"
docker image inspect "$RUNNER_IMAGE" >/dev/null 2>&1 \
  || { echo "runner image $RUNNER_IMAGE not built; run docker/build.sh" >&2; exit 1; }

PASS=0; FAIL=0
expect() { # expect <json> <key> <expected>
  local got
  got="$(_json_field "$1" "$2")"
  if [[ "$got" == "$3" ]]; then
    printf '  \033[32mPASS\033[0m %s\n' "${4:-$2}"; PASS=$((PASS+1))
  else
    printf '  \033[31mFAIL\033[0m %s\n       want %q\n        got %q\n' "${4:-$2}" "$3" "$got"
    FAIL=$((FAIL+1))
  fi
}
expect_err() {
  if _json_field "$1" "$2" >/dev/null 2>&1; then
    printf '  \033[31mFAIL\033[0m %s (expected an error)\n' "$3"; FAIL=$((FAIL+1))
  else
    printf '  \033[32mPASS\033[0m %s\n' "$3"; PASS=$((PASS+1))
  fi
}

echo "== ordinary values =="
expect '{"restic_password":"hunter2","aws_access_key_id":"AKIAX"}' restic_password hunter2
expect '{"restic_password":"hunter2","aws_access_key_id":"AKIAX"}' aws_access_key_id AKIAX
expect '{"restic_password":"hunter2"}' aws_access_key_id "" "missing key yields empty"

echo
echo "== values a naive parser gets wrong =="
# base64 password bytes: '/' and '+' are ordinary, but '"' and '\' are not.
expect '{"restic_password":"a\"b"}'      restic_password 'a"b'   'value containing a double quote'
expect '{"restic_password":"a\\b"}'      restic_password 'a\b'   'value containing a backslash'
expect '{"restic_password":"a\nb"}'      restic_password $'a\nb' 'value containing a newline'
expect '{"restic_password":"a\tb"}'      restic_password $'a\tb' 'value containing a tab'
expect '{"restic_password":"tR/9+Qk=","x":"y"}' restic_password 'tR/9+Qk=' 'base64 padding and slashes'
expect '{"x":"decoy","restic_password":"real"}' restic_password real 'later key wins over an earlier decoy'
expect '{"restic_password":"{\"nested\":\"json\"}"}' restic_password '{"nested":"json"}' \
       'value that is itself JSON'

echo
echo "== type handling =="
expect '{"restic_password":12345}'  restic_password "" 'numeric value is not treated as a password'
expect '{"restic_password":null}'   restic_password "" 'null value yields empty'
expect '{"restic_password":["a"]}'  restic_password "" 'array value yields empty'

echo
echo "== malformed input is an error, not a silent empty =="
expect_err '["not","an","object"]' restic_password 'a JSON array is rejected'
expect_err '"just a string"'       restic_password 'a bare JSON string is rejected'
expect_err '{not json at all'      restic_password 'malformed JSON is rejected'

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
