#!/usr/bin/env bash
# Wazuh all-in-one (manager + indexer + dashboard) on sec-wazuh.
# Run inside the sec-wazuh VM. Needs ~8 GB RAM.
source "$(dirname "$0")/_common.sh"
require_root; banner

[[ $(free -m | awk '/^Mem:/{print $2}') -ge 7000 ]] || warn "less than 7 GB RAM visible; the indexer may OOM"

run apt-get update
run apt-get install -y curl ca-certificates

run curl -sO https://packages.wazuh.com/4.x/wazuh-install.sh
run bash ./wazuh-install.sh -a -i

ok "Wazuh installed"
cat <<'NOTE'

  The installer prints the admin password ONCE. Put it straight into OpenBao on
  sec-auth. Do not leave it in scrollback or a note.

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

  Ports agents need: 1514/tcp events, 1515/tcp enrollment, 55000/tcp API.
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
