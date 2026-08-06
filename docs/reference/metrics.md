# Metrics catalogue

Everything this repo adds on top of stock `node_exporter` and cAdvisor.

## Package and reboot state

Written to `fleet-updates.prom` by `fleet-update-metrics.sh`, every 15 minutes
and at boot.

| Metric | Type | Meaning |
|---|---|---|
| `apt_upgrades_pending{origin}` | gauge | pending packages, by origin |
| `apt_upgrades_security_pending` | gauge | the security subset |
| `node_reboot_required` | gauge | `1` while a reboot is outstanding |
| `fleet_reboot_required_since_timestamp_seconds` | gauge | when it first became required |
| `needrestart_kernel_status` | gauge | `0` unknown, `1` current, `2` ABI pending, `3` version pending |
| `needrestart_services_count` | gauge | services running outdated binaries |
| `fleet_unattended_upgrades_enabled` | gauge | `1` if auto-patching is configured |
| `fleet_unattended_upgrades_last_run_timestamp_seconds` | gauge | last unattended run |
| `fleet_apt_last_update_timestamp_seconds` | gauge | package cache freshness |
| `fleet_dpkg_needs_configure` | gauge | `1` if dpkg is wedged |

!!! info "`fleet_reboot_required_since_timestamp_seconds` is emitted only while pending"
    That is what makes the alert self-resolve on reboot, and what lets the
    dashboard table empty itself without a stale row.

!!! warning "`needrestart_kernel_status` is unreliable on Raspberry Pi OS"
    It compares the running kernel against the newest `/boot/vmlinuz-*`, but
    the Pi kernel comes from `/boot/firmware`. A Pi with both Debian and RPi
    kernels installed reports `3` indefinitely. **No alert keys off it** for
    this reason; it is dashboard information only.

## Container update state

Written to `fleet-docker.prom` by `fleet-docker-update.sh`, after each run.

| Metric | Type | Meaning |
|---|---|---|
| `fleet_docker_update_last_run_timestamp_seconds` | gauge | when it last completed |
| `fleet_docker_update_duration_seconds` | gauge | how long it took |
| `fleet_docker_update_failed` | gauge | `1` if anything errored |
| `fleet_docker_compose_projects` | gauge | projects found on the host |
| `fleet_docker_projects_updated` | gauge | projects whose images changed |
| `fleet_docker_containers_recreated` | gauge | containers moved to a new image |
| `fleet_docker_image_bytes_reclaimed` | gauge | freed by pruning |
| `fleet_docker_containers_running` | gauge | running after the update |
| `fleet_docker_containers_unhealthy` | gauge | failing a Docker healthcheck |
| `fleet_docker_containers_restarting` | gauge | stuck restarting |

## Stock metrics worth knowing

| Metric | Used by |
|---|---|
| `up{job=~"integrations/unix\|host-unix"}` | host-down, freshness. **[Not what you think](../architecture/push-model.md)** |
| `node_textfile_mtime_seconds` | detects a dead exporter script, free |
| `node_systemd_unit_state{state="failed"}` | `fleet-systemd-unit-failed` |
| `node_vmstat_oom_kill` | host-level OOM |
| `node_timex_sync_status` | clock drift |
| `prometheus_remote_storage_samples_failed_total` | remote-write health |
| `container_last_seen{name!=""}` | container presence |
| `container_start_time_seconds` | restarts and uptime |
| `container_oom_events_total` | per-container OOM |
| `container_health_state` | **[ambiguous](../monitoring/container-metrics.md#container_health_state-does-not-mean-what-it-looks-like)** |

## Query recipes

```promql
# per-host data age; every value should be < 30
time() - max by (host) (
  max_over_time(timestamp(up{job=~"integrations/unix|host-unix"})[6h:1m])
)

# hosts awaiting a reboot, with how long
(time() - fleet_reboot_required_since_timestamp_seconds)
  or (node_reboot_required == 1) - 1

# filesystem percent used, with the Proxmox exclusions
100 * (1 - (
  node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|ramfs|autofs|nsfs|fuse\\.lxcfs",mountpoint!~"/(run|dev|sys|proc|var/lib/lxcfs|etc/pve)($|/).*"}
  /
  node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs|ramfs|autofs|nsfs|fuse\\.lxcfs",mountpoint!~"/(run|dev|sys|proc|var/lib/lxcfs|etc/pve)($|/).*"}
))

# load normalised by core count, so a Pi and a hypervisor share a threshold
max by (host) (node_load15)
  / on (host)
count by (host) (count by (host, cpu) (node_cpu_seconds_total{mode="idle"}))

# containers per host
count by (host) (count by (host, name) (container_last_seen{name!=""}))

# when did each host last patch itself, in hours
(time() - fleet_unattended_upgrades_last_run_timestamp_seconds) / 3600
```

!!! danger "Always exclude `/etc/pve` and lxcfs"
    `/etc/pve` is a FUSE mount that **always** reports 100% full, and lxcfs
    binds mirror guest filesystems. Without the exclusions the disk alerts
    fire forever on any PVE host.

## LogQL recipes

```logql
# every package applied, fleet-wide audit trail
{unit="unattended-upgrades.service"} |= "Packages that will be upgraded"

# patching errors
{unit="unattended-upgrades.service"} |~ "(?i)(traceback|^E: |error|could not|failed to)"

# container logs for one service
{source="docker", container="matrix-synapse"}

# everything noisy on one host
{host="rpi5"} |~ "(?i)(error|failed|fatal|panic)"

# log volume split by source
sum by (host) (count_over_time({source="journal"}[5m]))
sum by (host) (count_over_time({source="docker"}[5m]))
```

## Adding your own

Anything a shell script can compute becomes a metric. Write a `.prom` file
into `/var/lib/node_exporter/textfile_collector/`.

```bash
TMP=$(mktemp /var/lib/node_exporter/textfile_collector/.mine.XXXXXX)
{
  echo "# HELP my_metric What it measures."
  echo "# TYPE my_metric gauge"
  echo "my_metric $value"
} > "$TMP"
chmod 0644 "$TMP"
mv -f "$TMP" /var/lib/node_exporter/textfile_collector/mine.prom
```

!!! danger "Write-then-rename, always"
    The collector will read a half-written file and export garbage. Create the
    temp file **in the same directory** so the rename is atomic within one
    filesystem.
