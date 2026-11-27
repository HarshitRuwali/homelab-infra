#!/usr/bin/env bash
# Behavioural tests for install.sh, run against stubs in a temporary HOME.
#
# Run from vault-viewer/:  ./tests/test_install.sh
# Exit 0 = all pass. Nothing touches the real systemd, sudo, AppArmor or network:
# install.sh runs from a copy next to a stub artifacts.py, and every external
# command (systemctl, sudo, apparmor_parser, curl) is a stub that logs its argv.
# shellcheck disable=SC2016  # single quotes are deliberate: the text is for stubs and bash -c
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# The program under test is a copy of install.sh and the unit template, beside a
# stub artifacts.py that implements the flags install.sh calls.
FAKE="$TMP/fake"
mkdir -p "$FAKE/systemd"
cp "$HERE/install.sh" "$FAKE/install.sh"
cp "$HERE/systemd/artifacts.service" "$FAKE/systemd/artifacts.service"
cat > "$FAKE/artifacts.py" <<'STUBPY'
import os, sys
a = sys.argv[1:]
if os.environ.get("LOG"):
    with open(os.environ["LOG"], "a") as f:
        f.write("artifacts.py " + " ".join(a) + "\n")
def runtime_dir():
    if "--pdf-runtime" in a:
        return a[a.index("--pdf-runtime") + 1]
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return data + "/artifacts/pdf-runtime"
if "--print-apparmor-profile" in a:
    print("# stub profile\nprofile stub {}\n" + runtime_dir() + "/*/chrome/chrome-headless-shell")
elif "--install-pdf-runtime" in a:
    if os.environ.get("STUB_INSTALL_RC", "0") != "0":
        print("download failed", file=sys.stderr)
        sys.exit(1)
    print("installed")
elif "--check-pdf-runtime" in a:
    out = os.environ.get("STUB_CHECK_OUT", "ok")
    print(out)
    sys.exit(0 if out == "ok" else 1)
STUBPY
INSTALL="$FAKE/install.sh"
FAILS=0

check() { # check <label> <command...>
  local label="$1"; shift
  if "$@"; then echo "  [ ok ] $label"; else echo "  [FAIL] $label"; FAILS=$((FAILS + 1)); fi
}
has() { grep -qF -- "$2" <<<"$1"; }          # has <text> <fixed string>
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

# --- stubs: every one logs "name argv" to $LOG ---------------------------------
STUBS="$TMP/stubs"
mkdir -p "$STUBS"
mkstub() { # mkstub <name> <body>
  printf '#!/usr/bin/env bash\necho "%s $*" >> "$LOG"\n%s\n' "$1" "$2" > "$STUBS/$1"
  chmod +x "$STUBS/$1"
}
# STUB_ACTIVE_RC controls `systemctl --user is-active`; STUB_CURL_CODE the HTTP code;
# STUB_INSTALL_RC the exit status of `artifacts.py --install-pdf-runtime`;
# STUB_CHECK_OUT the output of `artifacts.py --check-pdf-runtime` ("ok" passes);
# STUB_SUDO_FAIL makes the sudo stub fail.
mkstub systemctl 'case "$*" in *is-active*) exit "${STUB_ACTIVE_RC:-0}" ;; esac; exit 0'
mkstub tailscale 'exit 0'
mkstub sleep 'exit 0'
mkstub curl 'echo "${STUB_CURL_CODE:-200}"'
mkstub sudo '[ -z "${STUB_SUDO_FAIL:-}" ] || exit 1; exec "$@"'
mkstub apparmor_parser 'exit 0'

# The AppArmor seams: the kernel switch, the profile file, sudo and apparmor_parser.
mkdir -p "$TMP/aa"
AA_RESTRICT="$TMP/aa/restrict"
AA_FILE="$TMP/aa/artifacts-chrome-headless-shell"
echo 0 > "$AA_RESTRICT"
export ARTIFACTS_APPARMOR_RESTRICT_FILE="$AA_RESTRICT"
export ARTIFACTS_APPARMOR_PROFILE_FILE="$AA_FILE"
export ARTIFACTS_SUDO="$STUBS/sudo"
export ARTIFACTS_APPARMOR_PARSER="$STUBS/apparmor_parser"
aa_restricted() { echo 1 > "$AA_RESTRICT"; rm -f "$AA_FILE"; }
aa_open() { echo 0 > "$AA_RESTRICT"; rm -f "$AA_FILE"; }
# stub_profile <runtime dir>: what the stub artifacts.py prints for that directory.
stub_profile() { printf '# stub profile\nprofile stub {}\n%s/*/chrome/chrome-headless-shell' "$1"; }

# A PATH with the basics but no tailscale, for the "missing tool" cases.
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
check "--help no longer offers the removed --pdf-base" lacks "$OUT" "--pdf-base"
check "--help no longer offers the removed --build-pdf-image" lacks "$OUT" "--build-pdf-image"
check "--help does not mention docker" lacks "$OUT" "docker"
check "--help gives the runtime download and install sizes" has "$OUT" "about 180 MB"
for flag in --bind --port --allow-host --tailnet-user --title --install-pdf-runtime --no-apparmor --obsidian-cli --obsidian-vault --dry-run -h --help; do
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
for form in "--pdf-base mcr.example/playwright:v1.56.0-noble" "--pdf-base=mcr.example/playwright:v1.56.0-noble" "--build-pdf-image"; do
  # shellcheck disable=SC2086  # split the option and its value on purpose
  run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 $form "$ROOT"
  check "removed ${form%% *} exits 2" test "$RC" -eq 2
  check "removed ${form%% *} says it was removed" has "$OUT" "${form%%[ =]*} was removed"
  check "removed ${form%% *} names --install-pdf-runtime" has "$OUT" "--install-pdf-runtime"
  check "removed ${form%% *} runs nothing" test ! -s "$LOG"
  check "removed ${form%% *} writes nothing" test ! -e "$UNITP"
done
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
check "preflight failures write nothing" test ! -e "$BINP"

echo "dry run"
fresh_home
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 --tailnet-user me@github "$ROOT"
check "--dry-run exits 0" test "$RC" -eq 0
check "--dry-run writes no program" test ! -e "$BINP"
check "--dry-run writes no unit" test ! -e "$UNITP"
check "--dry-run creates no config dir" test ! -e "$TMP/home/.config"
check "--dry-run calls no systemctl" not_logged "systemctl"
check "--dry-run calls no sudo" not_logged "sudo"
check "--dry-run does not install the runtime" not_logged "--install-pdf-runtime"
check "--dry-run says the runtime is left alone" has "$OUT" "leave the PDF runtime alone"
check "--dry-run prints the unit" has "$OUT" "ExecStart=/usr/bin/python3 $BINP --bind 10.1.2.3 --port 7777 --tailnet-user me@github"
run "$HOME_DIR" "$WITH_STUBS" --dry-run --install-pdf-runtime --bind 10.1.2.3 "$ROOT"
check "--dry-run with --install-pdf-runtime installs nothing" not_logged "--install-pdf-runtime"
check "--dry-run plans the runtime install" has "$OUT" "artifacts.py --install-pdf-runtime (downloads about 180 MB once, installs about 235 MB under"
check "--dry-run names the runtime directory" has "$OUT" "artifacts/pdf-runtime"
aa_restricted
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 "$ROOT"
check "--dry-run with the restriction on exits 0" test "$RC" -eq 0
check "--dry-run plans the AppArmor profile with sudo" has "$OUT" "install the AppArmor profile with sudo: write $AA_FILE, then apparmor_parser -r it"
check "--dry-run runs no sudo even when the profile is missing" not_logged "sudo"
check "--dry-run writes no profile" test ! -e "$AA_FILE"
run "$HOME_DIR" "$WITH_STUBS" --dry-run --no-apparmor --bind 10.1.2.3 "$ROOT"
check "--dry-run --no-apparmor plans to skip the profile" has "$OUT" "skip the AppArmor profile (--no-apparmor)"
aa_open
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 "$ROOT"
check "--dry-run without the restriction needs no profile" has "$OUT" "leave AppArmor alone"

echo "real run"
fresh_home
STUB_CURL_CODE=403 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --port 7788 --allow-host box --allow-host box.example.ts.net \
  --tailnet-user Me@github --title "my vault" "$ROOT" "$ROOT2"
check "install exits 0" test "$RC" -eq 0
check "program installed identical to artifacts.py" cmp -s "$FAKE/artifacts.py" "$BINP"
check "program is mode 0755" test "$(stat -c %a "$BINP")" = 755
check "unit written" test -f "$UNITP"
EXEC="$(grep '^ExecStart=' "$UNITP")"
WANT="ExecStart=/usr/bin/python3 $BINP --bind 10.1.2.3 --port 7788 --allow-host box --allow-host box.example.ts.net"
WANT+=" --tailnet-user Me@github --title \"my vault\" --obsidian-cli none \"$TMP/vault dir %%x\" $TMP/other"
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
check "no runtime install without the flag" not_logged "--install-pdf-runtime"
check "no sudo when the kernel does not restrict user namespaces" not_logged "sudo"
for line in "MemoryMax=1536M" "OOMPolicy=continue" "TasksMax=1024"; do
  check "unit has $line" grep -qx "$line" "$UNITP"
done
check "unit no longer mentions docker" bash -c '! grep -qi docker "$1"' _ "$UNITP"
check "output has the URL" has "$OUT" "http://10.1.2.3:7788"
check "output has the logs command" has "$OUT" "journalctl --user -u artifacts.service -f"
check "output says the PDF button works" has "$OUT" "present and working"
check "output explains the 403" has "$OUT" "HTTP 403"
check "status comes from artifacts.py --check-pdf-runtime" logged "artifacts.py --check-pdf-runtime"

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
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --install-pdf-runtime "$ROOT"
check "--install-pdf-runtime exits 0" test "$RC" -eq 0
check "--install-pdf-runtime runs the program's installer" logged "artifacts.py --install-pdf-runtime"
check "--install-pdf-runtime installs before the service is touched" before "--install-pdf-runtime" "systemctl --user daemon-reload"
check "--install-pdf-runtime says how big the download is" has "$OUT" "about 180 MB"
check "--install-pdf-runtime reports a working PDF button" has "$OUT" "present and working"
fresh_home
STUB_INSTALL_RC=1 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --install-pdf-runtime "$ROOT"
check "failed runtime install gives the retry command" has "$OUT" "artifacts.py --install-pdf-runtime"
fresh_home
STUB_CHECK_OUT="the PDF runtime is not installed: run \`artifacts --install-pdf-runtime\`" run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "missing runtime: exits 0" test "$RC" -eq 0
check "missing runtime: status says not installed" has "$OUT" "not installed: the PDF button will report that the runtime is missing"
check "missing runtime: status gives the install command" has "$OUT" "$BINP --install-pdf-runtime"
fresh_home
STUB_CHECK_OUT="Chromium's sandbox is not usable on this host" run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "unusable sandbox: status says so" has "$OUT" "sandbox unusable"
check "unusable sandbox: status gives the tee command" has "$OUT" "sudo tee $AA_FILE"
check "unusable sandbox: status gives the apparmor_parser command" has "$OUT" "sudo apparmor_parser -r $AA_FILE"
fresh_home
STUB_CHECK_OUT="rendering failed" run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "any other check failure is shown" has "$OUT" "installed but not working: rendering failed"
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

echo "existing unit and --install-pdf-runtime ordering"
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
STUB_INSTALL_RC=1 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --install-pdf-runtime "$ROOT"
check "failing runtime install exits 1" test "$RC" -eq 1
check "failing runtime install says so" has "$OUT" "installing the PDF runtime failed"
check "failing runtime install writes no unit" test ! -e "$UNITP"
check "failing runtime install installs no program" test ! -e "$BINP"
check "failing runtime install calls no systemctl" not_logged "systemctl"
check "failing runtime install asks for no sudo" not_logged "sudo"

echo "AppArmor profile"
fresh_home; aa_restricted
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "restricted kernel: install exits 0" test "$RC" -eq 0
check "restricted kernel: profile written through sudo tee" logged "sudo tee $AA_FILE"
check "restricted kernel: profile loaded through sudo apparmor_parser -r" logged "sudo $STUBS/apparmor_parser -r $AA_FILE"
check "restricted kernel: the profile text names the exact runtime dir" test "$(cat "$AA_FILE")" = "$(stub_profile "$HOME_DIR/.local/share/artifacts/pdf-runtime")"
check "restricted kernel: the profile was requested for that runtime dir" logged "artifacts.py --print-apparmor-profile --pdf-runtime $HOME_DIR/.local/share/artifacts/pdf-runtime"
check "restricted kernel: profile written before the service is touched" before "sudo tee" "systemctl --user daemon-reload"
check "restricted kernel: status is from the check, not the sandbox hint" lacks "$OUT" "sandbox unusable"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "profile already installed: install exits 0" test "$RC" -eq 0
check "profile already installed: no sudo" not_logged "sudo"
echo "# an older profile" > "$AA_FILE"
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "profile differs: rewritten through sudo" logged "sudo tee $AA_FILE"
check "profile differs: file now matches" test "$(cat "$AA_FILE")" = "$(stub_profile "$HOME_DIR/.local/share/artifacts/pdf-runtime")"
fresh_home; aa_restricted
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --no-apparmor "$ROOT"
check "--no-apparmor exits 0" test "$RC" -eq 0
check "--no-apparmor runs no sudo" not_logged "sudo"
check "--no-apparmor writes no profile" test ! -e "$AA_FILE"
check "--no-apparmor status says the sandbox is unusable" has "$OUT" "sandbox unusable"
check "--no-apparmor status says the PDF button reports it" has "$OUT" "the PDF button will report that Chromium's sandbox is not usable"
check "--no-apparmor status gives the tee command" has "$OUT" "$BINP --print-apparmor-profile --pdf-runtime $HOME_DIR/.local/share/artifacts/pdf-runtime | sudo tee $AA_FILE >/dev/null"
check "--no-apparmor status gives the apparmor_parser command" has "$OUT" "sudo apparmor_parser -r $AA_FILE"
fresh_home; aa_restricted
STUB_SUDO_FAIL=1 run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "failing sudo does not fail the install" test "$RC" -eq 0
check "failing sudo warns" has "$OUT" "could not install the AppArmor profile"
check "failing sudo still starts the viewer" has "$OUT" "the viewer is up"
check "failing sudo status gives the commands" has "$OUT" "sudo apparmor_parser -r $AA_FILE"
fresh_home; aa_restricted
XDG_DATA_HOME="$TMP/data dir" run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "a runtime dir with a space is refused: exits 1" test "$RC" -eq 1
check "a runtime dir with a space: the message names it" has "$OUT" "without spaces"
check "a runtime dir with a space: nothing installed" test ! -e "$BINP"
check "a runtime dir with a space: no sudo" not_logged "sudo"
XDG_DATA_HOME="$TMP/data dir" run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 "$ROOT"
check "a runtime dir with a space is refused by --dry-run too" test "$RC" -eq 1
XDG_DATA_HOME="relative/dir" run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 "$ROOT"
check "a relative runtime dir is refused" test "$RC" -eq 1
fresh_home; aa_open
run "$HOME_DIR" "$WITH_STUBS" --bind 10.1.2.3 --no-apparmor "$ROOT"
check "--no-apparmor without the restriction: no sandbox warning" lacks "$OUT" "sandbox unusable"

echo "obsidian cli"
OBS="$TMP/obs bin/obsidian"; mkdir -p "$TMP/obs bin"; printf '#!/bin/sh\n' > "$OBS"; chmod +x "$OBS"
fresh_home
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 "$ROOT"
check "without --obsidian-cli the unit turns Bases off" has "$OUT" "--obsidian-cli none"
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 --obsidian-cli none "$ROOT"
check "--obsidian-cli none is accepted" test "$RC" -eq 0
check "--obsidian-cli none turns Bases off" has "$OUT" "--obsidian-cli none"
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 --obsidian-cli "$OBS" --obsidian-vault "$ROOT2" "$ROOT"
check "--obsidian-cli and --obsidian-vault exit 0" test "$RC" -eq 0
check "the unit passes --obsidian-cli quoted for systemd" has "$OUT" "--obsidian-cli \"$OBS\""
check "the unit passes --obsidian-vault" has "$OUT" "--obsidian-vault $ROOT2"
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 --obsidian-cli relative/obsidian "$ROOT"
check "a relative --obsidian-cli exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 --obsidian-cli "$TMP/missing" "$ROOT"
check "a missing --obsidian-cli fails" test "$RC" -eq 1
check "a missing --obsidian-cli points at setup.sh" has "$OUT" "obsidian/setup.sh"
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 --obsidian-vault "$ROOT2" "$ROOT"
check "--obsidian-vault without --obsidian-cli exits 2" test "$RC" -eq 2
run "$HOME_DIR" "$WITH_STUBS" --dry-run --bind 10.1.2.3 --obsidian-cli "$OBS" --obsidian-vault "$TMP/missing" "$ROOT"
check "a missing --obsidian-vault fails" test "$RC" -eq 1

echo "no docker"
check "install.sh keeps no docker call or check" bash -c '! grep -i docker "$1" | grep -v "was removed"' _ "$HERE/install.sh"

echo
if [ "$FAILS" -ne 0 ]; then echo "$FAILS check(s) failed"; exit 1; fi
echo "all checks passed"
