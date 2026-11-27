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
| `namedprocess_namegroup_memory_bytes{memtype="resident"}` | RSS; the collector drops the other memory types (`alloy_process_drop_memtypes`) |
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
| `fleet_pve_guests_collection_timestamp_seconds` | when the last complete run finished; if a read fails the previous file is kept, so its age is how a failure shows |
| `fleet_pve_tenant_conntrack_entries` | gauge: conntrack entries whose original source is a tenant or lease VM's address, labelled `vmid`, `guest`, `ip` and `segment` (`lease` or `work`) |
| `fleet_pve_firewall_dropped_packets_total` | counter: packets the tenant and lease firewall groups dropped, labelled `chain`, `rule` (`private`, `private_in` or `smtp`) and `family` (`ipv4` or `ipv6`); sum over `family` unless you want the split |
| `fleet_pve_tenant_policed_packets_total` | counter: packets dropped by a lease's packet-rate ceiling, labelled `vmid`, `guest` and `segment`; resets when the VM restarts, so use `rate()` or `increase()` |
| `fleet_pve_tenant_packet_limit_active` | gauge: `1` while a running lease has its packet-rate ceiling, `0` when it lost it (Proxmox's hot NIC update deletes the filter), labelled `vmid`, `guest` and `segment` |
| `fleet_pve_tenant_firewall_active` | gauge: `1` only when the Proxmox firewall is filtering a tenant or lease VM (datacenter firewall on, `firewall=1` on `net0`, `enable: 1` and the right group in its `.fw` file, and its tap chain exists), `0` when any of that fails, labelled `vmid`, `guest` and `segment` |

The last five are about the VMs tagged `tenant` and are read by the tenant
alert rules and the
[Tenant Activity](../monitoring/dashboards.md#tenant-activity) dashboard. A
stopped VM, or one without its address or filter, has no series, so an absent
series is normal where no lease runs. The file is rewritten every 5 minutes, so
a counter changes only at that step: query `rate()` and `increase()` over 15
minutes or more, because a shorter window can hold no update and read 0.

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

## Hardware sensors

Stock node_exporter hwmon series: `prometheus.exporter.unix` reads
`/sys/class/hwmon` on every host with no extra exporter. Only the hypervisor
has anything meaningful there. On t7920 (Dell Precision 7920 Tower, two Xeon
Gold 6138) the chips are:

| `chip` | Driver | What it reports |
|---|---|---|
| `platform_coretemp_0` / `_1` | `coretemp` | one per CPU socket. `sensor="temp1"` is the package; the rest are cores, named by `node_hwmon_sensor_label` |
| `platform_dell_smm_hwmon` | `dell_smm_hwmon` | six board temperatures, and `fan1`-`fan4` unnamed (use `fleet_dell_fan_rpm` instead) |
| `thermal_thermal_zone0` | `pch_lewisburg` | the C620 chipset |
| `nvme_nvme0` | `nvme` | the NVMe drive's own sensors, `temp1` is the composite |

| Metric | Meaning |
|---|---|
| `node_hwmon_temp_celsius` | current temperature |
| `node_hwmon_temp_max_celsius` / `..._crit_celsius` | the part's own limits: 83 C and 93 C (TjMax) for these CPUs |
| `node_hwmon_fan_rpm` | current fan speed |
| `node_hwmon_sensor_label` | the human name for a sensor, always `1`; join it with `group_left (label)` |

!!! warning "Every LXC guest reports the hypervisor's sensors too"
    An LXC shares the host kernel, so it sees the same `/sys/class/hwmon`.
    memory, sec-dns, tailscale-router and main-server all push exact
    copies of t7920's fans and temperatures under their own `host` label.
    Always scope hardware queries to `host="t7920"` or `role="hypervisor"`,
    as every rule in `rules-hardware.yaml` does.

!!! info "What the Dell SMM board sensors do not tell you"
    The BIOS reports no type for any board sensor, so their `_label` files
    return `Invalid argument` and none appear in `node_hwmon_sensor_label`.
    What is known from the data: SMM `temp1` and `temp2` track the CPU 0 and
    CPU 1 packages within a few degrees. Fan control is BIOS-automatic
    (`pwm_enable=2`), so there is no duty-cycle figure, except while the
    fan boost below holds manual mode.

### Named chassis fans

`dell_smm_hwmon` hardcodes four fans; this firmware knows twelve. Written to
`fleet-dell-fans.prom` every 30 s by `fleet-dell-fan-metrics`, from
`ansible/roles/dell_fan_metrics`, which reads each fan through the same
driver's `/proc/i8k` ioctl and names it from SMBIOS.

| Metric | Meaning |
|---|---|
| `fleet_dell_fan_rpm{fan, zone, index}` | current speed; `fan` is the firmware name, `zone` is `cpu`, `front`, `rear`, `drive bay` or `psu` |
| `fleet_dell_fans_total` | fans SMBIOS lists with a Dell SMM index |
| `fleet_dell_fan_read_errors` | fans whose read failed on the last run |
| `fleet_dell_fan_collection_timestamp_seconds` | when collection last ran |

Where each fan sits, from the Owner's Manual:

| `fan` | Position | Notes |
|---|---|---|
| `SYS0`-`SYS3` | four fans behind the front bezel, bottom to top | intake, cool CPU 0 directly |
| `CPU1` | inside the air shroud, between the sockets | pushes air on to CPU 1 |
| `REAR0`, `REAR1` | back wall beside the CPUs | exhaust |
| `FB0`, `FB1` | behind front drive bays 0 and 1 (right side) | smaller fans, idle near 2500 RPM |
| `FB4` | behind optional rear drive bay 4 (right side) | this machine has no rear bays, so no fan; the BIOS lists it anyway and it reads 0 |
| `CPU0` | a header on the board, no fan fitted on this model | always 0 |
| `PSU` | inside the power supply | no tachometer on SMM, always 0 |

FB2 and FB3 exist on the chassis but this machine's BIOS does not list them.

!!! note "How a name is matched to a reading"
    Each SMBIOS type 27 entry carries a Dell OEM word `0x0000DDnn`, where `nn`
    is the SMM fan index. Verified 2026-09-23: indices 0 to 3 matched
    `dell_smm_hwmon`'s `fan1`-`fan4` exactly in the same instant, so
    `fan1` is the empty CPU0 header, `fan2` is CPU1, and `fan3`/`fan4` are
    SYS0/SYS1. The seven mandatory fans read together at 780 to 950 RPM and
    the drive-bay fans in their own band, as the manual's fan list predicts.

### CPU fan boost

Written to `fleet-dell-fan-boost.prom` every 5 s by `fleet-dell-fan-boost`, from
`ansible/roles/dell_fan_boost`, which runs the CPU1 fan at full speed while
CPU 1's package is hot.

| Metric | Meaning |
|---|---|
| `fleet_dell_fan_boost_active` | 1 while a fan is held at full speed in manual mode |
| `fleet_dell_fan_boost_guard_active` | 1 while another package is too hot to allow manual mode |
| `fleet_dell_fan_boost_transitions_total` | boost starts and ends since the service started |
| `fleet_dell_fan_boost_package_celsius` | the package temperature the boost follows |
| `fleet_dell_fan_boost_timestamp_seconds` | when the controller last ran; stale means it is down, and the fans are then on automatic |

## TP-Link router

Written to `tplink-router-<name>.prom` every 60 s by `tplink-router-poll`, from
`ansible/roles/tplink_router_poll`, on a host on the router's LAN. Every series
carries `router` (the poller's `--name`); client series also carry `mac`,
`name` and `ip`, and Wi-Fi series `band`. The [Router TPLink Blr](../monitoring/dashboards.md#router-tplink-blr)
dashboard reads them.

| Metric | Meaning |
|---|---|
| `tplink_router_poll_success` | 1 if the last poll read the router |
| `tplink_router_poll_skipped` | 1 if the last poll was skipped on purpose; `reason` is `busy` (someone is logged in to the web page) or `wrong_network` (the router MAC did not match) |
| `tplink_router_poll_timestamp_seconds`, `tplink_router_poll_duration_seconds` | when the poller last ran and how long it took; stale means the host is off or cannot reach central |
| `tplink_router_info` | model, hardware and firmware labels, value 1 |
| `tplink_router_uptime_seconds` | the router's uptime; every counter below restarts at zero when it reboots |
| `tplink_router_cpu_percent`, `tplink_router_memory_bytes{state}` | router load |
| `tplink_router_wan_online{family}` | 1 while the internet connection is up |
| `tplink_router_wan_bytes_total{direction}` | the router's own internet counter; it misses traffic forwarded in hardware, so compare, do not bill from it |
| `tplink_router_client_info` | every client the router knows, value 1 while active |
| `tplink_router_client_bytes_total`, `tplink_router_client_packets_total` | the router's per-IP traffic statistics, both directions together; reads lower than the Wi-Fi counters |
| `tplink_router_wifi_client_bytes_total{direction}` | **the usage figure**: bytes per Wi-Fi client, `download` is router to client; restarts when the client reconnects |
| `tplink_router_wifi_client_packets_total{direction}`, `tplink_router_wifi_client_retransmissions_total` | Wi-Fi packets and resends |
| `tplink_router_wifi_client_signal_dbm`, `tplink_router_wifi_client_link_rate_bits{direction}` | link quality per client |
| `tplink_router_log_forwarding{server,port}` | 1 while the router's remote log points at the poller host; 0 means its log is not arriving |
| `tplink_router_log_messages_total{severity}`, `tplink_router_log_rejected_total` | router log lines received since the poller started, and datagrams from any other address, dropped |

Usage per client over a range, robust to the counter restarts:

```promql
sum by (name, mac) (increase(tplink_router_wifi_client_bytes_total{router="blr"}[$__range]))
```

The poller also writes logfmt lines to its journal, which Alloy ships to Loki
under `{host="<poller host>", unit="tplink-router-poll.service"}`:

| Line | Fields |
|---|---|
| `usage` | `kind` (`measured` or `carried`), `download_bytes`, `upload_bytes`, `seconds`, `mac`, `name`, `ip`, `band`; one per client with traffic, per poll |
| `session_start`, `session_end` | `start`, and for the end `end`, `seconds` and the session's bytes; plus the client labels |
| `router_log` | `severity`, `facility`, `msg`: a line from the router's own log |

Usage per client over the time picker, up to Loki's 30 days:

```logql
sum by (name, mac) (sum_over_time({host="skullsaints", unit="tplink-router-poll.service"} |~ "^usage " | logfmt | unwrap download_bytes [$__range]))
```

!!! note "Why the Wi-Fi counters and not the router's statistics"
    Measured 2026-11-20 on an Archer C5 v6.8: a 20.2 MB download moved the
    client's Wi-Fi `download` counter by 22.7 MB, the router's internet counter
    by 8.1 MB, and it never caught up. The router forwards most traffic in
    hardware, past the counters its statistics page uses. A wired client would
    not appear in the Wi-Fi counters at all; none was connected.

## Internet probe

Written to `internet-probe.prom` every 15 s (and at once when an outage starts or
ends) by `internet-probe`, from `ansible/roles/internet_probe`, on one host per
site (`home`, `blr`, `homelab`). Every series carries `site`; Alloy adds `host` and
`instance`. Target series also carry `kind` (`tcp`, `dns`, `http`) and `target`
(`HOST:PORT`); LAN series carry `name` and `target`, path series `name` (and `target` for per-target series). A round reaches the internet when any `tcp` or `dns` target
answers; `http` and the gateway never decide.

| Metric | Meaning |
|---|---|
| `internet_probe_up` | 0 if any round failed to reach the internet within the last 15 s (the write interval), else 1; the window is by time, not by write, so a blip or short outage between two scrapes still shows |
| `internet_probe_outage_active` | 1 while a confirmed outage is open |
| `internet_probe_outage_seconds` | seconds into the open outage, 0 when none |
| `internet_probe_down_seconds_total` | seconds of confirmed outage: the closed outages in the ledger plus the open one; restored from the ledger at start-up, so it survives restarts |
| `internet_probe_outages_total` | confirmed outages, counted from the ledger |
| `internet_probe_blips_total` | failed rounds too short to confirm, since the process started |
| `internet_probe_rounds_total`, `internet_probe_failed_rounds_total` | rounds, and rounds that did not reach the internet, since the process started |
| `internet_probe_target_up{kind,target}` | 1 if the target answered in the last round |
| `internet_probe_target_rtt_seconds{kind,target}` | round trip of the last round; absent when it failed |
| `internet_probe_target_failures_total{kind,target}` | failed probes since the process started |
| `internet_probe_gateway_up{gateway}` | 1 if the gateway answered or refused in the last round |
| `internet_probe_gateway_rtt_seconds{gateway}` | gateway round trip; absent when down |
| `internet_probe_last_outage_start_timestamp_seconds`, `internet_probe_last_outage_end_timestamp_seconds` | the latest closed outage; absent until there is one |
| `internet_probe_last_outage_duration_seconds{cause}` | its length; `cause` is `local` (the gateway did not answer) or `upstream` (it did) |
| `internet_probe_lan_up{name,target}` | 0 if any round failed to reach the LAN target within the last 15 s, else 1; a connect that succeeds or is refused is up. Never changes `internet_probe_up` |
| `internet_probe_lan_rtt_seconds{name,target}` | connect time in the last round; absent when it failed |
| `internet_probe_lan_failures_total{name,target}` | failed rounds since the process started |
| `internet_probe_lan_outage_active{name}` | 1 while a confirmed LAN outage is open (same `--confirm-rounds` as the internet) |
| `internet_probe_lan_down_seconds_total{name}`, `internet_probe_lan_outages_total{name}` | restored from the ledger table `lan_outages`, so they survive restarts; the first includes the open outage |
| `internet_probe_path_up{name}` | 0 if any round failed to reach the internet over the named path within the last 15 s, else 1; the path is up when any of its `tcp` or `dns` targets answers. Never changes `internet_probe_up` |
| `internet_probe_path_rtt_seconds{name,target}` | round trip of a path target in the last round; absent when it failed |
| `internet_probe_path_failures_total{name,target}` | failed probes of a path target since the process started |
| `internet_probe_path_outage_active{name}` | 1 while a confirmed path outage is open (same `--confirm-rounds` as the internet) |
| `internet_probe_path_down_seconds_total{name}`, `internet_probe_path_outages_total{name}` | restored from the ledger table `path_outages`, so they survive restarts; the first includes the open outage |
| `internet_probe_ledger_ok` | 1 if the last ledger operation succeeded, 0 if it failed or the ledger could not be opened (a failed ledger is re-checked every minute, so it recovers without waiting for an outage); the probe keeps running and counts in memory (`ledger_error` line in the journal) |
| `internet_probe_interval_seconds` | seconds between rounds |
| `internet_probe_timestamp_seconds` | when the file was written; stale means the host or the probe is down |

Downtime over the time picker, robust to restarts:

```promql
sum by (site) (increase(internet_probe_down_seconds_total[$__range]))
```

The probe also writes logfmt lines to its journal, which Alloy ships to Loki
under `{host="<probe host>", unit="internet-probe.service"}`. The event is the
first word:

| Line | Fields |
|---|---|
| `startup` | `site`, `interval`, `confirm_rounds`, `targets`, `gateway` |
| `outage_start` | `site`, `start`, `cause`, `gateway_up` |
| `outage_end` | `site`, `start`, `end`, `duration_seconds`, `cause`, `closed_by` (`probe`, or `restart` if the process died during the outage) |
| `blip` | `site`, `start`, `duration_seconds`, `gateway_up` |
| `lan_down` | `site`, `name`, `target`, `start` |
| `lan_up` | `site`, `name`, `target`, `start`, `end`, `duration_seconds`, `closed_by` (`probe` or `restart`) |
| `lan_blip` | `site`, `name`, `target`, `start`, `duration_seconds` |
| `path_down` | `site`, `name`, `start` |
| `path_up` | `site`, `name`, `start`, `end`, `duration_seconds`, `closed_by` (`probe` or `restart`) |
| `path_blip` | `site`, `name`, `start`, `duration_seconds` |
| `ledger_error`, `textfile_error` | `site`, `op` (ledger only), `error`; at most one per operation per 10 minutes |

```logql
{unit="internet-probe.service"} |~ "^outage_end " | logfmt
```

Prometheus keeps 7 days and Loki 30. The ledger on the probe host keeps every
outage; read it with `internet-probe.py --report outages` or `--report daily` (see
the `internet_probe` section of the Ansible playbooks reference).

## ER605 home router

Written to `er605.prom` every 30 s by `er605-poll`, from `ansible/roles/er605_snmp`
on ubuntu-dev, which reads the ER605 over SNMPv3 through a local `snmp_exporter`.
Alloy adds `host` (`ubuntu-dev`). The router forwards in hardware and syncs its
counters tens of seconds late, so rates under a minute are not meaningful; totals
are exact. A failed poll writes only `er605_poll_success 0`, the poll duration and
the last good `er605_last_scrape_timestamp_seconds`, so the counters show a gap
instead of a frozen value.

| Metric | Meaning |
|---|---|
| `er605_if_octets_total{interface,role,direction}` | bytes on every WAN port (`role="wan"`, `interface` is the port, for example `eth1`; `in` is download, `out` is upload) and on `eth0` (`role="lan"`, the LAN side); `sum(...{role="wan"})` is the household total whatever the number of WANs; restarts from zero on a router reboot |
| `er605_if_errors_total{interface,role,direction}` | input errors on `eth1` and `eth0` |
| `er605_if_discards_total{interface,role,direction}` | input and output discards on `eth1` |
| `er605_last_scrape_timestamp_seconds` | when the poller last read the router; stale means the poller stopped or the router did not answer |
| `er605_cpu_percent{core}` | load per core (0 to 3) |
| `er605_memory_bytes{state}` | `total`, `used`, `cached` and `buffers` |
| `er605_uptime_seconds` | time since the router booted |
| `er605_if_up{interface,role}` | 1 if the interface is up: each WAN port (`wan`), `eth0` (`lan`) and each PPPoE session interface such as `pe-wan1_poe` (`pppoe`) |
| `er605_wan_info{wan,interface,pppoe,isp}` | one series per configured WAN, value 1; `pppoe` and `isp` are empty when unset. Source of the `wan` variable on the dashboards |
| `er605_wan_up{wan}` | 1 if the WAN port is up and, for a PPPoE WAN, its session interface exists and is up. A configured WAN whose interface the router does not report is 0 |
| `er605_pppoe_session_seconds{wan}` | age of the WAN's current PPPoE session (uptime minus the last change of its session interface); drops to zero on a reconnect |
| `er605_pppoe_reconnects_total{wan}` | PPPoE reconnects per WAN, counted from the ledger table `reconnects`, so it survives poller restarts; a router reboot is not counted |
| `er605_pppoe_octets_total{wan,direction}` | bytes of the WAN's current PPPoE session; restarts from zero on a reconnect |
| `er605_poll_success` | 1 if the last poll read the router, 0 if it failed |
| `er605_poll_duration_seconds` | how long the last poll took |

Household download over the range, robust to counter restarts:

```promql
sum(increase(er605_if_octets_total{role="wan",direction="in"}[$__range]))
```

The poller also writes logfmt lines to its stdout. Alloy ships container logs to
Loki under `{host="ubuntu-dev", job="docker", container="er605-poll"}`
(`compose_service` is `er605-poll` too). The event is the first word:

| Line | Fields |
|---|---|
| `startup` | `interval`, `keep_days`, `data` |
| `usage` | `hour` (UTC hour start), `wan`, `download_bytes`, `upload_bytes` from that WAN's port; one per WAN per closed hour |
| `pppoe_reconnect` | `at` (start of the new session), `wan`, `previous_session_seconds` |
| `wan_down` | `at`, `wan`; the WAN's `er605_wan_up` went to 0 (also logged once for a WAN first seen down or whose interface is missing) |
| `wan_up` | `at`, `wan`, `duration_seconds` |
| `router_reboot` | `at` (estimated boot time) |
| `poll_error` | `error`; at most one per 10 minutes |

```logql
sum(sum_over_time({host="ubuntu-dev", job="docker", container="er605-poll"} |~ "^usage " | logfmt | unwrap download_bytes [1d]))
```

Prometheus keeps 7 days and Loki 30. The ledger `usage.db` keeps every hour, and
the raw CSV days are kept 400 days; read the ledger with `--report hour|day|month|reconnects|wan`
(see the `er605_snmp` section of the Ansible playbooks reference).

## OPNsense

Scraped by the central Alloy from the firewall's LAN (`alloy_extra_scrape_targets`
in `ansible/inventory/group_vars/central`), with `host="opnsense"`,
`role="firewall"`. How it is set up: [Routers and firewall](../monitoring/routers.md#opnsense).

| Job | Source | Notable series |
|---|---|---|
| `opnsense-node` | the `os-node_exporter` plugin, `10.10.50.1:9100` | `node_cpu_seconds_total`, `node_memory_*`, `node_network_*`, `node_filesystem_*`, `node_load1` (FreeBSD names differ from Linux in places) |
| `opnsense-api` | `opnsense-exporter`, `10.10.50.125:9201` | `opnsense_interfaces_{received,transmitted}_bytes_total`, `opnsense_firewall_{in,out}_ipv{4,6}_{pass,block}_packets`, `opnsense_pf_states_{current,limit}`, `opnsense_gateways_info`, `opnsense_gateways_monitor_info`, `opnsense_services_status`, `opnsense_firmware_*`, `opnsense_arp_table_entries`, `opnsense_protocol_tcp_connection_count_by_state`, `opnsense_unbound_dns_*` |

`opnsense_up` reads 0 on OPNsense 26.7 even when everything works; see
[the warning](../monitoring/routers.md#opnsense).


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

## Inter-segment traffic

Written by `segflows.py` on the firewall (cron, every 5 minutes) to
`/var/tmp/node_exporter/segflows.prom`, which `os-node_exporter` serves as the
`opnsense-node` job (`host="opnsense"`, `role="firewall"`). The counters come
from NetFlow records, each counted once, and are restored from the firewall's
state file, so they only reset if that file is deleted. Setup and the
permanent ledger: [Routers and firewall](../monitoring/routers.md#inter-segment-traffic).

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `opnsense_segflow_bytes_total` | counter | `src_segment`, `dst_segment` | bytes of flows from one segment to another |
| `opnsense_segflow_packets_total` | counter | `src_segment`, `dst_segment` | packets of the same flows |
| `opnsense_segflow_flows_total` | counter | `src_segment`, `dst_segment` | NetFlow records (a long connection is several) |
| `opnsense_segflow_last_run_timestamp_seconds` | gauge | none | Unix time the last run finished |
| `opnsense_segflow_run_duration_seconds` | gauge | none | how long the last run took |
| `opnsense_segflow_lag_seconds` | gauge | none | now minus the receive time of the newest flow processed |
| `opnsense_segflow_capture_interfaces` | gauge | `interface`, `segment` | 1 for each interface that has a NetFlow capture node; `segment` is the OPNsense interface label (`TENANT`, `LEASE`, `WAN`, ...) |

Segment values: `LAN`, `TENANT`, `DMZ`, `LAB`, `LEASE` (the firewall's own
subnets), `HOME` (the home LAN, `10.10.0.0/24`, `10.10.1.0/24`, `10.10.2.0/24`,
`192.168.0.0/16`), `TAILNET` (`100.64.0.0/10`), `FIREWALL` (the firewall's own
addresses), `LINKLOCAL` (multicast, broadcast and link-local), `PRIVATE` (any
other private range) and `INTERNET` (everything else). Only traffic that
crosses a captured interface is a flow: traffic inside one segment is not
counted. Used by the [Inter-LAN dashboard](../monitoring/dashboards.md#inter-lan).

## Network history

Written by `net-history.py` (the `network_history` role) on the central host to
`net-history.prom`, which the central Alloy ships. No labels except where noted.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `net_history_last_run_timestamp_seconds` | gauge | none | Unix time the last aggregator run finished, successful or not |
| `net_history_run_success` | gauge | none | 1 if the last run succeeded, 0 if Prometheus, Loki or the database failed |
| `net_history_rows` | gauge | `table` | rows stored in each table of schema `net` in PostgreSQL (`internet_daily`, `lan_daily`, `path_daily`, `wan_daily`, ..., `outage_events`); written only after a successful run, so absent after any failed run (Prometheus, Loki or the database failing) |

The 90-day tier (`prometheus-network.service`) holds the same metrics as the
central Prometheus for the network dashboards, copied by federation, so it adds
no metric names of its own except `up{job="federate-central"}` inside that store.
The permanent tables and views are listed by `SELECT * FROM net.catalog`; see
[Data flow](../architecture/data-flow.md#long-term-network-history).

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
