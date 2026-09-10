# Wiring it together

Provisioning gets you six running services. This page connects them to each
other and to the existing `monitoring/` stack, so alerts land somewhere you
already look.

Read [Architecture](../architecture/index.md) first for *why* it is wired this
way. This page is the *how*, with the actual configuration.

!!! warning "Order matters in two places"
    Add the firewall allow rules **in the same change** as any block rule, and
    point DHCP at the new resolver **only after** verifying it resolves. Both
    are explained below, and both fail quietly rather than loudly.

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

  stage.json {
    expressions = {
      rule_level  = "rule.level",
      rule_id     = "rule.id",
      agent_name  = "agent.name",
    }
  }

  stage.labels {
    values = {
      job        = "wazuh",
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
the block above never renders anywhere else.

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

## 3. NetFlow into ntopng

ntopng collects rather than sniffs, which is what keeps it an unprivileged
container.

On OPNsense: install the **softflowd** plugin, set the target to
`<sec-ntopng>:2055`, and select the interfaces you want flows from.

`install-sec-ntopng.sh` already configures nprobe to listen on 2055/udp and
feed ntopng over ZMQ. Confirm packets arrive before believing the UI:

```bash
tcpdump -ni any port 2055 -c 5      # on sec-ntopng
```

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
url: http://<sec-crowdsec>:8080
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
block  <sandbox net> -> <trusted net>          # must be BELOW the passes
```

Add the passes in the **same change** as the block. Doing it afterwards leaves
a window where sandbox telemetry stops and nothing tells you.

## 7. Grafana

Loki is already a datasource, so security logs need no new one. What is worth
adding:

| Panel or rule | Query |
|---|---|
| Wazuh alert rate by level | `sum by (rule_level) (count_over_time({job="wazuh"}[5m]))` |
| High-severity Wazuh alerts | `{job="wazuh"} \| json \| rule_level >= 10` |
| Suricata alerts by signature | `sum by (alert_signature) (count_over_time({job="suricata"} \| json [5m]))` |
| Agent stopped reporting | `absent_over_time({job="wazuh", agent_name="<host>"}[30m])` |

That last row is the one people forget. A silent agent looks identical to a
quiet host, and it is the failure mode this whole stack is most exposed to.

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

Then break it on purpose: stop one agent, confirm it goes Disconnected and that
the `absent_over_time` rule fires, and start it again. A pipeline you have only
ever seen green is untested infrastructure, not evidence.
