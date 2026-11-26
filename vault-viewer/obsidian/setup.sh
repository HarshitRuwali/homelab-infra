#!/usr/bin/env bash
# Run the Obsidian app headless in a container and install the `obsidian`
# command, so agents and the vault viewer can use Obsidian's CLI (search,
# backlinks, properties, Bases) on a host with no screen. See ../docs/obsidian.md.
#
# Idempotent: re-running rebuilds the image if needed and recreates the container.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER="${OBSIDIAN_DOCKER:-docker}"   # test seam
NAME="obsidian"
BIN_DIR="$HOME/.local/bin"
MEMORY="1g"
DRY_RUN=0
REMOVE=0
VAULT=""

usage() {
  cat <<'USAGE'
usage: setup.sh [--name NAME] [--bin-dir DIR] [--memory SIZE] [--dry-run] VAULT
       setup.sh --remove [--name NAME] [--bin-dir DIR] [--dry-run]
       setup.sh -h | --help

Build the headless Obsidian image, run it as the container NAME with VAULT open,
and install the `obsidian` command into DIR.

  --name NAME     container name (default obsidian); the command reads it from
                  OBSIDIAN_CONTAINER when it is not the default
  --bin-dir DIR   where to install the `obsidian` command (default ~/.local/bin)
  --memory SIZE   container memory limit (default 1g)
  --remove        stop and remove the container and the command; keep the
                  image and the app's settings volumes
  --dry-run       print what would be done, change nothing
  -h, --help      show this help
USAGE
}
die_usage() { echo "setup.sh: $1" >&2; usage >&2; exit 2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --name|--bin-dir|--memory)
      [ "$#" -ge 2 ] || die_usage "$1 needs a value"
      case "$1" in
        --name) NAME="$2" ;;
        --bin-dir) BIN_DIR="$2" ;;
        --memory) MEMORY="$2" ;;
      esac
      shift 2 ;;
    --remove) REMOVE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -*) die_usage "unknown option: $1" ;;
    *) [ -z "$VAULT" ] || die_usage "only one VAULT"; VAULT="$1"; shift ;;
  esac
done
[[ "$NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || die_usage "bad --name: $NAME"
[[ "$MEMORY" =~ ^[0-9]+[kmg]?$ ]] || die_usage "bad --memory: $MEMORY"

run() { # print in a dry run, run otherwise
  if [ "$DRY_RUN" -eq 1 ]; then printf 'would run  %s\n' "$(printf '%q ' "$@")"; else "$@"; fi
}

if [ "$REMOVE" -eq 1 ]; then
  [ -z "$VAULT" ] || die_usage "--remove takes no VAULT"
  run "$DOCKER" rm -f "$NAME"
  run rm -f "$BIN_DIR/obsidian"
  echo "removed the $NAME container and $BIN_DIR/obsidian (image and volumes kept)"
  exit 0
fi

[ -n "$VAULT" ] || die_usage "VAULT is required"
[ -d "$VAULT" ] || { echo "setup.sh: not a directory: $VAULT" >&2; exit 1; }
VAULT="$(realpath "$VAULT")"
# --mount takes comma-separated fields, so a comma cannot be quoted in a path.
[[ "$VAULT" != *,* ]] || { echo "setup.sh: the vault path must not contain a comma: $VAULT" >&2; exit 1; }
command -v "$DOCKER" >/dev/null 2>&1 || { echo "setup.sh: $DOCKER not found" >&2; exit 1; }

VERSION="$(sed -n 's/^ARG OBSIDIAN_VERSION=//p' "$HERE/Dockerfile")"
[ -n "$VERSION" ] || { echo "setup.sh: no OBSIDIAN_VERSION in $HERE/Dockerfile" >&2; exit 1; }
IMAGE="obsidian-cli:$VERSION"
UID_GID="$(id -u):$(id -g)"
# The app's settings live in volumes, never in the vault: <vault>/.obsidian
# inside the container is a volume, so this host's app settings do not sync
# to other devices and theirs do not change this one. Docker needs the folder
# to exist as a mount point; create it as the vault owner rather than let
# docker create it as root.
HOME_VOL="$NAME-home"
CONFIG_VOL="$NAME-vault-config"

echo "Obsidian $VERSION, vault $VAULT, container $NAME"
[ -e "$VAULT/.obsidian" ] || run mkdir "$VAULT/.obsidian"
run "$DOCKER" build -q -t "$IMAGE" --build-arg "UID=$(id -u)" --build-arg "GID=$(id -g)" "$HERE"
run "$DOCKER" rm -f "$NAME"
# No network (the app needs none), no capabilities, read-only root, a memory
# and process cap; the CLI socket and display live in the /tmp tmpfs.
run "$DOCKER" run -d --name "$NAME" --restart unless-stopped --init \
  --network none --user "$UID_GID" --read-only \
  --tmpfs /tmp:mode=1777,size=256m --shm-size 256m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory "$MEMORY" --pids-limit 512 \
  -v "$HOME_VOL:/home/obsidian" \
  --mount "type=bind,src=$VAULT,dst=/vault" \
  -v "$CONFIG_VOL:/vault/.obsidian" \
  "$IMAGE"
run install -D -m 0755 "$HERE/obsidian" "$BIN_DIR/obsidian"
if [ "$NAME" != obsidian ]; then
  echo "note: the container is not called obsidian; export OBSIDIAN_CONTAINER=$NAME for the command"
fi
[ "$DRY_RUN" -eq 0 ] || exit 0

# Wait for the app to answer on its CLI socket.
export OBSIDIAN_CONTAINER="$NAME" OBSIDIAN_DOCKER="$DOCKER"
for _ in $(seq 1 90); do
  if "$BIN_DIR/obsidian" version >/dev/null 2>&1; then
    echo "ready: $("$BIN_DIR/obsidian" version 2>&1 | head -1)"
    echo
    echo "Next: let the vault viewer show Bases (re-run its installer with your other options plus):"
    printf '  %q ... --obsidian-cli %q --obsidian-vault %q\n' "$(dirname "$HERE")/install.sh" "$BIN_DIR/obsidian" "$VAULT"
    exit 0
  fi
  sleep 1
done
echo "setup.sh: the app did not answer within 90 s; see: $DOCKER logs $NAME" >&2
exit 1
