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
    [`alloy_enable_process`](../reference/variables.md#collector).

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

`ubuntu-ai` additionally has a GPU row: utilization, memory, temperature,
power and fan, both as top-strip stats and as per-GPU time series (labelled
by `uuid`, so a multi-GPU box gets one line per card). See
[GPU telemetry](../reference/metrics.md#gpu-telemetry) for the metric names,
and `ansible/roles/gpu_exporter` for how it gets
there. Empty GPU panels on a host that never had `nvidia-smi` mean the
exporter correctly never installed, not a scrape failure.

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
cd ansible
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
