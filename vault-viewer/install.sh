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
PDF_IMAGE="artifacts-pdf:local"

usage() {
  cat <<'USAGE'
usage: install.sh --bind ADDR [--port N] [--allow-host NAME]... [--tailnet-user LOGIN]
                  [--title NAME] [--build-pdf-image] [--dry-run] ROOT...
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
  --build-pdf-image     build the PDF export image with docker (several minutes,
                        about 1 GB of downloads)
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
BUILD_PDF=0
DRY_RUN=0
ROOTS_IN=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --bind|--port|--allow-host|--tailnet-user|--title)
      [ "$#" -ge 2 ] || die_usage "$1 needs a value"
      no_control "$1 value" "$2"
      case "$1" in
        --bind) BIND="$2" ;;
        --port) PORT="$2" ;;
        --allow-host) ALLOW_HOSTS+=("$2") ;;
        --tailnet-user) TAILNET_USER="$2" ;;
        --title) TITLE="$2" ;;
      esac
      shift 2 ;;
    --build-pdf-image) BUILD_PDF=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; ROOTS_IN+=("$@"); break ;;
    -*) die_usage "unknown option: $1" ;;
    *) ROOTS_IN+=("$1"); shift ;;
  esac
done
for r in "${ROOTS_IN[@]+"${ROOTS_IN[@]}"}"; do no_control "ROOT" "$r"; done
no_control "ARTIFACTS_BIN" "$BIN"

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

# --- preflight ---------------------------------------------------------------
if ! "$PYTHON" -c 'import markdown' 2>/dev/null; then
  echo "warning: $PYTHON cannot import markdown, notes will render as plain text" >&2
  echo "         (Debian/Ubuntu: sudo apt install python3-markdown)" >&2
fi
if [ -n "$TAILNET_USER" ] && ! command -v tailscale >/dev/null 2>&1; then
  echo "install.sh: --tailnet-user needs the tailscale CLI, which is not on PATH" >&2
  exit 1
fi
if [ "$BUILD_PDF" -eq 1 ] && ! command -v docker >/dev/null 2>&1; then
  echo "install.sh: --build-pdf-image needs docker, which is not on PATH" >&2
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
for r in "${ROOTS[@]}"; do
  ARGS+=" $(unit_quote "$r")"
done

TEMPLATE="$(<"$HERE/systemd/$UNIT_NAME")"
UNIT="${TEMPLATE//__BIN__/"$(unit_quote "$BIN")"}"
UNIT="${UNIT//__ARGS__/"$ARGS"}"

if [ "$BUILD_PDF" -eq 1 ]; then
  PDF_PLAN="build the PDF image $PDF_IMAGE with docker (several minutes, about 1 GB of downloads)"
else
  PDF_PLAN="leave the PDF image alone (not building it)"
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
  echo "would check    http://$BIND:$PORT/ answers within 10 s"
  exit 0
fi

# --- install -----------------------------------------------------------------
# Build the PDF image first: if it fails, nothing else has been changed.
if [ "$BUILD_PDF" -eq 1 ]; then
  echo "building $PDF_IMAGE: this takes several minutes and downloads about 1 GB..."
  if ! "$PYTHON" "$HERE/artifacts.py" --build-pdf-image; then
    echo "install.sh: building $PDF_IMAGE failed; nothing was installed or changed." >&2
    echo "            Fix docker (is the daemon running, can you reach the registry?) and re-run." >&2
    exit 1
  fi
  PDF_STATUS="built: $PDF_IMAGE is ready, the PDF button works."
elif command -v docker >/dev/null 2>&1 && docker image inspect "$PDF_IMAGE" >/dev/null 2>&1; then
  PDF_STATUS="present: $PDF_IMAGE is ready, the PDF button works."
elif command -v docker >/dev/null 2>&1; then
  PDF_STATUS="missing: the PDF button will report that $PDF_IMAGE is missing until you build it
  (several minutes, about 1 GB of downloads):
    $PYTHON $BIN --build-pdf-image"
else
  PDF_STATUS="docker not found: the PDF button will report that it cannot run docker.
  Install docker, then build the image:
    $PYTHON $BIN --build-pdf-image"
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
