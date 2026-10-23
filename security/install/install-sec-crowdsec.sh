#!/usr/bin/env bash
# CrowdSec Local API on sec-crowdsec. Agents on other hosts register against this.
# Run inside the sec-crowdsec LXC.
source "$(dirname "$0")/_common.sh"
require_root; banner

for input in "${CROWDSEC_TLS_CERT_FILE:-/root/security-bootstrap/crowdsec.crt}" \
  "${CROWDSEC_TLS_KEY_FILE:-/root/security-bootstrap/crowdsec.key}" \
  "${CROWDSEC_TLS_CA_FILE:-/root/security-bootstrap/ca.crt}"; do
  require_input_file "$input"
done
run apt-get update
run apt-get install -y curl gnupg ca-certificates openssl python3-yaml
run install -d -m 0700 /var/cache/homelab-security
download_file https://install.crowdsec.net /var/cache/homelab-security/crowdsec-repo.sh
run sh /var/cache/homelab-security/crowdsec-repo.sh
run apt-get install -y crowdsec
run bash "$(dirname "$0")/configure-sec-crowdsec.sh" --apply

cat <<'NOTE'

  Enrol each monitored host (run ON the LAPI, then on the agent):
    cscli machines add <agent-hostname> --auto        # prints credentials
    # on the agent: /etc/crowdsec/local_api_credentials.yaml -> url: https://<lapi-certificate-dns-name>:8080
    # also set ca_cert_path to the trusted CA file on that agent; never disable TLS verification

  Cloudflare bouncer belongs on whichever host terminates your tunnel:
    cscli bouncers add cloudflare-bouncer              # keep the key out of shell history

  Verify:
    cscli machines list
    cscli decisions list
    cscli metrics
NOTE
