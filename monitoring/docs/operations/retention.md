# Retention

How long data is kept, and the disk arithmetic that decided it.

## Current settings

| Store | Setting | Where |
|---|---|---|
| Prometheus | `PROMETHEUS_RETENTION`, default `7d` | `.env` on the central node |
| Loki | `retention_period`, default `168h` (7d) | `loki/loki-config.yml` |
| systemd journal (local) | `SystemMaxUse`, uncapped by default | `journald_system_max_use` per group |

Both were shortened from 30 days. That was not a preference; it was arithmetic.

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

    ```yaml
    # loki/loki-config.yml
    limits_config:
      retention_period: 336h
    ```

    ```bash
    scripts/lxc-update.sh central --config-only
    systemctl restart loki
    ```

    Retention only takes effect if the compactor is running with
    `retention_enabled: true`.

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
