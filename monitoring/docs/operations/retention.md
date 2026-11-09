# Retention

How long data is kept, and the disk arithmetic that decided it.

## Current settings

| Store | Setting | Where |
|---|---|---|
| Prometheus | `PROMETHEUS_RETENTION`, default `7d` | `.env` on the central node |
| Loki, direct LXC install | `LOKI_RETENTION_PERIOD`, default `168h` (7d) in `scripts/lxc-install.sh` | `.env` on the central node; the live install sets `720h` (30 days), see [below](#loki-retention-for-the-tenant-logs) |
| Loki, Docker Compose | `retention_period` in `loki/loki-config.yml`, committed as `720h` | the file itself; the Compose path does not read `LOKI_RETENTION_PERIOD` |
| systemd journal (local) | `SystemMaxUse`, uncapped by default | `journald_system_max_use` per group |

The 7 day defaults were shortened from 30 days. That was not a preference; it
was arithmetic.

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
    # in .env on the central node
    LOKI_RETENTION_PERIOD=720h
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
evidence by then. The live install therefore runs Loki at 30 days.

| | Value |
|---|---|
| Default in `scripts/lxc-install.sh` | `168h` (7 days) |
| Live install | `720h` (30 days), set by `LOKI_RETENTION_PERIOD` in `.env` on the central node |
| Applied with | `scripts/lxc-update.sh central --config-only` |
| Prometheus | unchanged, `7d` |

The default stays at 7 days on purpose: a fresh install of this repository has
no leased VMs and a small disk. Raise it where the lease logs exist.

**Disk cost.** Before the tenant logs were added, 7 days of Loki for the whole
fleet was about 185 MB on disk. A linear extrapolation to 30 days is about
790 MB for that same data (185 MB times 30 over 7), not a measurement. The new
streams are not measured yet: the firewall sends about 55 MB a day of syslog
before the two automatic rules in `alloy_syslog_drop_filterlog_rules` are
dropped at ingest, and Loki compresses it, so the stored size is smaller. Check
free space before raising retention and measure after a week:

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
