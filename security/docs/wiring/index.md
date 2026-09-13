# Wiring it together

Provisioning gets you four guests, and the firewall runs Suricata and ntopng
itself. This page connects them to each other and to the existing
`monitoring/` stack, so alerts land somewhere you already look.

Read [Architecture](../architecture/index.md) first for *why* it is wired this
way. This page is the *how*, with the actual configuration.

!!! warning "Order matters in two places"
    Add the firewall allow rules **in the same change** as any block rule, and
    point DHCP at the new resolver **only after** verifying it resolves. Both
    are explained below, and both fail quietly rather than loudly.

## Guest host metrics and logs

For CPU, memory, disk, network, uptime, systemd state and journal logs, use
[Host monitoring in Grafana](../getting-started/index.md#host-monitoring-in-grafana).
All four guests use the existing fleet Alloy role, authenticated push endpoints
and Servers dashboards. `sec-scan` also contributes Docker metrics and logs
through the collector's Docker autodetection.

## 1. Wazuh alerts into Loki

The goal is Grafana as the single pane, with the Wazuh dashboard kept for deep
investigation. Wazuh writes every alert as JSON lines to
`/var/ossec/logs/alerts/alerts.json`, and every monitored host already runs an
Alloy collector, so this is a new source block, not new infrastructure.

Add to `ansible/roles/alloy_collector/templates/config.alloy.j2`, guarded so it
only appears on the manager:

```river
{% raw %}{% if 'wazuh_manager' in group_names %}
local.file_match "wazuh_alerts" {
  path_targets = [{__path__ = "/var/ossec/logs/alerts/alerts.json"}]
}

loki.source.file "wazuh" {
  targets    = local.file_match.wazuh_alerts.targets
  forward_to = [loki.process.wazuh.receiver]
}

// Lift the fields worth querying on into labels. Keep this list short:
// every distinct value becomes a stream, and high-cardinality labels are
// how a Loki instance falls over.
loki.process "wazuh" {
  forward_to = [loki.write.central.receiver]

  stage.static_labels {
    values = {job = "wazuh"}
  }

  stage.json {
    expressions = {
      rule_level  = "rule.level",
      rule_id     = "rule.id",
      agent_name  = "agent.name",
    }
  }

  stage.labels {
    values = {
      rule_level = "",
      agent_name = "",
    }
  }
}
{% endif %}{% endraw %}
```

`loki.write.central` is the existing writer in that template; do not declare a
second one.

!!! danger "Do not label on rule_id"
    It is tempting, and it is the fastest way to blow up your Loki cardinality:
    the default ruleset has thousands of rule IDs. Keep it in the log line and
    query it with `| json` instead.

Then add a `wazuh_manager` group to the inventory with the one guest in it, so
the block above never renders anywhere else. The native Alloy service also
needs read access to `/var/ossec/logs/alerts/alerts.json` and traversal of its
parent directories. Grant a narrowly scoped ACL (including a default ACL on
the alerts directory for rotation), then verify access as `alloy`; its existing
`adm` group alone does not grant access to Wazuh logs. Do not make the logs
world-readable. This remains a wiring example, not an automatically installed
collector configuration.

## 2. Suricata EVE into Loki

Suricata runs on the firewall, not on a guest, so it ships by syslog rather
than by Alloy.

On OPNsense: **Services > Intrusion Detection > Administration**, enable
`EVE syslog output`. Then point the firewall's remote syslog target at the
central stack.

On the central stack, Alloy accepts it:

```river
loki.source.syslog "firewall" {
  listener {
    address  = "0.0.0.0:1514"
    protocol = "tcp"
    labels   = {job = "suricata", source = "opnsense"}
  }
  forward_to = [loki.write.central.receiver]
}
```

!!! note "1514 collides with Wazuh"
    Wazuh agents also use 1514/tcp. These are different hosts, so there is no
    actual conflict, but if you ever co-locate them, move one. Picking a
    different syslog port now costs nothing and avoids a confusing afternoon
    later.

## 3. ntopng on the firewall

ntopng runs on the firewall and captures there directly, so there is no flow
export to wire up and no guest to point it at.

On OPNsense, in this order:

1. **System > Firmware > Plugins**: install `os-redis`, then enable it under
   **Services > Redis**. ntopng needs it running.
2. Install `os-ntopng`. Under **Services > Ntopng**, enable it and select the
   **LAN** interface. On WAN, NAT has already rewritten every sandbox address to
   the firewall's own.
3. Open `http://<firewall>:3000`, log in as `admin` / `admin`, and set the new
   password ntopng asks for.

Confirm it sees real traffic before believing the UI: generate traffic from one
sandbox host and find it under **Hosts** with a recent last-seen time.

Remember the scope limit: this sees traffic that **crosses the firewall**. It
cannot see two hosts talking on the same segment, nor a multi-homed host
routing around it. That gap is what the Wazuh agents cover.

## 4. CrowdSec agents and the edge bouncer

Enrol each host against the central LAPI. Run the first command on
`sec-crowdsec`, the second on the agent:

```bash
cscli machines add <agent-hostname> --auto        # prints credentials
```

```yaml
# /etc/crowdsec/local_api_credentials.yaml on the agent
url: https://<sec-crowdsec-certificate-dns-name>:8080
ca_cert_path: /etc/crowdsec/tls/ca.crt
login: <from above>
password: <from above>
```

The bouncer belongs on whichever host terminates your public tunnel, so
decisions are enforced at the edge rather than at the origin:

```bash
cscli bouncers add edge-bouncer     # keep the key out of shell history
```

Verify it is making decisions rather than merely running:

```bash
cscli metrics          # parsers should show non-zero lines read
cscli decisions list
```

## 5. AdGuard as the fleet resolver

This is the step that fails quietly if done in the wrong order.

1. Install and configure AdGuard, set **query log retention to 30 days** so it
   expires alongside the Wazuh indices.
2. Point its upstream at your firewall's resolver, or at DoH directly.
3. **Verify from a test client, not from the server:**

    ```bash
    dig +short example.com @<sec-dns>
    ```

4. Only then change your router's DHCP DNS servers. Keep a public resolver as
   **secondary** for the first week, so a failure degrades instead of taking
   the LAN offline.

## 6. Firewall rules

If your sandbox segment is blocked from the trusted segment, the agent paths
need explicit allows, and they must sit **above** the block in evaluation
order:

```
pass   <sandbox net> -> sec-wazuh      tcp 1514, 1515
pass   <sandbox net> -> sec-crowdsec   tcp 8080
pass   <sandbox net> -> sec-dns        tcp/udp 53
pass   sec-scan      -> <trusted net>          # only if it should scan the trusted side
block  <sandbox net> -> <trusted net>          # must be BELOW the passes
```

Add the passes in the **same change** as the block. Doing it afterwards leaves
a window where sandbox telemetry stops and nothing tells you.

`sec-scan` is on the sandbox side, so the block catches it too. Its pass rule
is a deliberate trade, explained in
[Reference](../reference/index.md#firewall-rules).

## 7. Grafana

Loki is already a datasource, so security logs need no new one. What is worth
adding:

| Panel or rule | Query |
|---|---|
| Wazuh alert rate by level | `sum by (rule_level) (count_over_time({job="wazuh"}[5m]))` |
| High-severity Wazuh alerts | `{job="wazuh"} \| json \| rule_level >= 10` |
| Suricata alerts by signature | `sum by (alert_signature) (count_over_time({job="suricata"} \| json [5m]))` |
| Agent disconnected or stopped (event) | `sum by (agent_name) (count_over_time({job="wazuh"} \| json \| rule_id =~ "504\|506" [10m])) > 0` |

The last row uses Wazuh's explicit disconnect/stop events (rules 504 and 506).
Keep those level-3 alerts in the forwarding pipeline. An absence of security
alerts is **not** a heartbeat: a healthy idle host may emit none for hours.
This query reports recent events, not current connection state; it ages out
after ten minutes even if the agent remains offline. Check current state with
`/var/ossec/bin/agent_control -l`, or export periodic manager API connection
status for a persistent per-agent availability alert. Monitor manager/Alloy
availability separately through the existing fleet host-down rules.

Route these through the existing Matrix notification path rather than inventing
a second one: `monitoring/grafana/provisioning/alerting/` already carries the
contact points and mute timings.

## 8. Prove it works

Do not accept "the service is running" as evidence.

```bash
# an agent's events actually arrive
/var/ossec/bin/agent_control -l                # on the manager, expect Active

# FIM fires end to end, without opening the Wazuh dashboard
touch /etc/wiring-test && sleep 60             # on an agent
# then query Grafana: {job="wazuh"} |= "wiring-test"
rm /etc/wiring-test
```

Then stop one test agent, confirm it goes Disconnected in the manager and that
a rule 504 or 506 event reaches Loki, and start it again. A quiet connected
agent must not trigger an availability alert. A pipeline you have only
ever seen green is untested infrastructure, not evidence.
