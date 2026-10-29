#!/usr/bin/env bash
# Run every test for the ingress module. Needs docker for the Caddy, dnsmasq,
# access and installer checks (each is skipped with a reason without it).
# Nothing here touches /etc/ingress or a running service.
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || exit 1

rc=0
for t in render-test.sh verify-target-test.sh tailnet-test.sh install-test.sh docs-consistency-test.sh; do
  printf '\n##### %s\n' "$t"
  ./"$t" || rc=1
done
exit "$rc"
