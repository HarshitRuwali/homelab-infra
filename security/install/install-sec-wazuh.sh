#!/usr/bin/env bash
# Wazuh all-in-one (manager + indexer + dashboard) on sec-wazuh.
# Run inside the sec-wazuh VM. Needs ~8 GB RAM.
source "$(dirname "$0")/_common.sh"
require_root; banner
WAZUH_VERSION="${WAZUH_VERSION:-4.14}"
WAZUH_WORKDIR="${WAZUH_WORKDIR:-/root/wazuh-install}"
[[ "$WAZUH_VERSION" =~ ^[0-9]+\.[0-9]+$ ]] || die "WAZUH_VERSION must be a major.minor release"
for input in "${WAZUH_ENROLLMENT_PASSWORD_FILE:-/root/security-bootstrap/wazuh-enrollment.password}" \
  "${WAZUH_TLS_CERT_FILE:-/root/security-bootstrap/wazuh.crt}" \
  "${WAZUH_TLS_KEY_FILE:-/root/security-bootstrap/wazuh.key}"; do
  require_input_file "$input"
done

[[ $(free -m | awk '/^Mem:/{print $2}') -ge 7000 ]] || warn "less than 7 GB RAM visible; the indexer may OOM"

run apt-get update
# The VM is created with --agent enabled=1, but Debian's genericcloud image does
# not ship the agent. Without it PVE shows no IP for the guest, which is exactly
# what you need to set the DHCP reservation in the provisioning script's step 1.
run apt-get install -y qemu-guest-agent
run systemctl enable --now qemu-guest-agent
run apt-get install -y curl ca-certificates openssl python3

validate_tls_pair "${WAZUH_TLS_CERT_FILE:-/root/security-bootstrap/wazuh.crt}" \
  "${WAZUH_TLS_KEY_FILE:-/root/security-bootstrap/wazuh.key}"
if (( APPLY )); then
  python3 "$(dirname "$0")/_configure.py" check-password \
    "${WAZUH_ENROLLMENT_PASSWORD_FILE:-/root/security-bootstrap/wazuh-enrollment.password}"
fi
[[ "${WAZUH_AGENT_GROUP:-homelab}" =~ ^[A-Za-z0-9_-]+$ ]] || die "invalid WAZUH_AGENT_GROUP"
run install -d -m 0700 "$WAZUH_WORKDIR"
download_file "https://packages.wazuh.com/${WAZUH_VERSION}/wazuh-install.sh" "$WAZUH_WORKDIR/wazuh-install.sh"
run bash -c 'cd "$1" && bash ./wazuh-install.sh -a -i' _ "$WAZUH_WORKDIR"
run bash "$(dirname "$0")/configure-sec-wazuh.sh" --apply
# The vulnerability feed updater leaves its downloads behind; left alone they
# filled the 25 GB root on 2026-09-23. See the script for the details.
run bash "$(dirname "$0")/configure-sec-wazuh-cleanup.sh" --apply
# The installer leaves Wazuh's apt repo enabled. The fleet's nightly run only
# takes Debian-Security, but force-updates.yml does a full upgrade, which would
# move these four independently when a release lands. They must upgrade
# together, by hand.
run apt-mark hold wazuh-manager wazuh-indexer wazuh-dashboard filebeat
run apt-get clean

ok "Wazuh installed"
cat <<'NOTE'

  THE ADMIN PASSWORD is printed at the end of the install. It is ALSO written,
  with every internal password and the TLS certificates, to
  wazuh-install-files.tar in WAZUH_WORKDIR (default /root/wazuh-install):

      cd /root/wazuh-install
      tar -O -xvf wazuh-install-files.tar wazuh-install-files/wazuh-passwords.txt

  Put the admin password in your password manager, then move that tar OFF this
  guest. It holds every credential the stack uses.

  RETENTION: set this before you have data, not after. In Dashboard >
  Index Management > State management policies, create a policy with:
      hot   7 days, then rollover
      delete at 30 days
  Apply it to the wazuh-alerts-* index pattern. Without it, indices grow until the
  disk fills, and ingestion stops silently rather than alerting.

  LONGER-TAIL ARCHIVES, cheaply: Wazuh already gzips daily alert files into
  /var/ossec/logs/alerts/YYYY/MMM/. Those are alerts only, a few MB per day. Attach a
  second disk from the HDD tier and bind-mount it so you keep a year for a couple of GB:
      /mnt/archive/alerts  /var/ossec/logs/alerts  none  bind  0 0
  Do NOT enable <logall> or <logall_json> to archive more: that is the full event
  stream and is genuinely large.

  FORWARD ALERTS INTO LOKI so Grafana stays the single pane and the Wazuh
  dashboard is kept for deep investigation only. Wazuh writes every alert to
  /var/ossec/logs/alerts/alerts.json; point an Alloy collector at that file and
  label it job="wazuh". The monitoring module already ships the collector role,
  so this is a scrape target, not new infrastructure:

      loki.source.file "wazuh" {
        targets    = [{__path__ = "/var/ossec/logs/alerts/alerts.json", job = "wazuh"}]
        forward_to = [loki.write.central.receiver]
      }

  Ports agents need: 1514/tcp events, 1515/tcp enrollment. Port 55000 is admin-only.
  Enrollment requires the configured password and a trusted manager CA on each agent.
  The manager group homelab is created by configure-sec-wazuh.sh.
  DO NOT install agents by hand. Use the ansible role, which is how every other
  fleet-wide package in this repository is deployed:

      ansible-playbook playbooks/wazuh-agents.yml --limit <host>

  Membership of the `wazuh_agents` inventory group is the only thing that enables
  an agent, the same contract as `autoupdate`. Roll out in stages and tune the
  alert volume after the first two hosts: an untuned default ruleset across a
  whole fleet produces a volume nobody reads, which is this phase's known
  failure mode.

  Verify, and do not accept "service is running" as evidence:
      /var/ossec/bin/agent_control -l          # every host Active
      # then stop one agent, confirm it goes Disconnected, start it again
NOTE
