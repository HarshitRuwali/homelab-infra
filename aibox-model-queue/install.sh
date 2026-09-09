#!/usr/bin/env bash
# Install and start the model queue as a systemd user unit.
#
# Idempotent: safe to re-run. Verifies the proxy answers before exiting.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="hermes-model-queue.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PYTHON="${MODEL_QUEUE_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
PORT="${MODEL_PROXY_PORT:-8099}"
UPSTREAM="${1:-${MODEL_PROXY_UPSTREAM:-http://10.10.50.122:8080}}"

if [ "$#" -gt 1 ]; then
  echo "usage: $0 [upstream-url]" >&2
  exit 2
fi

# Escape values used as sed replacement text so custom URLs remain literal.
UPSTREAM_ESCAPED="$(printf '%s' "$UPSTREAM" | sed 's/[&|\\]/\\&/g')"

[ -x "$PYTHON" ] || { echo "python not found: $PYTHON" >&2; exit 1; }
"$PYTHON" -c 'import aiohttp' 2>/dev/null || {
  echo "aiohttp missing from $PYTHON" >&2; exit 1; }

mkdir -p "$UNIT_DIR"
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__HERE__#$HERE#g" \
  -e "s#__UPSTREAM__#$UPSTREAM_ESCAPED#g" \
    "$HERE/systemd/$UNIT_NAME" > "$UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

for _ in $(seq 1 20); do
  if curl -fsS -m 2 "http://127.0.0.1:${PORT}/_proxy/status" >/dev/null 2>&1; then
    echo "ok: model queue answering on 127.0.0.1:${PORT}"
    curl -s "http://127.0.0.1:${PORT}/_proxy/status"; echo
    echo
    echo "Next: point clients at  http://127.0.0.1:${PORT}/v1"
    exit 0
  fi
  sleep 1
done

echo "FAILED: no response on 127.0.0.1:${PORT} after 20s" >&2
systemctl --user status "$UNIT_NAME" --no-pager | tail -20 >&2
exit 1
