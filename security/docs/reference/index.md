# Reference

## Guest specifications

| Guest | Type | VMID | vCPU | RAM | Disk | Tier | Bridge |
|---|---|---|---|---|---|---|---|
| `sec-wazuh` | VM | 200 | 4 | 8 GB | 25 GB | SSD | trusted (`vmbr0`) |
| `sec-scan` | VM | 201 | 2 | 6 GB | 40 GB | HDD | sandbox (`vmbr1`) |
| `sec-crowdsec` | LXC | 210 | 1 | 1 GB | 8 GB | SSD | trusted (`vmbr0`) |
| `sec-dns` | LXC | 211 | 1 | 512 MB | 8 GB | SSD | trusted (`vmbr0`) |

Totals: **8 vCPU, 15.5 GB RAM, 81 GB disk**, of which 41 GB on SSD and 40 GB on
bulk storage.

ntopng runs [on the firewall](../components/index.md#ntopng). Budget an
additional 2 GB RAM and 2 vCPU for the firewall VM, then measure.

### Why each guest is sized as it is

- **`sec-wazuh` 25 GB**: the only guest with real data growth. Working in
  [Operations](../operations/index.md). Started below the 40 GB the
  generous working gives, because growing a disk is a one-minute job.
- **`sec-crowdsec` 8 GB, 1 GB RAM**: the LAPI stores decisions and alerts in
  SQLite, which stays in the tens of MB at this scale. Almost all disk is OS.
- **`sec-dns` 8 GB, 512 MB RAM**: a single Go binary. Only the query log grows,
  at roughly 200 bytes per query. At 30 day retention it stays under 2 GB.
- **`sec-scan` 40 GB, 6 GB RAM**: dominated by feeds, not results. SCAP and CVE
  data in PostgreSQL runs 10 to 20 GB, plus 1 to 2 GB of NVTs. RAM peaks during
  a scan, not at idle.
- **ntopng on the firewall**: Community Edition keeps timeseries in RRD, which
  is **fixed size by design**. The ClickHouse flow export that would grow
  without bound is an Enterprise feature.

Right-size down further if you like, with one caution: `sec-scan` failing a
feed sync because the disk filled is a confusing failure to diagnose. Leave it
the headroom.

## Why VM and not LXC

LXC is lighter and boots faster, but it shares the host kernel, which matters
for anything wanting kernel tunables, raw sockets or its own memory locking.

- **`sec-wazuh` must be a VM.** The indexer is OpenSearch, which wants
  `vm.max_map_count=262144` and unlimited `memlock`. Those are kernel-level
  settings an unprivileged container cannot own. Forcing it into a privileged
  container works but is fragile across upgrades.
- **`sec-scan` must be a VM.** Greenbone performs raw-socket scanning and needs
  `NET_RAW`. Possible in a privileged LXC, but a scanner is exactly the
  workload you do not want running privileged beside everything else.
- **`sec-crowdsec` and `sec-dns` use unprivileged LXC.** Their services do not
  need their own kernel. ntopng captures directly on the firewall.
- **Both LXCs get `nesting=1`**, the default the Proxmox UI applies to every
  unprivileged container. The Debian 13 template's systemd cannot mount `/tmp`
  or `/run/lock` without it, and the guest boots with failed units.

## Ports

| From | To | Ports | Purpose |
|---|---|---|---|
| every monitored host | `sec-wazuh` | 1514/tcp | Agent events |
| every monitored host | `sec-wazuh` | 1515/tcp | Agent enrollment |
| admin | `sec-wazuh` | 55000/tcp | Wazuh API |
| admin | `sec-wazuh` | 443/tcp | Dashboard |
| every monitored host | `sec-crowdsec` | 8080/tcp | CrowdSec LAPI |
| LAN clients | `sec-dns` | 53/tcp, 53/udp | DNS |
| admin | `sec-dns` | 3000/tcp | AdGuard UI |
| admin | the firewall | 3000/tcp | ntopng UI. Listens on **every** firewall interface; keep WAN closed |
| the firewall | the central stack | 5514/tcp | Suricata EVE syslog into Loki. Not 1514, which is Wazuh's |
| admin | `sec-scan` | 9392/tcp, 443/tcp | Greenbone UI, **bound to loopback**; reach it over an SSH tunnel through your jump host |
| `sec-scan` | everything it scans | any | Scans, originated from the sandbox bridge. Trusted-side targets see the firewall's address |

## Firewall rules

If your sandbox segment is blocked from reaching the trusted segment, the
agent paths need explicit allows, and **they must sit above the block rule** in
the evaluation order or the block shadows them:

```
pass   <sandbox net> -> sec-wazuh      tcp 1514, 1515
pass   <sandbox net> -> sec-crowdsec   tcp 8080
pass   sec-scan      -> <trusted net>          # only if it should scan the trusted side
block  <sandbox net> -> <trusted net>          # must be BELOW the passes
```

Add the passes in the **same change** as the block. Adding them afterwards
means a window where sandbox telemetry silently stops.

The `sec-scan` pass is a real trade: it lets one sandbox host reach the whole
trusted segment, which is exactly what makes a scanner useful and exactly what
makes a compromised one dangerous. Without it, `sec-scan` covers the sandbox
only.

Keep the management UIs unpublished, and reach them over SSH tunnels.

## Integration with the monitoring module

This module provisions and installs. It does not own the fleet, and it does not
own the dashboard.

| Concern | Owned by | How |
|---|---|---|
| Manager guest | `security/` | `provision-security-stack.sh`, `install/install-sec-wazuh.sh` |
| Guest host metrics and logs | `ansible/` | `monitored` platform groups plus `security_guests`; `playbooks/onboard.yml` installs Alloy and update metrics |
| Per-guest resource dashboards | `monitoring/` | `grafana/dashboards/servers/sec-*.json`, installed by Ansible's dashboard role |
| Agents on fleet hosts | `ansible/` | `roles/wazuh_agent`, gated by the `wazuh_agents` group |
| Alert display and routing | `monitoring/` | Loki datasource, Grafana unified alerting |

Wazuh alerts reach Grafana by tailing `/var/ossec/logs/alerts/alerts.json` with
the Alloy collector that monitored hosts already run, labelled `job="wazuh"`.
Suricata's EVE output goes to Loki as `job="suricata"`. Neither needs new
collector software.

## Licences

Verified 2026-09-09 against primary sources, because several projects in this
space have changed licence recently.

| Tool | Licence | Notes |
|---|---|---|
| Suricata | GPLv2 | Bundled with OPNsense and pfSense |
| CrowdSec | MIT | Engine has no paywalled features; console tier optional |
| Wazuh | GPLv2 | Indexer and dashboard on Apache-2.0 OpenSearch. No feature paywall |
| AdGuard Home | GPLv3 | |
| ntopng Community | GPLv3 | Runs on the firewall via `os-ntopng`. Pro and Enterprise add retention, LDAP, SNMP |
| Greenbone GVM | GPLv2 | Community Feed is delayed relative to Enterprise Feed. Runs from Greenbone's containers |

### Operational limitations

- **ntopng Community** is enough to verify segmentation. Long-term flow history,
  graphical reports, LDAP and SNMP are paid features.
- **Greenbone Community Feed** is free but delayed and reduced relative to the
  Enterprise Feed. Fine for drift detection; not parity with a commercial
  scanner.
- **Greenbone uses the published Community Containers.** `gvm` is in **sid only** and is
  absent from bookworm, trixie and forky, so `apt-get install gvm` fails on any
  stable release. `install-sec-scan.sh` uses Greenbone's published Community
  Containers, which is the path their own documentation leads with.
