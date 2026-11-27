#!/usr/bin/env bash
# One-line installer: fetch this repository and run homelab.sh with the given
# arguments. Needs only bash, curl and tar; no git, no clone left behind.
#
#   curl -fsSL https://raw.githubusercontent.com/HarshitRuwali/homelab-infra/master/get.sh | bash -s -- list
#
#   # on the server itself (HOST "local"), dry run, then install:
#   curl -fsSL https://raw.githubusercontent.com/HarshitRuwali/homelab-infra/master/get.sh | sudo bash -s -- install media local
#   curl -fsSL https://raw.githubusercontent.com/HarshitRuwali/homelab-infra/master/get.sh | sudo bash -s -- install media local --apply
#
#   # or from any machine, to another host over ssh:
#   curl -fsSL https://raw.githubusercontent.com/HarshitRuwali/homelab-infra/master/get.sh | bash -s -- install media root@media-host
#
# Environment:
#   HOMELAB_REF   branch, tag or commit to fetch (default master); pin a tag or
#                 commit to know exactly what runs
#   HOMELAB_DIR   keep the checkout in this directory (replaced on each run)
#                 instead of a temporary one removed at exit
#   HOMELAB_REPO  owner/name on GitHub (default HarshitRuwali/homelab-infra)
#
# Without arguments it prints homelab.sh's help.

# Everything is inside main, called on the last line, so bash has read the
# whole script before anything runs: piped into bash, a partial download runs
# nothing, and the commands below cannot read the rest of the script as input.
main() {
  set -euo pipefail
  local repo="${HOMELAB_REPO:-HarshitRuwali/homelab-infra}" ref="${HOMELAB_REF:-master}" dir
  local t
  for t in curl tar; do
    command -v "$t" >/dev/null 2>&1 || { echo "get.sh: $t is required" >&2; return 1; }
  done
  case "$repo$ref" in
    *[!A-Za-z0-9._/-]*) echo "get.sh: HOMELAB_REPO and HOMELAB_REF may only hold letters, digits and . _ / -" >&2; return 2 ;;
  esac
  if [ -n "${HOMELAB_DIR:-}" ]; then
    dir="$HOMELAB_DIR"
    rm -rf "${dir:?}.new"; mkdir -p "$dir.new"
  else
    GET_TMP="$(mktemp -d)"   # global: the EXIT trap runs after main returns
    trap 'rm -rf "$GET_TMP"' EXIT
    dir="$GET_TMP/homelab-infra"; mkdir -p "$dir.new"
  fi
  local url="https://codeload.github.com/$repo/tar.gz/$ref"
  echo "get.sh: fetching $repo at $ref" >&2
  if ! curl -fsSL --proto '=https' --tlsv1.2 "$url" | tar -xz -C "$dir.new" --strip-components=1; then
    echo "get.sh: could not download $url" >&2
    rm -rf "$dir.new"
    return 1
  fi
  [ -x "$dir.new/homelab.sh" ] || { echo "get.sh: $repo at $ref has no homelab.sh" >&2; rm -rf "$dir.new"; return 1; }
  rm -rf "$dir"; mv "$dir.new" "$dir"
  [ -z "${HOMELAB_DIR:-}" ] || echo "get.sh: checkout kept in $dir" >&2
  if [ "$#" -eq 0 ]; then set -- --help; fi
  # stdin is this script when piped into bash: give homelab.sh none.
  "$dir/homelab.sh" "$@" </dev/null
}

main "$@"
