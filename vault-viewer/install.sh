#!/usr/bin/env bash
# Install and start the vault viewer as a systemd user unit.
#
# Idempotent: safe to re-run (a reinstall restarts the service so it picks up a
# new program). Verifies the viewer answers before exiting. See usage() below.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="artifacts.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN="${ARTIFACTS_BIN:-$HOME/.local/bin/artifacts}"
PYTHON="/usr/bin/python3"
PDF_RUNTIME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/artifacts/pdf-runtime"
# Chromium's sandbox needs an AppArmor profile when the kernel restricts
# unprivileged user namespaces. The ARTIFACTS_* paths and commands below are
# test seams; the defaults are the real ones.
AA_RESTRICT_FILE="${ARTIFACTS_APPARMOR_RESTRICT_FILE:-/proc/sys/kernel/apparmor_restrict_unprivileged_userns}"
AA_PROFILE_FILE="${ARTIFACTS_APPARMOR_PROFILE_FILE:-/etc/apparmor.d/artifacts-chrome-headless-shell}"
SUDO="${ARTIFACTS_SUDO:-sudo}"
APPARMOR_PARSER="${ARTIFACTS_APPARMOR_PARSER:-apparmor_parser}"

usage() {
  cat <<'USAGE'
usage: install.sh --bind ADDR [--port N] [--allow-host NAME]... [--tailnet-user LOGIN]
                  [--title NAME] [--install-pdf-runtime] [--no-apparmor]
                  [--obsidian-cli CMD] [--obsidian-vault DIR] [--dry-run] ROOT...
       install.sh -h | --help

Install the vault viewer (artifacts.py) and run it as the systemd user unit
artifacts.service, serving the vault folders ROOT....

  --bind ADDR           unicast IPv4 address to listen on (required), e.g. a tailnet IP;
                        not 0.0.0.0, not multicast
  --port N              port to listen on (default 7777)
  --allow-host NAME     extra Host header name to accept; repeatable
  --tailnet-user LOGIN  admit only this tailnet user's untagged devices
                        (needs the tailscale CLI)
  --title NAME          name shown in the sidebar
  --install-pdf-runtime download and install the PDF export runtime (Chromium's
                        headless shell, page libraries, fonts): about 180 MB
                        once, about 235 MB installed under
                        ~/.local/share/artifacts/pdf-runtime
  --no-apparmor         do not install the AppArmor profile that Chromium's
                        sandbox needs on Ubuntu 23.10+ (it is installed with
                        sudo when the kernel restricts user namespaces)
  --obsidian-cli CMD    the Obsidian CLI the viewer runs to show Bases, an
                        absolute path such as ~/.local/bin/obsidian from
                        obsidian/setup.sh, or "none" (default: none)
  --obsidian-vault DIR  the vault that Obsidian has open (default: the first ROOT)
  --dry-run             print the unit and the actions, change nothing
  -h, --help            show this help

Environment: ARTIFACTS_BIN sets the install path of the program
(default ~/.local/bin/artifacts).
USAGE
}

die_usage() {
  echo "install.sh: $1" >&2
  usage >&2
  exit 2
}

# A control character (newline, CR, tab, ...) would split or corrupt a unit line.
no_control() { # no_control <what> <value>
  if [[ "$2" == *[[:cntrl:]]* ]]; then
    die_usage "$1 must not contain a control character (newline, tab, ...)"
  fi
}

# Quote one argument for a systemd ExecStart= line: % -> %%, $ -> $$, and
# double quotes around anything that is not plainly safe (\ and " escaped).
unit_quote() {
  local s="$1"
  if [[ "$s" =~ ^[A-Za-z0-9_@+=:,./-]+$ ]]; then
    printf '%s' "$s"
    return
  fi
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//%/%%}"
  s="${s//\$/\$\$}"
  printf '"%s"' "$s"
}

BIND=""
PORT=7777
ALLOW_HOSTS=()
TAILNET_USER=""
TITLE=""
OBSIDIAN_CLI=""
OBSIDIAN_VAULT=""
INSTALL_PDF=0
NO_APPARMOR=0
DRY_RUN=0
ROOTS_IN=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --bind|--port|--allow-host|--tailnet-user|--title|--obsidian-cli|--obsidian-vault)
      [ "$#" -ge 2 ] || die_usage "$1 needs a value"
      no_control "$1 value" "$2"
      case "$1" in
        --bind) BIND="$2" ;;
        --port) PORT="$2" ;;
        --allow-host) ALLOW_HOSTS+=("$2") ;;
        --tailnet-user) TAILNET_USER="$2" ;;
        --title) TITLE="$2" ;;
        --obsidian-cli) OBSIDIAN_CLI="$2" ;;
        --obsidian-vault) OBSIDIAN_VAULT="$2" ;;
      esac
      shift 2 ;;
    --install-pdf-runtime) INSTALL_PDF=1; shift ;;
    --no-apparmor) NO_APPARMOR=1; shift ;;
    --build-pdf-image) die_usage "--build-pdf-image was removed: PDFs no longer use docker; install the runtime with --install-pdf-runtime" ;;
    --pdf-base|--pdf-base=*) die_usage "--pdf-base was removed: PDFs no longer use docker; install the runtime with --install-pdf-runtime" ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; ROOTS_IN+=("$@"); break ;;
    -*) die_usage "unknown option: $1" ;;
    *) ROOTS_IN+=("$1"); shift ;;
  esac
done
for r in "${ROOTS_IN[@]+"${ROOTS_IN[@]}"}"; do no_control "ROOT" "$r"; done
no_control "ARTIFACTS_BIN" "$BIN"
# The runtime dir goes into the AppArmor profile and into commands: artifacts.py
# refuses whitespace and profile glob characters in it, so refuse them here, early.
if ! [[ "$PDF_RUNTIME_DIR" =~ ^/[A-Za-z0-9_@%+=:,./~-]*$ ]]; then
  echo "install.sh: the PDF runtime directory must be an absolute path without spaces or" >&2
  echo "            special characters (it is built from XDG_DATA_HOME or HOME), got: $PDF_RUNTIME_DIR" >&2
  exit 1
fi

[ -n "$BIND" ] || die_usage "--bind is required"
# A dotted-quad IPv4 address, checked by the interpreter the unit runs (it rejects
# out-of-range octets and, on current Python, leading zeros); the regex rejects
# leading zeros on older Pythons too. Not 0.0.0.0 (all interfaces), not multicast.
[ -x "$PYTHON" ] || { echo "install.sh: $PYTHON not found (the unit runs it)" >&2; exit 1; }
if ! [[ "$BIND" =~ ^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$ ]] \
  || ! "$PYTHON" -c 'import ipaddress,sys; a=ipaddress.IPv4Address(sys.argv[1]); sys.exit(1 if a.is_unspecified or a.is_multicast else 0)' "$BIND" 2>/dev/null; then
  die_usage "--bind must be a unicast IPv4 address (not 0.0.0.0, not multicast, no leading zeros), got: $BIND"
fi
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
  || die_usage "--port must be 1-65535, got: $PORT"
[ "${#ROOTS_IN[@]}" -gt 0 ] || die_usage "at least one ROOT folder is required"

ROOTS=()
for r in "${ROOTS_IN[@]}"; do
  [ -d "$r" ] || { echo "install.sh: not a directory: $r" >&2; exit 1; }
  ROOTS+=("$(realpath "$r")")
done

# Bases are evaluated by the Obsidian app through its CLI (obsidian/setup.sh
# installs a wrapper). Off unless asked for; "none" is the same as leaving it out.
[ "$OBSIDIAN_CLI" != none ] || OBSIDIAN_CLI=""
if [ -n "$OBSIDIAN_CLI" ]; then
  [[ "$OBSIDIAN_CLI" == /* ]] || die_usage "--obsidian-cli must be an absolute path or none, got: $OBSIDIAN_CLI"
  [ -x "$OBSIDIAN_CLI" ] || { echo "install.sh: --obsidian-cli is not an executable file: $OBSIDIAN_CLI" >&2
    echo "            set it up first: $HERE/obsidian/setup.sh VAULT" >&2; exit 1; }
fi
if [ -n "$OBSIDIAN_VAULT" ]; then
  [ -n "$OBSIDIAN_CLI" ] || die_usage "--obsidian-vault needs --obsidian-cli"
  [ -d "$OBSIDIAN_VAULT" ] || { echo "install.sh: not a directory: $OBSIDIAN_VAULT" >&2; exit 1; }
  OBSIDIAN_VAULT="$(realpath "$OBSIDIAN_VAULT")"
fi

# --- preflight ---------------------------------------------------------------
if ! "$PYTHON" -c 'import markdown' 2>/dev/null; then
  echo "warning: $PYTHON cannot import markdown, notes will render as plain text" >&2
  echo "         (Debian/Ubuntu: sudo apt install python3-markdown)" >&2
fi
if [ -n "$TAILNET_USER" ] && ! command -v tailscale >/dev/null 2>&1; then
  echo "install.sh: --tailnet-user needs the tailscale CLI, which is not on PATH" >&2
  exit 1
fi
[ -f "$HERE/artifacts.py" ] || { echo "install.sh: $HERE/artifacts.py missing" >&2; exit 1; }
[ -f "$HERE/systemd/$UNIT_NAME" ] || { echo "install.sh: $HERE/systemd/$UNIT_NAME missing" >&2; exit 1; }

# --- render the unit ---------------------------------------------------------
ARGS="--bind $(unit_quote "$BIND") --port $PORT"
for h in "${ALLOW_HOSTS[@]+"${ALLOW_HOSTS[@]}"}"; do
  ARGS+=" --allow-host $(unit_quote "$h")"
done
[ -z "$TAILNET_USER" ] || ARGS+=" --tailnet-user $(unit_quote "$TAILNET_USER")"
[ -z "$TITLE" ] || ARGS+=" --title $(unit_quote "$TITLE")"
if [ -n "$OBSIDIAN_CLI" ]; then
  ARGS+=" --obsidian-cli $(unit_quote "$OBSIDIAN_CLI")"
  [ -z "$OBSIDIAN_VAULT" ] || ARGS+=" --obsidian-vault $(unit_quote "$OBSIDIAN_VAULT")"
else
  ARGS+=" --obsidian-cli none"
fi
for r in "${ROOTS[@]}"; do
  ARGS+=" $(unit_quote "$r")"
done

TEMPLATE="$(<"$HERE/systemd/$UNIT_NAME")"
UNIT="${TEMPLATE//__BIN__/"$(unit_quote "$BIN")"}"
UNIT="${UNIT//__ARGS__/"$ARGS"}"

if [ "$INSTALL_PDF" -eq 1 ]; then
  PDF_PLAN="run $PYTHON $HERE/artifacts.py --install-pdf-runtime (downloads about 180 MB once, installs about 235 MB under $PDF_RUNTIME_DIR)"
else
  PDF_PLAN="leave the PDF runtime alone (not installing it)"
fi

# AppArmor: needed when the kernel restricts unprivileged user namespaces and
# the profile is missing or differs from the one this program ships.
# The profile is for the runtime directory this install uses (its attachment is that exact path).
AA_PROFILE="$("$PYTHON" "$HERE/artifacts.py" --print-apparmor-profile --pdf-runtime "$PDF_RUNTIME_DIR")" \
  || { echo "install.sh: $HERE/artifacts.py --print-apparmor-profile failed" >&2; exit 1; }
AA_NEEDED=0
if [ "$(cat "$AA_RESTRICT_FILE" 2>/dev/null || true)" = 1 ] \
  && [ "$(cat "$AA_PROFILE_FILE" 2>/dev/null || true)" != "$AA_PROFILE" ]; then
  AA_NEEDED=1
fi
AA_COMMANDS="    $PYTHON $(printf '%q' "$BIN") --print-apparmor-profile --pdf-runtime $(printf '%q' "$PDF_RUNTIME_DIR") | sudo tee $(printf '%q' "$AA_PROFILE_FILE") >/dev/null
    sudo apparmor_parser -r $(printf '%q' "$AA_PROFILE_FILE")"
if [ "$AA_NEEDED" -eq 0 ]; then
  AA_PLAN="leave AppArmor alone (no profile needed or it is already installed)"
elif [ "$NO_APPARMOR" -eq 1 ]; then
  AA_PLAN="skip the AppArmor profile (--no-apparmor)"
else
  AA_PLAN="install the AppArmor profile with sudo: write $AA_PROFILE_FILE, then apparmor_parser -r it"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "dry run: nothing will be changed"
  echo
  echo "would install  $HERE/artifacts.py -> $BIN (mode 0755)"
  echo "would write    $UNIT_DIR/$UNIT_NAME:"
  echo "-----"
  printf '%s\n' "$UNIT"
  echo "-----"
  echo "would run      systemctl --user daemon-reload"
  echo "would run      systemctl --user enable $UNIT_NAME"
  echo "would run      systemctl --user restart $UNIT_NAME"
  echo "would          $PDF_PLAN"
  echo "would          $AA_PLAN"
  echo "would check    http://$BIND:$PORT/ answers within 10 s"
  exit 0
fi

# --- install -----------------------------------------------------------------
# Install the PDF runtime first: if it fails, nothing else has been changed.
if [ "$INSTALL_PDF" -eq 1 ]; then
  echo "installing the PDF runtime: this downloads about 180 MB once..."
  if ! "$PYTHON" "$HERE/artifacts.py" --install-pdf-runtime; then
    echo "install.sh: installing the PDF runtime failed; nothing was installed or changed." >&2
    echo "            Check the network and the free disk space (about 235 MB) and re-run." >&2
    echo "            To retry only the runtime: $PYTHON $HERE/artifacts.py --install-pdf-runtime" >&2
    exit 1
  fi
fi

# The AppArmor profile, with sudo. A failure is not fatal: the viewer works, only
# the PDF button needs the profile.
AA_FAILED=0
if [ "$AA_NEEDED" -eq 1 ] && [ "$NO_APPARMOR" -eq 0 ]; then
  echo "installing the AppArmor profile $AA_PROFILE_FILE (sudo)..."
  if printf '%s\n' "$AA_PROFILE" | "$SUDO" tee "$AA_PROFILE_FILE" >/dev/null \
    && "$SUDO" "$APPARMOR_PARSER" -r "$AA_PROFILE_FILE"; then
    AA_NEEDED=0
  else
    AA_FAILED=1
    echo "warning: could not install the AppArmor profile (sudo failed)" >&2
  fi
fi

# PDF status, from the program's own check (it prints a page to PDF).
if [ "$AA_NEEDED" -eq 1 ] && { [ "$NO_APPARMOR" -eq 1 ] || [ "$AA_FAILED" -eq 1 ]; }; then
  PDF_STATUS="sandbox unusable: the PDF button will report that Chromium's sandbox is not usable
  until the AppArmor profile is installed (needs sudo):
$AA_COMMANDS"
elif PDF_CHECK="$("$PYTHON" "$HERE/artifacts.py" --check-pdf-runtime 2>&1)"; then
  PDF_STATUS="present and working: the PDF button works."
elif [[ "$PDF_CHECK" == *"not installed"* ]]; then
  PDF_STATUS="not installed: the PDF button will report that the runtime is missing until you
  install it (downloads about 180 MB once, installs about 235 MB):
    $PYTHON $BIN --install-pdf-runtime"
elif [[ "$PDF_CHECK" == *sandbox* ]]; then
  PDF_STATUS="sandbox unusable: $PDF_CHECK
  Install the AppArmor profile (needs sudo):
$AA_COMMANDS"
else
  PDF_STATUS="installed but not working: $PDF_CHECK"
fi

install -D -m 0755 "$HERE/artifacts.py" "$BIN"
echo "installed $BIN"
mkdir -p "$UNIT_DIR"
if [ -e "$UNIT_DIR/$UNIT_NAME" ] && ! printf '%s\n' "$UNIT" | cmp -s - "$UNIT_DIR/$UNIT_NAME"; then
  cp -p "$UNIT_DIR/$UNIT_NAME" "$UNIT_DIR/$UNIT_NAME.bak"
  echo "existing unit differs: saved it as $UNIT_DIR/$UNIT_NAME.bak"
fi
printf '%s\n' "$UNIT" > "$UNIT_DIR/$UNIT_NAME"
echo "wrote $UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"

# --- verify ------------------------------------------------------------------
# On failure, show the unit's state, then disable it so it does not crash-loop.
verify_failed() {
  echo "FAILED: $1" >&2
  systemctl --user status "$UNIT_NAME" --no-pager 2>&1 | tail -20 >&2 || true
  systemctl --user disable --now "$UNIT_NAME" >&2 || true
  echo "The unit was disabled and stopped. Fix the problem and re-run install.sh." >&2
  exit 1
}

code="000"
for _ in $(seq 1 10); do
  code="$(curl -s -o /dev/null -m 2 -w '%{http_code}' -H "Host: $BIND:$PORT" "http://$BIND:$PORT/" 2>/dev/null || true)"
  [ "$code" != "000" ] && [ -n "$code" ] && break
  sleep 1
done

if ! systemctl --user is-active --quiet "$UNIT_NAME"; then
  verify_failed "$UNIT_NAME is not active (last HTTP code: ${code:-none})"
fi
case "$code" in
  200)
    echo "ok: the viewer is up on $BIND:$PORT (HTTP 200)" ;;
  403)
    if [ -n "$TAILNET_USER" ]; then
      echo "ok: the viewer is up on $BIND:$PORT (HTTP 403: expected when this host is refused by the"
      echo "    --tailnet-user gate, for example a tagged server; other tailnet devices are admitted)"
    else
      verify_failed "HTTP 403 from $BIND:$PORT without --tailnet-user means a Host header mismatch (or another program owns the port)"
    fi ;;
  000|"")
    verify_failed "no response on $BIND:$PORT after 10 s" ;;
  *)
    verify_failed "unexpected HTTP $code from $BIND:$PORT" ;;
esac

cat <<NEXT

Next:
  Open the viewer:       http://$BIND:$PORT
  Follow its logs:       journalctl --user -u $UNIT_NAME -f
  Stay up after logout:  loginctl enable-linger "\$USER"
  Change options:        re-run install.sh with the new options (it restarts the service)

PDF export: $PDF_STATUS
NEXT
