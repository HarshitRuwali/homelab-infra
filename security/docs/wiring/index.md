# Wiring it together

Provisioning gets you four guests, and the firewall runs Suricata and captures
NetFlow itself. This page connects them to each other and to the existing
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

**Interfaces.** It watches `vtnet1` to `vtnet5`:

| Interface | Segment |
|---|---|
| `vtnet1` | lan |
| `vtnet2` | tenants: the work VMs, 10.10.60.0/24 |
| `vtnet3` | DMZ |
| `vtnet4` | LAB |
| `vtnet5` | lease: the leased VMs, 10.10.90.0/24 |

Every EVE event names the interface that saw it in its `in_iface` field, which
is how Grafana tells the work segment from the lease segment.

**What reaches Loki.** The syslog output carries **alerts only**
(`"event_type":"alert"`). TLS server names, HTTP hosts and the rest of EVE's
metadata are kept in the firewall's local `/var/log/suricata/eve.json` and are
not in Loki. To see which server name a VM asked for, read that file on the
firewall.

On OPNsense: **Services > Intrusion Detection > Administration**, enable
`EVE syslog output`. Then point the firewall's remote syslog target at the
central stack.

On the central stack, Alloy accepts it, and every other syslog program the
firewall sends ([section 3](#3-firewall-streams-and-flow-visibility)):

```river
loki.relabel "firewall_syslog" {
  rule {
    source_labels = ["__syslog_message_app_name"]
    regex         = "^(suricata|filterlog|unbound|pfstats)$"
    target_label  = "job"
    replacement   = "$1"
  }
  forward_to = []
}

loki.source.syslog "firewall" {
  listener {
    address  = "0.0.0.0:5514"
    protocol = "tcp"
    labels   = {job = "firewall-other", source = "opnsense", host = "opnsense", role = "firewall"}
  }
  relabel_rules = loki.relabel.firewall_syslog.rules
  forward_to    = [loki.process.firewall_syslog.receiver]
}
```

`host` and `role` are not decoration. The central collector's `loki.write`
stamps everything it ships with its own identity, so without them every
firewall line is stored as coming from the monitoring host itself. The syslog
program name becomes `job` for the four programs the firewall runs, so Suricata
arrives as `job="suricata"`, the value every dashboard and rule already selects,
and the others get their own. Any other program name keeps the listener's
`job="firewall-other"`: the listener cannot tell who is connecting, and copying
a name unchecked would let anyone who can reach the port create streams without
limit.

`ansible/roles/alloy_collector` renders this block, and the
`loki.process "firewall_syslog"` stage that feeds `loki.write.central`, on
whichever host sets `alloy_syslog_listener_port`. It is set in `group_vars/central`
and empty everywhere else, so one host listens rather than all of them:

```bash
ansible-playbook playbooks/collectors.yml --limit central
```

!!! note "Why 5514 and not 1514"
    1514/tcp is Wazuh's agent event port. The two would sit on different
    hosts, so nothing actually collides, but a Suricata listener answering on
    the Wazuh port is a bad thing to discover mid-investigation. Choosing a
    different port costs nothing today.

!!! warning "Send RFC5424"
    Alloy's syslog source expects RFC5424 framing. Tick the RFC5424 option on
    the firewall's remote syslog target; with BSD-format syslog the messages
    arrive and fail to parse, which looks like a broken listener.

Then prove it end to end with a harmless trigger. The usual target,
`testmynids.org`, no longer resolves. `httpbin.org` echoes back any string it
is given, and `uid=0(root)` in a response trips sid 2100498, in the
`emerging-attack_response` category:

```bash
# from any host whose traffic crosses the firewall's LAN interface
curl "http://httpbin.org/base64/$(printf 'uid=0(root) gid=0(root) groups=0(root)\n' | base64)"
```

In Grafana, open **Drilldown > Logs** and pick `suricata`, or query Loki in
Explore:

```logql
{job="suricata"} | json | alert_signature_id="2100498"
```

!!! warning "Wait for that signature, not for any alert"
    An ordinary LAN trips other rules within minutes: UPnP discovery against
    the firewall, a torrent client resolving `.to` trackers. A check that stops
    at the first alert it sees can report success, or failure, before the test
    alert has even arrived.

## 3. Firewall streams and flow visibility

The firewall sees the flows of the work and lease VMs that cross it, so it holds
the best record there is of what they did, though not a complete one
([what is not covered](#what-is-not-covered)). It is FreeBSD and outside
Ansible's reach, so what it knows leaves as syslog, through the listener in
section 2. The hypervisor adds one more stream of its own.

| `job` | From | What it holds | Other labels |
|---|---|---|---|
| `suricata` | firewall | EVE alerts, alerts only (section 2) | |
| `filterlog` | firewall | pf's filter log: one CSV line per new flow that a logging pass rule lets through on `vtnet2` and `vtnet5`, and per block | `iface` (`vtnet0` to `vtnet5`), `action` (`pass` or `block`) |
| `unbound` | firewall | the resolver's query and reply lines, plain DNS on port 53 only | |
| `pfstats` | firewall | one logfmt line a minute: state table, limit hits, banned sources, interface counters | |
| `pve-firewall` | hypervisor `t7920` | what the Proxmox firewall dropped, at most one line a second per rule | `chain`, `vmid` |

All carry `host="opnsense"` and `role="firewall"` except `pve-firewall`, which
carries the hypervisor's own `host`. A query for the firewall's streams should
therefore select on `job`, not on `host`: `{host="opnsense"}` matches all of
them, and anything else the firewall sends as `job="firewall-other"`.

### filterlog

A new flow of `vtnet2` or `vtnet5` that one of their pass rules lets through is
logged, because those rules have logging on, and blocks are logged. The source
address in a line identifies the VM, since the log is written before NAT. Some
of the volume is not kept,
all set in `group_vars/central`: OPNsense's automatic rule for the firewall's
own outbound traffic is dropped at the listener, and so is the default deny on
the WAN interface only (`alloy_syslog_drop_filterlog_rules` and
`alloy_syslog_drop_filterlog_iface`); on the lease and tenant interfaces the
default deny is kept. The automatic rule that logs every packet from a banned
source is rate-limited to a few lines a second
(`alloy_syslog_ratelimit_filterlog_rules`), so a ban cannot flood Loki and the
blocked count understates it. The firewall's own filter log still keeps every
line, for 31 days.

The fields are `rule,sub,anchor,label,interface,reason,action,dir,ipver`, then
for IPv4 `tos,ecn,ttl,id,offset,flags,protonum,protoname,length,src,dst,sport,dport,datalen`.
This extracts the ones worth grouping on, and is what the Tenant Activity
dashboard uses:

```logql
{job="filterlog", iface="vtnet5"} | regexp `^[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,4,(?:[^,]*,){6}[^,]*,(?P<proto>[^,]*),[^,]*,(?P<src>[^,]*),(?P<dst>[^,]*),(?P<sport>[^,]*),(?P<dport>[^,]*)`
```

### unbound

One query line and one reply line per lookup:

```text
info: 10.10.60.20 example.com. A IN
info: 10.10.60.20 example.com. A IN NOERROR 0.03 0 105
```

Plain DNS on port 53 is what reaches Unbound: a lease VM that asks another
resolver is redirected to it, and the client address is the VM's. Encrypted DNS
is a gap, set out under [what is not covered](#what-is-not-covered).

### pfstats

The firewall has no metrics agent, so a small script writes one line a minute
to syslog with the pf state table and a set of counters per interface:

```text
states=392 states_max=405300 state_limit_hits=0 src_limit_hits=0 src_nodes=2 banned=0 load1=0.99 mem_free_mb=451 vtnet0_ipkts=... vtnet0_opkts=... vtnet0_ibytes=... vtnet0_obytes=... vtnet0_idrop=...
```

and the same five for `vtnet1` to `vtnet5`. The counters are cumulative since
boot. `banned` is the number of sources in the `virusprot` table: a source that
exceeds its connection-rate limit is banned for up to an hour.

!!! warning "Do not use `| logfmt` in a metric query"
    On this line `logfmt` turns every field into a label, so an aggregation
    sees dozens of series and `sum` or `max` no longer returns one number. Pull
    out the one field with a single-capture regexp instead:

    ```logql
    max(max_over_time({job="pfstats"} | regexp "\\bstates=(?P<v>[0-9]+)" | unwrap v [5m]))
    ```

Every `pfstats` alert rule, and the dead-man rule, reads this line, so its key
names are an interface: rename one and those rules go blind. The producer is in
the repository, in `security/opnsense/pfstats/`, and
`ansible/tests/pfstats-test.py` runs it against fixture output in the shapes
`pfctl` and `netstat` print, and checks the line against the regexps the rules
use.

| File | Goes to | Mode | What it is |
|---|---|---|---|
| `pfstats-log.sh` | `/usr/local/sbin/pfstats-log.sh` | 0755 | reads `pfctl`, `netstat` and `sysctl`, writes the line with `logger -t pfstats` |
| `actions_pfstats.conf` | `/usr/local/opnsense/service/conf/actions.d/actions_pfstats.conf` | 0644 | the configd action `pfstats log`, which runs the script |
| `pfstats.inc` | `/usr/local/etc/inc/plugins.inc.d/pfstats.inc` | 0644 | registers `pfstats` as a syslog program |
| `add-pfstats-cron.php` | run once, not installed | | adds the every-minute cron job `pfstats log` through OPNsense's own configuration API |

**Install.** Copy `security/opnsense/pfstats/` to the firewall and, as root
there:

```sh
cd pfstats
sh install.sh            # dry run: prints what it would do, changes nothing
sh install.sh --apply
```

The login shell on OPNsense is csh, so run it as `sh install.sh`. It refuses to
run if `/usr/local/opnsense` is missing. With `--apply` it copies the three
files, restarts `configd`, runs `add-pfstats-cron.php` from
`/usr/local/opnsense/mvc` (which that script needs), reloads cron, then runs
`PFSTATS_STDOUT=1 /usr/local/sbin/pfstats-log.sh` and checks that the line has
`states`, `states_max`, `src_limit_hits` and `banned`. It can be run again: the
cron script reports an existing job and exits cleanly.

**Forward the program.** The installer does not touch the syslog destination.
In the firewall's remote syslog destination (System > Settings > Logging, the
one that already carries Suricata and the filter log), also forward the program
`pfstats`. The destination offers only programs that a plugin registers, which
is what `pfstats.inc` does: without that file `pfstats` is not offered, and a
line logged under a name no plugin has registered is not selectable for
forwarding. On the central stack, the listener in section 2 turns the program
name into `job="pfstats"`.

**Revert.** Remove the three files, restart `configd`, delete the `pfstats log`
job under System > Settings > Cron, and stop forwarding the program:

```sh
rm /usr/local/sbin/pfstats-log.sh \
   /usr/local/opnsense/service/conf/actions.d/actions_pfstats.conf \
   /usr/local/etc/inc/plugins.inc.d/pfstats.inc
service configd restart
```

With the producer gone, `Firewall Stats Stopped` fires after about 10 minutes.

### pve-firewall

The Proxmox firewall drops tenant and lease traffic that breaks its rules, and
logs it. With `alloy_pve_firewall_log: true` in `host_vars/t7920.yml`, the
hypervisor's Alloy ships `/var/log/pve-firewall.log` as `job="pve-firewall"`,
with the rule `chain` and the guest's `vmid` as labels. The chain is
`GROUP-tenant-OUT`, `GROUP-tenant-IN`, `GROUP-lease-OUT` or `GROUP-lease-IN`,
or a per-VM chain for a policy drop. The SMTP rule logs at `warning`.

Proxmox rate-limits each log rule to one line per second, so a count of lines
understates the packets. The exact counts are in Prometheus as
`fleet_pve_firewall_dropped_packets_total`, from `ansible/roles/pve_guest_metrics`.
The work VM's Tailscale client probes LAN peers on UDP 41641 about every 5
seconds; those lines are benign, and a query about probing should exclude
`DPT=41641 `.

### NetFlow and Insight

Beside the logs, OPNsense captures NetFlow on the work and lease interfaces to
its own local collector, and the Insight reports in the OPNsense UI read from
it. Nothing is wired for it: it is on the firewall and stays there. See
[NetFlow and Insight](../components/index.md#netflow-and-insight).

### What is not covered

These streams are a floor, not a full account of what a tenant did. Treat a
quiet panel or rule as "nothing seen", not "nothing happened".

- **DNS.** Only plain DNS on port 53 reaches Unbound and appears as
  `job="unbound"`; a lease VM that asks another resolver is redirected to it.
  DNS over TLS (TCP 853) is blocked on the lease segment only, so a work VM can
  use it unseen. DNS over QUIC (UDP 853) is being blocked on the lease segment.
  DNS over HTTPS, and DNS inside a tenant's own Tailscale tunnel, are not
  visible.
- **Tunnels.** Traffic inside a tenant's own Tailscale tunnel, or through their
  exit node, appears as one flow to a Tailscale relay or peer, not as the inner
  destinations. The filter log and the flow records see that one flow, and
  Suricata cannot inspect inside the tunnel.
- **The filter log.** It holds a new flow only when a pass rule with logging on
  lets it through, and blocks. Lines from a banned source are rate-limited at
  ingest, and the default deny on the WAN interface and the firewall's own
  outbound traffic are dropped at the listener. The firewall's own filter log
  keeps every line, for 31 days.
- **The hypervisor log.** The Proxmox firewall logs drops, not accepted traffic.
  PVE writes at most one line a second per rule, so counts of lines understate
  the packets, and inbound flows to `--allow` ports are not logged on the
  hypervisor.
- **Suricata.** Only alerts reach Loki; TLS server names and HTTP hosts stay in
  the firewall's own `eve.json`.
- **Between guests.** A flow between two guests on one bridge never crosses the
  firewall, so none of this sees it. The hypervisor's per-guest byte counts (the
  Guest Traffic dashboard) do.

### What reads these

The Tenant Activity dashboard and the `tenant-*` alert rules in `monitoring/`
read all of the above; see
[the dashboard](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/dashboards/#tenant-activity)
and [the rules](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/alerting/#tenant-and-lease-alerts).
The lease VMs' logs are the main record there is of what they did, so the live
Loki keeps them for 30 days; see
[Retention](https://harshitruwali.github.io/homelab-infra/monitoring/operations/retention/#loki-retention-for-the-tenant-logs).
To check the streams arrive, see [Verification](../operations/index.md#verification).

Remember the scope limit: all of this sees traffic that **crosses the
firewall**. It cannot see two hosts talking on the same segment, nor a
multi-homed host routing around it. That gap is what the Wazuh agents cover.

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
| Agent disconnected or stopped (event) | `sum by (agent_name) (count_over_time({job="wazuh"} \| json \| rule_id =~ "50[46]" [10m])) > 0` |

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
