#!/usr/bin/env bash
# Behavioural tests for deploy.sh. A stub `ssh` runs the remote side locally,
# in a temporary HOME with stub systemctl, curl, sudo and apparmor_parser, and
# deploy.sh runs from a copy next to a stub artifacts.py.
#
# Run from vault-viewer/:  ./tests/test_deploy.sh
# Exit 0 = all pass. Nothing touches a real host, systemd, sudo, AppArmor or network.
# shellcheck disable=SC2016,SC2329  # quotes are for stubs; helpers are called through check
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0

# The program under test is a copy of deploy.sh, install.sh and the unit
# template, beside a stub artifacts.py that implements the flags they call.
FAKE="$TMP/fake"
mkdir -p "$FAKE/systemd"
cp "$HERE/deploy.sh" "$FAKE/deploy.sh"
cp "$HERE/install.sh" "$FAKE/install.sh"
cp "$HERE/systemd/artifacts.service" "$FAKE/systemd/artifacts.service"
cat > "$FAKE/artifacts.py" <<'STUBPY'
import os, sys
REV = "abc123def456"
a = sys.argv[1:]
side = "remote" if os.environ.get("REMOTE") else "local"
if os.environ.get("LOG"):
    with open(os.environ["LOG"], "a") as f:
        f.write(side + " artifacts.py " + " ".join(a) + "\n")
def runtime_dir():
    if "--pdf-runtime" in a:
        return a[a.index("--pdf-runtime") + 1]
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return data + "/artifacts/pdf-runtime"
if "--pdf-rev" in a:
    print(REV)
elif "--print-apparmor-profile" in a:
    print("# stub profile\nprofile stub {}\n" + runtime_dir() + "/*/chrome/chrome-headless-shell")
elif "--install-pdf-runtime" in a:
    import shutil
    d = runtime_dir()
    fail = os.environ.get("STUB_FETCH_FAIL", "")
    if fail:
        if fail == "late":
            # the real installer drops the other revisions; do that, then fail
            for e in os.listdir(d) if os.path.isdir(d) else []:
                shutil.rmtree(os.path.join(d, e), ignore_errors=True)
        print("download failed", file=sys.stderr)
        sys.exit(1)
    os.makedirs(d + "/" + REV, exist_ok=True)
    open(d + "/" + REV + "/REV", "w").write(REV)
    for e in os.listdir(d):
        if e != REV:
            shutil.rmtree(os.path.join(d, e), ignore_errors=True)
    print("installed")
elif "--check-pdf-runtime" in a:
    out = os.environ.get("STUB_CHECK_OUT", "")
    if not out:
        out = "ok" if os.path.exists(runtime_dir() + "/" + REV + "/REV") else "the PDF runtime is not installed"
    print(out)
    sys.exit(0 if out == "ok" else 1)
STUBPY
DEPLOY="$FAKE/deploy.sh"
REV="$(python3 "$FAKE/artifacts.py" --pdf-rev)"

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
STUBS="$TMP/stubs"
mkdir -p "$STUBS"
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
# sudo: the real one is never called. -n is accepted and ignored.
cat > "$STUBS/sudo" <<'EOF'
#!/usr/bin/env bash
echo "remote sudo $*" >> "$LOG"
[ -z "${STUB_SUDO_FAIL:-}" ] || { echo "sudo: a password is required" >&2; exit 1; }
[ "${1:-}" != -n ] || shift
exec "$@"
EOF
printf '#!/bin/sh\nexit 0\n' > "$STUBS/apparmor_parser"
# tar: log the arguments, then run the real one. STUB_TAR_DROP=<name> leaves that
# member out of a stream being created: a valid archive cut at a member boundary.
cat > "$STUBS/tar" <<'EOF'
#!/usr/bin/env bash
echo "tar $*" >> "$LOG"
if [ -n "${STUB_TAR_DROP:-}" ] && [ "$1" = -c ]; then
  exec /usr/bin/tar --exclude="$STUB_TAR_DROP" "$@"
fi
exec /usr/bin/tar "$@"
EOF
# gunzip: STUB_GUNZIP_FAIL makes it fail after passing all the data on (a cut stream
# whose tar still exits 0).
cat > "$STUBS/gunzip" <<'EOF'
#!/usr/bin/env bash
/usr/bin/gunzip "$@"
rc=$?
[ -z "${STUB_GUNZIP_FAIL:-}" ] || rc=1
exit "$rc"
EOF
# df: a fixed amount of free space when STUB_DF_MB is set.
cat > "$STUBS/df" <<'EOF'
#!/usr/bin/env bash
[ -n "${STUB_DF_MB:-}" ] || exec /usr/bin/df "$@"
printf 'Filesystem 1M-blocks Used Available Use%% Mounted on\nstub 1000 1 %s 1%% /\n' "$STUB_DF_MB"
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
REMOTE=1 HOME="$RHOME" TMPDIR="$TMP/rtmp" XDG_CONFIG_HOME="" XDG_DATA_HOME="" exec bash -c "$*"
EOF
chmod +x "$STUBS"/*

# The AppArmor seams, read by the remote scripts and by install.sh on the "host".
mkdir -p "$TMP/aa"
AA_RESTRICT="$TMP/aa/restrict"
AA_FILE="$TMP/aa/artifacts-chrome-headless-shell"
export ARTIFACTS_APPARMOR_RESTRICT_FILE="$AA_RESTRICT"
export ARTIFACTS_APPARMOR_PROFILE_FILE="$AA_FILE"
export ARTIFACTS_SUDO="$STUBS/sudo"
export ARTIFACTS_APPARMOR_PARSER="$STUBS/apparmor_parser"
aa_restricted() { echo 1 > "$AA_RESTRICT"; rm -f "$AA_FILE"; }
aa_open() { echo 0 > "$AA_RESTRICT"; rm -f "$AA_FILE"; }
aa_open
# stub_profile <runtime dir>: what the stub artifacts.py prints for that directory.
stub_profile() { printf '# stub profile\nprofile stub {}\n%s/*/chrome/chrome-headless-shell' "$1"; }

LOG="$TMP/log"
LOCAL_XDG="$TMP/xdg"
LOCAL_RT="$LOCAL_XDG/artifacts/pdf-runtime"
RHOME="$TMP/rhome"
RT="$RHOME/.local/share/artifacts/pdf-runtime"
UNIT="$RHOME/.config/systemd/user/artifacts.service"
BINP="$RHOME/.local/bin/artifacts"
ROOT="$TMP/vault"
mkdir -p "$ROOT"

reset() { rm -rf "$RHOME" "$LOCAL_XDG" "$TMP/rtmp"; mkdir -p "$RHOME"; aa_open; }
# A host that already runs the viewer: an old program and a unit for it.
installed_host() {
  reset; mkdir -p "$RHOME/.local/bin" "$(dirname "$UNIT")"
  echo "old program" > "$BINP"
  printf '[Service]\nExecStart=/usr/bin/python3 %s --bind 10.1.2.3 --port 7777 %s\n' "$BINP" "$ROOT" > "$UNIT"
}
fresh_host() { reset; }
# runtime_at <dir> <rev>: a runtime revision with a marker file in it.
runtime_at() { mkdir -p "$1/$2/chrome"; echo "$2" > "$1/$2/REV"; echo "bytes-$2" > "$1/$2/chrome/blob"; }
local_runtime() { runtime_at "$LOCAL_RT" "$REV"; }
remote_rev() { cat "$1/$REV/REV" 2>/dev/null; }

# run [VAR=value...] -- args...: sets OUT and RC.
run() {
  local envs=()
  while [ "$1" != -- ]; do envs+=("$1"); shift; done
  shift
  : > "$LOG"
  OUT="$(env "${envs[@]+"${envs[@]}"}" PATH="$STUBS:$PATH" LOG="$LOG" TMP="$TMP" RHOME="$RHOME" XDG_DATA_HOME="$LOCAL_XDG" \
    bash "$DEPLOY" "$@" 2>&1)"
  RC=$?
}
no_temp_dirs() { ! find "$TMP/rtmp" -maxdepth 1 -name 'vault-viewer.*' 2>/dev/null | grep -q .; }
no_leftovers() { # no_leftovers <runtime dir>: no stash, no temporary extraction directory
  [ ! -e "$1.keep" ] && ! find "$1" -maxdepth 1 -name '.tmp-*' 2>/dev/null | grep -q .
}

echo "help and argument errors"
run -- --help
check "--help exits 0" test "$RC" -eq 0
for word in --pdf auto ship fetch skip -i --dry-run INSTALL_OPTIONS sudo; do
  check "--help mentions $word" has "$OUT" "$word"
done
check "--help does not mention docker" lacks "$OUT" "docker"
check "--help no longer offers --image" lacks "$OUT" "--image"
run -- ; check "no HOST exits 2" test "$RC" -eq 2
run -- --pdf fast box; check "bad --pdf exits 2" test "$RC" -eq 2
run -- --pdf; check "--pdf without a value exits 2" test "$RC" -eq 2
run -- --pdf build box
check "--pdf build is a tombstone: exits 2" test "$RC" -eq 2
check "--pdf build names --pdf fetch" has "$OUT" "use --pdf fetch"
run -- --image auto box
check "--image is a tombstone: exits 2" test "$RC" -eq 2
check "--image names --pdf" has "$OUT" "use --pdf MODE"
run -- --image=skip box; check "--image=MODE is a tombstone too" test "$RC" -eq 2
run -- -i "$TMP/no-such-key" box; check "unreadable -i exits 2" test "$RC" -eq 2
run -- --bogus box; check "unknown option exits 2" test "$RC" -eq 2
run -- a b -- --bind 10.1.2.3 "$ROOT"
check "INSTALL_OPTIONS with two hosts exits 2" test "$RC" -eq 2
check "argument errors contact no host" not_logged "ssh "

echo "dry run"
installed_host; local_runtime; aa_restricted
run -- --dry-run box
check "--dry-run exits 0" test "$RC" -eq 0
check "--dry-run plans an update" has "$OUT" "viewer: update"
check "--dry-run plans to copy the runtime" has "$OUT" "pdf:    copy the PDF runtime from here to $RT"
check "--dry-run plans the AppArmor profile with sudo -n" has "$OUT" "install the profile with sudo -n"
check "--dry-run leaves the program" test "$(cat "$BINP")" = "old program"
check "--dry-run copies no runtime" test ! -e "$RT"
check "--dry-run extracts nothing" not_logged "tar -x"
check "--dry-run runs no sudo" not_logged "sudo"
check "--dry-run restarts nothing" not_logged "systemctl --user restart"
run -- --dry-run --pdf fetch box
check "--dry-run --pdf fetch plans a download on the host" has "$OUT" "download the PDF runtime on the host into $RT"
check "--dry-run --pdf fetch installs nothing" not_logged "--install-pdf-runtime"

echo "update, runtime copied from here"
installed_host; local_runtime; runtime_at "$RT" 0123456789ab
run -- box
check "update exits 0" test "$RC" -eq 0
check "runtime extracted to a temporary name first" logged "tar -x -C $RT/.tmp-ship-"
check "runtime copied before the restart" before "tar -x -C" "systemctl --user restart"
check "host has the program's runtime revision" test "$(remote_rev "$RT")" = "$REV"
check "host runtime content is complete" test "$(cat "$RT/$REV/chrome/blob")" = "bytes-$REV"
check "old runtime revision removed after the check" test ! -e "$RT/0123456789ab"
check "no stash or temporary directory left in the runtime dir" no_leftovers "$RT"
check "program replaced" cmp -s "$FAKE/artifacts.py" "$BINP"
check "previous program kept as .prev" test "$(cat "$BINP.prev")" = "old program"
check "service restarted" logged "systemctl --user restart artifacts.service"
check "check ran the program's PDF check on the host's runtime" logged "remote artifacts.py --check-pdf-runtime --pdf-runtime $RT"
check "check printed ok for the PDF" has "$OUT" "PDF runtime: ok"
check "the AppArmor restriction is off: no sudo" not_logged "sudo"
check "no temporary directory left" no_temp_dirs
check "summary names the host as ok" has "$OUT" "ok: box"

echo "re-run: nothing to do"
run -- box
check "re-run exits 0" test "$RC" -eq 0
check "re-run: runtime is current" has "$OUT" "is current"
check "re-run: no runtime copied" not_logged "tar -x -C"
check "re-run: program unchanged" has "$OUT" "program unchanged"
check "re-run: no restart" not_logged "systemctl --user restart"
check "re-run keeps the real .prev" test "$(cat "$BINP.prev")" = "old program"

echo "remote probe: the runtime revision decides"
installed_host; local_runtime; runtime_at "$RT" "$REV"
run -- --dry-run box
check "matching host revision is current" has "$OUT" "runtime $REV is current"
installed_host; local_runtime; runtime_at "$RT" "0123456789ab"
run -- --dry-run box
check "host with another revision is replaced" has "$OUT" "(revision none -> $REV"
installed_host; local_runtime; mkdir -p "$RT/$REV"
run -- --dry-run box
check "a revision directory without REV counts as missing" has "$OUT" "(revision none -> $REV"
installed_host; local_runtime; runtime_at "$RT" "$REV"
run -- --dry-run --pdf ship box
check "--pdf ship copies even a current runtime" has "$OUT" "copy the PDF runtime from here"

installed_host; local_runtime; runtime_at "$RT" "$REV"; echo "stale" > "$RT/$REV/chrome/blob"
run -- --pdf ship box
check "--pdf ship over an existing same-revision runtime exits 0" test "$RC" -eq 0
check "--pdf ship replaces the existing runtime content" test "$(cat "$RT/$REV/chrome/blob")" = "bytes-$REV"
check "--pdf ship leaves nothing behind" no_leftovers "$RT"

echo "a custom --pdf-runtime in the unit is honoured"
installed_host; local_runtime
CUSTOM="$RHOME/custom/artifacts/pdf-runtime"
printf '[Service]\nExecStart=/usr/bin/python3 %s --bind 10.1.2.3 --port 7777 --pdf-runtime %s %s\n' "$BINP" "$CUSTOM" "$ROOT" > "$UNIT"
run -- box
check "custom runtime dir: exits 0" test "$RC" -eq 0
check "custom runtime dir receives the runtime" test "$(remote_rev "$CUSTOM")" = "$REV"
check "default runtime dir is not used" test ! -e "$RT"
check "check uses the custom dir" logged "--check-pdf-runtime --pdf-runtime $CUSTOM"

echo "auto downloads on the host when this machine has no runtime"
installed_host; runtime_at "$RT" "0123456789ab"
run -- box
check "fetch exits 0" test "$RC" -eq 0
check "the host ran artifacts.py --install-pdf-runtime into its runtime dir" logged "remote artifacts.py --install-pdf-runtime --pdf-runtime $RT"
check "nothing was copied from here" not_logged "tar -x -C"
check "fetched before the program restart" before "--install-pdf-runtime" "systemctl --user restart"
check "host has the program's runtime revision" test "$(remote_rev "$RT")" = "$REV"
check "old revision gone" test ! -e "$RT/0123456789ab"
check "no stash left" no_leftovers "$RT"
installed_host; local_runtime
run -- --pdf fetch box
check "--pdf fetch downloads even when a local runtime exists" logged "remote artifacts.py --install-pdf-runtime"
check "--pdf fetch copies nothing" not_logged "tar -x -C"

echo "failed fetch changes nothing"
installed_host; runtime_at "$RT" "0123456789ab"
run STUB_FETCH_FAIL=late -- --pdf fetch box
check "failed fetch exits 1" test "$RC" -eq 1
check "failed fetch says nothing changed" has "$OUT" "nothing on box was changed"
check "failed fetch leaves the program" test "$(cat "$BINP")" = "old program"
check "failed fetch brings the old revision back" test "$(cat "$RT/0123456789ab/chrome/blob")" = "bytes-0123456789ab"
check "failed fetch leaves no new revision" test ! -e "$RT/$REV"
check "failed fetch leaves no stash" no_leftovers "$RT"
check "failed fetch restarts nothing" not_logged "systemctl --user restart"
check "failed fetch leaves no temporary directory" no_temp_dirs

echo "the stream carries a manifest first and no REV"
installed_host; local_runtime
run -- box
check "the tar stream starts with the manifest" logged "tar -c -C "
check "the manifest member comes before the tree" logged " MANIFEST -C $LOCAL_RT "
check "REV is not sent" logged "--exclude=$REV/REV"
check "the host wrote REV itself" test "$(cat "$RT/$REV/REV")" = "$REV"
check "the manifest is not left in the runtime" test ! -e "$RT/$REV/MANIFEST"

echo "a stream cut at a member boundary installs nothing"
for cut in STUB_TAR_DROP=blob STUB_GUNZIP_FAIL=1; do
  installed_host; local_runtime; runtime_at "$RT" "0123456789ab"
  run "$cut" -- box
  check "$cut: exits 1" test "$RC" -eq 1
  check "$cut: no runtime under the final name" test ! -e "$RT/$REV"
  check "$cut: the failure is reported" has "$OUT" "the PDF runtime step failed; nothing on box was changed"
  check "$cut: no temporary directory or stash left" no_leftovers "$RT"
  check "$cut: the old revision is untouched" test "$(cat "$RT/0123456789ab/chrome/blob")" = "bytes-0123456789ab"
  check "$cut: the program is untouched" test "$(cat "$BINP")" = "old program"
  check "$cut: nothing restarted" not_logged "systemctl --user restart"
  check "$cut: summary names the host as failed" has "$OUT" "failed: box"
done
installed_host; local_runtime
run STUB_TAR_DROP=blob -- box
check "a mismatch is explained" has "$OUT" "does not match its manifest"

echo "cleanup removes only 12-hex revisions"
installed_host; local_runtime
for n in data backup-notes 0ld abcdef012345 ABCDEF012345 abcdef0123456; do runtime_at "$RT" "$n"; done
mkdir -p "$RT/.tmp-x-123"; echo keep > "$RT/.tmp-x-123/f"
run -- box
check "cleanup run exits 0" test "$RC" -eq 0
check "the current revision stays" test "$(remote_rev "$RT")" = "$REV"
check "another 12-hex revision goes" test ! -e "$RT/abcdef012345"
for n in data backup-notes 0ld ABCDEF012345 abcdef0123456 .tmp-x-123; do
  check "cleanup keeps $n" test -e "$RT/$n"
done
check "a foreign in-flight temp dir keeps its content" test "$(cat "$RT/.tmp-x-123/f")" = keep
check "the stash is gone" test ! -e "$RT.keep"

echo "a runtime dir with a space is refused early"
installed_host; local_runtime
printf '[Service]\nExecStart=/usr/bin/python3 %s --bind 10.1.2.3 --port 7777 --pdf-runtime "%s" %s\n' "$BINP" "$RHOME/my rt/artifacts/pdf-runtime" "$ROOT" > "$UNIT"
run -- box
check "space in the runtime dir: exits 1" test "$RC" -eq 1
check "space in the runtime dir: says why" has "$OUT" "without spaces or special"
check "space in the runtime dir: program untouched" test "$(cat "$BINP")" = "old program"
check "space in the runtime dir: nothing copied or restarted" not_logged "tar -x"
run -- --dry-run box
check "space in the runtime dir: refused by --dry-run too" test "$RC" -eq 1

echo "the profile is for the runtime dir the host uses"
installed_host; local_runtime; aa_restricted
CUSTOM="$RHOME/custom/artifacts/pdf-runtime"
printf '[Service]\nExecStart=/usr/bin/python3 %s --bind 10.1.2.3 --port 7777 --pdf-runtime %s %s\n' "$BINP" "$CUSTOM" "$ROOT" > "$UNIT"
run -- box
check "custom dir: exits 0" test "$RC" -eq 0
check "custom dir: the profile was requested for it" logged "local artifacts.py --print-apparmor-profile --pdf-runtime $CUSTOM"
check "custom dir: the installed profile names that exact dir" test "$(cat "$AA_FILE")" = "$(stub_profile "$CUSTOM")"
run -- box
check "custom dir: the profile is now present, no sudo" not_logged "sudo"
stub_profile "$RT" > "$AA_FILE"
run -- --dry-run box
check "a profile for another dir does not count as installed" has "$OUT" "install the profile with sudo -n"

echo "--pdf ship needs a runtime here"
installed_host
run -- --pdf ship box
check "ship without a local runtime exits 1" test "$RC" -eq 1
check "ship says how to install it here" has "$OUT" "--install-pdf-runtime"
check "ship without a local runtime changes nothing" test "$(cat "$BINP")" = "old program"

echo "free space"
installed_host; local_runtime
run STUB_DF_MB=100 -- box
check "too little space exits 1" test "$RC" -eq 1
check "too little space names the need" has "$OUT" "needs about 250 MB, 100 MB free"
check "too little space changes nothing" test "$(cat "$BINP")" = "old program"
run STUB_DF_MB=100 -- --dry-run box
check "too little space is reported by --dry-run too" test "$RC" -eq 1
run STUB_DF_MB=100 -- --pdf skip box
check "--pdf skip does not need the space" test "$RC" -eq 0
installed_host; local_runtime; runtime_at "$RT" "$REV"
run STUB_DF_MB=100 -- box
check "a current runtime does not need the space" test "$RC" -eq 0

echo "failed check rolls back program and runtime"
for fail in STUB_ACTIVE_RC=3 STUB_CHECK_OUT=boom STUB_CURL_CODE=000; do
  installed_host; local_runtime; runtime_at "$RT" "0123456789ab"
  run "$fail" -- box
  check "$fail: exits 1" test "$RC" -eq 1
  check "$fail: says it rolls back" has "$OUT" "rolling back the program and PDF runtime"
  check "$fail: program restored" test "$(cat "$BINP")" = "old program"
  check "$fail: old runtime revision restored" test "$(cat "$RT/0123456789ab/chrome/blob")" = "bytes-0123456789ab"
  check "$fail: new runtime revision removed" test ! -e "$RT/$REV"
  check "$fail: no stash left" no_leftovers "$RT"
  check "$fail: summary names the host as failed" has "$OUT" "failed: box"
done
installed_host; runtime_at "$RT" "0123456789ab"
run STUB_FETCH_FAIL= STUB_CHECK_OUT=boom -- box
check "rollback after a fetch brings the old revision back too" test "$(cat "$RT/0123456789ab/chrome/blob")" = "bytes-0123456789ab"

echo "--pdf skip"
installed_host; runtime_at "$RT" "0123456789ab"
run -- --pdf skip box
check "skip exits 0" test "$RC" -eq 0
check "skip copies no runtime" not_logged "tar -x -C"
check "skip downloads nothing" not_logged "--install-pdf-runtime"
check "skip runs no sudo" not_logged "sudo"
check "skip runs no PDF check" not_logged "--check-pdf-runtime"
check "skip leaves the old revision" test -e "$RT/0123456789ab"

echo "AppArmor profile on the host"
installed_host; local_runtime; aa_restricted
run -- box
check "restricted host: exits 0" test "$RC" -eq 0
check "restricted host: profile written with sudo -n tee" logged "sudo -n tee $AA_FILE"
check "restricted host: profile loaded with sudo -n apparmor_parser -r" logged "sudo -n $STUBS/apparmor_parser -r $AA_FILE"
check "restricted host: profile file names the exact runtime dir" test "$(cat "$AA_FILE")" = "$(stub_profile "$RT")"
check "restricted host: the profile was requested for that runtime dir" logged "local artifacts.py --print-apparmor-profile --pdf-runtime $RT"
check "restricted host: profile installed before the restart" before "sudo -n tee" "systemctl --user restart"
check "restricted host: says so" has "$OUT" "apparmor profile installed"
check "restricted host: PDF checked" has "$OUT" "PDF runtime: ok"
run -- box
check "profile already installed: no sudo" not_logged "sudo"
check "profile already installed: dry run says so" has "$(run -- --dry-run box; echo "$OUT")" "apparmor: the profile is installed"
installed_host; local_runtime; aa_restricted
run STUB_SUDO_FAIL=1 -- box
check "no passwordless sudo: the host is still ok" test "$RC" -eq 0
check "no passwordless sudo: says PDF is not working" has "$OUT" "PDF export is NOT working on box"
check "no passwordless sudo: prints the tee command" has "$OUT" "$BINP --print-apparmor-profile --pdf-runtime $RT | sudo tee /etc/apparmor.d/artifacts-chrome-headless-shell"
check "no passwordless sudo: prints the apparmor_parser command" has "$OUT" "sudo apparmor_parser -r /etc/apparmor.d/artifacts-chrome-headless-shell"
check "no passwordless sudo: no PDF check" not_logged "--check-pdf-runtime"
check "no passwordless sudo: the runtime was still copied" test "$(remote_rev "$RT")" = "$REV"
check "no passwordless sudo: the program was still replaced" cmp -s "$FAKE/artifacts.py" "$BINP"
check "no passwordless sudo: no profile written" test ! -e "$AA_FILE"
check "no passwordless sudo: final reminder" has "$OUT" "not working until the AppArmor profile is installed"
installed_host; local_runtime
run -- box
check "unrestricted host: no sudo" not_logged "sudo"

echo "a host without the viewer"
fresh_host; local_runtime
run -- box
check "not installed, no options: exits 1" test "$RC" -eq 1
check "not installed: says to pass install options" has "$OUT" "pass install.sh options after --"
run STUB_CURL_CODE=200 -- box -- --bind 10.1.2.3 "$ROOT"
check "install exits 0" test "$RC" -eq 0
check "install.sh wrote the unit" test -f "$UNIT"
check "install.sh installed the program" cmp -s "$FAKE/artifacts.py" "$BINP"
check "install copied the runtime first" test "$(remote_rev "$RT")" = "$REV"
check "install copied the runtime before the unit was set up" before "tar -x -C" "systemctl --user daemon-reload"
check "install output is shown" has "$OUT" "| installed $BINP"
check "install reports a working PDF button" has "$OUT" "present and working"
fresh_host; local_runtime; aa_restricted
run STUB_CURL_CODE=200 STUB_SUDO_FAIL=1 -- box -- --bind 10.1.2.3 "$ROOT"
check "install without sudo: exits 0" test "$RC" -eq 0
check "install without sudo: deploy prints the commands" has "$OUT" "PDF export is NOT working on box"
check "install without sudo: install.sh is told --no-apparmor, so no second sudo" not_logged "remote sudo tee"
check "install without sudo: install.sh status says the sandbox is unusable" has "$OUT" "sandbox unusable"
fresh_host; local_runtime; aa_restricted
run STUB_CURL_CODE=200 -- box -- --bind 10.1.2.3 "$ROOT"
check "install on a restricted host: profile installed once by deploy" test "$(grep -c 'sudo -n tee' "$LOG")" -eq 1
check "install on a restricted host: install.sh needs no sudo of its own" not_logged "sudo tee"

echo "several hosts"
installed_host; local_runtime; runtime_at "$RT" "$REV"
cp "$FAKE/artifacts.py" "$BINP"
run -- box unreachable
check "one unreachable host exits 1" test "$RC" -eq 1
check "the reachable host is ok" has "$OUT" "ok: box"
check "the unreachable host is named" has "$OUT" "cannot reach unreachable"
check "summary lists the failed host" has "$OUT" "failed: unreachable"

echo "no docker"
check "deploy.sh keeps no docker call or probe" bash -c '! grep -i docker "$1" | grep -v "no longer use docker"' _ "$HERE/deploy.sh"

echo
if [ "$FAILS" -eq 0 ]; then echo "all checks passed"; exit 0; fi
echo "$FAILS check(s) failed"; exit 1
