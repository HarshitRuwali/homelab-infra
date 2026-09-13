# Reference

## Guest specifications

| Guest | Type | VMID | vCPU | RAM | Disk | Tier |
|---|---|---|---|---|---|---|
| `sec-wazuh` | VM | 200 | 4 | 8 GB | 40 GB | SSD |
| `sec-scan` | VM | 201 | 2 | 6 GB | 40 GB | HDD |
| `sec-crowdsec` | LXC | 210 | 1 | 1 GB | 8 GB | SSD |
| `sec-dns` | LXC | 211 | 1 | 512 MB | 8 GB | SSD |
| `sec-auth` | LXC | 212 | 1 | 1 GB | 8 GB | SSD |
| `sec-ntopng` | LXC | 213 | 2 | 2 GB | 16 GB | HDD |

Totals: **11 vCPU, 18.5 GB RAM, 120 GB disk**, of which 64 GB on SSD and 56 GB
on bulk storage.

### Why each guest is sized as it is

- **`sec-wazuh` 40 GB**: the only guest with real data growth. Working in
  [Operations](../operations/index.md).
- **`sec-crowdsec` 8 GB, 1 GB RAM**: the LAPI stores decisions and alerts in
  SQLite, which stays in the tens of MB at this scale. Almost all disk is OS.
- **`sec-dns` 8 GB, 512 MB RAM**: a single Go binary. Only the query log grows,
  at roughly 200 bytes per query. At 30 day retention it stays under 2 GB.
- **`sec-auth` 8 GB, 1 GB RAM**: Authelia's user and session database and
  OpenBao's raft store are both measured in MB for a homelab.
- **`sec-ntopng` 16 GB, 2 GB RAM**: Community Edition keeps timeseries in RRD,
  which is **fixed size by design**. The ClickHouse flow export that would grow
  without bound is an Enterprise feature.
- **`sec-scan` 40 GB, 6 GB RAM**: dominated by feeds, not results. SCAP and CVE
  data in PostgreSQL runs 10 to 20 GB, plus 1 to 2 GB of NVTs. RAM peaks during
  a scan, not at idle.

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
- **The rest are fine as unprivileged LXC**, including ntopng, *provided you
  collect NetFlow rather than sniff*. Sniffing needs `NET_ADMIN` and `NET_RAW`
  and pushes it to a VM for no benefit.

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
| firewall | `sec-ntopng` | 2055/udp | NetFlow export |
| admin | `sec-ntopng` | 3000/tcp | ntopng UI |
| admin | `sec-scan` | 9392/tcp, 443/tcp | Greenbone UI, **bound to loopback**; reach it over an SSH tunnel |

## Firewall rules

If your sandbox segment is blocked from reaching the trusted segment, the
agent paths need explicit allows, and **they must sit above the block rule** in
the evaluation order or the block shadows them:

```
pass   <sandbox net> -> sec-wazuh      tcp 1514, 1515
pass   <sandbox net> -> sec-crowdsec   tcp 8080
block  <sandbox net> -> <trusted net>          # must be BELOW the passes
```

Add the passes in the **same change** as the block. Adding them afterwards
means a window where sandbox telemetry silently stops.

Put the management UIs behind Authelia rather than exposing them directly, and
do not publish any of them through an internet-facing tunnel.

## Integration with the monitoring module

This module provisions and installs. It does not own the fleet, and it does not
own the dashboard.

| Concern | Owned by | How |
|---|---|---|
| Manager guest | `security/` | `provision-security-stack.sh`, `install/install-sec-wazuh.sh` |
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
| Authelia | Apache-2.0 | |
| OpenBao | MPL-2.0 | Linux Foundation fork of Vault 1.14.0 |
| ntopng Community | GPLv3 | Pro and Enterprise add retention, LDAP, SNMP |
| Greenbone GVM | GPLv2 | Community Feed is delayed relative to Enterprise Feed. Debian dropped the packages; runs from Greenbone's containers |
| nprobe | **proprietary** | Not GPL, unlike ntopng. See the caveat below |

### Caveats worth knowing before you commit

- **ntopng Community** is enough to verify segmentation. Long-term flow history,
  graphical reports, LDAP and SNMP are paid. If you want months of retained
  flows, use Zeek logs into Loki instead of buying up.
- **Greenbone Community Feed** is free but delayed and reduced relative to the
  Enterprise Feed. Fine for drift detection; not parity with a commercial
  scanner.
- **Greenbone is no longer packaged by Debian.** `gvm` is in **sid only** and is
  absent from bookworm, trixie and forky, so `apt-get install gvm` fails on any
  stable release. `install-sec-scan.sh` uses Greenbone's published Community
  Containers, which is the path their own documentation leads with.
- **`nprobe` is not open source**, although `ntopng` is GPLv3. ntopng cannot
  collect NetFlow without it, and collecting rather than sniffing is what keeps
  `sec-ntopng` an unprivileged container. If the licence does not suit you, the
  alternatives are to sniff instead (which needs `NET_ADMIN` and `NET_RAW`, so a
  VM) or to drop flow visibility and lean on the Wazuh agents.
- **Security Onion** is a tempting all-in-one bundle, and it is free, but its
  Elastic components ship under the Elastic Licence, which is source-available
  rather than OSI open source.
