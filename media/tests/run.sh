#!/usr/bin/env bash
# Run every test for the media module. Needs docker with the Compose plugin and
# python3. Nothing here starts a container or touches /opt/media.
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

rc=0
for t in compose-test.sh docs-consistency-test.sh install-test.sh; do
  printf '\n##### %s\n' "$t"
  ./"$t" || rc=1
done
exit "$rc"
