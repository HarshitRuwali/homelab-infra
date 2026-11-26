#!/usr/bin/env bash
# Behavioural tests for obsidian/setup.sh and the obsidian command, against a
# stub docker that logs its argv. No real docker, image build or network.
#
# Run from vault-viewer/:  ./tests/test_obsidian.sh      Exit 0 = all pass.
# shellcheck disable=SC2016  # single quotes are deliberate: the text is for the stub
# shellcheck disable=SC2034  # variables set here are read inside check's eval strings
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
ok() { printf '  PASS %s\n' "$*"; PASS=$((PASS+1)); }
no() { printf '  FAIL %s\n' "$*"; FAIL=$((FAIL+1)); }
check() { if eval "$2"; then ok "$1"; else no "$1"; fi; }

# The stub: logs every call; `exec` prints $STUB_OUT and exits $STUB_RC;
# `inspect` says running unless STUB_STOPPED is set.
STUB="$TMP/docker"
cat > "$STUB" <<'STUBSH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$LOG"
printf '%s\n' "$@" > "$LOG.argv"   # one argument per line, for the last call
case "$1" in
  exec) printf '%s' "${STUB_OUT:-}"; [ -z "${STUB_ERR:-}" ] || printf '%s\n' "$STUB_ERR" >&2; exit "${STUB_RC:-0}" ;;
  inspect) [ -n "${STUB_STOPPED:-}" ] && exit 1; echo true ;;
esac
exit 0
STUBSH
chmod +x "$STUB"
export OBSIDIAN_DOCKER="$STUB" LOG="$TMP/log"
VAULT="$TMP/vault"; mkdir -p "$VAULT"
BIN="$TMP/bin"
W="$HERE/obsidian/obsidian"

echo "obsidian (the command)"
: > "$LOG"
out="$(STUB_OUT=$'a.md\nb.md' "$W" search query="x y")"; rc=$?
check "passes the arguments through to the app's CLI as separate words" \
  '[ "$(cat "$LOG.argv")" = "$(printf "%s\n" exec -w /vault obsidian timeout 60 /opt/obsidian/obsidian-cli search "query=x y")" ]'
check "prints the output and exits 0" '[ "$rc" -eq 0 ] && [ "$out" = $'"'"'a.md\nb.md'"'"' ]'
err="$(STUB_OUT="Error: Base file not found: nope.base" "$W" base:query path=nope.base 2>&1 >/dev/null)"; rc=$?
check "an Error: reply exits 1 with the message on stderr" '[ "$rc" -eq 1 ] && [ "$err" = "Error: Base file not found: nope.base" ]'
out="$(STUB_OUT='[{"path":"a.md"}]' STUB_ERR="some warning" "$W" base:query path=a.base 2>/dev/null)"; rc=$?
check "a warning on stderr stays out of the output" '[ "$rc" -eq 0 ] && [ "$out" = "[{\"path\":\"a.md\"}]" ]'
STUB_OUT="Error: Base file not found: a.base" STUB_ERR="some warning" "$W" base:query path=a.base >/dev/null 2>&1; rc=$?
check "an Error: reply after a warning still exits 1" '[ "$rc" -eq 1 ]'
STUB_RC=124 "$W" search query=x >/dev/null 2>"$TMP/err"; rc=$?
check "a timeout inside the container exits 124 and says so" '[ "$rc" -eq 124 ] && grep -q "timed out after 60s" "$TMP/err"'
OBSIDIAN_TIMEOUT=5 "$W" version >/dev/null 2>&1
check "OBSIDIAN_TIMEOUT sets the limit inside the container" 'grep -q " timeout 5 /opt/obsidian/obsidian-cli version" "$LOG"'
OBSIDIAN_TIMEOUT=abc "$W" version >/dev/null 2>&1; rc=$?
check "a bad OBSIDIAN_TIMEOUT is refused" '[ "$rc" -eq 2 ]'
STUB_OUT="Command line interface is not enabled. Please turn it on" "$W" version >/dev/null 2>&1; rc=$?
check "CLI switched off exits 1" '[ "$rc" -eq 1 ]'
err="$(STUB_RC=1 STUB_STOPPED=1 "$W" version 2>&1)"; rc=$?
check "a stopped container says so and exits non-zero" '[ "$rc" -ne 0 ] && [[ "$err" == *"container is not running"* ]]'
: > "$LOG"
OBSIDIAN_CONTAINER=other "$W" version >/dev/null 2>&1
check "OBSIDIAN_CONTAINER picks the container" 'grep -q "^exec -w /vault other " "$LOG"'

echo "setup.sh"
: > "$LOG"
out="$("$HERE/obsidian/setup.sh" --dry-run --bin-dir "$BIN" "$VAULT")"; rc=$?
check "dry run exits 0" '[ "$rc" -eq 0 ]'
check "dry run changes nothing" '[ ! -s "$LOG" ] && [ ! -e "$VAULT/.obsidian" ] && [ ! -e "$BIN/obsidian" ]'
check "dry run shows the docker run" '[[ "$out" == *"would run"*" run -d --name obsidian "* ]]'

: > "$LOG"
"$HERE/obsidian/setup.sh" --bin-dir "$BIN" --memory 2g "$VAULT" >"$TMP/out" 2>&1; rc=$?
run_line="$(grep '^run ' "$LOG")"
check "setup exits 0 once the app answers" '[ "$rc" -eq 0 ]'
check "builds the image tagged with the pinned version" \
  'grep -q "^build -q -t obsidian-cli:$(sed -n "s/^ARG OBSIDIAN_VERSION=//p" "$HERE/obsidian/Dockerfile") " "$LOG"'
for flag in "--network none" "--read-only" "--cap-drop ALL" "--security-opt no-new-privileges" \
            "--user $(id -u):$(id -g)" "--memory 2g" "--restart unless-stopped" "--init" \
            "--mount type=bind,src=$VAULT,dst=/vault " "-v obsidian-vault-config:/vault/.obsidian" "-v obsidian-home:/home/obsidian"; do
  check "container runs with $flag" '[[ "$run_line " == *"$flag"* ]]'
done
check "the vault's .obsidian mount point is created as the user" '[ -d "$VAULT/.obsidian" ] && [ "$(stat -c %u "$VAULT/.obsidian")" = "$(id -u)" ]'
check "installs the command" '[ -x "$BIN/obsidian" ] && cmp -s "$BIN/obsidian" "$W"'
check "prints the install.sh flags to use next" 'grep -qF -- "--obsidian-cli $(printf %q "$BIN/obsidian") --obsidian-vault $(printf %q "$VAULT")" "$TMP/out"'
SPACED="$TMP/my vault"; mkdir -p "$SPACED"; : > "$LOG"
"$HERE/obsidian/setup.sh" --bin-dir "$BIN" "$SPACED" >"$TMP/out2" 2>&1; rc=$?
check "a vault path with a space is one mount source" '[ "$rc" -eq 0 ] && grep -q "src=$SPACED,dst=/vault" "$LOG"'
check "the printed next step quotes a path with a space" 'grep -qF -- "--obsidian-vault $(printf %q "$SPACED")" "$TMP/out2"'
COMMA="$TMP/a,b"; mkdir -p "$COMMA"
"$HERE/obsidian/setup.sh" --bin-dir "$BIN" "$COMMA" >/dev/null 2>&1; rc=$?
check "a vault path with a comma is refused" '[ "$rc" -eq 1 ]'

"$HERE/obsidian/setup.sh" "$TMP/missing" >/dev/null 2>&1; rc=$?
check "a missing vault fails" '[ "$rc" -ne 0 ]'
"$HERE/obsidian/setup.sh" --name 'bad name' "$VAULT" >/dev/null 2>&1; rc=$?
check "a bad container name is refused" '[ "$rc" -eq 2 ]'

: > "$LOG"
"$HERE/obsidian/setup.sh" --remove --bin-dir "$BIN" >/dev/null; rc=$?
check "--remove removes the container and the command" '[ "$rc" -eq 0 ] && grep -qx "rm -f obsidian" "$LOG" && [ ! -e "$BIN/obsidian" ]'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
