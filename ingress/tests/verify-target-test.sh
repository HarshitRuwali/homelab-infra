#!/usr/bin/env bash
#
# verify-target-test.sh - check which site install.sh curls after an apply.
# A tailnet site refuses a client that is not on the tailnet, so the check
# must use the first lan site from 127.0.0.1, and only when there is none the
# first tailnet site from the tailnet address.
#
#   ./tests/verify-target-test.sh
#
# Needs no docker and touches nothing outside a temporary directory.
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0
ok() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

TS=100.64.0.50
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

# target <label> <expected stdout> <sites text>
target() {
  local label="$1" want="$2" got
  printf '%s' "$3" >"$tmp/s.tsv"
  got="$(./verify-target.sh "$tmp/s.tsv" "$TS" 2>&1)"; rc=$?
  if (( rc == 0 )) && [[ "$got" == "$want" ]]; then ok "$label"
  else no "$label: wanted '$want', got rc=$rc '$got'"; fi
}

echo "== the first lan site is checked from 127.0.0.1 =="
target "lan site first" "grafana 127.0.0.1" $'grafana lan http://192.0.2.29:80 -\nqb tailnet http://100.64.0.10:8081 -\n'
target "a tailnet site first is skipped" "grafana 127.0.0.1" $'qb tailnet http://100.64.0.10:8081 -\nlm tailnet http://100.64.0.22:8080 -\ngrafana lan http://192.0.2.29:80 -\nplex lan http://100.64.0.10:32400 -\n'
target "comments and blank lines are skipped" "grafana 127.0.0.1" $'# sites\n\n  # qb tailnet http://100.64.0.10:8081 -\nqb tailnet http://100.64.0.10:8081 - # note\ngrafana\tlan\thttp://192.0.2.29:80\t-\n'
target "CRLF line endings" "grafana 127.0.0.1" $'qb tailnet http://100.64.0.10:8081 -\r\ngrafana lan http://192.0.2.29:80 -\r\n'

echo
echo "== only tailnet sites: the first one, from the tailnet address =="
target "first tailnet site" "qb $TS" $'qb tailnet http://100.64.0.10:8081 -\nlm tailnet http://100.64.0.22:8080 -\n'
target "a commented lan site does not count" "qb $TS" $'# grafana lan http://192.0.2.29:80 -\nqb tailnet http://100.64.0.10:8081 -\n'

echo
echo "== no site =="
for text in '' $'# nothing\n\n'; do
  printf '%s' "$text" >"$tmp/s.tsv"
  out="$(./verify-target.sh "$tmp/s.tsv" "$TS" 2>&1)"; rc=$?
  if (( rc == 1 )) && grep -qF 'no site' <<<"$out"; then ok "an empty sites file exits 1 and says so"
  else no "an empty sites file exited $rc: $out"; fi
done
./verify-target.sh >/dev/null 2>&1; rc=$?
if (( rc == 2 )); then ok "no arguments exits 2"; else no "no arguments exited $rc, wanted 2"; fi

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
