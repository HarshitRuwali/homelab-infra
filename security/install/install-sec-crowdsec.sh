#!/usr/bin/env bash
# CrowdSec Local API on sec-crowdsec. Agents on other hosts register against this.
# Run inside the sec-crowdsec LXC.
source "$(dirname "$0")/_common.sh"
require_root; banner

run apt-get update
run apt-get install -y curl gnupg ca-certificates

run sh -c 'curl -s https://install.crowdsec.net | sh'
run apt-get install -y crowdsec

# Listen on the LAN so lab-side agents can reach it, not just localhost.
run sed -i 's/^  listen_uri: 127.0.0.1:8080/  listen_uri: 0.0.0.0:8080/' /etc/crowdsec/config.yaml
run systemctl restart crowdsec
run systemctl enable crowdsec

ok "CrowdSec LAPI listening on 8080"
cat <<'NOTE'

  Enrol each monitored host (run ON the LAPI, then on the agent):
    cscli machines add <agent-hostname> --auto        # prints credentials
    # on the agent: /etc/crowdsec/local_api_credentials.yaml -> url: http://<lapi>:8080

  Cloudflare bouncer belongs on whichever host terminates your tunnel:
    cscli bouncers add cloudflare-bouncer              # keep the key out of shell history

  Verify:
    cscli machines list
    cscli decisions list
    cscli metrics
NOTE
