#!/usr/bin/env bash
# Install and start the model queue as a systemd user unit.
#
# Copies exactly the files in this directory that the service runs (proxy.py,
# __init__.py, plugin.yaml, systemd/) into the install directory, writes the unit
# pointing at that copy, and starts it. Idempotent: safe to re-run. Verifies the
# proxy answers before exiting.
#
# Environment:
#   MODEL_QUEUE_INSTALL_DIR  where the code is installed
#                            (default ~/.hermes/plugins/aibox-model-queue, which is also
#                            where Hermes loads it as a plugin)
#   MODEL_QUEUE_PYTHON       interpreter with aiohttp installed
#   MODEL_PROXY_HOST         comma-separated bind addresses (default: the value in the
#                            already installed unit, else 127.0.0.1)
#   MODEL_PROXY_PORT         bind port (default: the installed unit's, else 8099)
#   MODEL_PROXY_UPSTREAM     upstream URL when no argument is given
#                            (default http://10.10.50.122:8090, llama-server)
# A bearer token goes in ~/.config/hermes-model-queue.env (MODEL_PROXY_TOKEN=...),
# which the unit reads when present; see README.md.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="hermes-model-queue.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PYTHON="${MODEL_QUEUE_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
# When MODEL_PROXY_HOST / MODEL_PROXY_PORT are unset, keep the values of an already
# rendered unit so a re-run never narrows a bind chosen earlier (for example the tailnet
# address). Only a fresh install falls back to 127.0.0.1:8099.
EXISTING_UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT_NAME"
unit_value() {
  [ -f "$EXISTING_UNIT" ] || return 0
  sed -n "s/^Environment=$1=//p" "$EXISTING_UNIT" | head -n 1
}
PORT="${MODEL_PROXY_PORT:-$(unit_value MODEL_PROXY_PORT)}"
PORT="${PORT:-8099}"
HOST="${MODEL_PROXY_HOST:-$(unit_value MODEL_PROXY_HOST)}"
HOST="${HOST:-127.0.0.1}"
INSTALL_DIR="${MODEL_QUEUE_INSTALL_DIR:-$HOME/.hermes/plugins/aibox-model-queue}"
UPSTREAM="${1:-${MODEL_PROXY_UPSTREAM:-http://10.10.50.122:8090}}"

if [ "$#" -gt 1 ]; then
  echo "usage: $0 [upstream-url]" >&2
  exit 2
fi

# Escape values used as sed replacement text so custom URLs remain literal.
UPSTREAM_ESCAPED="$(printf '%s' "$UPSTREAM" | sed 's/[&|\\]/\\&/g')"

[ -x "$PYTHON" ] || { echo "python not found: $PYTHON" >&2; exit 1; }
"$PYTHON" -c 'import aiohttp' 2>/dev/null || {
  echo "aiohttp missing from $PYTHON" >&2; exit 1; }

mkdir -p "$UNIT_DIR" "$INSTALL_DIR/systemd"
if [ "$HERE" != "$INSTALL_DIR" ]; then
  install -m 755 "$HERE/proxy.py" "$INSTALL_DIR/proxy.py"
  install -m 644 "$HERE/__init__.py" "$INSTALL_DIR/__init__.py"
  install -m 644 "$HERE/plugin.yaml" "$INSTALL_DIR/plugin.yaml"
  install -m 644 "$HERE/systemd/$UNIT_NAME" "$INSTALL_DIR/systemd/$UNIT_NAME"
fi
sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" \
  -e "s#__UPSTREAM__#$UPSTREAM_ESCAPED#g" -e "s#__HOST__#$HOST#g" \
  -e "s#__PORT__#$PORT#g" \
    "$HERE/systemd/$UNIT_NAME" > "$UNIT_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user restart "$UNIT_NAME"

# Probe every bound address: 0.0.0.0 and :: answer on loopback.
IFS=',' read -r -a BIND_LIST <<< "$HOST"
probe() {
  curl -s -m 2 -o /dev/null -w '%{http_code}' "http://$1:${PORT}/_proxy/status" || true
}
addr_for_probe() {
  case "$1" in 0.0.0.0|"") echo 127.0.0.1 ;; ::|"[::]") echo "[::1]" ;; *:*) echo "[$1]" ;; *) echo "$1" ;; esac
}
BAD=""
for _ in $(seq 1 20); do
  BAD=""
  CODE=""
  for raw in "${BIND_LIST[@]}"; do
    a="$(addr_for_probe "$(echo "$raw" | tr -d ' ')")"
    c="$(probe "$a")"
    # 200 = answering; 401 = answering and a bearer token is configured.
    if [ "$c" != "200" ] && [ "$c" != "401" ]; then BAD="$BAD $a"; else CODE="${CODE:-$c}"; fi
  done
  if [ -z "$BAD" ]; then
    echo "ok: model queue answering on ${HOST} port ${PORT} (HTTP ${CODE}), code in ${INSTALL_DIR}"
    first="$(addr_for_probe "$(echo "${BIND_LIST[0]}" | tr -d ' ')")"
    [ "$CODE" = "200" ] && { curl -s "http://${first}:${PORT}/_proxy/status"; echo; }
    echo
    echo "Next: point clients at  http://${first}:${PORT}/v1"
    exit 0
  fi
  sleep 1
done

echo "FAILED: no response on${BAD} port ${PORT} after 20s" >&2
systemctl --user status "$UNIT_NAME" --no-pager | tail -20 >&2
exit 1
