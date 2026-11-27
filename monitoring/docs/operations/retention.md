# Retention

How long data is kept, and the disk arithmetic that decided it.

## Current settings

| Store | Setting | Where |
|---|---|---|
| Prometheus | `PROMETHEUS_RETENTION`, default `7d` | `.env` on the central node |
| Loki, direct LXC install | `LOKI_RETENTION_PERIOD`, default `720h` (30d) in `scripts/lxc-install.sh` | `.env` on the central node |
| Loki, Docker Compose | `retention_period` in `loki/loki-config.yml`, committed as `720h` | the file itself; the Compose path does not read `LOKI_RETENTION_PERIOD` |
| systemd journal (local) | `SystemMaxUse`, uncapped by default | `journald_system_max_use` per group |

Prometheus was shortened from 30 days to 7. That was not a preference; it was
arithmetic. Loki stays at 30 days because it is small: the live store holds
about 355 MB, which fits the disk without trimming, and the tenant logs need
the longer window ([below](#loki-retention-for-the-tenant-logs)).

## The measurement

On this fleet, ingest costs roughly **28 MB per host per day** across metrics
and logs combined.

```text
10 hosts × 28 MB/day × 14 days ≈ 3.9 GB
```

The central LXC had **1.9 GB free**. Fourteen days did not fit; seven did,
with headroom. Vacuuming the local journal freed a further 483 MB and took the
box from 75% to 60% used.

!!! tip "Measure yours before choosing"
    ```promql
    # Prometheus on-disk size
    prometheus_tsdb_storage_blocks_bytes

    # samples ingested per second, a proxy for growth rate
    rate(prometheus_tsdb_head_samples_appended_total[1h])
    ```

    ```bash
    du -sh /var/lib/prometheus /var/lib/loki
    ```

## Changing it

=== "Prometheus"

    ```bash
    # in .env on the central node
    PROMETHEUS_RETENTION=14d
    ```

    ```bash
    scripts/lxc-update.sh central --config-only
    systemctl restart prometheus
    ```

=== "Loki"

    Direct LXC install: set the value in `.env` on the central node, then
    re-render the config. `lxc-update.sh` restarts Loki itself.

    ```bash
    # in .env on the central node (default 720h)
    LOKI_RETENTION_PERIOD=336h
    ```

    ```bash
    scripts/lxc-update.sh central --config-only
    ```

    A value already exported in the shell wins over `.env`, so unset it first
    if the change does not appear in `/etc/loki/loki-config.yml`.

    Docker Compose: edit `retention_period` under `limits_config` in
    `loki/loki-config.yml` and restart with `scripts/monitoring.sh central
    restart`.

    Retention only takes effect if the compactor is running with
    `retention_enabled: true`; both configs set it.

=== "Local journal"

    ```yaml
    # group_vars/<group>/main.yml
    journald_system_max_use: 64M
    ```

    ```bash
    ansible-playbook playbooks/collectors.yml --limit <group>
    ```

    Applies on the next rotation, not instantly. To reclaim now:

    ```bash
    journalctl --vacuum-size=64M
    ```

!!! info "Capping the local journal is safe here"
    Alloy ships every line to Loki within seconds. The on-disk journal is only
    a buffer between a write and its shipment, plus enough history to make
    `journalctl` useful over SSH. **Central retention is what you actually
    query.**

## Loki retention for the tenant logs

The firewall's filter log, Unbound's query log, `pfstats`, Suricata's alerts
and the Proxmox firewall log record much of what the work VMs and the leased
VMs did, though not all of it: the wiring page lists
[what is not covered](https://harshitruwali.github.io/homelab-infra/security/wiring/#what-is-not-covered).
For a leased VM they are the main record there is. An abuse report, a complaint
from the other end of a connection, or a dispute with the tenant usually
arrives days or weeks after the traffic, and a 7 day default has deleted the
evidence by then. Loki therefore keeps 30 days.

| | Value |
|---|---|
| Default in `scripts/lxc-install.sh` and `loki/loki-config.yml` | `720h` (30 days) |
| Live install | `720h`, from `LOKI_RETENTION_PERIOD` in `.env` on the central node |
| Applied with | `scripts/lxc-update.sh central --config-only` |
| Prometheus | unchanged, `7d` |

**Disk cost.** Measured on the live store with the tenant streams included:
about 355 MB on 2026-11-13 and 531 MB on 2026-11-19, while the 30 days fill
(the fleet ingests roughly 85 MB a day before Loki compresses it). Check free
space before raising retention further, and measure again once 30 days of
tenant logs exist:

```bash
du -sh /var/lib/loki
df -h /var/lib/loki
```

!!! note "Retention is not the ingest age limit"
    `reject_old_samples_max_age: 168h` in the same config is a separate limit:
    it refuses lines older than 7 days at the door, for example during a
    backfill. Raising retention does not change it.

## Ingest limits

Separate from retention, and the more common first-run failure. Loki's
defaults are 4 MB/s overall and 3 MB/s per stream. Onboarding several hosts at
once with `max_age = "12h"` backfills half a day of journals simultaneously
and will `429`.

```yaml
limits_config:
  ingestion_rate_mb: 16
  per_stream_rate_limit: 8MB
```

Raise these **before** onboarding, not after.

## Disk pressure elsewhere

The alerting covers the central box once it is monitored, but two non-obvious
consumers caused real incidents here:

| Consumer | Symptom | Fix |
|---|---|---|
| apt cache under `o=*` | 199 MB on a 2.0 GB root, 24h from full | `APT::Periodic::CleanInterval` |
| uncapped journal | 192 MB on the same host | `journald_system_max_use` |
| stale apt cache on a Pi | 12 GB, 83% used | `apt-get clean` |
| Docker image layers | grows by one image set per release | `docker image prune -f` after each update |

See [Package patching](https://harshitruwali.github.io/homelab-infra/ansible/fleet/patching/#cache-growth) and
[Container updates](https://harshitruwali.github.io/homelab-infra/ansible/fleet/container-updates/#pruning-is-dangling-only).
