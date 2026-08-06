# Fleet management

Ansible is the primary way to manage collectors, patching and container
updates across the fleet. `scripts/lxc-install.sh collector` is a legacy
bootstrap path.

!!! tip "New to Ansible?"
    This page assumes you know what an inventory, a playbook and a role are.
    If not, read [Getting started](../getting-started/index.md) first; it
    takes about fifteen minutes and uses examples from this repo.

```bash
cd ansible
ansible-playbook playbooks/site.yml                  # everything, in order
ansible-playbook playbooks/site.yml --limit rpi5     # one host
ansible-playbook playbooks/site.yml --limit pi       # one group
ansible-playbook playbooks/site.yml --check --diff   # dry run
```

Safe to re-run. A clean fleet reports `changed=0` everywhere.

## What this manages

| Group | Hosts | Collector | Auto-patched | Container updates |
|---|---|---|---|---|
| `tailnet_fleet` | ubuntu-dev, ubuntu-ai, cloud-services | yes | yes | yes |
| `pi` | rpi5, rpi4b | yes | yes | rpi5 only |
| `proxmox` | tailscale-router | yes | yes | no Docker |
| `lan_guests` | plex, memory, matrix | yes | yes | memory, matrix |
| `central` | monitor-lxc | yes | yes, see below | no Docker |

`lan_guests` are Proxmox guests that are **not** Tailscale members. They are
reached with `ProxyJump` through `lan_jump_host` and still push telemetry to
the same public ingest endpoint as everything else.

## The one rule that decides patching

!!! danger "Group membership, never a role conditional"
    Whether a host is auto-patched is decided **solely** by its membership of
    the `autoupdate` group in the inventory. The `unattended_upgrades` role
    has no internal host conditional, because a conditional inside a role is
    one typo away from auto-upgrading a hypervisor.

    The `docker_updates` role does check for a Docker socket, but that is a
    *capability* gate, not a policy gate: it decides whether the role is even
    applicable, not whether the host is allowed to update.

## The central node is a special case

On `monitor-lxc`, OS and security patches apply automatically, but the
monitoring stack itself is upgraded deliberately with
`scripts/lxc-update.sh central`, which re-renders the configs in the same run.

Otherwise a 03:00 unattended run could:

- restart Grafana and Loki, blinding the fleet at exactly the wrong moment
- reload nginx, briefly 502-ing every collector's ingest path
- upgrade a package whose config file this repo owns, without re-rendering it

`playbooks/update-metrics.yml` runs there too, so anything held back still
shows up in `apt_upgrades_pending`. **The blacklist suppresses installs, not
visibility.**

## Deliberately out of scope

Documented so the omission is a decision, not a gap.

??? note "qbittorrent (LXC 103)"
    Dropped for two independent reasons. Its sshd rejects every key we hold,
    so it was never onboarded and has no collector. And its data filesystem is
    *expected* to run at ~100% full, which made the disk alert permanently
    noisy. While it was still in `monitored` it also sat in the host-down
    or-chain, so it fired `Host Down` every minute forever.

    To bring it back: install a key, re-add it under `lan_guests` **and**
    `autoupdate`, and give it a mountpoint exclusion in `rules-resources.yaml`
    **before** you do, or you will re-create the disk noise.

??? note "t7920, the Proxmox host itself"
    All of its guests are monitored, but the hypervisor is not. Accept the
    consequence: host-level CPU, RAM, disk and ZFS pressure on the machine
    everything else runs on is invisible here. If a guest looks starved, there
    is no fleet metric explaining why. Watch it in the Proxmox UI.

??? note "OPNsense (VM 102, FreeBSD)"
    Monitor via the `os-node_exporter` plugin if you want it, scraped rather
    than pushed. Firmware updates stay manual: an unattended firewall upgrade
    takes out remote access to everything else.

??? note "Others"
    - **arr-stack (LXC 104)**: stopped in Proxmox.
    - **win11 (VM 108)**: would need `windows_exporter`, a separate path.
    - **skullsiants, expl01t**: pentest tooling is deliberately version-pinned.
    - **Macs, iPhone**: excluded by request.

## Inventory layout

```text
ansible/inventory/
  hosts.local.yml        real site data, GITIGNORED
  hosts.example.yml      committed placeholders
  group_vars/
    all/main.yml         shared paths and endpoints
    all/vault.yml        ansible-vault, committed encrypted
    pi/main.yml          RPi kernel/bootloader blacklist
    proxmox/main.yml     PVE-safe overrides, small-rootfs caps
    central/main.yml     loopback ingest, stack blacklist
    lan_guests/main.yml  ProxyJump defaults
  host_vars/
    monitor-lxc.yml      pins MONITOR_HOSTNAME
```

!!! warning "The repo is public"
    All real domains, IPs and usernames live only in `hosts.local.yml`, which
    `.gitignore` excludes via `ansible/**/*.local.yml`. Note the pre-existing
    `*.local` pattern matches only names *ending* in `.local`, which would
    **not** catch `secrets.local.yml`.

## Variable precedence gotcha

`group_vars/<group>/main.yml` beats vars set in the inventory file itself.
A placeholder left in `group_vars/lan_guests` silently overrode the real
`lan_jump_host` from `hosts.local.yml` and cost an afternoon. If a value is
site-specific, put it in `hosts.local.yml` and keep it out of `group_vars`.
