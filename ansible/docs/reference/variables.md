# Variables

The knobs that matter, where they live, and what they cost.

For how variable precedence works at all, see
[Ansible concepts](../getting-started/ansible-basics.md#variables-and-where-they-live).

!!! warning "Precedence"
    `group_vars/<group>/main.yml` beats vars set in the inventory file itself.
    Put site-specific values in `hosts.local.yml` and keep them **out** of
    `group_vars`, or a placeholder will silently win.

## Collector

`roles/alloy_collector/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `alloy_package_state` | `present` | never `latest`; bump deliberately with `-e alloy_package_state=latest` |
| `alloy_scrape_interval` | `15s` | |
| `alloy_journal_max_age` | `12h` | lower to `1h` on hosts with huge journals |
| `alloy_enable_docker` | `auto` | `auto` stats `/var/run/docker.sock` |
| `alloy_enable_process` | `false` | per-process metrics; **opt-in on cardinality grounds** |
| `alloy_syslog_listener_port` | `""` | empty means no listener; `5514` in `group_vars/central` for the firewall's Suricata output |
| `alloy_fs_mount_points_exclude` | see file | |
| `alloy_fs_types_exclude` | see file | |
| `alloy_systemd_unit_exclude` | see file | |
| `journald_system_max_use` | `""` | empty leaves journald's own default |
| `collector_basic_auth_required` | `true` | `false` on the central node |

!!! warning "`alloy_enable_process` is off by default for a measured reason"
    It emits about **1000 series per host** (measured on `ubuntu-dev`: 47
    process groups, 1072 series) against a fleet head of ~52k. Enabling it
    everywhere is roughly a 20% jump in series, and at 7-day retention that is
    real disk on the central LXC, which has already hit 100% once.

    Turn it on where you would actually open a process view, and set it in
    `host_vars`, not with `-e`: a command-line override lasts one run, and the
    next `collectors.yml` pass would re-render the config without it and
    silently drop the metrics.

    ```yaml
    # inventory/host_vars/<host>.yml
    alloy_enable_process: true
    ```

!!! danger "Backslashes in these regexes"
    They land inside double quotes in the generated `.alloy` file, and Alloy
    uses **Go string escaping** there. A lone `\.` is an unknown escape
    sequence and `alloy fmt` rejects the whole config.

    Write `\\.` so the file contains `\\.`, which Alloy unescapes to the `\.`
    the regex engine wants. Keep these single-quoted in YAML.

## SMART disk health

`roles/smart_metrics/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `smart_metrics_enabled` | `false` | **the only switch**; set `true` in `group_vars/metal` |
| `smart_metrics_oncalendar` | `*:0/15` | every 15 minutes, like `update_metrics` |
| `smart_metrics_randomized_delay` | `300` | staggers the fleet |
| `smart_metrics_nocheck` | `standby` | skips a spun-down drive rather than waking it |

!!! note "Inventory-gated, not autodetected, unlike `gpu_exporter`"
    Detecting SMART capability requires smartmontools to already be installed,
    so an autodetecting role would install it on every host to discover that
    almost none can use it, then report `changed` forever removing it again.
    It is also not guessable from the platform. One line in `group_vars/metal`
    is the honest way to express "this host has real disks", matching how
    `autoupdate` gates patching.

!!! danger "Never let `{` and `%` become adjacent in the exporter template"
    `templates/fleet-smart-metrics.j2` is rendered as Jinja before install, so
    a Prometheus format string like the obvious one for `name{labels} value`
    is read as a Jinja statement tag and the play dies with
    `Encountered unknown tag 's'`. The script builds that line by
    concatenation instead. This applies to comments too.

## Proxmox guest NIC names

`roles/pve_guest_metrics/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `pve_guest_metrics_enabled` | `false` | **the only switch**; set `true` in `group_vars/metal` |
| `pve_guest_metrics_oncalendar` | `*:0/5` | every 5 minutes, so a new guest is named quickly |
| `pve_guest_metrics_randomized_delay` | `60` | seconds of jitter |

node_exporter on a Proxmox VE node already reports every guest NIC, because
each one has a host-side device: `tap<vmid>i<n>` for a VM, `veth<vmid>i<n>` for
a container. This role writes `fleet_pve_guest_nic_info`, the join key that
names those counters, for the Guest Traffic dashboard. The metric reference is
in the monitoring docs, under
[Proxmox guest NICs](https://harshitruwali.github.io/homelab-infra/monitoring/reference/metrics/#proxmox-guest-nics).

!!! note "It checks the switch rather than trusting it"
    Enabled on a host without `/usr/bin/pvesh`, the role fails the play with a
    message saying so, instead of installing a timer that fails every five
    minutes on a host that cannot answer.

!!! danger "Same Jinja trap as the SMART exporter"
    `templates/fleet-pve-guests.j2` is a Python script rendered as Jinja, so it
    too builds each exposition line by concatenation. Never let `{` and `%`
    become adjacent in it, comments included.

## GPU exporter

`roles/gpu_exporter/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `gpu_exporter_version` | `1.14.0` | pinned; no apt repo exists for this exporter, so this is what stands in for `alloy_package_state: present` |
| `gpu_exporter_listen_addr` | `127.0.0.1:9835` | loopback only; Alloy scrapes it locally |
| `gpu_exporter_collect_processes` | `true` | per-process VRAM, the nvtop process table |
| `gpu_exporter_checksums` | see file | sha256 per arch, from the release's signed `checksums.txt`; bump alongside the version |

!!! tip "`gpu_exporter_collect_processes` is what makes the GPU process table work"
    It passes `--collect.compute-apps`, which the exporter leaves off by
    default. Without it you can see that the GPU is full but not *what* is
    filling it, which is the one question the panel exists to answer.

    Cardinality is bounded by the number of processes holding a CUDA context,
    a handful even on a busy box, so this is nothing like the per-process CPU
    exporter. An empty table means nothing holds a context right now, not that
    collection is broken.

Installed only where `nvidia-smi` is found on the host (autodetected, same
`auto` idiom as `alloy_enable_docker`), and purged again if it disappears.
`gpu_exporter_available` is the fact the role sets, read by
`config.alloy.j2` to decide whether to emit the scrape block.

## Patching

`roles/unattended_upgrades/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `uu_apply_security_only` | `false` | `false` means `o=*`, every origin |
| `uu_automatic_reboot` | `false` | **hard requirement**, asserted every run |
| `uu_package_blacklist` | `[]` | only the `pi_debian` group overrides this |
| `uu_remove_unused_kernels` | `true` | `false` on `proxmox` and `central` |
| `uu_clean_interval_days` | `7` | `1` on small-rootfs hosts |
| `uu_upgrade_oncalendar` | `*-*-* 03:00` | |
| `uu_upgrade_randomized_delay` | `3600` | staggers the fleet |

## Container updates

`roles/docker_updates/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `docker_update_oncalendar` | `*-*-* 04:00` | must not overlap the apt window |
| `docker_update_randomized_delay` | `1800` | registry rate limits are per source IP |
| `docker_update_skip_projects` | `[]` | by Compose project name |
| `docker_update_prune` | `true` | dangling images only |
| `docker_update_health_wait_seconds` | `60` | settle time before the host-wide unhealthy/restarting check; skipped when no image changed |
| `docker_update_run_now` | unset | `-e docker_update_run_now=true` for a supervised run |

## Update metrics

`roles/update_metrics/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `update_metrics_oncalendar` | `*:0/15` | every 15 minutes |
| `update_metrics_randomized_delay` | `300` | `600` on the Pis |
| `update_metrics_use_needrestart` | `true` | installed in non-interactive mode |

## Dashboards

`roles/grafana_dashboards/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `grafana_dashboard_root` | `/var/lib/grafana/dashboards` | parent of the two provider dirs |
| `grafana_dashboard_dirs` | `[fleet, servers]` | **must stay disjoint**; Grafana scans provider paths recursively |
| `grafana_dashboard_prune` | `true` | remove dashboards no longer committed |

!!! warning "Turning off `grafana_dashboard_prune` makes the deploy add-only"
    The provider runs with `disableDeletion: false`, so Grafana removes a
    dashboard from its database when the file disappears. Leave a stale file
    behind and it keeps resurrecting a dashboard you deleted from git.

## Alerting

`roles/grafana_alerting/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `grafana_admin_user` | `admin` | **override this**; `lxc-install.sh` writes a real username |
| `grafana_api_url` | `http://127.0.0.1:3000` | |
| `central_host_label` | from `host_vars` | must match `MONITOR_HOSTNAME` |
| `grafana_alerting_static_files` | 11 files | `rules-availability.yaml` is templated, not listed |

## Site-wide

`group_vars/all/main.yml` and `hosts.local.yml`

| Variable | Notes |
|---|---|
| `monitoring_domain` | builds both ingest URLs |
| `lan_jump_host` | ProxyJump target for `lan_guests` |
| `lan_use_jump_host` | set `false` when running from the LAN |
| `alloy_textfile_dir` | `/var/lib/node_exporter/textfile_collector` |
| `monitor_role` | per host: `server`, `pi`, `router`, `service`, `central` |
| `monitor_hostname` | per host; **never rename**, it forks Prometheus history |

## Vault

`group_vars/all/vault.yml`, ansible-vault encrypted and committed.

| Key | Used by |
|---|---|
| `vault_collector_basic_auth_password` | every collector, nginx htpasswd |
| `vault_grafana_admin_password` | `grafana_alerting` readback |
| `vault_matrix_homeserver_url` | relay |
| `vault_matrix_bot_mxid` | relay |
| `vault_matrix_bot_password` | relay |
| `vault_matrix_webhook_api_key` | Grafana contact point, `$__env{}` |
| `vault_matrix_alert_room_id` | internal room ID, **not** the alias |

```bash
cd ansible                                              # from the repository root
ansible-vault edit inventory/group_vars/all/vault.yml
```

## Per-group overrides in this fleet

=== "`pi`"

    ```yaml
    uu_package_blacklist:            # the only blacklist anywhere
      - "raspberrypi-kernel"
      - "raspberrypi-bootloader"
      - "linux-image-rpi-.*"
    update_metrics_randomized_delay: 600   # SD-card IO
    ```

=== "`metal`"

    ```yaml
    smart_metrics_enabled: true      # the only host with real disks
    pve_guest_metrics_enabled: true  # the only host with guests to name
    alloy_systemd_unit_exclude: '...(lxc|qemu|pve-container)@.+'   # cardinality
    alloy_fs_mount_points_exclude: '...|etc/pve|rpool...'          # always-full FUSE
    alloy_journal_max_age: 1h        # a hypervisor journal is enormous
    alloy_enable_docker: false       # PVE uses its own tooling, not Docker
    uu_remove_unused_kernels: false
    ```

    Platform-level: true because it is a real machine running PVE, not because
    of anything installed on it. Note this group is **not** in `autoupdate`.

=== "`tailscale-router` (host_vars)"

    ```yaml
    alloy_systemd_unit_exclude: '...(lxc|qemu|pve-container)@.+'   # cardinality
    alloy_fs_mount_points_exclude: '...|etc/pve|rpool...'          # always-full FUSE
    alloy_journal_max_age: 1h
    alloy_enable_docker: false
    uu_remove_unused_kernels: false
    uu_clean_interval_days: 1        # 2.0 GB root
    journald_system_max_use: 64M
    ```

    These are [host-specific overrides](../fleet/index.md#inventory-layout).
    The block looks identical to `metal` above and is deliberately **not** factored
    out: this host is an LXC that merely reports a `-pve` kernel and has no
    `/etc/pve`, so changing one must not silently change the other.

=== "`central`"

    ```yaml
    prometheus_remote_write_url: http://127.0.0.1:9090/api/v1/write
    loki_write_url: http://127.0.0.1:3100/loki/api/v1/push
    collector_basic_auth_required: false     # nginx owns auth
    uu_remove_unused_kernels: false
    ```

    Loopback, so the central node's own telemetry never depends on the WAN
    being up, which is exactly when you most need it.

## Wazuh enrollment and log inputs

| Variable | Default | Purpose |
|---|---|---|
| `wazuh_manager_address` | empty | Address agents use for the manager |
| `wazuh_manager_inventory_host` | first `wazuh_manager` member | Ansible delegate for group verification |
| `wazuh_manager_ca_src` | empty | Trusted CA PEM on the controller |
| `wazuh_manager_ca_path` | `/var/ossec/etc/manager-ca.pem` | Installed agent CA path |
| `vault_wazuh_enrollment_password` | required secret | Manager enrollment password, in Ansible Vault |
| `wazuh_enrollment_password_path` | `/var/ossec/etc/enrollment.pass` | Protected agent password file |
| `wazuh_agent_group` | `homelab` | Must exist on the manager before rollout |
| `wazuh_log_sources` | journald | List of `location` / `log_format` mappings |

See [Wazuh rollout prerequisites](playbooks.md#wazuh-agents).
