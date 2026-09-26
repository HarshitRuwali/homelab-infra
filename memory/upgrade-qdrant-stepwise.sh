#!/usr/bin/env bash
# Supervised Qdrant upgrade, one minor release at a time, ending on :latest.
#
# Usage: upgrade-qdrant-stepwise.sh COMPOSE_FILE [TAG...]
#   upgrade-qdrant-stepwise.sh /root/memory/docker-compose.yml v1.20.3 v1.21.1
#
# Qdrant migrates its storage on startup and supports only adjacent minor
# versions, so a server more than one minor behind :latest must pass through
# each missed minor. List them oldest first, using the newest patch of each
# (https://github.com/qdrant/qdrant/releases). With no tags it only moves a
# server that is already within one minor of :latest. The nightly
# fleet-docker-update refuses bigger jumps and points here.
#
# Needs a collection snapshot copied outside the Docker volume within the last
# day, in $BACKUP_DIR.
set -euo pipefail

compose=${1:?usage: $0 COMPOSE_FILE [TAG...]}
shift
backup_dir=${BACKUP_DIR:-/root/qdrant-upgrade-backup}
qdrant_url=${QDRANT_URL:-http://127.0.0.1:6333}

if [[ -z "$(find "$backup_dir" -maxdepth 1 -name '*.snapshot' -type f -mmin -1440 2>/dev/null)" ]]; then
  echo "No Qdrant snapshot from the last day in $backup_dir; take one and copy it out of the volume first" >&2
  exit 1
fi

version() {
  curl -fsS --max-time 5 "$qdrant_url/" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'
}

# Total points across all collections; fails unless every collection is green.
points() {
  python3 - "$qdrant_url" <<'EOF'
import json, sys, urllib.request
base = sys.argv[1]
get = lambda p: json.load(urllib.request.urlopen(base + p, timeout=5))["result"]
total = 0
for c in get("/collections")["collections"]:
    r = get("/collections/" + c["name"])
    assert r["status"] == "green", c["name"] + " is " + r["status"]
    total += r["points_count"] or 0
print(total)
EOF
}

minor_step_ok() {
  local re='^v?([0-9]+)\.([0-9]+)\.[0-9]+$' cmaj cmin
  [[ $1 =~ $re ]] || return 1
  cmaj=${BASH_REMATCH[1]} cmin=${BASH_REMATCH[2]}
  [[ $2 =~ $re ]] || return 1
  [[ ${BASH_REMATCH[1]} == "$cmaj" ]] &&
    (( BASH_REMATCH[2] >= cmin && BASH_REMATCH[2] <= cmin + 1 ))
}

set_image() {
  sed -i -E "s#^    image: qdrant/qdrant:.*#    image: qdrant/qdrant:$1#" "$compose"
}

# Recreate qdrant on the image now in the compose file and wait until it
# reports version $1 with every collection green and no points lost.
apply() {
  docker compose -f "$compose" up -d --no-deps qdrant
  local current count
  for _ in $(seq 1 60); do
    if current=$(version 2>/dev/null) && [[ "$current" == "${1#v}" ]] &&
      count=$(points 2>/dev/null) && (( count >= initial_points )); then
      echo "Qdrant $current healthy with $count points"
      return 0
    fi
    sleep 5
  done
  echo "Qdrant $1 did not become healthy; stopped before the next step" >&2
  exit 1
}

current=$(version)
initial_points=$(points)
echo "Starting from Qdrant $current with $initial_points points"

# Check the whole plan before touching anything.
previous=$current
for tag in "$@"; do
  if ! minor_step_ok "$previous" "$tag"; then
    echo "$previous to $tag is not a single minor step; list every minor in between" >&2
    exit 1
  fi
  previous=$tag
done

for tag in "$@"; do
  echo "Upgrading Qdrant to $tag"
  set_image "$tag"
  docker compose -f "$compose" pull qdrant
  apply "$tag"
done

set_image latest
docker compose -f "$compose" pull qdrant
latest=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' qdrant/qdrant:latest)
if ! minor_step_ok "$previous" "$latest"; then
  echo "Now on $previous, but :latest is $latest; rerun with the minors in between" >&2
  set_image "$previous"
  exit 1
fi
apply "$latest"
echo "Qdrant follows latest; running $(version), points $(points)"
