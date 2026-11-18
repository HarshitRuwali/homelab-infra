#!/usr/bin/env bash
#
# e2e.sh - load the viewer page in headless Chromium, from the Playwright
# container (nothing is installed on the host).
#
#   ./tests/e2e.sh                           # page checks against a scratch vault
#   ./tests/e2e.sh --bench URL NOTE [RUNS]   # cold-load timings of a running viewer
#
# Checks: the page loads with the note path in the URL hash written with plain
# or %2F-encoded slashes, says it is loading until the file list arrives, and
# shows a refused file list and retries on its own.
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
# program. Needs docker; the first run downloads the image and the Playwright
# package (into ~/.cache/vault-viewer-e2e).
set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."

IMAGE=mcr.microsoft.com/playwright/python:v1.49.0-noble
PW_VERSION=1.49.0   # must match the image's browsers
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/vault-viewer-e2e"
PROGRAM="${ARTIFACTS_PY:-artifacts.py}"

usage() { sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//'; }

in_browser() {
  mkdir -p "$CACHE"
  docker run --rm --network host --ipc host --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD/tests/e2e_page.py:/e2e/e2e_page.py:ro" -v "$CACHE:/cache" "$IMAGE" sh -c '
      [ -f /cache/pw-'"$PW_VERSION"'/playwright/__init__.py ] ||
        pip install -q --disable-pip-version-check --target /cache/pw-'"$PW_VERSION"' playwright=='"$PW_VERSION"' >&2
      cd /tmp && PYTHONPATH=/cache/pw-'"$PW_VERSION"' exec python3 /e2e/e2e_page.py "$@"' sh "$@"
}

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
