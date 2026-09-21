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
run /var/ossec/bin/wazuh-authd -t
run systemctl start wazuh-manager
run systemctl is-active --quiet wazuh-manager

# The group is created through wazuh-db, so only once the manager is up.
# Created while it was stopped, agent_groups printed "Some Wazuh daemons are
# not ready yet", exited 0 anyway, and this script reported success with no
# group. That is why the directory is checked afterwards rather than trusting
# the exit status. First install on 2026-09-21 hit exactly that.
if [[ ! -d "/var/ossec/etc/shared/$WAZUH_AGENT_GROUP" ]]; then
  if (( APPLY )); then
    for _ in $(seq 1 30); do
      /var/ossec/bin/agent_groups -a -g "$WAZUH_AGENT_GROUP" -q >/dev/null 2>&1 || true
      [[ -d "/var/ossec/etc/shared/$WAZUH_AGENT_GROUP" ]] && break
      sleep 5
    done
    [[ -d "/var/ossec/etc/shared/$WAZUH_AGENT_GROUP" ]] \
      || die "agent group $WAZUH_AGENT_GROUP was not created; the agent role will refuse to run"
  else
    run /var/ossec/bin/agent_groups -a -g "$WAZUH_AGENT_GROUP" -q
  fi
fi
if (( APPLY )); then ok "agent group $WAZUH_AGENT_GROUP exists"; fi
ok "Wazuh enrollment requires a password; distribute the trusted CA and password through Ansible before enrolling agents"
