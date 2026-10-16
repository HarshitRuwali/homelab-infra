#!/usr/bin/env bash
# Behavioural tests for install.sh, run against stubs in a temporary HOME.
#
# Run from vault-viewer/:  ./tests/test_install.sh
# Exit 0 = all pass. Nothing touches the real systemd, docker or network.
# shellcheck disable=SC2016  # single quotes are deliberate: the text is for stubs and bash -c
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL="$HERE/install.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0

check() { # check <label> <command...>
  local label="$1"; shift
  if "$@"; then echo "  [ ok ] $label"; else echo "  [FAIL] $label"; FAILS=$((FAILS + 1)); fi
}
has() { grep -qF -- "$2" <<<"$1"; }          # has <text> <fixed string>
lacks() { ! grep -qF -- "$2" <<<"$1"; }
logged() { grep -qF -- "$1" "$LOG"; }
not_logged() { ! grep -qF -- "$1" "$LOG"; }

# --- stubs: every one logs "name argv" to $LOG ---------------------------------
STUBS="$TMP/stubs"
mkdir -p "$STUBS"
mkstub() { # mkstub <name> <body>
  printf '#!/usr/bin/env bash\necho "%s $*" >> "$LOG"\n%s\n' "$1" "$2" > "$STUBS/$1"
  chmod +x "$STUBS/$1"
}
# STUB_ACTIVE_RC controls `systemctl --user is-active`; STUB_CURL_CODE the HTTP code;
# STUB_DOCKER_BUILD_RC the exit status of `docker build`.
mkstub systemctl 'case "$*" in *is-active*) exit "${STUB_ACTIVE_RC:-0}" ;; esac; exit 0'
mkstub tailscale 'exit 0'
mkstub sleep 'exit 0'
mkstub curl 'echo "${STUB_CURL_CODE:-200}"'
mkstub docker 'case "$1 ${2:-}" in "image inspect") exit 1 ;; build*) cat >/dev/null; exit "${STUB_DOCKER_BUILD_RC:-0}" ;; esac; exit 0'

# A PATH with the basics but no docker or tailscale, for the "missing tool" cases.
MINBIN="$TMP/minbin"
mkdir -p "$MINBIN"
for t in realpath install seq mkdir dirname cat tail printf date; do
  [ -e "$(command -v "$t")" ] && ln -sf "$(command -v "$t")" "$MINBIN/$t"
done
for t in systemctl curl sleep; do ln -sf "$STUBS/$t" "$MINBIN/$t"; done

# run <home> <path> args... : sets OUT and RC; LOG is reset per call.
run() {
  local home="$1" path="$2"; shift 2
  : > "$LOG"
  OUT="$(HOME="$home" XDG_CONFIG_HOME="" PATH="$path" LOG="$LOG" /usr/bin/bash "$INSTALL" "$@" 2>&1)"
  RC=$?
}
LOG="$TMP/log"
fresh_home() { rm -rf "${TMP:?}/home"; mkdir -p "$TMP/home"; HOME_DIR="$TMP/home"; }

ROOT="$TMP/vault dir %x"
ROOT2="$TMP/other"
mkdir -p "$ROOT" "$ROOT2"
BINP="$TMP/home/.local/bin/artifacts"
UNITP="$TMP/home/.config/systemd/user/artifacts.service"
WITH_STUBS="$STUBS:$PATH"

echo "help and argument errors"
fresh_home
run "$HOME_DIR" "$WITH_STUBS" --help
check "--help exits 0" test "$RC" -eq 0
check "--help describes --pdf-base" has "$OUT" "--pdf-base IMAGE  "
for flag in --bind --port --allow-host --tailnet-user --title --build-pdf-image --pdf-base --dry-run -h --help; do
  check "--help lists $flag" has "$OUT" "$flag"
done
run "$HOME_DIR" "$WITH_STUBS" "$ROOT"
check "missing --bind exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3
check "missing ROOT exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --bogus "$ROOT"
check "unknown option exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --port 99999 "$ROOT"
check "bad port exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --bind
check "option without a value exits 2" test "$RC" -eq 2
for bad in 0.0.0.0 999.1.1.1 010.1.1.1 224.0.0.1 239.255.255.250 1.2.3 myhost.example.ts.net; do
  run "$HOME_DIR" "$WITH_STUBS" --bind "$bad" "$ROOT"
  check "--bind $bad exits 2" test "$RC" -eq 2
  check "--bind $bad names the rule" has "$OUT" "--bind must be a unicast IPv4 address"
done
check "rejected --bind writes nothing" test ! -e "$UNITP"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --title $'a\nb' "$ROOT"
check "newline in --title exits 2" test "$RC" -eq 2
check "newline in --title names control characters" has "$OUT" "control character"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --build-pdf-image --pdf-base $'a\nb' "$ROOT"
check "newline in --pdf-base exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --build-pdf-image --pdf-base "" "$ROOT"
check "empty --pdf-base exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --pdf-base mcr.example/playwright:v1.56.0-noble "$ROOT"
check "--pdf-base without --build-pdf-image exits 2" test "$RC" -eq 2
check "--pdf-base without --build-pdf-image names the rule" has "$OUT" "--pdf-base only applies together with --build-pdf-image"
check "--pdf-base without --build-pdf-image writes nothing" test ! -e "$UNITP"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --allow-host $'a\rb' "$ROOT"
check "CR in --allow-host exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --tailnet-user $'a\tb' "$ROOT"
check "tab in --tailnet-user exits 2" test "$RC" -eq 2
mkdir -p "$TMP/nl"$'\n'"dir"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$TMP/nl"$'\n'"dir"
check "newline in ROOT exits 2" test "$RC" -eq 2
check "control characters write nothing" test ! -e "$UNITP"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$TMP/does-not-exist"
check "nonexistent ROOT fails" test "$RC" -ne 0
check "nonexistent ROOT writes nothing" test ! -e "$UNITP"
run "$HOME_DIR" "$MINBIN" --bind 10.1.2.3 --tailnet-user me@github "$ROOT"
check "--tailnet-user without tailscale fails" test "$RC" -eq 1
check "--tailnet-user failure names tailscale" has "$OUT" "tailscale"
run "$HOME_DIR" "$MINBIN" --bind 10.1.2.3 --build-pdf-image "$ROOT"
check "--build-pdf-image without docker fails" test "$RC" -eq 1
check "--build-pdf-image failure names docker" has "$OUT" "docker"
check "preflight failures write nothing" test ! -e "$BINP"

echo "dry run"
fresh_home
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 --tailnet-user me@github "$ROOT"
check "--dry-run exits 0" test "$RC" -eq 0
check "--dry-run writes no program" test ! -e "$BINP"
check "--dry-run writes no unit" test ! -e "$UNITP"
check "--dry-run creates no config dir" test ! -e "$TMP/home/.config"
check "--dry-run calls no systemctl" not_logged "systemctl"
check "--dry-run calls no docker" not_logged "docker"
check "--dry-run prints the unit" has "$OUT" "ExecStart=/usr/bin/python3 $BINP --bind 10.1.2.3 --port 7777 --tailnet-user me@github"
run "$HOME_DIR" "$WITH_STUBS" --dry-run --build-pdf-image --bind 10.1.2.3 "$ROOT"
check "--dry-run with --build-pdf-image builds nothing" not_logged "docker"
run "$HOME_DIR" "$WITH_STUBS" --dry-run --build-pdf-image --pdf-base mcr.example/playwright:v1.56.0-noble --bind 10.1.2.3 "$ROOT"
check "--dry-run with --pdf-base exits 0" test "$RC" -eq 0
check "--dry-run with --pdf-base builds nothing" not_logged "docker"

echo "real run"
fresh_home
STUB_CURL_CODE=403 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --port 7788 --allow-host box --allow-host box.example.ts.net \
  --tailnet-user Me@github --title "my vault" "$ROOT" "$ROOT2"
check "install exits 0" test "$RC" -eq 0
check "program installed identical to artifacts.py" cmp -s "$HERE/artifacts.py" "$BINP"
check "program is mode 0755" test "$(stat -c %a "$BINP")" = 755
check "unit written" test -f "$UNITP"
EXEC="$(grep '^ExecStart=' "$UNITP")"
WANT="ExecStart=/usr/bin/python3 $BINP --bind 10.1.2.3 --port 7788 --allow-host box --allow-host box.example.ts.net"
WANT+=" --tailnet-user Me@github --title \"my vault\" \"$TMP/vault dir %%x\" $TMP/other"
check "ExecStart is exactly the expected line" test "$EXEC" = "$WANT"
check "unit has no unreplaced placeholder" bash -c '! grep -q "__[A-Z]*__" "$1"' _ "$UNITP"
for line in "Restart=always" "RestartSec=5" "Nice=10" "After=network-online.target" "WantedBy=default.target"; do
  check "unit keeps $line" grep -qx "$line" "$UNITP"
done
check "unit template has no host-specific value (no IPv4 address)" bash -c '! grep -qE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" "$1"' _ "$HERE/systemd/artifacts.service"
check "systemctl daemon-reload" logged "systemctl --user daemon-reload"
check "systemctl enable" logged "systemctl --user enable artifacts.service"
check "systemctl restart" logged "systemctl --user restart artifacts.service"
check "curl checked with the Host header" logged "Host: 10.1.2.3:7788"
check "curl checked the bind URL" logged "http://10.1.2.3:7788/"
check "no docker build without the flag" not_logged "docker build"
check "output has the URL" has "$OUT" "http://10.1.2.3:7788"
check "output has the logs command" has "$OUT" "journalctl --user -u artifacts.service -f"
check "output has the PDF image build hint" has "$OUT" "--build-pdf-image"
check "output names the missing PDF image" has "$OUT" "artifacts-pdf:local"
check "output explains the 403" has "$OUT" "HTTP 403"
check "docker was asked about the image" logged "docker image inspect artifacts-pdf:local"

echo "idempotence"
cp "$UNITP" "$TMP/unit.first"
cp "$BINP" "$TMP/bin.first"
STUB_CURL_CODE=403 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --port 7788 --allow-host box --allow-host box.example.ts.net \
  --tailnet-user Me@github --title "my vault" "$ROOT" "$ROOT2"
check "second run exits 0" test "$RC" -eq 0
check "second run leaves the same unit bytes" cmp -s "$UNITP" "$TMP/unit.first"
check "second run leaves the same program bytes" cmp -s "$BINP" "$TMP/bin.first"
check "second run restarts the service" logged "systemctl --user restart artifacts.service"
check "second run of the same unit makes no .bak" test ! -e "$UNITP.bak"

echo "quoting"
fresh_home
QROOT="$TMP/q\"uote\\back"
mkdir -p "$QROOT"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --title 'a "b" \c %d $e' "$QROOT"
EXEC="$(grep '^ExecStart=' "$UNITP")"
check "quote, backslash, % and \$ are escaped in a title" has "$EXEC" '--title "a \"b\" \\c %%d $$e"'
check "quote and backslash are escaped in a root" has "$EXEC" "\"$TMP/q\\\"uote\\\\back\""

echo "options and failure paths"
fresh_home
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --build-pdf-image "$ROOT"
check "--build-pdf-image exits 0" test "$RC" -eq 0
check "--build-pdf-image runs docker build for the image" logged "docker build --pull -t artifacts-pdf:local --build-arg BASE=mcr.microsoft.com/playwright/python:v1.63.0-noble --build-arg PLAYWRIGHT=1.63.0 -"
check "--build-pdf-image says the image is built" has "$OUT" "built: artifacts-pdf:local"
fresh_home
NODE_BASE="mcr.microsoft.com/playwright:v1.56.0-noble"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --build-pdf-image --pdf-base "$NODE_BASE" "$ROOT"
check "--pdf-base build exits 0" test "$RC" -eq 0
check "--pdf-base build passes the base and its version" logged "docker build -t artifacts-pdf:local --build-arg BASE=$NODE_BASE --build-arg PLAYWRIGHT=1.56.0 -"
check "--pdf-base build does not pull" not_logged "--pull"
fresh_home
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --build-pdf-image --pdf-base mcr.example/playwright:latest "$ROOT"
check "--pdf-base without a version tag exits 1" test "$RC" -eq 1
check "--pdf-base without a version tag says why" has "$OUT" "tag must carry the Playwright version"
check "--pdf-base without a version tag runs no docker build" not_logged "docker build"
check "--pdf-base without a version tag installs nothing" test ! -e "$UNITP"
fresh_home
STUB_DOCKER_BUILD_RC=1 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --build-pdf-image --pdf-base "$NODE_BASE" "$ROOT"
check "failed --pdf-base build repeats --pdf-base in the retry hint" has "$OUT" "artifacts.py --build-pdf-image --pdf-base $NODE_BASE"
fresh_home
STUB_CURL_CODE=200 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "HTTP 200 counts as up" test "$RC" -eq 0
check "HTTP 200 is reported" has "$OUT" "HTTP 200"
check "no tailscale call without --tailnet-user" not_logged "tailscale"
check "default port is 7777" has "$OUT" "http://10.1.2.3:7777"
check "no title or tailnet flag by default" lacks "$(grep '^ExecStart=' "$UNITP")" "--tailnet-user"
fresh_home
STUB_CURL_CODE=000 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "no answer exits 1" test "$RC" -eq 1
check "no answer says FAILED" has "$OUT" "FAILED"
check "no answer shows systemctl status" logged "systemctl --user status artifacts.service"

echo "verification rule"
fresh_home
STUB_CURL_CODE=500 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "HTTP 500 exits 1" test "$RC" -eq 1
check "HTTP 500 says FAILED" has "$OUT" "FAILED"
check "HTTP 500 disables the unit" logged "systemctl --user disable --now artifacts.service"
fresh_home
STUB_CURL_CODE=403 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "403 without --tailnet-user exits 1" test "$RC" -eq 1
check "403 without --tailnet-user blames the Host check" has "$OUT" "Host header mismatch"
check "403 without --tailnet-user disables the unit" logged "systemctl --user disable --now artifacts.service"
fresh_home
STUB_CURL_CODE=403 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --tailnet-user me@github "$ROOT"
check "403 with --tailnet-user exits 0" test "$RC" -eq 0
check "403 with --tailnet-user explains the tagged host" has "$OUT" "tagged"
check "403 with --tailnet-user keeps the unit enabled" not_logged "disable --now"
fresh_home
STUB_ACTIVE_RC=3 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "inactive unit exits 1 even with HTTP 200" test "$RC" -eq 1
check "inactive unit says not active" has "$OUT" "not active"
check "inactive unit is disabled" logged "systemctl --user disable --now artifacts.service"
check "success asks systemctl is-active" logged "systemctl --user is-active --quiet artifacts.service"
fresh_home
STUB_CURL_CODE=000 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "no answer disables the unit" logged "systemctl --user disable --now artifacts.service"

echo "existing unit and --build-pdf-image ordering"
fresh_home
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "first install makes no .bak" test ! -e "$UNITP.bak"
cp "$UNITP" "$TMP/unit.old"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.4 "$ROOT"
check "changed options exit 0" test "$RC" -eq 0
check "changed unit keeps the old one as .bak" cmp -s "$UNITP.bak" "$TMP/unit.old"
check "the new unit replaced the old one" has "$(<"$UNITP")" "--bind 10.1.2.4"
check "the .bak is announced" has "$OUT" "artifacts.service.bak"
fresh_home
STUB_DOCKER_BUILD_RC=1 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --build-pdf-image "$ROOT"
check "failing image build exits 1" test "$RC" -eq 1
check "failing image build says so" has "$OUT" "building artifacts-pdf:local failed"
check "failing image build writes no unit" test ! -e "$UNITP"
check "failing image build installs no program" test ! -e "$BINP"
check "failing image build calls no systemctl" not_logged "systemctl"

echo
if [ "$FAILS" -ne 0 ]; then echo "$FAILS check(s) failed"; exit 1; fi
echo "all checks passed"
