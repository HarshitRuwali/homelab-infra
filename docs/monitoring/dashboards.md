# Dashboards

Three dashboards, provisioned from `grafana/dashboards/` and loaded at
startup. They are read-only in the UI in the same way alert rules are: edit
the JSON and redeploy.

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
# edit grafana/dashboards/<name>.json, then:
ansible monitor-lxc -m copy -a \
  "src=../grafana/dashboards/<name>.json \
   dest=/var/lib/grafana/dashboards/<name>.json \
   owner=grafana group=grafana mode=0644"
```

The file provider reloads within seconds; no Grafana restart needed, unlike
alerting.

!!! warning "Do not reformat the JSON wholesale"
    These files use a compact style, several keys per line. Running them
    through a formatter turns a 5-line change into a 400-line diff that hides
    what actually changed. Make surgical edits.

Validate before deploying:

```bash
python3 -c "import json; json.load(open('grafana/dashboards/x.json'))"
```
