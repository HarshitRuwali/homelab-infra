#!/usr/bin/env bash
# Behavioural tests for deploy.sh. A stub `ssh` runs the remote side locally,
# in a temporary HOME with stub docker, systemctl and curl.
#
# Run from vault-viewer/:  ./tests/test_deploy.sh
# Exit 0 = all pass. Nothing touches a real host, systemd, docker or network.
# shellcheck disable=SC2016,SC2329  # quotes are for stubs; helpers are called through check
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$HERE/deploy.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0
REV="$(python3 "$HERE/artifacts.py" --pdf-rev)"

check() { # check <label> <command...>
  local label="$1"; shift
  if "$@"; then echo "  [ ok ] $label"; else echo "  [FAIL] $label"; FAILS=$((FAILS + 1)); fi
}
has() { grep -qF -- "$2" <<<"$1"; }
lacks() { ! grep -qF -- "$2" <<<"$1"; }
logged() { grep -qF -- "$1" "$LOG"; }
not_logged() { ! grep -qF -- "$1" "$LOG"; }
# before <first> <second>: the first log line containing <first> comes before the one with <second>
before() {
  local a b
  a="$(grep -nF -- "$1" "$LOG" | head -n 1 | cut -d: -f1)"
  b="$(grep -nF -- "$2" "$LOG" | head -n 1 | cut -d: -f1)"
  [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]
}

# --- stubs -----------------------------------------------------------------------
# Images are files in $IMAGES (local) or $RHOME/.images (remote): name -> revision.
STUBS="$TMP/stubs"
mkdir -p "$STUBS"
cat > "$STUBS/docker" <<'EOF'
#!/usr/bin/env bash
side="${REMOTE:+remote}"; side="${side:-local}"
echo "$side docker $*" >> "$LOG"
store="$IMAGES"; [ -n "${REMOTE:-}" ] && store="$HOME/.images"
mkdir -p "$store"
f() { printf '%s/%s' "$store" "${1//[\/:]/_}"; }
case "$1" in
  info) [ -z "${STUB_NO_DOCKER:-}" ] ;;
  image)
    case "$2" in
      inspect) img="${*: -1}"; [ -f "$(f "$img")" ] || exit 1
               case "$*" in *-f*) r="$(cat "$(f "$img")")"; echo "${r:-<no value>}" ;; esac ;;
      rm) rm -f "$(f "$3")" ;;
    esac ;;
  tag) [ -f "$(f "$2")" ] && cp "$(f "$2")" "$(f "$3")" ;;
  save) echo "image-bytes" ;;
  load) cat >/dev/null; [ -z "${STUB_LOAD_FAIL:-}" ] || exit 1
        echo "${STUB_LOAD_REV:-$PROGRAM_REV}" > "$(f artifacts-pdf:local)"; echo "Loaded image: artifacts-pdf:local" ;;
  build) df="$(cat)"; [ -z "${STUB_BUILD_FAIL:-}" ] || exit 1
         img="$(sed -n 's/.*-t \([^ ]*\).*/\1/p' <<<"$*")"
         sed -n 's/^LABEL vault-viewer.pdf-rev=//p' <<<"$df" > "$(f "$img")" ;;
  run) cat >/dev/null; [ -z "${STUB_RUN_FAIL:-}" ] || exit 1; printf '%%PDF-1.7 stub' ;;
esac
EOF
cat > "$STUBS/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "remote systemctl $*" >> "$LOG"
case "$*" in *is-active*) exit "${STUB_ACTIVE_RC:-0}" ;; esac
exit 0
EOF
cat > "$STUBS/curl" <<'EOF'
#!/usr/bin/env bash
echo "${STUB_CURL_CODE:-403}"
EOF
printf '#!/bin/sh\nexit 0\n' > "$STUBS/sleep"
printf '#!/bin/sh\nexit 0\n' > "$STUBS/tailscale"
# ssh: skip options, take the host, run the command locally as "the host".
cat > "$STUBS/ssh" <<'EOF'
#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do
  case "$1" in -o|-i) shift 2 ;; -*) shift ;; *) break ;; esac
done
host="$1"; shift
echo "ssh $host" >> "$LOG"
[ "$host" != unreachable ] || exit 255
mkdir -p "$TMP/rtmp"
REMOTE=1 HOME="$RHOME" TMPDIR="$TMP/rtmp" XDG_CONFIG_HOME="" exec bash -c "$*"
EOF
chmod +x "$STUBS"/*

LOG="$TMP/log"
IMAGES="$TMP/images"
RHOME="$TMP/rhome"
UNIT="$RHOME/.config/systemd/user/artifacts.service"
BINP="$RHOME/.local/bin/artifacts"
ROOT="$TMP/vault"
mkdir -p "$ROOT"

# A host that already runs the viewer: an old program and a unit for it.
installed_host() {
  rm -rf "$RHOME" "$IMAGES" "$TMP/rtmp"; mkdir -p "$RHOME/.local/bin" "$(dirname "$UNIT")" "$RHOME/.images" "$IMAGES"
  echo "old program" > "$BINP"
  printf '[Service]\nExecStart=/usr/bin/python3 %s --bind 10.1.2.3 --port 7777 %s\n' "$BINP" "$ROOT" > "$UNIT"
}
fresh_host() { rm -rf "$RHOME" "$IMAGES" "$TMP/rtmp"; mkdir -p "$RHOME" "$IMAGES"; }
remote_image() { echo "$2" > "$RHOME/.images/${1//[\/:]/_}"; }
remote_rev() { cat "$RHOME/.images/${1//[\/:]/_}" 2>/dev/null; }
local_image() { echo "$1" > "$IMAGES/artifacts-pdf_local"; }

# run [VAR=value...] -- args...: sets OUT and RC.
run() {
  local envs=()
  while [ "$1" != -- ]; do envs+=("$1"); shift; done
  shift
  : > "$LOG"
  OUT="$(env "${envs[@]+"${envs[@]}"}" PATH="$STUBS:$PATH" LOG="$LOG" TMP="$TMP" IMAGES="$IMAGES" RHOME="$RHOME" \
    PROGRAM_REV="$REV" bash "$DEPLOY" "$@" 2>&1)"
  RC=$?
}
no_temp_dirs() { ! find "$TMP/rtmp" -maxdepth 1 -name 'vault-viewer.*' 2>/dev/null | grep -q .; }

echo "help and argument errors"
run -- --help
check "--help exits 0" test "$RC" -eq 0
for word in --image auto ship build skip -i --dry-run INSTALL_OPTIONS; do
  check "--help mentions $word" has "$OUT" "$word"
done
run -- ; check "no HOST exits 2" test "$RC" -eq 2
run -- --image fast box; check "bad --image exits 2" test "$RC" -eq 2
run -- -i "$TMP/no-such-key" box; check "unreadable -i exits 2" test "$RC" -eq 2
run -- --bogus box; check "unknown option exits 2" test "$RC" -eq 2
run -- a b -- --bind 10.1.2.3 "$ROOT"
check "INSTALL_OPTIONS with two hosts exits 2" test "$RC" -eq 2
check "argument errors contact no host" not_logged "ssh "

echo "dry run"
installed_host; local_image "$REV"
run -- --dry-run box
check "--dry-run exits 0" test "$RC" -eq 0
check "--dry-run plans an update" has "$OUT" "viewer: update"
check "--dry-run plans to copy the image" has "$OUT" "image:  copy artifacts-pdf:local"
check "--dry-run leaves the program" test "$(cat "$BINP")" = "old program"
check "--dry-run loads nothing" not_logged "docker load"
check "--dry-run restarts nothing" not_logged "systemctl --user restart"

echo "update, image copied from here"
installed_host; local_image "$REV"; remote_image artifacts-pdf:local "oldrev"
run -- box
check "update exits 0" test "$RC" -eq 0
check "image loaded on the host" logged "remote docker load"
check "image loaded before the restart" before "remote docker load" "systemctl --user restart"
check "host image now has the program's revision" test "$(remote_rev artifacts-pdf:local)" = "$REV"
check "program replaced" cmp -s "$HERE/artifacts.py" "$BINP"
check "previous program kept as .prev" test "$(cat "$BINP.prev")" = "old program"
check "service restarted" logged "systemctl --user restart artifacts.service"
check "check printed a test PDF" has "$OUT" "test page printed to PDF"
check "old image tag removed after the check" test ! -e "$RHOME/.images/artifacts-pdf_local-prev"
check "no temporary directory left" no_temp_dirs
check "summary names the host as ok" has "$OUT" "ok: box"

echo "re-run: nothing to do"
run -- box
check "re-run exits 0" test "$RC" -eq 0
check "re-run: image is current" has "$OUT" "is current"
check "re-run: no image copied" not_logged "docker load"
check "re-run: program unchanged" has "$OUT" "program unchanged"
check "re-run: no restart" not_logged "systemctl --user restart"
check "re-run keeps the real .prev" test "$(cat "$BINP.prev")" = "old program"

echo "auto builds on the host when this machine's image does not match"
installed_host; local_image "stale"; remote_image artifacts-pdf:local "oldrev"
run -- box
check "build exits 0" test "$RC" -eq 0
check "image built on the host" logged "remote docker build"
check "nothing copied from here" not_logged "local docker save"
check "built before the program restart" before "remote docker build" "systemctl --user restart"
check "built image has the program's revision" test "$(remote_rev artifacts-pdf:local)" = "$REV"

echo "failed image build changes nothing"
installed_host; remote_image artifacts-pdf:local "oldrev"
run STUB_BUILD_FAIL=1 -- --image build box
check "failed build exits 1" test "$RC" -eq 1
check "failed build says nothing changed" has "$OUT" "nothing on box was changed"
check "failed build leaves the program" test "$(cat "$BINP")" = "old program"
check "failed build leaves the image" test "$(remote_rev artifacts-pdf:local)" = "oldrev"
check "failed build drops the -prev tag" test ! -e "$RHOME/.images/artifacts-pdf_local-prev"
check "failed build restarts nothing" not_logged "systemctl --user restart"
check "failed build leaves no temporary directory" no_temp_dirs

echo "--image ship needs a matching image here"
installed_host; local_image "stale"
run -- --image ship box
check "ship with a stale local image exits 1" test "$RC" -eq 1
check "ship says how to build it here" has "$OUT" "--build-pdf-image"
check "ship with a stale local image changes nothing" test "$(cat "$BINP")" = "old program"

echo "failed check rolls back program and image"
for fail in STUB_ACTIVE_RC=3 STUB_RUN_FAIL=1 STUB_CURL_CODE=000; do
  installed_host; local_image "$REV"; remote_image artifacts-pdf:local "oldrev"
  run "$fail" -- box
  check "$fail: exits 1" test "$RC" -eq 1
  check "$fail: says it rolls back" has "$OUT" "rolling back the program and image"
  check "$fail: program restored" test "$(cat "$BINP")" = "old program"
  check "$fail: image restored" test "$(remote_rev artifacts-pdf:local)" = "oldrev"
  check "$fail: summary names the host as failed" has "$OUT" "failed: box"
done

echo "--image skip"
installed_host; remote_image artifacts-pdf:local "oldrev"
run -- --image skip box
check "skip exits 0" test "$RC" -eq 0
check "skip touches no image" not_logged "docker load"
check "skip builds nothing" not_logged "docker build"
check "skip prints no test PDF" lacks "$OUT" "test page printed"

echo "docker missing on the host"
installed_host; local_image "$REV"
run STUB_NO_DOCKER=1 -- box
check "no docker exits 1" test "$RC" -eq 1
check "no docker is named" has "$OUT" "docker is missing"
check "no docker changes nothing" test "$(cat "$BINP")" = "old program"

echo "a host without the viewer"
fresh_host; local_image "$REV"
run -- box
check "not installed, no options: exits 1" test "$RC" -eq 1
check "not installed: says to pass install options" has "$OUT" "pass install.sh options after --"
run STUB_CURL_CODE=200 -- box -- --bind 10.1.2.3 "$ROOT"
check "install exits 0" test "$RC" -eq 0
check "install.sh wrote the unit" test -f "$UNIT"
check "install.sh installed the program" cmp -s "$HERE/artifacts.py" "$BINP"
check "install copied the image first" before "remote docker load" "systemctl --user daemon-reload"
check "install output is shown" has "$OUT" "| installed $BINP"

echo "several hosts"
installed_host; local_image "$REV"; remote_image artifacts-pdf:local "$REV"
cp "$HERE/artifacts.py" "$BINP"
run -- box unreachable
check "one unreachable host exits 1" test "$RC" -eq 1
check "the reachable host is ok" has "$OUT" "ok: box"
check "the unreachable host is named" has "$OUT" "cannot reach unreachable"
check "summary lists the failed host" has "$OUT" "failed: unreachable"

echo
if [ "$FAILS" -eq 0 ]; then echo "all checks passed"; exit 0; fi
echo "$FAILS check(s) failed"; exit 1
