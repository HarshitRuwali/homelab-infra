#!/usr/bin/env bash
# Configure an existing manager without reinstalling it. Dry run by default.
source "$(dirname "$0")/_common.sh"
require_root; banner

WAZUH_ENROLLMENT_PASSWORD_FILE="${WAZUH_ENROLLMENT_PASSWORD_FILE:-/root/security-bootstrap/wazuh-enrollment.password}"
WAZUH_TLS_CERT_FILE="${WAZUH_TLS_CERT_FILE:-/root/security-bootstrap/wazuh.crt}"
WAZUH_TLS_KEY_FILE="${WAZUH_TLS_KEY_FILE:-/root/security-bootstrap/wazuh.key}"
WAZUH_AGENT_GROUP="${WAZUH_AGENT_GROUP:-homelab}"
[[ "$WAZUH_AGENT_GROUP" =~ ^[A-Za-z0-9_-]+$ ]] || die "invalid WAZUH_AGENT_GROUP"
for input in "$WAZUH_ENROLLMENT_PASSWORD_FILE" "$WAZUH_TLS_CERT_FILE" "$WAZUH_TLS_KEY_FILE"; do
  require_input_file "$input"
done
validate_tls_pair "$WAZUH_TLS_CERT_FILE" "$WAZUH_TLS_KEY_FILE"
if (( APPLY )); then
  python3 "$(dirname "$0")/_configure.py" check-password "$WAZUH_ENROLLMENT_PASSWORD_FILE"
fi

# Stop before changing credentials. A validation failure below leaves enrollment
# closed instead of restarting a manager with a partially changed configuration.
run systemctl stop wazuh-manager
run install -o root -g wazuh -m 0640 "$WAZUH_ENROLLMENT_PASSWORD_FILE" /var/ossec/etc/authd.pass
run install -o root -g wazuh -m 0640 "$WAZUH_TLS_CERT_FILE" /var/ossec/etc/sslmanager.cert
run install -o root -g wazuh -m 0640 "$WAZUH_TLS_KEY_FILE" /var/ossec/etc/sslmanager.key
run python3 "$(dirname "$0")/_configure.py" wazuh /var/ossec/etc/ossec.conf
if [[ ! -d "/var/ossec/etc/shared/$WAZUH_AGENT_GROUP" ]]; then
  run /var/ossec/bin/agent_groups -a -g "$WAZUH_AGENT_GROUP" -q
fi
run /var/ossec/bin/wazuh-authd -t
run systemctl start wazuh-manager
run systemctl is-active --quiet wazuh-manager
ok "Wazuh enrollment requires a password; distribute the trusted CA and password through Ansible before enrolling agents"
