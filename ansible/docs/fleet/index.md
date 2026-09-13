# Fleet management

Ansible is the primary way to manage collectors, patching and container
updates across the fleet. `scripts/lxc-install.sh collector` is a legacy
bootstrap path.

!!! tip "New to Ansible?"
    This page assumes you know what an inventory, a playbook and a role are.
    If not, read [Getting started](../getting-started/index.md) first; it
    takes about fifteen minutes and uses examples from this repo.

```bash
cd ansible                                           # from the repository root
ansible-playbook playbooks/site.yml                  # everything, in order
ansible-playbook playbooks/site.yml --limit rpi5     # one host
ansible-playbook playbooks/site.yml --limit pi       # one group
ansible-playbook playbooks/site.yml --check --diff   # dry run
```

Safe to re-run. A clean fleet reports `changed=0` everywhere.

## What this manages

`monitored` is bifurcated by **platform**, then by **distro family**. Every
monitored host sits in exactly one leaf.

| Group | Hosts | Collector | Auto-patched | Container updates |
|---|---|---|---|---|
| `lxc` → `lxc_debian` | tailscale-router, plex, memory, monitor-lxc | yes | yes, `central` see below | memory only |
| `vm` → `vm_debian` | ubuntu-dev, ubuntu-ai, cloud-services, matrix | yes | yes | yes |
| `pi` → `pi_debian` | rpi5, rpi4b | yes | yes | rpi5 only |
| `metal` → `metal_debian` | t7920 (the Proxmox host) | yes | **no**, deliberately | no |

Today every leaf is `_debian`. That is the point of the split rather than an
argument against it: a future `vm_fedora` slots in beside its sibling without
disturbing anything, and `--limit vm` keeps meaning "every VM". Variables
divide along the same seam, and deeper groups win:

- **platform** (`lxc` / `vm` / `pi` / `metal`): true because of the hardware
  or the virtualisation. SD-card IO on the Pis. Shared kernel on the
  containers. Real disks, so real SMART, on `metal`.
- **distro** (`*_debian`): true because of the package manager. Anything
  naming an apt package belongs here, because that is exactly what a
  `_fedora` sibling would not share.

!!! warning "`_debian` means the Debian *family*"
    Four members of `vm_debian` actually run Ubuntu 26.04. They are grouped
    with Debian because the axis every role in this repo cares about is
    apt-vs-dnf, and Ubuntu is on the apt side of it. Reserve a new leaf for
    distros that would genuinely break these roles.

!!! danger "Read the platform off the host, never off its name"
    Two hosts are not what they look like. `tailscale-router` is an **LXC**,
    not a VM: it reports a `-pve` kernel only because a container shares the
    hypervisor's kernel, and it has no `/etc/pve`. `matrix` is a **VM**, not
    a container, despite sitting on the LAN beside the LXC guests, which is
    why it is the one guest with a real `sudo` setup. Check before you move
    one: `ansible <host> -m setup -a 'filter=ansible_virtualization*'`.

### Overlay groups

These cut across the platform tree because they answer different questions. A
host is in as many as apply.

| Group | Hosts | Answers |
|---|---|---|
| `lan_guests` | plex, memory, matrix | **how** it is reached: `ProxyJump` via `lan_jump_host` |
| `central` | monitor-lxc | **what it runs**: the monitoring stack itself |
| `autoupdate` | every host except t7920 | **whether** it is auto-patched |
| `no_autoupdate` | t7920 | monitored, package lists refreshed, but **never** patched automatically |

Do not fold an overlay into the platform tree. `lxc_debian` holds both
`tailscale-router` (reached directly over the tailnet) and `monitor-lxc`
(reached through the jump host), so "is a container" and "needs a jump host"
are genuinely independent facts. `lan_guests` are Proxmox guests that are
**not** Tailscale members; they still push telemetry to the same public ingest
endpoint as everything else.

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

    To bring it back: install a key, re-add it under `lxc_debian`,
    `lan_guests` **and** `autoupdate`, and give it a mountpoint exclusion in
    `rules-resources.yaml` **before** you do, or you will re-create the disk
    noise.

??? note "t7920, the Proxmox host: monitored, never auto-patched"
    It **is** monitored, as the sole member of the `metal` platform group, and
    it is the only host that can report SMART disk health. What is deliberately
    out of scope is **patching**: it is kept out of `autoupdate`, because an
    unattended upgrade there restarts pveproxy and pvedaemon underneath every
    running guest. Updates on this box are a deliberate, supervised act.

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
    pi/main.yml          SD-card IO tuning (platform)
    pi_debian/main.yml   RPi kernel/bootloader blacklist (distro)
    metal/main.yml       SMART, PVE cardinality and mount exclusions
    central/main.yml     loopback ingest, stack blacklist
    lan_guests/main.yml  ProxyJump defaults
  host_vars/
    monitor-lxc.yml         pins MONITOR_HOSTNAME
    tailscale-router.yml    PVE-safe overrides, small-rootfs caps
```

`lxc`, `lxc_debian`, `vm`, `vm_debian` and `metal_debian` have no group
variable files. Their hosts inherit the applicable parent-group settings.

!!! note "Host-specific overrides for `tailscale-router`"
    Its `host_vars` contain settings for its 2.0 GB rootfs and exit-node role.
    Keeping them scoped to this host avoids applying them to plex, memory and
    monitor-lxc.

!!! warning "The repo is public"
    All real domains, IPs and usernames live only in `hosts.local.yml`, which
    the **repository-root** `.gitignore` excludes via `ansible/**/*.local.yml`.
    That pattern is spelled out rather than relying on a bare `*.local`, which
    matches only names *ending* in `.local` and would **not** catch
    `secrets.local.yml`. `monitoring/.gitignore` does have a bare `*.local`,
    but it governs this directory only, never `ansible/`.

## Variable precedence gotcha

`group_vars/<group>/main.yml` beats vars set in the inventory file itself.
A placeholder left in `group_vars/lan_guests` silently overrode the real
`lan_jump_host` from `hosts.local.yml` and cost an afternoon. If a value is
site-specific, put it in `hosts.local.yml` and keep it out of `group_vars`.
