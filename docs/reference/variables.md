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
| `alloy_fs_mount_points_exclude` | see file | |
| `alloy_fs_types_exclude` | see file | |
| `alloy_systemd_unit_exclude` | see file | |
| `journald_system_max_use` | `""` | empty leaves journald's own default |
| `collector_basic_auth_required` | `true` | `false` on the central node |

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

## GPU exporter

`roles/gpu_exporter/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `gpu_exporter_version` | `1.14.0` | pinned; no apt repo exists for this exporter, so this is what stands in for `alloy_package_state: present` |
| `gpu_exporter_listen_addr` | `127.0.0.1:9835` | loopback only; Alloy scrapes it locally |
| `gpu_exporter_checksums` | see file | sha256 per arch, from the release's signed `checksums.txt`; bump alongside the version |

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
| `docker_update_health_wait_seconds` | `60` | before checking for restart loops |
| `docker_update_run_now` | unset | `-e docker_update_run_now=true` for a supervised run |

## Update metrics

`roles/update_metrics/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `update_metrics_oncalendar` | `*:0/15` | every 15 minutes |
| `update_metrics_randomized_delay` | `300` | `600` on the Pis |
| `update_metrics_use_needrestart` | `true` | installed in non-interactive mode |

## Alerting

`roles/grafana_alerting/defaults/main.yml`

| Variable | Default | Notes |
|---|---|---|
| `grafana_admin_user` | `admin` | **override this**; `lxc-install.sh` writes a real username |
| `grafana_api_url` | `http://127.0.0.1:3000` | |
| `central_host_label` | from `host_vars` | must match `MONITOR_HOSTNAME` |
| `grafana_alerting_static_files` | 7 files | `rules-availability.yaml` is templated, not listed |

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
cd ansible                                              # required
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

=== "`proxmox`"

    ```yaml
    alloy_systemd_unit_exclude: '...(lxc|qemu|pve-container)@.+'   # cardinality
    alloy_fs_mount_points_exclude: '...|etc/pve|rpool...'          # always-full FUSE
    alloy_journal_max_age: 1h
    uu_remove_unused_kernels: false
    uu_clean_interval_days: 1        # 2.0 GB root
    journald_system_max_use: 64M
    ```

=== "`central`"

    ```yaml
    prometheus_remote_write_url: http://127.0.0.1:9090/api/v1/write
    loki_write_url: http://127.0.0.1:3100/loki/api/v1/push
    collector_basic_auth_required: false     # nginx owns auth
    uu_remove_unused_kernels: false
    ```

    Loopback, so the central node's own telemetry never depends on the WAN
    being up, which is exactly when you most need it.
