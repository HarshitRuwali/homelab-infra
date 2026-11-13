#!/usr/bin/env bash
#
# compose-test.sh - render compose.yaml and assert the properties that keep the
# stack safe. Needs docker with the Compose plugin and python3; it starts no
# container and touches nothing outside a temporary directory.
#
#   ./tests/compose-test.sh
#
# The compose file reads two secrets from MEDIA_DIR/secrets, which do not exist
# in a checkout, so the test renders against a scratch MEDIA_DIR holding empty
# stand-ins (the same trick install.sh uses to validate).
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0
ok() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

MEDIA_COMPOSE_FILE="${MEDIA_COMPOSE_FILE:-compose.yaml}"
MEDIA_ENV_EXAMPLE="${MEDIA_ENV_EXAMPLE:-.env.example}"

scratch="$(mktemp -d)"; trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/secrets"
: > "$scratch/secrets/plex-tunnel.env"
: > "$scratch/secrets/wireguard_private_key"
sed "s#^MEDIA_DIR=.*#MEDIA_DIR=$scratch#" "$MEDIA_ENV_EXAMPLE" > "$scratch/.env"

compose() {
  docker compose --project-directory "$scratch" -f "$MEDIA_COMPOSE_FILE" \
    --env-file "$scratch/.env" "$@"
}

echo "== the file renders =="
if compose config -q 2>"$scratch/err"; then
  ok "docker compose config -q accepts $MEDIA_COMPOSE_FILE"
else
  no "docker compose config -q rejects $MEDIA_COMPOSE_FILE: $(head -3 "$scratch/err" | tr '\n' ' ')"
fi

rendered="$scratch/rendered.json"
if ! compose config --format json > "$rendered" 2>"$scratch/err"; then
  no "could not render JSON: $(head -3 "$scratch/err" | tr '\n' ' ')"
  printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
  exit 1
fi

# One python call prints a line per finding: "PASS <text>" or "FAIL <text>".
echo
echo "== exposure, isolation, limits and logs of the rendered stack =="
while IFS= read -r line; do
  case "$line" in
    PASS\ *) ok "${line#PASS }" ;;
    FAIL\ *) no "${line#FAIL }" ;;
  esac
done < <(python3 -I - "$rendered" <<'PY'
import json
import sys

cfg = json.load(open(sys.argv[1]))
services = cfg["services"]


def say(ok, text):
    print(("PASS " if ok else "FAIL ") + text)


# 1. No service publishes a port except on 127.0.0.1. A port with no host_ip
#    binds 0.0.0.0, which is what this rule exists to catch.
published = []
bad = []
for name, svc in sorted(services.items()):
    for port in svc.get("ports", []):
        published.append((name, port))
        if port.get("host_ip") != "127.0.0.1":
            bad.append(f"{name} publishes {port.get('published')} on "
                       f"{port.get('host_ip') or '0.0.0.0 (no host_ip)'}")
for item in bad:
    say(False, item)
if not bad:
    say(True, f"every published port ({len(published)}) is bound to 127.0.0.1")

# 2. Only gluetun owns published ports: qbittorrent shares its namespace, and
#    the two host-network services must not also publish anything.
owners = sorted({name for name, _ in published})
say(owners == ["gluetun"], f"ports are published by gluetun only (found: {owners})")

# 3. qbittorrent has no network of its own.
mode = services["qbittorrent"].get("network_mode")
say(mode == "service:gluetun", f"qbittorrent network_mode is service:gluetun (found: {mode!r})")
say("networks" not in services["qbittorrent"],
    "qbittorrent declares no networks of its own")

# 4. gluetun gets the WireGuard key as a compose secret, not an env value.
secrets = [s.get("source") for s in services["gluetun"].get("secrets", [])]
say("wireguard_private_key" in secrets,
    f"gluetun has the wireguard_private_key secret (found: {secrets})")
env = services["gluetun"].get("environment", {})
say("WIREGUARD_PRIVATE_KEY" not in env,
    "gluetun is not given the key as an environment value")
say(env.get("WIREGUARD_PRIVATE_KEY_SECRETFILE") == "/run/secrets/wireguard_private_key",
    "gluetun reads the key from /run/secrets/wireguard_private_key")

# 5. The host-network services are exactly plex and plex-tunnel.
host = sorted(n for n, s in services.items() if s.get("network_mode") == "host")
say(host == ["plex", "plex-tunnel"], f"host network is used by plex and plex-tunnel only (found: {host})")

# 6. Every service has a memory cap and rotated json-file logs. The host has no
#    swap, so an uncapped container can take RAM from its neighbours.
for name, svc in sorted(services.items()):
    mem = svc.get("mem_limit")
    say(bool(mem) and str(mem) not in ("0", "0b"),
        f"{name} has a memory limit (found: {mem!r})")
    log = svc.get("logging") or {}
    opts = log.get("options") or {}
    say(log.get("driver") == "json-file" and bool(opts.get("max-size")) and bool(opts.get("max-file")),
        f"{name} rotates its logs (json-file with max-size and max-file; found: {log!r})")

# 7. The two services that had no health state have a healthcheck.
for name in ("qbittorrent", "plex-tunnel"):
    hc = services[name].get("healthcheck") or {}
    say(bool(hc.get("test")) and not hc.get("disable"), f"{name} has a healthcheck")

# 8. plex-tunnel shares the host network, so its metrics endpoint must stay on
#    loopback (cloudflared's default is 0.0.0.0:20241).
metrics = services["plex-tunnel"].get("environment", {}).get("TUNNEL_METRICS", "")
say(metrics.startswith("127.0.0.1:"), f"plex-tunnel metrics listen on loopback (found: {metrics!r})")
PY
)

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
