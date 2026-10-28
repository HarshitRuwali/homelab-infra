#!/usr/bin/env bash
# Install or update the media stack (Plex, qBittorrent behind gluetun) on the
# host it runs on. Dry run by default: prints every action and changes nothing.
#
# Idempotent. Re-run it after editing compose.yaml or bumping an image. It
# never overwrites MEDIA_DIR/.env or anything under MEDIA_DIR/secrets, and it
# only starts a service whose secret is in place, so Plex can go live before
# the VPN key exists. See usage() below.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLY=0
TAILSCALE=1

usage() {
  cat <<'USAGE'
usage: install.sh [--apply] [--no-tailscale-serve]
       install.sh -h | --help

Copy compose.yaml into MEDIA_DIR (default /opt/media), create the config,
transcode and secrets directories, validate the stack, pull and start it.

  --apply               do it; without this flag nothing is changed
  --no-tailscale-serve  do not publish the qBittorrent WebUI on the tailnet
                        with `tailscale serve`, and remove an existing serve
                        for its port
  -h, --help            show this help

Run on the target host as root or as a user with sudo. Settings come from
MEDIA_DIR/.env, created from .env.example on the first run. MEDIA_DIR itself is
chosen at install time (`MEDIA_DIR=/path ./install.sh --apply`) and must not be
edited in the installed .env afterwards. Secrets:
  MEDIA_DIR/secrets/plex-tunnel.env          TUNNEL_TOKEN=... (plex-tunnel)
  MEDIA_DIR/secrets/wireguard_private_key    Proton WireGuard key (gluetun)
USAGE
}

while (($#)); do
  case "$1" in
    --apply) APPLY=1 ;;
    --no-tailscale-serve) TAILSCALE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

SUDO=(); (( EUID == 0 )) || SUDO=(sudo)
say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '  \033[31mfail\033[0m %s\n' "$*" >&2; exit 1; }
run()  { # run <cmd...>: echo it, and execute only with --apply
  printf '       + %s\n' "$*"
  (( APPLY )) && "$@"
  return 0
}
run_try() { # like run, but a failing command is reported and ignored
  printf '       + %s\n' "$*"
  if (( APPLY )); then "$@" || warn "ignored: command failed"; fi
  return 0
}

# Settings: an existing MEDIA_DIR/.env wins over the example.
MEDIA_DIR_DEFAULT="$(sed -n 's/^MEDIA_DIR=//p' "$HERE/.env.example")"
MEDIA_DIR="${MEDIA_DIR:-$MEDIA_DIR_DEFAULT}"
ENV_FILE="$MEDIA_DIR/.env"
[[ -f "$ENV_FILE" ]] && SRC_ENV="$ENV_FILE" || SRC_ENV="$HERE/.env.example"
# compose interpolates MEDIA_DIR from the installed .env, this script from the
# environment or .env.example. They must agree, so MEDIA_DIR is install-time only.
if [[ -f "$ENV_FILE" ]]; then
  installed_dir="$(sed -n 's/^MEDIA_DIR=//p' "$ENV_FILE" | tail -1)"
  [[ "$installed_dir" == "$MEDIA_DIR" ]] \
    || die "MEDIA_DIR is '$MEDIA_DIR' but $ENV_FILE says '${installed_dir:-<unset>}'. MEDIA_DIR is chosen at install time and is not edited in the installed .env: restore that line, or reinstall elsewhere with MEDIA_DIR=/path ./install.sh --apply"
fi
getenv() { sed -n "s/^$1=//p" "$SRC_ENV" | tail -1; }
MEDIA_ROOT="$(getenv MEDIA_ROOT)"
MEDIA_UID="$(getenv MEDIA_UID)"
MEDIA_GID="$(getenv MEDIA_GID)"
QBT_WEBUI_PORT="$(getenv QBT_WEBUI_PORT)"
SECRETS="$MEDIA_DIR/secrets"

say "MODE: $( (( APPLY )) && echo APPLY || echo 'DRY RUN, add --apply to execute' )"
say "preflight"
command -v docker >/dev/null || die "docker is not installed"
"${SUDO[@]}" docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
ok "docker compose $("${SUDO[@]}" docker compose version --short)"
findmnt -n "$MEDIA_ROOT" >/dev/null || die "$MEDIA_ROOT is not mounted"
[[ -d "$MEDIA_ROOT/Torrent" ]] || die "$MEDIA_ROOT/Torrent does not exist"
ok "media share $MEDIA_ROOT mounted"
[[ -e /dev/net/tun ]] || die "/dev/net/tun is missing (gluetun needs it)"
if ss -ltnH "sport = :$QBT_WEBUI_PORT" | grep -q . \
   && ! "${SUDO[@]}" docker ps --format '{{.Names}}' | grep -qx gluetun; then
  die "port $QBT_WEBUI_PORT is already taken by something other than this stack"
fi
ok "WebUI port $QBT_WEBUI_PORT free or ours"

PLEX_READY=0; VPN_READY=0
"${SUDO[@]}" test -s "$SECRETS/plex-tunnel.env" && PLEX_READY=1
"${SUDO[@]}" test -s "$SECRETS/wireguard_private_key" && VPN_READY=1
if (( PLEX_READY )); then
  ok "secret plex-tunnel.env present"
else
  warn "no $SECRETS/plex-tunnel.env: plex starts, plex-tunnel does not"
fi
if (( VPN_READY )); then
  ok "secret wireguard_private_key present"
else
  warn "no $SECRETS/wireguard_private_key: gluetun and qbittorrent do not start"
fi

SERVICES=(plex)
(( PLEX_READY )) && SERVICES+=(plex-tunnel)
(( VPN_READY )) && SERVICES+=(gluetun qbittorrent)

say "validate"
# Before anything is installed, so a broken compose.yaml never replaces the
# installed copy. `compose config` fails when a referenced file is missing, and
# the secrets may legitimately not exist yet. Validate against a scratch
# MEDIA_DIR holding empty stand-ins, so the file itself is always checked in full.
scratch="$(mktemp -d)"; trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/secrets"; : > "$scratch/secrets/plex-tunnel.env"; : > "$scratch/secrets/wireguard_private_key"
sed "s#^MEDIA_DIR=.*#MEDIA_DIR=$scratch#" "$SRC_ENV" > "$scratch/.env"
docker compose --project-directory "$scratch" -f "$HERE/compose.yaml" --env-file "$scratch/.env" config -q \
  || die "compose.yaml does not validate"
ok "compose file validates"

say "directories"
run "${SUDO[@]}" install -d -m 0755 "$MEDIA_DIR"
run "${SUDO[@]}" install -d -m 0700 -o root -g root "$SECRETS"
for d in config/plex config/qbittorrent transcode; do
  run "${SUDO[@]}" install -d -m 0750 -o "$MEDIA_UID" -g "$MEDIA_GID" "$MEDIA_DIR/$d"
done

say "files"
run "${SUDO[@]}" install -m 0644 "$HERE/compose.yaml" "$MEDIA_DIR/compose.yaml"
if [[ -f "$ENV_FILE" ]]; then
  ok "$ENV_FILE kept as is"
else
  run "${SUDO[@]}" install -m 0644 "$HERE/.env.example" "$ENV_FILE"
fi

# The installed copy, in both modes, so a dry run shows exactly what --apply runs.
COMPOSE=("${SUDO[@]}" docker compose --project-directory "$MEDIA_DIR" -f "$MEDIA_DIR/compose.yaml" --env-file "$ENV_FILE")

say "qbittorrent config"
# gluetun's port-forward hook calls the WebUI API on 127.0.0.1 without a login,
# so a fresh install needs LocalHostAuth off. An existing file is never touched:
# a migrated one is adapted by hand (docs/migration.md).
QBT_CONF_DIR="$MEDIA_DIR/config/qbittorrent/qBittorrent"
QBT_CONF="$QBT_CONF_DIR/qBittorrent.conf"
if "${SUDO[@]}" test -e "$QBT_CONF"; then
  ok "$QBT_CONF kept as is"
else
  printf '%s\n' '[Preferences]' "WebUI\\Port=$QBT_WEBUI_PORT" 'WebUI\LocalHostAuth=false' > "$scratch/qBittorrent.conf"
  run "${SUDO[@]}" install -d -m 0750 -o "$MEDIA_UID" -g "$MEDIA_GID" "$QBT_CONF_DIR"
  run "${SUDO[@]}" install -m 0640 -o "$MEDIA_UID" -g "$MEDIA_GID" "$scratch/qBittorrent.conf" "$QBT_CONF"
fi

say "start: ${SERVICES[*]}"
run "${COMPOSE[@]}" pull -q "${SERVICES[@]}"
run "${COMPOSE[@]}" up -d "${SERVICES[@]}"

if (( TAILSCALE )); then
  if (( VPN_READY )); then
    say "tailnet"
    if command -v tailscale >/dev/null; then
      run "${SUDO[@]}" tailscale serve --bg --tcp "$QBT_WEBUI_PORT" "tcp://127.0.0.1:$QBT_WEBUI_PORT"
    else
      warn "tailscale is not installed: the WebUI stays on 127.0.0.1 only"
    fi
  fi
elif command -v tailscale >/dev/null; then
  say "tailnet"
  run_try "${SUDO[@]}" tailscale serve --tcp "$QBT_WEBUI_PORT" off
fi

(( APPLY )) || { say ""; say "This was a dry run. Re-run with --apply."; exit 0; }

say "verify"
for _ in $(seq 60); do
  curl -fs -o /dev/null http://127.0.0.1:32400/identity 2>/dev/null && break
  sleep 2
done
if curl -fsS -o /dev/null http://127.0.0.1:32400/identity; then
  ok "plex answers on :32400"
else
  die "plex does not answer on 127.0.0.1:32400"
fi
if (( VPN_READY )); then
  for _ in $(seq 60); do
    [[ "$("${SUDO[@]}" docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null)" == healthy ]] && break; sleep 3
  done
  # IPv4 on both sides: busybox wget in gluetun has no -4, so use an IPv4-only endpoint.
  vpn_ip="$("${SUDO[@]}" docker exec gluetun wget -qO- https://ipv4.icanhazip.com 2>/dev/null | tr -d '[:space:]' || true)"
  host_ip="$(curl -4 -fsS -m 10 https://ipv4.icanhazip.com 2>/dev/null | tr -d '[:space:]' || true)"
  [[ -n "$host_ip" ]] || die "cannot determine the host's public IPv4 address, so the VPN exit cannot be checked"
  if [[ -n "$vpn_ip" && "$vpn_ip" != "$host_ip" ]]; then
    ok "torrent traffic exits via the VPN, not the host address"
  else
    die "gluetun has no VPN exit address distinct from the host's"
  fi
  code=000
  for _ in $(seq 60); do
    code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$QBT_WEBUI_PORT/" || true)"
    [[ "$code" == 200 ]] && break; sleep 2
  done
  if [[ "$code" == 200 ]]; then
    ok "qBittorrent WebUI answers on 127.0.0.1:$QBT_WEBUI_PORT"
  else
    die "qBittorrent WebUI answered $code"
  fi
fi
say "done"
