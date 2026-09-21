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
| `fleet_unattended_upgrades_enabled` | gauge | `1` only if apt will actually **apply** upgrades unattended |
| `fleet_autoupdate_expected` | gauge | `1` if the inventory puts this host in `autoupdate` |
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

!!! warning "`fleet_unattended_upgrades_enabled` needs all three conditions"
    The timer alone is not enough. `apt-daily-upgrade.timer` ships **enabled**
    on stock Debian, so testing only the timer reports "auto-patching is on"
    for a box that has never been configured to patch itself and may not even
    have the `unattended-upgrade` binary. That is the dangerous direction to be
    wrong in: an unpatched host looks covered.

    It now requires the timer, an effective
    `APT::Periodic::Unattended-Upgrade "1"`, and the binary on disk.

!!! tip "INTENT vs STATE: `fleet_autoupdate_expected` against `fleet_unattended_upgrades_enabled`"
    Templated from group membership, so it says whether a host is *supposed*
    to auto-patch, while the other says whether it *does*. Together they let
    `fleet-autoupdates-disabled` fire only on genuine drift, and stay silent on
    a deliberately hand-patched host like the hypervisor. Move a host between
    `autoupdate` and `no_autoupdate` and the alert's scope follows on its own.

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

## Per-process metrics

From `prometheus.exporter.process`, **opt-in per host** via
`alloy_enable_process`; see [Variables](https://harshitruwali.github.io/homelab-infra/ansible/reference/variables/#collector).

| Metric | Meaning |
|---|---|
| `namedprocess_namegroup_num_procs` | processes in the group |
| `namedprocess_namegroup_cpu_seconds_total` | CPU seconds, by mode |
| `namedprocess_namegroup_memory_bytes{memtype="resident"}` | RSS; `virtual` exists but is misleading |
| `namedprocess_namegroup_num_threads` / `..._open_filedesc` | threads and open FDs |
| `namedprocess_namegroup_oldest_start_time_seconds` | age of the oldest PID in the group |

Grouped by `groupname`, which is the command name, so every PID of the same
program is one series and a restart does not mint a new one.

!!! danger "`comm` and `cmdline` are not interchangeable in the matcher"
    `comm` is a list of **exact** command names; `cmdline` is a list of
    **regexes**. So `comm = [".+"]` is not "match everything", it matches a
    process literally named `.+`, which is nothing. The symptom is the worst
    kind: the component starts cleanly, the scrape pool comes up, and exactly
    zero series are produced. Use `cmdline = [".+"]`.

## SMART disk health

Written to `fleet-smart.prom` by `fleet-smart-metrics`, every 15 minutes and
at boot, from `ansible/roles/smart_metrics`.

| Metric | Meaning |
|---|---|
| `fleet_smart_device_health_ok` | `1` if the drive's own SMART self-assessment passes |
| `fleet_smart_temperature_celsius` | current drive temperature |
| `fleet_smart_power_on_hours` / `fleet_smart_power_cycle_count` | age and spin-ups |
| `fleet_smart_reallocated_sectors` | remapped bad sectors (SATA) |
| `fleet_smart_pending_sectors` | unreadable, not yet remapped: **the urgent one** |
| `fleet_smart_offline_uncorrectable` | sectors that failed offline scan |
| `fleet_smart_crc_errors` | cumulative SATA link faults; read the slope, not the value |
| `fleet_smart_nvme_percentage_used_ratio` | rated write endurance consumed, `0`-`1` |
| `fleet_smart_nvme_available_spare_ratio` | spare blocks left, `0`-`1` |
| `fleet_smart_nvme_media_errors` / `..._unsafe_shutdowns` | NVMe integrity counters |
| `fleet_smart_devices_total` | SMART-capable devices found |
| `fleet_smart_collection_timestamp_seconds` | when collection last ran |

All are labelled `host`, `device`, `model` and `serial`, one series per
physical drive.

!!! warning "Only bare metal can report these, and the reason differs per platform"
    An **LXC** guest sees the host's disks in `/sys`, so `lsblk` lists them,
    but has no `/dev` nodes and cannot open them. A **KVM** guest has a
    `/dev/sda` that answers `device lacks SMART capability`. An **SD card**
    does not implement SMART, and the eMMC health fields (`life_time`,
    `pre_eol_info`) are absent on real SD media.

    So "no data" is the correct permanent state for every virtualised host,
    which is why every rule in `rules-storage.yaml` uses `noDataState: OK` and
    why `fleet-smart-collector-stale` exists to catch a genuinely dead
    collector instead.

!!! tip "Alert on the slope of `fleet_smart_crc_errors`, never the value"
    It is a cumulative lifetime counter that never resets, and it usually
    means a SATA cable rather than a dying platter. The HDD in this estate
    already carries 23 from a past event, so a `> 0` rule would fire forever.
    `fleet-smart-crc-errors-rising` uses `increase(...[24h]) > 0`.

## Proxmox guest NICs

Written to `fleet-pve-guests.prom` by `fleet-pve-guests`, every 5 minutes and
at boot, from `ansible/roles/pve_guest_metrics`. Only on a Proxmox VE node,
which today means the `metal` group.

| Metric | Meaning |
|---|---|
| `fleet_pve_guest_nic_info` | always `1`; one series per guest NIC, labelled `device`, `guest`, `vmid`, `type`, `nic` and `bridge` |
| `fleet_pve_guest_nics_total` | guest NICs named on this node |
| `fleet_pve_guests_collection_timestamp_seconds` | when naming last ran |

It carries no traffic itself. It names the traffic node_exporter already
reports: Proxmox gives every guest NIC a device on the host, `tap<vmid>i<n>` for
a VM and `veth<vmid>i<n>` for a container, and `device` is the join key.

```promql
# bytes per second each guest NIC SENDS
rate(node_network_receive_bytes_total{device=~"(tap|veth)[0-9]+i[0-9]+"}[5m])
  * on (host, device) group_left (guest, nic, bridge) fleet_pve_guest_nic_info
```

!!! warning "Receive on the host is send on the guest"
    The device is the host's end of the guest's virtual cable, so the host's
    *receive* counter is what the guest *sent*. Every panel on
    [Guest Traffic](../monitoring/dashboards.md#guest-traffic) swaps the two;
    do the same in your own queries.

## GPU telemetry

Scraped from the standalone `nvidia_gpu_exporter` service (see
`ansible/roles/gpu_exporter`), installed only on
hosts where `nvidia-smi` is present. Same figures `nvtop` shows interactively.

| Metric | Meaning |
|---|---|
| `nvidia_smi_utilization_gpu_ratio` | GPU compute utilization, `0`-`1` |
| `nvidia_smi_memory_used_bytes` / `nvidia_smi_memory_total_bytes` | VRAM used / installed |
| `nvidia_smi_temperature_gpu` | die temperature, Celsius |
| `nvidia_smi_power_draw_watts` | current power draw |
| `nvidia_smi_fan_speed_ratio` | fan duty cycle, `0`-`1` |

All are labelled `host` and `uuid` (one series per physical GPU), same as
every other exporter in this stack.

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
