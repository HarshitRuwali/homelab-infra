#!/usr/bin/env bash
# AdGuard Home on sec-dns. Gives the LAN a resolver you control, with query logging.
# Run inside the sec-dns LXC.
source "$(dirname "$0")/_common.sh"
require_root; banner

run apt-get update
run apt-get install -y curl ca-certificates

# Download fully before execution and verify the service, not just the shell exit.
run install -d -m 0700 /var/cache/homelab-security
download_file https://raw.githubusercontent.com/AdguardTeam/AdGuardHome/master/scripts/install.sh \
  /var/cache/homelab-security/adguard-install.sh
run sh /var/cache/homelab-security/adguard-install.sh -v
run systemctl is-active --quiet AdGuardHome

ok "AdGuard installed. Finish setup at http://<this-guest>:3000"
cat <<'NOTE'

  Post-install, in the AdGuard UI:
    - Settings > General: set "Query log retention" to 30 days, to match Wazuh.
    - Settings > DNS: upstream to your firewall's resolver, or DoH directly.

  ONLY THEN point your router's DHCP DNS servers at this guest.
  Keep 1.1.1.1 as SECONDARY for the first week so a failure degrades rather than
  taking the home LAN offline. Verify from a test client first:

    resolvectl status | grep 'DNS Servers'
    dig +short example.com @<this-guest>
NOTE
