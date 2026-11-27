# Playbooks and roles

A reference list. For what a playbook or a role *is*, see
[Ansible concepts](../getting-started/ansible-basics.md).

## Playbooks

| Playbook | Targets | Restarts | Use when |
|---|---|---|---|
| `site.yml` | all | services, if a role changed | everyday; runs everything in order |
| `preflight.yml` | `monitored` | **nothing** | before a first rollout, or to debug access |
| `onboard.yml` | one host, `--limit` required | `alloy` | adding a new host; installs the collector and proves data landed centrally |
| `collectors.yml` | `monitored` | `alloy`, `systemd-journald` | Alloy config, container metrics, journal caps |
| `update-metrics.yml` | `monitored` | nothing meaningful | the apt/reboot exporter, plus SMART and Proxmox guest names where `group_vars/metal` enables them |
| `dell-fan-boost.yml` | `metal` | `fleet-dell-fan-boost`, if its script or unit changed; stopping it puts the fans back on automatic | a CPU fan the firmware runs too slowly; inert except where `dell_fan_boost_enabled` is set |
| `tplink-router.yml` | `monitored` | `tplink-router-poll`, if its script, library or unit changed | per-client traffic from a TP-Link router; inert except where `tplink_router_poll_enabled` is set |
| `internet-probe.yml` | `monitored` | `internet-probe`, if its script or unit changed | tracks internet outages (probes, ledger, metrics); inert except where `internet_probe_enabled` is set |
| `opnsense-exporter.yml` | `monitored` | the `opnsense-exporter` container, when its compose file changed | OPNsense API metrics; inert except where `opnsense_exporter_enabled` is set |
| `er605-snmp.yml` | `monitored` | the `er605-snmp` and `er605-poll` containers, when the compose file changed (the poller also when its script changed) | household ISP monitoring from the ER605 over SNMPv3; inert except where `er605_snmp_enabled` is set |
| `network-history.yml` | `central`, then `monitored` for the client | `prometheus-network` and `grafana-server` (when its datasource file changed) | the 90-day network metrics tier, the permanent PostgreSQL history and its Grafana datasources; `nethist` and the agent skill where `network_history_client_enabled` is set |
| `dashboards.yml` | `central` | only if the provider config changed | Grafana dashboards; prunes ones deleted from git |
| `central-alerting.yml` | `central` | **`grafana-server`** | alert rules, Matrix relay |
| `unattended-upgrades.yml` | `autoupdate` | nothing | patching policy |
| `docker-updates.yml` | `autoupdate` | containers, only with `run_now` | container update schedule |
| `rotate-collector-password.yml` | `central` | `nginx`, central `alloy` | rotating collector auth |
| `desk-appliance.yml` | `desk_appliances` | `docker` (started on first install, reloaded for `live-restore`); udev rules reloaded; `systemd-logind` told to re-read its config; `btusb` reloaded when its DKMS build changes (Bluetooth connections drop and return); never a running session | an always-on X11 desk appliance; not part of `site.yml` |
| `tailscale-node.yml` | `tailscale_nodes` | nothing: `tailscaled` is never restarted by this role unless `tailscale_node_manage_upgrades` is true | a host on the tailnet under a tag, with services published by `tailscale serve`; not part of `site.yml` |
| `image-registry.yml` | `image_registries` | the registry container, only when its configuration changed; during a garbage collection | the private container image registry; not part of `site.yml` |
| `force-updates.yml` | `monitored`, except `desk_appliances` | **services, via apt** | installing pending updates NOW; never reboots |

### Common invocations

```bash
cd ansible                                                 # from the repository root

ansible-playbook playbooks/site.yml                        # everything
ansible-playbook playbooks/site.yml --limit rpi5           # one host
ansible-playbook playbooks/site.yml --limit pi             # one group
ansible-playbook playbooks/site.yml --limit 'lan_guests,!matrix'
ansible-playbook playbooks/site.yml --check --diff         # dry run
ansible-playbook playbooks/site.yml -e lan_use_jump_host=false   # from the LAN
```

!!! danger "`central-alerting.yml` restarts Grafana"
    Grafana reads provisioning **only at startup**, so the restart is the
    point. There is a short gap in evaluation and dashboards.

## Roles

### `alloy_collector`

Installs Alloy from the Grafana APT repo and owns `/etc/alloy/config.alloy`
on every native install.

| Does | Note |
|---|---|
| Adds the Grafana APT repo and installs `alloy` | `state: present`, never `latest` |
| Grants `alloy` the groups it needs | `systemd-journal`, `adm`, `docker` |
| Runs Alloy as root **on Docker hosts** | drop-in, for [cAdvisor](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/container-metrics/) |
| Templates the config | `validate: alloy fmt %s`: a bad config never lands |
| Writes `/etc/alloy/.ansible-managed` | stops `lxc-install.sh` reverting it |
| Caps the journal where set | `journald_system_max_use` |
| Waits for `/-/ready` | proof of life, not just "started" |

### `update_metrics`

The apt and reboot textfile exporter, plus its timer.

Installs `needrestart` in **non-interactive list mode**
(`$nrconf{restart} = 'l'`). Its default APT hook prompts during upgrades,
which would hang an unattended run indefinitely.

The service carries `[Install] WantedBy=multi-user.target` so a reboot
refreshes metrics immediately rather than waiting out the timer jitter.

### `dell_fan_boost`

A small controller on a Dell workstation, `fleet-dell-fan-boost.service`, that
runs one fan at full speed while a CPU package is hot and hands every fan back
to the firmware once it cools. On t7920 it drives the CPU1 fan from CPU1's
package temperature.

| Does | Note |
|---|---|
| Boosts at `dell_fan_boost_on_celsius`, releases at `dell_fan_boost_off_celsius` | after at least `dell_fan_boost_min_hold_seconds`, so it does not cycle |
| Writes `pwmN_enable=1` and `pwmN=255` through `dell_smm_hwmon` | `128` is slower than automatic on this firmware, so full speed is the only useful manual setting |
| Hands back early if another package reaches `dell_fan_boost_guard_celsius` | manual mode is one switch for every fan, and the firmware stops raising the others while it is on |
| Puts every fan back on automatic on any exit | in the script, and in `ExecStopPost`, which also runs after SIGKILL and the 60 s watchdog |
| Writes `fleet-dell-fan-boost.prom` | the `fleet_dell_fan_boost_*` metrics |

Stop it with `systemctl stop fleet-dell-fan-boost`; the fans go back to the
firmware's curve at once.

### `tplink_router_poll`

A poller, `tplink-router-poll.service`, on a host that sits on a TP-Link
router's LAN. Every 60 s it logs in to the router's web API, reads the client
list and the traffic counters, and logs out. The "Router TPLink Blr" dashboard
reads what it writes.

| Does | Note |
|---|---|
| Checks the router's MAC before sending the password | `tplink_router_poll_expected_mac`; a laptop on another network with the same gateway address skips the poll |
| Skips a poll while someone is logged in to the router's web page | the router allows one admin session; the poller never logs a person out |
| Reads only | `go` and `gl` operations of the `DEV2` API, plus login and logout |
| Writes `tplink-router-<name>.prom` | the `tplink_router_*` metrics; Alloy ships them, buffering while the link is down |
| Keeps a daily JSONL copy in `/var/lib/tplink-router-poll` | one line per poll, older days gzipped, `tplink_router_poll_keep_days` kept |
| Keeps a usage ledger, `usage.db`, forever | SQLite: bytes per client per UTC hour, every connection's start, end and bytes, every name and IP a client had, and the router's log |
| Prints usage, connection and router log lines to the journal | Alloy ships them to Loki (30 days), which the dashboard's History row reads |
| Receives the router's own log on `tplink_router_poll_syslog_port` | only from the router's address; checks every poll that the router still points here |
| Copies its data nightly to another host | where that host sets `tplink_router_poll_mirror_from`; a user timer, `tplink-router-mirror.timer` |
| Runs from a virtualenv in `/opt/tplink-router-poll` | `tplinkrouterc6u`, pinned; no system packages |

The password is not managed by Ansible. Put it on the host once, root-only,
before the first run; the role stops with the command to use if it is missing:

```bash
sudo sh -c 'umask 077; install -d -m 0700 /etc/tplink-router-poll; cat > /etc/tplink-router-poll/password'
```

systemd hands it to the process with `LoadCredential`, so it never appears on a
command line or in the environment.

**The ledger.** Usage comes from the per-client Wi-Fi counters. Each poll books
the change since the previous one to that UTC hour (`kind` `measured`). What a
client already had on its counter when first seen, or across a gap of more than
15 minutes in polling, is booked to the hour it was found (`kind` `carried`). A
counter that goes down means the client reconnected and opens a new connection.
A client missing for more than 5 minutes closes its connection at the last poll
it was seen. Read it on the poller host:

```bash
P=/opt/tplink-router-poll
sudo $P/venv/bin/python $P/tplink-router-poll.py --report day --since 2026-11-01
sudo $P/venv/bin/python $P/tplink-router-poll.py --report hour --client corex --csv
sudo $P/venv/bin/python $P/tplink-router-poll.py --report sessions   # visits; ~ marks an estimated start
sudo $P/venv/bin/python $P/tplink-router-poll.py --report log        # the router's own log
```

`--report month` sums by month. If `usage.db` is lost, the service rebuilds it
from the JSONL days at its next start (`--rebuild-ledger` does it on demand).

**The router's log.** The router keeps its log only in RAM. Point its remote
log at the poller host once, in the router's system log settings (the API
object `DEV2_SYSLOG_CFG`): remote log on, server the poller host's address,
port `tplink_router_poll_syslog_port`, level Info. The poller never changes
router settings. It reads that setting
every poll and reports `tplink_router_log_forwarding`, which drops to 0 when the
poller host's address changes. A DHCP reservation for the poller host prevents
that. The router does not send RFC 3164 syslog (no timestamp or hostname), so
Alloy's syslog listener rejects it; that is why the poller receives it.

### `network_history`

Long-term network history, in three tiers. It runs on the central host and
delegates the database setup to the PostgreSQL host (the memory LXC, inventory
name `network_history_db_host`, which must be in the inventory).

| Tier | Holds | Where |
|---|---|---|
| Prometheus | every metric, 15 s, 7 days | `prometheus.service`, installed by `scripts/lxc-install.sh` |
| `prometheus-network.service` | the network metrics only, 30 s, 90 days (4 GB cap) | second Prometheus on `127.0.0.1:9091`, federating `/federate` from the first |
| `net-history.timer` | one row per IST day per key, forever, plus monthly views and every outage | PostgreSQL database `net_history`, schema `net` |

| Does | Note |
|---|---|
| Runs `prometheus-network.service` | the system `/usr/bin/prometheus`, user `prometheus`, `MemoryMax=256M`, hardened; `track_timestamps_staleness` is on so a replaced series (a guest NIC's info series) ends at its last sample; the `match[]` list in `network_history_federation_selectors` is exactly what the network dashboards and the aggregator read, and `tests/network-history-test.py` fails when a dashboard queries a metric it does not list |
| Generates two passwords once | `/etc/net-history/writer.password` and `reader.password`, root 0600, never in git |
| Sets up the database | on the PostgreSQL host, through `docker exec -i postgres psql -U jarvis -d postgres` with the SQL on stdin: roles `net_history_writer` (owns schema `net`) and `net_history_reader` (SELECT only, read-only sessions), database `net_history` with CONNECT revoked from PUBLIC. It starts in the `postgres` maintenance database and connects to no database other than that and `net_history`. In `--check` mode it prints the SQL with the passwords masked and changes nothing |
| Installs the datasources | `/etc/grafana/provisioning/datasources/network-history.yml` (root:grafana 0640): `prometheus-90d` and the PostgreSQL datasource `net-history` (user `net_history_reader`, `sslmode: disable` because the link stays on the private LAN segment). Grafana restarts when the file changes |
| Installs the aggregator | `/opt/net-history/net-history.py` and a virtualenv holding the pure-python `pg8000` (pinned); `net-history.service` runs hourly from `net-history.timer` and two minutes after boot. It upserts today and the two days before on every run. The role does not back-fill 90 days: it runs the aggregator once with `--backfill 7` (the central Prometheus keeps 7 days, and the 90-day store starts empty), which also records that a back-fill happened, so the timer's runs stay at three days. To recompute more, run the aggregator by hand with `--backfill N`; each day is taken from the store that covers it best |
| Writes metrics | `net_history_last_run_timestamp_seconds`, `net_history_run_success` and `net_history_rows{table}` to the textfile directory |
| Installs `nethist` and the skill | where `network_history_client_enabled` is set: `~/.local/bin/nethist`, the reader password in `~/.config/net-history/reader.password` (copied from the central host, or placed by hand), and `SKILL.md` in `~/.claude/skills/network-history/`, `~/.agents/skills/network-history/` and, when `~/.hermes/skills` exists, `~/.hermes/skills/network-history/` |

Tables in schema `net` (all documented with `COMMENT`; `SELECT * FROM net.catalog` lists them with an example query each):
`internet_daily`, `lan_daily`, `path_daily`, `wan_daily` (one row per WAN), `segment_daily`, `segflow_daily`, `client_daily`,
`guest_daily`, `tailnet_daily`, `outage_events` (kinds `internet`, `lan`, `path`, `wan`), and the views `monthly_*`
(`monthly_wan` has a `wan = 'total'` row), `coverage`, `catalog`, `catalog_columns`.

Enable the client in the workstation's inventory entry (Docker is needed there; `nethist` runs `psql` from `postgres:16-alpine`):

```yaml
network_history_client_enabled: true
```

Coverage: neither Prometheus store reaches back past its first sample, so for every table and day `net-history` computes the value
from the store that covers most of that day (the 90-day store wins ties; `--central-prometheus` is the other one) and stores the
covered share in the row's `coverage` (0 to 1; today's is of the elapsed part). A stored row is **never replaced by one with lower
coverage**, except today's row, which is recomputed on every run. The monthly views count days with coverage below 1 in
`partial_days`, and `window_seconds` of the availability tables is the covered part of the day, so availability stays honest on a
partial day. Rows written before the `coverage` column existed (migration 2) get the best coverage any store has for their day
on the next run; rows imported earlier as `import` become `er605-ledger`.

Seed `wan_daily` from the ER605 ledger (a CSV with the columns `day,wan,download_bytes,upload_bytes,coverage`; `wan` and `coverage`
are optional and default to `wan1` and 1; rows get `source = 'er605-ledger'`). An existing row is replaced only when its coverage is
lower than the imported one (a Prometheus day that was only partly covered); a row of equal or higher coverage is skipped unless
`--force`, and later aggregator runs never replace a ledger row with a Prometheus row of equal or lower coverage:

```bash
/opt/net-history/venv/bin/python /opt/net-history/net-history.py --password-file /etc/net-history/writer.password \
  --db-host <postgres host> --import-wan-daily wan-history.csv        # add --wan wan1 (default) and --force as needed
/opt/net-history/venv/bin/python /opt/net-history/net-history.py --password-file /etc/net-history/writer.password \
  --db-host <postgres host> --report month --table wan                # print a monthly view
```

Limits: a day's value is `increase()` over the day at its IST end, so it extrapolates slightly at the edges; today's row is partial
(`coverage` says how much); a gap in the middle of a store (the central Prometheus down for an hour) is not detected by `coverage`,
which only looks at the first sample, and leaves a gap in that day's counter.
`sslmode: disable` is acceptable only because the two hosts share a private segment.

### `internet_probe`

A poller, `internet-probe.service`, that tracks internet outages from one host
per site (`home` on rpi5, `blr` on skullsaints, `homelab` on ubuntu-dev). Every
5 s it probes the internet and the local gateway. The "Internet and Usage"
dashboard reads what it writes.

| Does | Note |
|---|---|
| Probes in rounds | three `tcp` connects, two raw `dns` queries and one `http` check, all in parallel, each bounded by `internet_probe_timeout`; a round reaches the internet when any `tcp` or `dns` target succeeds, `http` never decides |
| Probes the gateway | the default route's gateway, port 80, unless `internet_probe_gateway` is set; a refused connection counts as up |
| Confirms an outage after `internet_probe_confirm_rounds` failed rounds | its start is the first failed round and its end the first good round; fewer failed rounds are a blip, counted and logged but not stored |
| Probes LAN targets | `internet_probe_lan_targets` (`NAME=HOST:PORT`), every round, up when a connect succeeds or is refused; each has its own outage ledger table `lan_outages` and the journal lines `lan_down`, `lan_up`, `lan_blip`; they never change `internet_probe_up` |
| Probes named paths | `internet_probe_paths` (`NAME=KIND:HOST:PORT,...`): a path is up in a round when any of its `tcp` or `dns` targets answers; each has its own outage ledger table `path_outages` and the journal lines `path_down`, `path_up`, `path_blip`; they never change `internet_probe_up` |
| Names the cause | `local` if the gateway did not answer, `upstream` if the router answered and the internet did not |
| Keeps a ledger, `outages.db`, forever | SQLite in `/var/lib/internet-probe`, written only when an outage starts or ends; an outage still open when the process restarts is closed with `closed_by=restart` |
| Writes `internet-probe.prom` | the `internet_probe_*` metrics, every 15 s and at once on an outage start or end; Alloy ships them. `internet_probe_up` is 0 if any round failed within the last 15 s, so a short outage shows in every scrape that follows it |
| Prints `startup`, `outage_start`, `outage_end` and `blip` lines to the journal | one per event, none per round; Alloy ships them to Loki (30 days) |
| Keeps probing if the ledger fails | a rate-limited `ledger_error` line, `internet_probe_ledger_ok` 0, counters kept in memory |
| Runs on the system Python | `/usr/bin/python3`, standard library only, no virtualenv, no secret, no capabilities |

Off by default. Enable it per host in the inventory entry, not in git:

```yaml
internet_probe_enabled: true
internet_probe_site: home      # lowercase letters, digits, dashes
```

The role checks the site name and the LAN targets, installs the script and the unit, waits for a
metrics file written after the restart (an older file does not count) and prints `internet_probe_up` from it. Where a host has
the probe disabled and a unit is left over, the role stops and removes the unit
`/opt/internet-probe` and the metrics file; the data directory with the ledger stays.

**Paths and multi-WAN.** A path is a named set of targets, for example
`wan2=tcp:149.112.112.112:443,dns:208.67.222.222:53`. The probe cannot choose
which uplink a connection takes; the router does. A path therefore measures one
WAN only when the router sends that path's targets out that WAN. On the ER605
that is a policy route per path: the destination is an IP group holding the
path's target IPs, the WAN is that WAN, and failover to the other WAN is off,
so a dead WAN fails the path instead of silently moving to the other one. Pick
target IPs that are distinct per path and not in the built-in set, so the
policy routes do not catch the main internet targets (for example `wan1`:
`1.0.0.1` and `8.8.4.4`; `wan2`: `149.112.112.112` and `208.67.222.222`).
With one WAN no policy route is needed: a path then simply tracks the same ISP
as the main targets, and still gets its own outage ledger and metrics. Paths
never change `internet_probe_up`, which stays any-of the main targets.

**The ledger.** Read it on the probe host (the ledger is root-owned):

```bash
sudo /usr/bin/python3 /opt/internet-probe/internet-probe.py --data-dir /var/lib/internet-probe --report outages
sudo /usr/bin/python3 /opt/internet-probe/internet-probe.py --data-dir /var/lib/internet-probe --report daily --days 7
sudo /usr/bin/python3 /opt/internet-probe/internet-probe.py --data-dir /var/lib/internet-probe --report lan
sudo /usr/bin/python3 /opt/internet-probe/internet-probe.py --data-dir /var/lib/internet-probe --report paths
```

`--report paths` does the same per named path. `--report lan` shows, per LAN target seen in the ledger, outages, downtime and
availability over `--days`, then every LAN outage. `--report outages` lists every outage of the last `--days` (default 30) with
start in local time, end, duration, cause and `closed_by`; an open outage shows
as `ongoing`. `--report daily` shows outages, downtime and availability per
local day. Prometheus keeps 7 days, Loki 30, the ledger forever.

### `opnsense_exporter`

`opnsense-exporter` (AthennaMind, pinned) in a container on a Docker host on
the firewall's LAN, reading the OPNsense API with a read-only key. The central
Alloy scrapes it (`alloy_extra_scrape_targets`).

| Does | Note |
|---|---|
| Runs the container as `nobody`, read-only, every capability dropped | `docker compose` file in `/opt/opnsense-exporter` |
| Mounts the API key and secret read-only from `/etc/opnsense-exporter` | placed by hand, owner 65534, mode 0400; the role checks and stops otherwise |
| Disables the collectors for features the firewall does not run | `opnsense_exporter_disable` |
| Waits for real API data before it reports success | per-interface byte counters, not `opnsense_up`, which is always 0 on OPNsense 26.7 |

The firewall side is not Ansible: `files/opnsense/` holds the four scripts that
created the API user, enabled node_exporter, opened port 9100 to monitor-lxc
and turned on Unbound's extended statistics. How to run them:
[Routers and firewall](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/routers/#opnsense).


Applied by **group membership only**. Ends with a validation gate:
`unattended-upgrade --dry-run --debug`, asserting no `Traceback` and no
blacklisted package appears.

### `er605_snmp`

The household's ISP monitoring: the ER605 home router read over SNMPv3 by a
local `snmp_exporter`, and a stdlib Python poller that publishes the counters
as textfile metrics, keeps an hourly usage ledger and logs reconnects and
reboots. Two containers in one compose project (`er605-snmp` in
`/opt/er605-snmp`), both with a read-only root filesystem, every capability
dropped and `no-new-privileges`. They run as root inside the container because
the auth file is root-only and the textfile directory is root-owned. Images are
pinned by tag. Dashboard:
[Home ISP](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/dashboards/#home-isp).

| Does | Note |
|---|---|
| Reads the router through `snmp_exporter` on `er605_snmp_listen` | loopback; the credentials stay in the exporter, the poller never sees them |
| Mounts the auth file read-only from `/etc/er605-snmp/auth.yml` | placed by hand, `root:root` 0600 or 0400; the role checks and stops otherwise, and never templates, copies or prints it |
| Polls every `er605_snmp_interval` seconds | WAN and LAN counters, CPU per core, memory, uptime, interface state, the PPPoE session |
| Writes `er605.prom` | the `er605_*` metrics, unchanged names for the counters; see [the metrics](https://harshitruwali.github.io/homelab-infra/monitoring/reference/metrics/#er605-home-router) |
| Keeps a raw CSV per UTC day in `/var/lib/er605-snmp` | gzipped after 2 days, deleted after `er605_snmp_keep_days` |
| Keeps the ledger `usage.db` there | hourly WAN bytes, PPPoE reconnects and router reboots; never pruned; a counter that went backwards books the new value, never a huge number |
| Logs events to the container log | `usage` (once per WAN per closed hour), `pppoe_reconnect`, `wan_down`, `wan_up`, `router_reboot`, `poll_error` (at most one per 10 minutes); Alloy ships them to Loki as `{host="ubuntu-dev", job="docker", container="er605-poll"}` |
| Refuses to start next to a manual setup | containers named `er605-snmp` or `er605-poll` that are not part of this compose project |

**Enable it** in the host's inventory entry:

```yaml
er605_snmp_enabled: true
```

**Place the SNMPv3 auth file once**, by hand. It is the `snmp_exporter` auth
file that defines the module named `er605_snmp_auth_name`. If it lives in a home
directory from a manual setup, move it and delete the copy:

```sh
sudo install -d -m 0700 /etc/er605-snmp
sudo install -m 0600 -o root -g root ~/.config/er605-snmp/auth.yml /etc/er605-snmp/auth.yml
rm ~/.config/er605-snmp/auth.yml
```

**Replace a manual setup.** The role stops with the exact command while
containers named `er605-snmp` or `er605-poll` exist outside its compose project
(they hold port 9116 and the names the role needs):

```sh
docker rm -f er605-snmp er605-poll
```

Run that, then the playbook. Their data (`~/.local/share/er605-snmp`) is not
touched. To let the role remove them itself, set `er605_snmp_replace_manual:
true` for that run.

**Deploy and check:**

```sh
ansible-playbook playbooks/er605-snmp.yml --limit ubuntu-dev --check --diff
ansible-playbook playbooks/er605-snmp.yml --limit ubuntu-dev
```

The run waits for `er605_poll_success 1` in the textfile.

**Import the old CSVs once.** The role migrates nothing. To load the manual
poller's CSV days into the ledger, copy them under the data directory and
import them. It is safe to repeat: an hour already imported is replaced with the
same values, and an hour the live poller booked is never touched.

```sh
sudo install -d /var/lib/er605-snmp/import
sudo cp ~/.local/share/er605-snmp/er605-*.csv /var/lib/er605-snmp/import/
docker exec er605-poll python3 /app/er605-poll.py import-csv /data/import
```

**Add a second ISP.** List every WAN in `er605_snmp_wans` (default: one WAN,
`wan1` on `eth1` with PPPoE session `pe-wan1_poe`). `interface` is the ER605
ifName without `default/`, `pppoe` is empty for a DHCP or static WAN:

```yaml
er605_snmp_wans:
  - {name: wan1, interface: eth1, pppoe: pe-wan1_poe, isp: "First ISP"}
  - {name: wan2, interface: eth2, pppoe: "", isp: "Second ISP"}
```

The role passes them as `--wan NAME=INTERFACE[,pppoe=IFNAME][,isp=LABEL]`. Every
WAN port is counted with `role="wan"`, so the household total is unchanged;
`er605_wan_up{wan}` and the PPPoE series are per WAN. A WAN that is configured
but missing from SNMP reports `er605_wan_up 0` and logs one `wan_down`. The raw
CSV holds the first WAN and the LAN side; the ledger keeps every WAN.

**Ledger migration.** A ledger written before multi-WAN support is migrated
when the poller starts, in one transaction: its usage rows, reconnects and
poller state become `wan1`, and `import-csv` history is `wan1`. A failed
migration rolls back and leaves the old ledger as it was.

**Read the ledger** (local time, Asia/Kolkata; `--days` defaults to 2 for
`hour`, 30 for `day`, 400 for `month`, 30 for `reconnects` and `wan`). Usage
rows show the totals, then one pair of columns per WAN; `--wan NAME` limits a
report to one WAN. `--report wan` lists each WAN's down and up events:

```sh
docker exec er605-poll python3 /app/er605-poll.py --report hour
docker exec er605-poll python3 /app/er605-poll.py --report day --days 14
docker exec er605-poll python3 /app/er605-poll.py --report month --wan wan2
docker exec er605-poll python3 /app/er605-poll.py --report reconnects
docker exec er605-poll python3 /app/er605-poll.py --report wan
docker exec er605-poll python3 /app/er605-poll.py report "2026-11-27 09:00" "2026-11-27 10:00"
```

The last form reads the raw CSV days for a window: volume, mean, p95 and peak
1-minute rate. The ledger's bins are UTC hours (they start at :30 in IST), so
`day` and `month` rows book each hour to the local date its start falls on; use
the window form for an exact figure at a day edge. The ER605 syncs its counters
late, so rates under a minute are not meaningful.

Not removed by the role when disabled: the data directory
`/var/lib/er605-snmp`. Disabling takes the compose project down and removes
`/opt/er605-snmp`.

### `docker_updates`

Compose-based container updates. Checks for a Docker socket (capability gate)
and for Compose v2, then installs the script, unit and timer.

The `docker_update_run_now` variable triggers a supervised run and asserts the
result.

### `matrix_webhook`

The Grafana-to-Matrix relay: a system user, a venv, a systemd unit and a smoke
test. Refuses to deploy with placeholder credentials.

### `desk_appliance`

Baseline for an always-on desk appliance, applied by membership of
`desk_appliances` only: LightDM autologin into a named X11 session, no
blanking or suspend, PipeWire, a serial udev symlink, a DKMS-patched `btusb`
for a Bluetooth adapter the kernel does not list yet, Docker, and security
updates with an automatic reboot window. Tagged per concern: `desktop`,
`power`, `keyring` (opt-in), `audio`, `serial`, `bluetooth` (opt-in),
`docker`, `updates`.

!!! warning "The one host class that reboots itself"
    It sets `Automatic-Reboot "true"`, so it fails early on a host in
    `autoupdate` or `no_autoupdate`, whose role asserts the opposite. On Kali
    Rolling the update layer is off unless `desk_appliance_updates_enabled` is
    set.

Runs on Ubuntu 26.04 and Kali Rolling; any other distribution fails the play
at the start.

Variables and behaviour are documented in the role itself:
`roles/desk_appliance/README.md`.

### `tailscale_node`

Applied by membership of `tailscale_nodes` only: Tailscale from its own apt
repository (key pinned by checksum), `tailscaled` enabled, a join under a tag
with an auth key from the vault (only while the node is not on the tailnet),
and `tailscale serve --bg --https=...` for each entry in
`tailscale_node_serve`. Tagged per concern: `install`, `join`, `serve`. Never
enables Funnel, and fails if any Funnel is on.

!!! danger "Never put the tailnet router in `tailscale_nodes`"
    The exit node and subnet router carries the estate's traffic, and
    restarting `tailscaled` there drops it. The join step refuses a node that
    already routes, but group membership is the control.

Variables, the steps only the owner can do (tags, auth keys, LXC TUN
passthrough) and behaviour are documented in the role itself:
`roles/tailscale_node/README.md`.

### `image_registry`

Applied by membership of `image_registries` only: Docker CE if the host has
none, a Compose project running `registry:3` on `127.0.0.1`, one htpasswd
credential from the vault, and a weekly garbage collection timer that stops the
registry for its run. Tagged per concern: `docker`, `config`, `gc`, `service`.
Reached from the tailnet through `tailscale_node` on the same host, which
supplies the HTTPS listener and certificate.

Variables and behaviour are documented in the role itself:
`roles/image_registry/README.md`.

### `grafana_alerting`

Copies the committed alerting files, **templates** `rules-availability.yaml`
from the inventory, injects secrets into Grafana's `EnvironmentFile`,
restarts Grafana, then asserts one known uid per rules file.

!!! info "Why one assert per file"
    Grafana aborts alerting provisioning for **all** files when any single one
    fails to parse, without failing startup. Probing one uid per file turns a
    silent total failure into a red play.

## Ad-hoc commands

```bash
# what is the fleet made of
ansible-inventory --graph
ansible-inventory --host rpi5

# reachability
ansible monitored -m ping

# force a metrics refresh
ansible monitored -m systemd -a 'name=fleet-update-metrics.service state=started' --become

# check a timer everywhere
ansible monitored -m shell -a 'systemctl list-timers <unit> --no-pager' --become

# assert the never-reboot policy
ansible autoupdate -m shell \
  -a 'apt-config dump | grep "Unattended-Upgrade::Automatic-Reboot "' --become
```

!!! bug "Jinja eats Go template braces"
    `docker ps --format '{{.Names}}'` fails in `-m shell` because Ansible
    templates the argument first. Put the command in a file and use
    `-m script` instead.

## Wazuh agents

`wazuh-agents.yml` targets `wazuh_agents`. Before running it:

1. Configure the manager with `security/install/configure-sec-wazuh.sh`; its
   `WAZUH_AGENT_GROUP` must match `wazuh_agent_group` (both default to `homelab`).
2. Add that manager to the `wazuh_manager` inventory group, or set
   `wazuh_manager_inventory_host` to an inventory host reachable by Ansible.
   The role verifies the group directory on this host before installing agents.
3. Set `wazuh_manager_address` and `wazuh_manager_ca_src` in private inventory.
   The latter is a controller-side PEM file from your trusted CA distribution.
4. Set `vault_wazuh_enrollment_password` in Ansible Vault to the manager's
   enrollment password (at least 20 characters, no whitespace).

The role installs CA/password files as root:wazuh `0640` without logging the
password, enables server verification during enrollment, and configures journald
collection for SSH, sudo and system logs. Add application file inputs through
`wazuh_log_sources` (`location` and `log_format`). This role owns the complete
`ossec.conf`; express local log inputs in that variable rather than editing the
managed file. Manager-shared configuration should be reviewed separately.

Rootcheck is disabled on LXC guests (`ansible_virtualization_type == 'lxc'`) and
stays on for VMs and bare metal; see `wazuh_rootcheck_enabled`. LXC guests
share the host's `/proc` and `/dev`, so rootcheck's `/dev` scan flags LXC's own
files (rule 510, level 7) on every scan.

The agent's `wazuh-logcollector` shows a large RSS on hosts with big journals
(about 230 MiB with a 510 MB journal). That figure is almost entirely
file-backed pages of the memory-mapped journal files (`RssFile`), which are
shared page cache the kernel reclaims under pressure, not heap. The role
collects `journald` unfiltered on every host, so there is nothing
host-specific to tune. Capping `SystemMaxUse` in `journald.conf` is the lever
if it matters.

After rollout, check `/var/ossec/bin/agent_control -l` on the manager and verify
a known SSH or sudo event in Wazuh. A running agent alone does not prove either
enrollment or log collection. Existing agents may continue using their existing
client keys; the new credentials secure future enrollment as well.

## Collector password handling

The rotation play supplies the new password to `htpasswd -iB` over stdin. It is
hidden from Ansible output and never placed in process arguments. Rotation still
changes the central endpoint first; rerun `collectors.yml` for the fleet promptly
in the same maintenance window so remote collectors stop receiving 401 responses.
