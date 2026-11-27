# Dashboards

Provisioned from `grafana/dashboards/` and loaded at startup, across two
Grafana folders: **Monitoring** (`fleet/`, fleet-wide and `$host`-filterable)
and **Servers** (`servers/`, one dashboard per host, no filter needed). Both
are read-only in the UI in the same way alert rules are: edit the JSON and
redeploy.

!!! danger "The two provider paths must never nest"
    Grafana's file reader walks each provider `path` **recursively**. A
    provider pointed at the root of the dashboards tree would also claim
    everything under `servers/`, and two providers provisioning the same UID
    makes those dashboards flip between folders and log duplicate-provisioning
    errors on every scan.

    That is the whole reason the fleet dashboards sit in `fleet/` rather than
    at the root: it makes the two paths disjoint siblings. Do not "tidy" them
    back up a level.

## VM Fleet Overview

`uid: vm-fleet-overview`: the "is the estate healthy" view.

| Panel | Query intent |
|---|---|
| Collectors Reporting (10m) | hosts that pushed in the last 10 minutes |
| Hosts Seen (6h) | distinct hosts over 6h, so a recently dead one still appears |
| Shortest VM Uptime | spots an unplanned reboot |
| Fleet Logs 5m | total ingest rate, journal plus Docker |
| **Fleet Freshness** | per-host age of the newest sample |
| Pending Package Updates | `sum by (host) (apt_upgrades_pending)` |
| Reboot Required | count of hosts awaiting a restart |
| **Hosts Needing Reboot** | *which* hosts, and for how long |
| Top CPU / Memory / Root Disk | `topk(10, …)` |
| Fleet Warnings and Errors | Loki, case-insensitive error regex |

!!! tip "Fleet Freshness has no `$host` filter, on purpose"
    It is the authoritative "is everyone here" view. A saved single-host
    selection must not be able to hide an outage.

!!! info "Reboot Required is a count; the table names them"
    `fleet_reboot_required_since_timestamp_seconds` is emitted **only** while
    a reboot is outstanding, so a row disappears by itself once the host comes
    back. The `or` clause is a fallback for a host reporting
    `node_reboot_required` that has not yet written the timestamp, which would
    otherwise show a count with an empty table.

    Colour steps: yellow immediately, orange at 3 days, red at 7 days,
    matching when `fleet-reboot-required-too-long` fires.

## Services and Logs

`uid: services-monitoring`: the "what is running" view.

| Panel | Notes |
|---|---|
| Active / Failed Systemd Units | fleet totals |
| Containers Seen | `count(count by (host, name) (container_last_seen{name!=""}))` |
| Error Logs 5m | Loki error regex |
| **Container Inventory** | one row per container: CPU, memory, uptime, restarts |
| Systemd Units by State | by host and state over time |
| Container CPU / Memory | per container time series |
| Log Volume 5m | journal vs docker, split by `source` |
| Systemd Journal Logs / Docker Logs | raw streams |

!!! tip "Scan the Restarts 30m column first"
    Anything above 1 is a container fighting with something. Uptime resetting
    between refreshes means the same thing. The column is colour-graded:
    green 0, yellow 1, red 3+.

!!! failure "If Container Inventory is blank but Docker Logs is not"
    The cAdvisor root drop-in is missing. See
    [Container metrics](container-metrics.md).

## System Overview

`uid: system-overview`: per-host resource detail. CPU, memory, root disk,
network throughput, uptime and host count.

A hardware row underneath carries the basics: hottest CPU, hottest disk,
slowest chassis fan, fans stalled, CPU temperature by host and fan speeds.
Only t7920 and the Pis have real sensors (the LXC guests echo the
hypervisor's), so these panels select `role="hypervisor"` and `role="pi"`.
Each panel links to the t7920 dashboard for the detail.

## Network

`uid: network-monitoring`: throughput, packet rates, interface errors and
drops, TCP retransmit share, conntrack usage, and an interface inventory.

!!! warning "Every panel excludes virtual interfaces"
    `veth`, `tap`, `fwbr`, `fwln`, `fwpr`, `vmbr`, `docker`, `cni`, `virbr`.
    On a Docker or Proxmox host those churn constantly and are routinely left
    administratively down, so an unfiltered view drowns the real NICs and an
    unfiltered link-down alert fires forever. What survives is the set you
    would actually name: `ens18`, `eth0`, `eth1`, `nic0`, `nic1`, `tailscale0`,
    `tun0`, `wlan0`.

Per-guest traffic lives on exactly the `tap` and `veth` devices this dashboard
drops, which is why it has its own: [Guest Traffic](#guest-traffic).

## Guest Traffic

`uid: guest-traffic`: how much each Proxmox guest sends and receives, read from
the hypervisor's side of each guest NIC and named by
`ansible/roles/pve_guest_metrics`.

| Panel | Query intent |
|---|---|
| Hypervisor NICs In / Out | the machine's physical NICs, tunnels excluded |
| Busiest Guest Now | the guest with the most sent plus received |
| **Guest Names Refreshed** | age of the naming file; yellow past 10 minutes, red past 20 |
| Physical NICs | in above the line, out below, per NIC |
| Sent by Guest / Received by Guest | per guest NIC, from the guest's point of view |
| Sent per Bridge | what the guests put onto each segment |
| Dropped Packets per Guest NIC | a guest not keeping up with its traffic |
| Most Data Sent / Received, Selected Range | totals over the time picker |
| Per Guest NIC Totals | 24 hours and 7 days, one row per NIC |

The totals join each five-minute increase to the guest name at that moment, then
sum them over the range. Proxmox reuses a VMID after a guest is deleted, so the
device `tap102i0` can name two guests in one window; joining the whole-range
increase to today's name would credit the old guest's traffic to the new one.

It sees what no other dashboard here can: guests that run no collector (the
firewall VM, Windows), and traffic between two guests on the same bridge, which
never reaches a firewall. The `$guest` and `$bridge` pickers filter every panel
except the physical NICs.

!!! warning "Empty until the naming role is deployed"
    The traffic counters are already in Prometheus; the names are not. Until
    `update-metrics.yml` has run on the hypervisor with
    `pve_guest_metrics_enabled` set, the host picker is empty and so is every
    panel. **Guest Names Refreshed** reads "No data" in exactly that state.

!!! info "Seven days is the longest total"
    The central Prometheus keeps 7 days (`PROMETHEUS_RETENTION` in
    `scripts/lxc-install.sh`), so the totals table stops at 7 days, and a
    wider time picker quietly shows less than its label says. Monthly figures
    need longer retention first.

!!! note "Tunnels are excluded from the physical NICs"
    `tailscale0`, `wg*` and `tun*` carry traffic that has already crossed a
    physical NIC, encrypted. Counting both would count it twice, so this
    dashboard drops them, unlike the Network dashboard.

!!! tip "A router guest double-counts, and that is correct"
    The firewall VM receives every forwarded byte on one NIC and sends it on
    another, so it usually tops **Busiest Guest Now** and shows traffic on both
    bridges. It is doing exactly that much work.

## Router TPLink Blr

`uid: router-tplink-blr`: per-client traffic on the TP-Link router on
skullsaints' LAN, a different network from the home LAN. Data comes from
`ansible/roles/tplink_router_poll` on skullsaints; see
[the metrics](../reference/metrics.md#tp-link-router).

| Panel | Query intent |
|---|---|
| Poller, Last poll | whether the poller is reading the router, and how long ago it ran |
| Clients online, Downloaded, Uploaded | totals over the time picker, from the Wi-Fi counters |
| Usage per client in the selected range | download, upload and total per client, largest first |
| Hourly usage per client, Daily usage per client | stacked bars, download plus upload |
| Download / Upload rate per client | five-minute averages in bits per second |
| Clients | every client the router knows, with IP, MAC, band and whether it is online |
| Signal, Link rate, Retransmissions per client | Wi-Fi quality |
| Internet, Router CPU, Router memory used | the router itself |
| Router's own internet counter vs Wi-Fi clients | for comparison only; the router's counter misses hardware-forwarded traffic |
| Router log forwarding | whether the router's own log still reaches skullsaints |
| History, 30 days (Loki) | usage per client and per day over up to 30 days, every connection and disconnection, and the router's log |
| Monthly history (permanent) | Usage per client per month, Top 10 clients per month | `net.monthly_client` for router `blr`, kept forever |

The `$client` picker filters the per-client panels by MAC. Two devices can share
a hostname, so every legend shows the MAC as well.

!!! info "Gaps are expected"
    The poller skips a minute while someone is logged in to the router's web
    page, and nothing is collected while skullsaints is off or on another
    network. Alloy on skullsaints buffers what it has collected while the link
    to central is down and sends it on reconnect. Usage totals use
    `increase()`, which adds across counter restarts. A gap just loses the
    traffic inside it.

!!! info "How far back"
    The live panels read Prometheus, which keeps 7 days. The History row reads
    Loki, which keeps 30. Beyond that, the usage ledger on skullsaints
    (`/var/lib/tplink-router-poll/usage.db`) keeps every hour and every
    connection for good, and is copied to ubuntu-dev nightly
    (`~/.local/share/tplink-router-blr`). Read it with `--report`; see
    [the role](https://harshitruwali.github.io/homelab-infra/ansible/reference/playbooks/#tplink_router_poll).

## Network dashboards: tiers and the history row

[Internet and Usage](#internet-and-usage), [Home ISP](#home-isp), [Inter-LAN](#inter-lan)
and [Router TPLink Blr](#router-tplink-blr) share two additions, described in
[Data flow](../architecture/data-flow.md#long-term-network-history).

- **A `Prometheus` picker (`ds`)** at the top chooses the tier every Prometheus panel
  reads: `Prometheus (network, 90 d)` (the default, 30 s resolution, 90 days) or
  `Prometheus` (the central one, 15 s, 7 days, every metric). A range beyond 7 days
  needs the 90-day tier. Right after the role is first deployed the 90-day store is
  empty; pick `Prometheus` until it fills. Loki panels do not follow the picker.
  The quick ranges include the last 30 and 90 days.
- **A row "Monthly history (permanent)"** at the bottom, built on the PostgreSQL
  datasource `Network history` (read-only role `net_history_reader`). These panels
  always show the last 24 months and ignore the time picker. They read the monthly
  views of the `net` schema; the outage lists read `net.outage_events`. They stay empty
  until `net-history` has run. Monthly availability is `1 - downtime / observed time`
  from the monthly sums.

## Internet and Usage

`uid: internet-usage`: is the internet up at each site, how long were the
outages, and how much data was used and by whom. Default range 7 days. The
`$site` picker (home, blr, homelab; multi, All by default) filters the probe
panels. Outage data comes from `internet-probe` on rpi5, skullsaints and
ubuntu-dev; see [Routers and firewall](routers.md#internet-uptime) and
[the metrics](../reference/metrics.md).

| Row | Panels | Query intent |
|---|---|---|
| Internet now | Internet now, Availability, Outages, Downtime, Current outage, Last outage ended, Last outage duration, Probe freshness, Ledger | one value per selected site; availability is `1 - downtime / range` from confirmed outages; freshness is red over 60 s; Ledger is red when the poller cannot write its outage ledger |
| Uptime | Internet up or down, Gateway up or down, Downtime per day, Outages per day | state timelines (lowest value per interval, so a short outage is not averaged away) and daily bars |
| Outages (30 days, Loki) | Outages, Outage events and blips | one row per closed outage (site, start, end, duration, cause, closed by) and the raw event lines, both from the `internet-probe.service` journal |
| Latency and loss | Round-trip time per target, Gateway round-trip time, Failed-round ratio, Target failures per minute | probe round-trip times and how often rounds and single targets fail |
| Home internet (ER605) | Download and upload rate, Downloaded and Uploaded in range, Daily usage, Hourly usage | the ER605 WAN port counters (`er605_if_octets_total{role="wan"}`, in is download, out is upload) |
| Home internet per WAN | Daily usage per WAN, Household usage per WAN in range | `er605_if_octets_total{role="wan"}` joined to `er605_wan_info` on `interface`; with one WAN, or before the poller exports `er605_wan_info`, everything is `wan1` |
| Who used how much | Homelab vs home internet, Homelab segments (bar gauge and table), Top 10 guests sent and received, Blr usage per client and per day, What is not measured | OPNsense per segment, the hypervisor's view per guest (the Guest Traffic queries), the Archer C5's Wi-Fi clients (the Router TPLink Blr queries) |
| Monthly history (permanent) | Availability, Downtime, Outages per month; Household download and upload, usage per WAN, bytes per segment per month; Top 5 guests per month; Outage history | `net.monthly_internet`, `monthly_wan`, `monthly_segment`, `monthly_guest` and `net.outage_events` (kinds `internet`, `path`, `wan`) |

Links at the top open [Router TPLink Blr](#router-tplink-blr),
[Guest Traffic](#guest-traffic) and [Network](#network); each of those links
back here.

!!! warning "Downtime means confirmed outages only"
    An outage is 2 failed rounds in a row (about 10 s at the default 5 s round).
    A single failed round is a blip: it shows in the up/down timeline and the
    event log but not in the availability, outage or downtime figures.

!!! warning "OPNsense WAN is an upper bound for the homelab"
    OPNsense WAN also carries traffic between the home LAN and the homelab
    (Plex to a Mac at home, for example), so it overstates the homelab's
    internet use, and ER605 WAN minus OPNsense WAN is not the household's
    share. Per-guest bars count all guest traffic, not only internet.

!!! info "No per-device usage for home devices"
    The ER605 forwards in hardware and its per-IP statistics see about 4% of
    the traffic. Per-device home usage needs the planned port mirror; see
    [ER605](routers.md#er605-home-router). Home usage is a household total.

!!! info "How far back"
    Panels that read Prometheus (everything except the Outages table and the
    event log) reach back 7 days, Loki 30. Beyond that, each host's outage
    ledger keeps every outage; read it with `internet-probe.py --report`.

## Inter-LAN

`uid: inter-lan`: is each site and segment reachable from the others, and how
much traffic flows between each pair of segments. Default range 24 hours. The
`$site` picker filters the connectivity row; `$src_segment` (From) and
`$dst_segment` (To) filter the traffic row. Connectivity comes from the
`internet-probe` LAN targets; segment pairs come from `segflows.py` on the
firewall; see [Routers and firewall](routers.md#inter-segment-traffic) and
[the metrics](../reference/metrics.md#inter-segment-traffic).

| Row | Panels | Query intent |
|---|---|---|
| Connectivity | LAN targets up or down, LAN availability and outages in range, LAN round-trip time per target, LAN outages (30 days, Loki) | one lane per site and target from `internet_probe_lan_up` (lowest value per interval, so a short outage shows); availability is `1 - downtime / range` from confirmed outages; the Loki table lists closed outages from the `lan_up` journal lines |
| Traffic between segments | Bytes between segments (bar gauge), Segment pairs table, Matrix, Rate between segments | `increase()` and `rate()` of `opnsense_segflow_*_total` per source and destination segment; the matrix has the source in rows and the destination in columns |
| Segments | Received per segment, Transmitted per segment | `opnsense_interfaces_{received,transmitted}_bytes_total` for LAN, TENANT, DMZ, LAB, LEASE and WAN; these see every interface, unlike the pair counters |
| Tailnet | Tailnet received per host, Tailnet transmitted per host | `node_network_{receive,transmit}_bytes_total{device="tailscale0"}`, the inter-site link |
| Coverage | Collector lag, Last run, Run duration, Interfaces NetFlow captures, Segments NetFlow does not capture, What is not measured | whether `segflows.py` is running and what it can see; WAN is never captured and is not listed, so an empty table means every internal interface is captured |
| Monthly history (permanent) | LAN availability and outages per target, bytes per segment pair, bytes per segment, tailnet per host, LAN outage history | `net.monthly_lan`, `monthly_segflow`, `monthly_segment`, `monthly_tailnet` and `net.outage_events` (kind `lan`) |

Links at the top open [Internet and Usage](#internet-and-usage),
[Guest Traffic](#guest-traffic) and [Network](#network).

!!! warning "Only captured interfaces produce flows"
    The pair panels count what crosses a NetFlow-captured firewall interface;
    the captured interfaces are listed in the Coverage row. A flow between two
    uncaptured segments is invisible, and traffic inside one segment or inside
    the home LAN switch never reaches the firewall, captured or not. The
    Segments row reads whole-interface counters, so it still shows the volume
    of an uncaptured segment.

!!! info "How far back"
    Panels read Prometheus (7 days) except the Loki outage table (30 days).
    Beyond that, the firewall's ledger keeps hourly pair totals and the top
    host pairs per day for good: `segflows.py --report pairs|hosts|hour|day`.

## Home ISP

The household internet and the ER605 router behind it: usage, quality, router
health and outages. Folder Monitoring (`fleet/`), uid `home-isp`, default range 24 hours,
refresh 1 minute. The ER605 is read over SNMPv3 every 30 s by
[`er605_snmp`](https://harshitruwali.github.io/homelab-infra/ansible/reference/playbooks/#er605_snmp)
on ubuntu-dev; availability and latency come from the `internet-probe` poller on
rpi5 (`site="home"`), which sits behind the ER605 only. Metrics are in
[the reference](../reference/metrics.md#er605-home-router). Links at the top open
[Internet and Usage](#internet-and-usage) and [Router TPLink Blr](#router-tplink-blr).

| Row | Panels | Query intent |
|---|---|---|
| Overview | Internet now, Availability over the range, WAN download now and upload now, Downloaded and Uploaded in range, PPPoE session age, PPPoE reconnects in range, Router uptime, Poller last read | the state of the line and the router at a glance; availability is `1 - downtime / range` from confirmed outages; the poller is red over 600 s |
| Usage | Household WAN throughput, Hourly volume, Daily volume, Daily volume 30 days (ledger), Downloaded and Uploaded last 24 h, p95 and peak 1-minute rate, Household vs t7920, Homelab (OPNsense WAN) vs household, Homelab share | WAN counters `er605_if_octets_total{role="wan"}`; p95 and peak are `quantile_over_time` and `max_over_time` over a 1-minute rate subquery across the selected range; the 30-day bars come from the ledger's `usage` lines in Loki |
| Quality | Round-trip time per target, Gateway round-trip time, Failed-round ratio, WAN and LAN errors and discards, PPPoE session age, PPPoE reconnects | probe latency and loss from rpi5, router error counters, and the PPPoE session sawtooth and reconnect bars |
| Router | Router CPU per core, Router memory, Router uptime, Interface up or down, PPPoE session traffic | `er605_cpu_percent`, `er605_memory_bytes`, `er605_uptime_seconds`, `er605_if_up`, `er605_pppoe_octets_total` |
| Outages (30 days, Loki) | Internet outages (home), PPPoE reconnects, Outage events and blips, Router events | the `internet-probe.service` journal on rpi5, and the `pppoe_reconnect`, `router_reboot` and `poll_error` lines of the `er605-poll` container |
| History, 30 days (Loki) | Downloaded and Uploaded in range (ledger), Hourly volume (ledger), Usage lines | the poller's hourly `usage` lines, for ranges beyond Prometheus's 7 days |
| WAN `$wan` (one row per WAN) | WAN state, Downloaded, Uploaded, PPPoE reconnects, Path availability, WAN up or down, Path up or down, Throughput, Daily volume, PPPoE session age and reconnects, Path round-trip time and failures | the row repeats for each value of the `$wan` picker (from `er605_wan_info`; `wan1` alone while there is one WAN or before the poller exports it). Byte panels join `er605_if_octets_total{role="wan"}` to `er605_wan_info` on `interface`; path panels read `internet_probe_path_*` of the path named like the WAN and say "no path probe" until one is configured. The overview totals stay summed over all WANs |
| Monthly history (permanent) | Download and upload per month per WAN, PPPoE reconnects, availability, path availability, Monthly volume per WAN, Outage history (home) | `net.monthly_wan` (a `wan = 'total'` row sums the WANs; the table shows `partial_days`, the days of the month whose data covered less than the whole day), `monthly_internet`, `monthly_path` and `net.outage_events` |

!!! warning "OPNsense WAN is an upper bound for the homelab"
    OPNsense WAN also carries traffic between the home LAN and the homelab
    (Plex to a Mac at home, for example), so the homelab share overstates its
    internet use, and ER605 WAN minus OPNsense WAN is not the rest of the
    household's share.

!!! info "Rates are smoothed, totals are exact"
    The ER605 forwards in hardware and syncs its counters tens of seconds late,
    so panels use 5 minute rates, and the p95 and peak use 1 minute rates, the
    finest that mean anything. Volumes are exact. Volume panels use binary units
    (GiB), as the other router dashboards do; the poller's `--report` prints
    decimal GB.

!!! info "How far back"
    Panels that read Prometheus reach back 7 days, Loki 30. Beyond that the
    ledger on ubuntu-dev keeps every hour: `--report hour|day|month|reconnects`,
    see [the role](https://harshitruwali.github.io/homelab-infra/ansible/reference/playbooks/#er605_snmp).
    A ledger hour is a UTC hour, so a Loki hourly bar sits up to an hour after
    its traffic and a daily bar is a UTC day.

## Suricata Alerts

`uid: suricata-alerts`: what the IDS on the firewall is flagging. Suricata runs
on OPNsense in alert-only mode and ships its EVE alerts as syslog to the central
collector, which stores them in Loki as `host="opnsense"`, `job="suricata"`.
Every query selects `job="suricata"`: the firewall's other streams (filter log,
resolver log, `pfstats`) share `host="opnsense"` and would be scanned for
nothing.
Setup and the end-to-end check are in the security module's wiring page,
section 2.

| Panel | Query intent |
|---|---|
| Alerts | every alert in range, after the pickers |
| **High Severity** | severity 1 only, and it ignores the Severity picker so a narrowed view never hides one |
| Signatures / Source Addresses | distinct rules that fired, and distinct sources behind them |
| **Firewall Log Lines** | everything Suricata sent, alerts or not; red at zero. Counts `job="suricata"` only: the firewall's other streams would hide a broken Suricata path |
| Alerts Over Time | per interval, stacked by severity |
| Top Signatures / Sources / Destinations | the rules, hosts and ports doing the talking |
| Recent Alerts | one line per alert with its SID; expand for every field |
| Engine Messages | rule reloads and flowbit warnings, not alerts |

Two pickers: **Severity** (1 high, 2 medium, 3 low) and **Search**, a
case-insensitive regex over the raw alert JSON, so an address, a SID or part of
a signature all work.

!!! warning "Zero alerts is only good news if Firewall Log Lines is not zero"
    Suricata only sends something when a rule fires or the engine reloads, so a
    quiet dashboard and a broken pipeline look the same in every alert panel.
    The daily rule update makes the engine reload and log, which is what makes
    **Firewall Log Lines** a health check: zero across a range that includes an
    update means the syslog path is broken, not that the network is clean.

!!! tip "Tune noisy rules in OPNsense, not here"
    UPnP discovery against the firewall trips sid 2019102 (SSDP amplification)
    within minutes on a normal LAN; here it is disabled for that reason. Disable
    a rule like that by SID under **Services > Intrusion Detection > Rules**
    rather than filtering it out of the dashboard; a filter hides it here and
    nowhere else.

## Tenant Activity

`uid: tenant-activity`: what the work VMs (`vtnet2`, 10.10.60.0/24) and the
leased VMs (`vtnet5`, 10.10.90.0/24) are doing. From Loki it reads the
firewall's filter log and `pfstats` line, Unbound's query log, Suricata's alerts
and the Proxmox firewall's drops; from Prometheus, the hypervisor's view of each
guest NIC. Each rule in `rules-tenant.yaml` opens one of its panels; the rules
are in [Alerting](alerting.md#tenant-and-lease-alerts). It shows what the
firewall and the hypervisor saw, which is not everything a tenant did: see
[what is not covered](https://harshitruwali.github.io/homelab-infra/security/wiring/#what-is-not-covered).

| Panel | Query intent |
|---|---|
| State Table Used | pf state table fill, red at 80% |
| Banned Sources | sources in the `virusprot` table now |
| Limit Hits, Selected Range | per-source and state limit refusals in the time range |
| Suricata Alerts, Selected Segment | alerts on the selected interface in the range |
| State Table Usage | fill level over time, with the 50% and 80% alert lines |
| Limit Hits and Banned Sources | the three small counters together; all should be zero |
| New Flows per Minute | passed and blocked new flows per interface |
| Interface Packet Rates | packets/s in and out of the lease and work interfaces; the lease alert is at 15000, 75% of one lease's ceiling |
| Egress per Guest | what each guest on the tenant and lease bridges (`vmbr2`, `vmbr5`) sends, from the tap device, with the 10 and 12.5 MB/s lines |
| Conntrack Entries per Tenant VM | conntrack entries per tenant or lease address |
| Packets Dropped by the Lease Limit | packets/s each lease's packet-rate ceiling dropped, per guest, averaged over 15 minutes, with the 100 packets/s alert line |
| Lease Packet Limit Active | 1 while a running lease has its ceiling, 0 when it lost it |
| Tenant Firewall Active | 1 while the Proxmox firewall is filtering a tenant or lease VM, 0 when it is not |
| Guest Exporter Age | seconds since the Proxmox guest exporter last completed a run; yellow past 10 minutes, red past 20 |
| Top Destinations by Flow Count | where the selected segment's new flows went |
| Flows by VM | which VM opened them, by source address |
| Suricata Alerts by Interface and Signature | what the IDS flagged, per interface |
| Top DNS Names | the names the clients asked Unbound for, most asked first; plain DNS on port 53 only |
| DNS Queries per Client | queries per client address over time; plain DNS on port 53 only |
| NXDOMAIN Replies per Client | clients getting "no such name" replies, the sign of a generated-domain scan |
| Blocked Attempts by Chain | what the Proxmox firewall dropped, per rule chain, Tailscale probing left out; an IN chain counts packets to a VM, so only OUT chains feed the alerts |
| Blocked Attempts by Chain and VM | the same per chain and guest `vmid`, over the range; for an IN chain the `vmid` is the receiver |
| Proxmox Firewall Drops by Rule | the exact counter behind those log lines (`smtp`, `private`, `private_in`, summed over IPv4 and IPv6), over the preceding 15 minutes |
| Recent Flows | the newest filter log lines, passes and blocks |

Two pickers. **Segment** (work `vtnet2`, lease `vtnet5`, or both) drives the
firewall panels: new flows, destinations, flows by VM, Suricata and Recent
Flows. It does not drive the DNS, Proxmox firewall, packet rate or per-guest
panels, because those are keyed by client address, chain or guest, not by
firewall interface. **Guest** narrows Egress per Guest.

!!! info "The source address is the VM, before NAT"
    The filter log records the address a VM used, so a row in Flows by VM
    names a VM. Unbound logs the client address the same way. The Proxmox
    firewall log carries a `vmid` instead; Guest Traffic or the Proxmox UI
    turns it into a name.

!!! note "Panels that need the newer logs and series"
    Conntrack Entries per Tenant VM, Proxmox Firewall Drops by Rule, Packets
    Dropped by the Lease Limit, Lease Packet Limit Active and Tenant Firewall
    Active stay empty until `roles/pve_guest_metrics` writes
    `fleet_pve_tenant_conntrack_entries`,
    `fleet_pve_firewall_dropped_packets_total`,
    `fleet_pve_tenant_policed_packets_total`,
    `fleet_pve_tenant_packet_limit_active` and
    `fleet_pve_tenant_firewall_active`; see the
    [metrics catalogue](../reference/metrics.md#proxmox-guest-nics). The two
    that come from a counter use a 15 minute window, because the exporter
    writes a new value only every 5 minutes. The Blocked Attempts panels
    need the hypervisor's Alloy to ship `/var/log/pve-firewall.log`
    (`alloy_pve_firewall_log`). Everything else needs the firewall's syslog
    to reach the central Alloy listener; see the security module's
    [wiring page](https://harshitruwali.github.io/homelab-infra/security/wiring/).

!!! warning "Range matters: logs are kept 30 days on the live install"
    The new flow panels read every filter log line in the range. A range of
    7 days or more on a busy segment is slow, and anything beyond the Loki
    retention is simply absent. See
    [Retention](../operations/retention.md#loki-retention-for-the-tenant-logs).

## Disk Health

`uid: disk-health`: SMART inventory, temperature, wear, bad sectors over time,
plus filesystem and inode health.

The SMART half only populates for hosts in the `metal` group; the filesystem
half covers everything. See [SMART disk health](../reference/metrics.md#smart-disk-health)
for why virtualised hosts cannot report it.

## Host Processes

`uid: host-processes`: htop as a dashboard. The header line (load, cores,
uptime, tasks, threads, running, blocked), per-core CPU meters, memory and
swap meters, and the process list with CPU%, MEM%, RSS, threads, FDs and age.

CPU% is **per-core-equivalent**, matching htop: 100 means one saturated core,
400 means four. Processes are grouped by command name, so a 32-worker service
is one row with a large Count rather than 32 near-identical rows.

!!! note "The host picker only lists hosts with the process exporter enabled"
    It is driven by `label_values(namedprocess_namegroup_num_procs, host)`, so
    it cannot offer a host that has no data. The exporter is opt-in; see
    [`alloy_enable_process`](https://harshitruwali.github.io/homelab-infra/ansible/reference/variables/#collector).

## GPU

`uid: gpu-monitoring`: nvtop as a dashboard. Gauges for the header numbers,
the utilisation and VRAM graphs nvtop scrolls, current clocks against maximum,
active throttle reasons, and the process table showing which PID holds the
memory.

!!! tip "Read utilisation and memory-bus together"
    `nvidia_smi_utilization_gpu_ratio` is the fraction of time at least one
    kernel was resident, **not** how much of the GPU's compute is in use: a
    tiny kernel pinning one SM reads 100%. High memory-bus utilisation
    alongside low GPU utilisation means the workload is bandwidth-bound.

!!! info "An empty process table is not a fault"
    It means no process holds a CUDA context right now. The table needs
    `--collect.compute-apps`, which `roles/gpu_exporter` enables by default.

## Servers folder

`grafana/dashboards/servers/<host>.json`, `uid: host-<host>`, one per host in
`monitored`: CPU, memory, disk, network, systemd units, container CPU/memory,
pending updates, and logs (errors/warnings plus the full stream), all
hardcoded to that host rather than filtered through `$host`. These are
detail views to jump into from an alert, not summaries; the fleet-wide
dashboards above stay the "is everyone healthy" entry point.

The security guests have four committed dashboards: `sec-wazuh`, `sec-scan`,
`sec-crowdsec` and `sec-dns`. All show host resources, systemd state, pending
updates and logs. `sec-scan` also shows Greenbone's Docker container metrics.
They use the standard Alloy pipeline and existing Prometheus/Loki datasources.
Add the guests to the appropriate `monitored` platform groups and the
`security_guests` overlay, then use the regular Ansible onboarding play. See
[Host monitoring in Grafana](https://harshitruwali.github.io/homelab-infra/security/getting-started/#host-monitoring-in-grafana).

`ubuntu-ai` additionally has a GPU row: utilization, memory, temperature,
power and fan, both as top-strip stats and as per-GPU time series (labelled
by `uuid`, so a multi-GPU box gets one line per card). See
[GPU telemetry](../reference/metrics.md#gpu-telemetry) for the metric names,
and `ansible/roles/gpu_exporter` for how it gets
there. Empty GPU panels on a host that never had `nvidia-smi` mean the
exporter correctly never installed, not a scrape failure.

`t7920` additionally has the detailed hardware view. The centrepiece is the
**Chassis Fan Map**, a canvas panel laid out as the two fan figures in Dell's
Owner's Manual (main side and drive side), with every fan at its physical
position showing live RPM, coloured by health. Positions the BIOS does not
list are drawn as "not fitted". Around it: both CPU package temperatures
against the alert thresholds, every fan by name as time series and as bars,
the per-socket core temperature spread, a per-core table sorted hottest
first, and the Dell SMM board, PCH and NVMe temperatures. See
[Hardware sensors](../reference/metrics.md#hardware-sensors) for where each
series comes from.

Add a new host's dashboard by copying an existing `servers/*.json`, replacing
every `host="<name>"` and the `uid`/`title`, and re-running the JSON
validation command below.

!!! warning "Query the label the host *pushes*, not its inventory name"
    They are usually the same, but not always. `monitor-lxc` pushes as
    `host="main-server"` because `monitor_hostname` is pinned in
    `host_vars/monitor-lxc.yml` (it has history predating the Ansible
    rollout, and renaming it would fork its identity in Prometheus and Loki),
    so `servers/monitor-lxc.json` queries `main-server` throughout and says so
    in its title. Check `monitor_hostname` before writing a new one:

    ```bash
    ansible-inventory --host <name> | grep monitor_hostname
    ```

## Conventions

**Stat panels with a unit need explicit thresholds.** Grafana's default is
green base, red above 80. A panel showing seconds or a log count is
permanently red for no reason. Panels that should never colour use a single
`text` step:

```json
"thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": null}]}
```

**Match both job labels.** `job=~"integrations/unix|host-unix"`, always. See
[the `job` label trap](../architecture/push-model.md#the-job-label-trap).

**Filter containers on `name!=""`.** Otherwise you are charting the root
cgroup alongside real containers.

## Editing

```bash
cd ansible                 # from the repository root
ansible-playbook playbooks/dashboards.yml
```

That is the whole deploy. It is also part of `site.yml`, so a full run keeps
Grafana matching git without a separate step.

The role validates each file by parsing it **on the target before it lands**,
so a truncated or malformed dashboard fails the play instead of being dropped
silently by the provider with an error nobody reads.

!!! tip "It prunes, so deleting a dashboard from git actually deletes it"
    `grafana_dashboard_prune` (default `true`) removes files on the box that
    are no longer committed. Without it the deploy only ever adds: the
    provider's `disableDeletion: false` means Grafana drops a dashboard when
    its FILE disappears, so a leftover file keeps resurrecting a dashboard you
    deleted. Verified by planting an uncommitted dashboard and confirming the
    next run removed exactly that file and nothing else.

!!! note "This does not restart Grafana; `central-alerting.yml` does"
    Dashboard JSON is picked up by the file provider within seconds, so
    pushing a panel edit costs nothing. Only a change to the **provider
    config** (`grafana/provisioning/dashboards/dashboards.yml`, which sets the
    folder names and paths) triggers a restart, because those are read at
    startup.

    Alert rules are the opposite: `central-alerting.yml` always restarts
    Grafana, which briefly stops alert evaluation.

The file provider reloads within seconds; no Grafana restart needed, unlike
alerting.

!!! warning "Do not reformat the JSON wholesale"
    These files use a compact style, several keys per line. Running them
    through a formatter turns a 5-line change into a 400-line diff that hides
    what actually changed. Make surgical edits.

Validate before deploying:

```bash
python3 -c "import json; json.load(open('grafana/dashboards/servers/x.json'))"
```
