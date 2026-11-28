#!/usr/bin/env bash
#
# e2e.sh - load the viewer page in Chromium's headless shell, driven over the
# DevTools pipe (no docker, no Playwright).
#
#   ./tests/e2e.sh                           # page checks against a scratch vault
#   ./tests/e2e.sh --bench URL NOTE [RUNS]   # cold-load timings of a running viewer
#
# Checks: the page loads with the note path in the URL hash written with plain
# or %2F-encoded slashes, says it is loading until the file list arrives, and
# shows a refused file list and retries on its own; back from an image or a
# linked note returns to the note where it was scrolled; the Obsidian button,
# the per-device "open links in Obsidian" setting and its fallback to this page.
#
# Bench: opens NOTE (a vault id such as personal/agent-notes/x.md) in a fresh
# browser context RUNS times (default 3) per hash form and prints one JSON line
# per run: first paint, file list, note title and note text, in ms from
# navigation. The tailnet gate refuses the host's own address, so to time the
# installed viewer on its host, start a loopback copy over the same vault:
#   python3 ~/.local/bin/artifacts --port 7778 ~/obsidian &
#   ./tests/e2e.sh --bench http://127.0.0.1:7778 obsidian/some/note.md
#
# ARTIFACTS_PY=/path/to/copy.py runs the checks against another copy of the
# program. Needs the PDF runtime (python3 artifacts.py --install-pdf-runtime):
# the browser is E2E_CHROME if set, else the runtime's chrome/chrome-headless-shell
# under ARTIFACTS_PDF_RUNTIME (default ${XDG_DATA_HOME:-~/.local/share}/artifacts/pdf-runtime).
# It runs with Chromium's sandbox on, so the AppArmor profile must allow it.
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."

PROGRAM="${ARTIFACTS_PY:-artifacts.py}"

usage() { sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//'; }

in_browser() { PYTHONDONTWRITEBYTECODE=1 python3 tests/e2e_page.py "$@"; }

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --bench)
    [[ $# -ge 3 ]] || { usage >&2; exit 2; }
    in_browser bench "$2" "$3" "${4:-3}"
    exit $? ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

TMP="$(mktemp -d)"
SERVER=""
cleanup() { [[ -n "$SERVER" ]] && kill "$SERVER" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

mkdir -p "$TMP/vault/sub"
printf '# README\n\nHome note.\n' > "$TMP/vault/README.md"
printf '# Deep Note\n\nA note in a subfolder, with a space in its name.\n' > "$TMP/vault/sub/Deep Note.md"
printf '# Fresh\n\nJust written.\n' > "$TMP/vault/sub/Fresh.md"
printf '<!doctype html><title>Art</title><p>An artifact.</p>\n' > "$TMP/vault/art.html"
# A long note with an image and a link half way down, for the back-keeps-the-scroll checks.
python3 -c 'import base64, sys; open(sys.argv[1], "wb").write(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))' "$TMP/vault/pic.png"
{ printf '# Long\n\n'; for i in $(seq 80); do printf 'Paragraph %s.\n\n' "$i"; done
  printf '![[pic.png]]\n\n[[README]]\n\n'; for i in $(seq 81 160); do printf 'Paragraph %s.\n\n' "$i"; done; } > "$TMP/vault/Long.md"
# Older than the viewer's 10-minute "may not have synced yet" window; Fresh.md is inside it.
touch -d '1 hour ago' "$TMP/vault/README.md" "$TMP/vault/sub/Deep Note.md"

PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
python3 "$PROGRAM" --bind 127.0.0.1 --port "$PORT" "$TMP/vault" 2>"$TMP/server.log" &
SERVER=$!
for _ in $(seq 50); do
  curl -fs -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 0.1
done
curl -fs -o /dev/null "http://127.0.0.1:$PORT/" || { echo "server did not start:"; cat "$TMP/server.log"; exit 1; }

echo "program under test: $PROGRAM"
in_browser check "http://127.0.0.1:$PORT"
