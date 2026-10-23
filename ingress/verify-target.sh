#!/usr/bin/env bash
# Choose the site and address install.sh checks over https after an apply.
#
#   verify-target.sh SITES_FILE TAILNET_IP
#
# Prints "<name> <address>" and exits 0, or exits 1 when the file has no site.
# The address is where to connect for that name:
#   - the first access=lan site, from 127.0.0.1: a lan site accepts any client;
#   - when every site is access=tailnet, the first one, from TAILNET_IP: Caddy
#     aborts a client that is not on the tailnet, and 127.0.0.1 is not.
set -euo pipefail

if (($# != 2)) || [[ "$1" == -h || "$1" == --help ]]; then
  echo "usage: verify-target.sh SITES_FILE TAILNET_IP" >&2
  exit 2
fi
sites="$1"; ts_ip="$2"

first_lan="" first_tailnet=""
while read -r name access _; do
  if [[ "$access" == lan ]]; then first_lan="$name"; break; fi
  [[ -n "$first_tailnet" ]] || first_tailnet="$name"
done < <(sed -E 's/\r$//; s/#.*//' "$sites" | awk 'NF')

if [[ -n "$first_lan" ]]; then
  echo "$first_lan 127.0.0.1"
elif [[ -n "$first_tailnet" ]]; then
  echo "$first_tailnet $ts_ip"
else
  echo "verify-target.sh: no site in $sites" >&2
  exit 1
fi
