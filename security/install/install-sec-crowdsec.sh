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
run sed -i -E 's|^([[:space:]]*)listen_uri:[[:space:]]*127\.0\.0\.1:8080|\1listen_uri: 0.0.0.0:8080|' /etc/crowdsec/config.yaml
# That sed is a silent no-op if upstream changes the indentation or the default
# value, and run() would still report success. The symptom would be every agent
# enrolment failing later, a long way from the cause. Assert it landed.
if (( APPLY )); then
  grep -qE '^[[:space:]]*listen_uri:[[:space:]]*0\.0\.0\.0:8080' /etc/crowdsec/config.yaml \
    || die "could not rewrite listen_uri in /etc/crowdsec/config.yaml. Set it by hand; until it is 0.0.0.0:8080 no remote agent can enrol."
fi
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
