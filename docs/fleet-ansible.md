# Fleet Rollout with Ansible

Ansible is the primary way to manage collectors and patching across the fleet.
`scripts/lxc-install.sh collector` is now a legacy bootstrap path.

## What this manages

| Group | Hosts | Collector | Auto-patched |
|---|---|---|---|
| `tailnet_fleet` | ubuntu-dev, ubuntu-ai, cloud-services | yes | yes |
| `pi` | rpi5, rpi4b | yes | yes |
| `proxmox` | tailscale-router | yes | yes (PVE-safe blacklist) |
| `lan_guests` | qbittorrent, arr-stack, plex, memory, matrix | yes | yes |
| `central` | monitor-lxc (Proxmox 112) | yes | yes, **except the stack itself** |

On the central node, `grafana`, `prometheus`, `loki`, `alloy` and `nginx` are
blacklisted from unattended-upgrades (`inventory/group_vars/central/main.yml`).
OS and security patches apply automatically; the monitoring stack is upgraded
deliberately with `scripts/lxc-update.sh central`, which re-renders the configs
in the same run. Otherwise a 03:00 unattended run could restart Grafana and
Loki, blinding the fleet, and reload nginx, briefly 502-ing every collector's
ingest path.

`playbooks/update-metrics.yml` runs there too, so anything the blacklist is
holding back still shows up in `apt_upgrades_pending`. The blacklist suppresses
installs, not visibility.

Deliberately out of scope:

- **t7920, the Proxmox host itself.** All of its guests are monitored, but the
  hypervisor is not. Accept the consequence: host-level CPU, RAM, disk and ZFS
  pressure on the machine everything else runs on is invisible here. If a guest
  looks starved, there is no fleet metric explaining why. Watch it in the
  Proxmox UI instead.
- **OPNsense** (VM 102, FreeBSD firewall). Monitor via the `os-node_exporter`
  plugin if you want it; firmware updates stay manual.
- win11 (needs `windows_exporter`), skullsiants and expl01t (version-pinned
  pentest tooling), the Macs, the iPhone.

Patching is controlled **only** by membership of the `autoupdate` group. The
`unattended_upgrades` role has no internal host conditional, because a
conditional inside a role is one typo away from auto-upgrading a hypervisor.

## One-time setup

You can drive this from macOS or from a Linux box. `ubuntu-dev` is the
recommended Linux controller: it is a VM on the Proxmox host, so it sits on
the LAN with every guest.

### macOS

```bash
brew install ansible
```

### Debian / Ubuntu (ubuntu-dev, cloud-services, a Pi)

Distro packages lag badly, so install into a venv rather than via apt:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git
python3 -m venv ~/.venvs/ansible
~/.venvs/ansible/bin/pip install --upgrade pip ansible
echo 'export PATH="$HOME/.venvs/ansible/bin:$PATH"' >> ~/.bashrc
exec bash
ansible --version    # expect core 2.21+
```

### Both platforms

```bash
git clone git@github.com:HarshitRuwali/monitorting-stack.git
cd monitorting-stack/ansible
cp inventory/hosts.example.yml inventory/hosts.local.yml
```

**`hosts.local.yml` is what `ansible.cfg` actually loads, and it is
gitignored.** This repository is public, so real addresses, usernames and the
monitoring domain must never be committed: an inventory is a complete map of
the estate (ingest endpoint, internal addressing, valid usernames, and which
box to hit to blind the monitoring). Fill it in from the example, including:

```yaml
all:
  vars:
    monitoring_domain: monitor.your-domain.tld   # builds the ingest URLs
    lan_jump_host: <host on both networks>       # for lan_guests
```

Because it is not in git, `hosts.local.yml` has to be copied to each
controller you run from, alongside the vault password and SSH key.

The vault password must exist at `~/.config/ansible/monitorting-vault-pass`
(mode 0600) on **whichever machine you run from**, since `ansible.cfg` points
there. It is deliberately outside the repo, so copy it across by hand:

```bash
# from the Mac, to a Linux controller
ssh xtubuntu-dev 'mkdir -p ~/.config/ansible && chmod 700 ~/.config/ansible'
scp ~/.config/ansible/monitorting-vault-pass xtubuntu-dev:~/.config/ansible/
ssh xtubuntu-dev 'chmod 600 ~/.config/ansible/monitorting-vault-pass'
```

**Back this file up.** Without it `inventory/group_vars/all/vault.yml` is
unrecoverable.

The SSH key must also be present on the controller. On a Linux controller,
either copy `~/.ssh/ssh` over or point `ansible_ssh_private_key_file` at
whatever key that box already uses.

Fill in the placeholders before Phase 6:

```bash
ansible-vault edit inventory/group_vars/all/vault.yml
```

### Which controller you use changes one thing

The non-tailnet guests (`lan_guests`) are reached by jumping through a host on
the Proxmox LAN. If the controller is *already* on that LAN, the jump is not
just unnecessary, it fails, because it would proxy through the machine running
the play.

| Controller | `lan_guests` reached via | Command |
|---|---|---|
| macOS, or any off-LAN host | ProxyJump through ubuntu-dev | default, nothing to add |
| ubuntu-dev / any host on 10.0.1.0/24 | direct | add `-e lan_use_jump_host=false` |

```bash
# running from ubuntu-dev
ansible-playbook playbooks/collectors.yml --limit lan_guests -e lan_use_jump_host=false
```

Everything else, including all the tailnet hosts, behaves identically from
either controller.

## Phase 0: discovery

Nothing here changes a target.

```bash
ansible-playbook playbooks/preflight.yml
```

Before that will work you must resolve three things:

1. **Addresses for the LAN guests.** Fill in every `ansible_host` in
   `hosts.local.yml`. Get them from the Proxmox UI or `pct config <vmid>` /
   `qm config <vmid>`.
2. **The central node's real `host` label.** This must match what is already
   in Prometheus, or the host-down alert watches a machine that never existed:
   ```bash
   ssh <monitor-lxc> 'grep MONITOR_HOSTNAME /etc/default/alloy'
   ```
   Put the answer in `inventory/host_vars/monitor-lxc.yml`.
3. **SSH and sudo on every host.** Containers often ship with
   `PermitRootLogin no`, and every role runs `become: true`, so a host without
   passwordless sudo is a no-op. `preflight.yml` reports both per host.

If your controller reaches the fleet over a VPN whose exit node is itself in
the inventory, confirm you are not routing through it, or a play that restarts
that daemon severs its own connection:

```bash
tailscale status | grep -i "exit node"
```

## Rollout order

Blast radius ascending. Do not skip the canary.

```bash
# Phase 1: central capacity, BEFORE any collector exists
scripts/lxc-update.sh central --config-only && systemctl restart loki
ansible-playbook playbooks/rotate-collector-password.yml

# Phase 3: collectors
ansible-playbook playbooks/collectors.yml --limit ubuntu-dev        # canary, verify fully
ansible-playbook playbooks/collectors.yml --limit 'ubuntu-ai,cloud-services,rpi5'
ansible-playbook playbooks/collectors.yml --limit rpi4b
ansible-playbook playbooks/collectors.yml --limit lan_guests
ansible-playbook playbooks/collectors.yml --limit tailscale-router  # risk: exit node
ansible-playbook playbooks/collectors.yml --limit monitor-lxc       # LAST

# Phase 4: update metrics everywhere
ansible-playbook playbooks/update-metrics.yml

# Phase 6: alerting (before enabling auto-updates, on purpose)
ansible-playbook playbooks/central-alerting.yml

# Phase 7: patching, staged
ansible-playbook playbooks/unattended-upgrades.yml --limit ubuntu-dev
ansible-playbook playbooks/unattended-upgrades.yml --limit ubuntu-ai
ansible-playbook playbooks/unattended-upgrades.yml --limit rpi5     # SUPERVISED
ansible-playbook playbooks/unattended-upgrades.yml --limit rpi4b    # SUPERVISED
ansible-playbook playbooks/unattended-upgrades.yml --limit 'cloud-services,lan_guests'
ansible-playbook playbooks/unattended-upgrades.yml --limit tailscale-router
```

`monitor-lxc` goes last in Phase 3 because that run plants
`/etc/alloy/.ansible-managed`, which transfers config ownership away from
`lxc-install.sh`.

The Pis carry 25 and 45 pending packages, on different Debian majors (12 and 13). That is the largest single change in
this project. Image the SD cards first and watch the first run.

## Why alerting lands before patching

So the first thing you learn about a bad upgrade is a Matrix message, not a
host that stopped answering.

## Verification

Every host reporting, from the central side (no SSH needed). Note the doubled
`loki`: nginx strips the prefix on `/prometheus/` but preserves it on `/loki/`.

```bash
PW=$(ansible-vault view inventory/group_vars/all/vault.yml | awk '/collector_basic_auth_password/{print $2}' | tr -d '"')

curl -sG -u "collector:$PW" https://monitor.example.com/prometheus/api/v1/query \
  --data-urlencode 'query=count by (host, role) (node_uname_info)' | jq -r '.data.result[].metric'

# Data age per host. This is the push-model health check: every value < 30.
curl -sG -u "collector:$PW" https://monitor.example.com/prometheus/api/v1/query \
  --data-urlencode 'query=time() - max by (host) (max_over_time(timestamp(up{job="host-unix"})[6h:1m]))'

curl -s -u "collector:$PW" \
  'https://monitor.example.com/loki/loki/api/v1/label/host/values' | jq -r '.data[]'
```

Patching actually applying. The config assertion is enforced by the role on
every run, but to check by hand:

```bash
ansible autoupdate -m shell -a 'apt-config dump | grep -i "Unattended-Upgrade::Automatic-Reboot"' --become
# MUST print: Unattended-Upgrade::Automatic-Reboot "false";

ansible autoupdate -m shell -a 'systemctl list-timers apt-daily-upgrade.timer fleet-update-metrics.timer --all --no-pager'
```

The best proof needs no SSH at all. Watch the counts fall in Grafana:

```promql
sum by (host) (apt_upgrades_pending)                    # rpi5 25 -> ~0, rpi4b 29 -> ~0
apt_upgrades_security_pending                           # 0 on every autoupdate host
fleet_unattended_upgrades_last_run_timestamp_seconds    # advances daily
```

Audit trail, free because `Unattended-Upgrade::SyslogEnable "true"` puts every
action in the journal and Alloy already ships it:

```logql
{unit="unattended-upgrades.service"} |= "Packages that will be upgraded"
```

## Testing alerts

Escalating, so each step isolates one link in the chain.

```bash
# 1. Relay only. Proves bot login + room membership.
ssh <monitor-lxc> 'curl -s -X POST "http://127.0.0.1:4785/?formatter=grafana&key=$KEY&room_id=$ROOM" \
  -H "Content-Type: application/json" -d "{\"title\":\"smoke\",\"message\":\"relay alive\"}"'

# 2. Grafana -> Alerting -> Contact points -> matrix-homelab -> Test.
#    Proves $__env{} resolution and loopback reachability.

# 3. Rule -> policy -> relay, end to end and reversible.
ssh xtrpi4b 'sudo systemd-run --unit=alert-smoke-test /bin/false'
#    ~10 min later fleet-systemd-unit-failed fires with host=rpi4b
ssh xtrpi4b 'sudo systemctl reset-failed alert-smoke-test'

# 4. The push-model down path. This is the one worth proving properly.
ssh xtrpi4b 'sudo systemctl stop alloy'
#    ~10-11 min: fleet-host-down fires WITH host="rpi4b" in the message,
#    not a bare NoData. That is the whole point of the or-chain.
ssh xtrpi4b 'sudo systemctl start alloy'
```

Use rpi4b: no Docker, nothing depends on it. **Never** run step 4 against
tailscale-router: stopping its collector is harmless, but it is the exit node
and subnet router, so it is the wrong place to practise.

## Gotchas

**Provisioned alert rules are read-only in the UI.** To quiet one temporarily
use a Silence, not an edit.

**Grafana reads provisioning only at startup.** Copying files without
restarting `grafana-server` is a silent no-op. The role handles this.

**Nothing here detects the central LXC being down**, because Grafana dies with
it. Add an external dead-man's-switch: a healthchecks.io ping from an
`OnCalendar` timer on the central box, or an Uptime Kuma elsewhere.

**`lxc-update.sh` no longer reverts the Alloy config.** `write_alloy_config()`
returns early when `/etc/alloy/.ansible-managed` exists. Delete that marker
only if you intend to hand ownership back to the shell script.

**Three copies of the Alloy config exist**, scoped deliberately:

| File | Scope |
|---|---|
| `ansible/roles/alloy_collector/templates/config.alloy.j2` | authoritative, all native installs |
| `alloy/config.alloy` | Docker collector only (sets `rootfs_path` for bind mounts) |
| `scripts/lxc-install.sh` heredoc | bootstrap only, guarded by the marker |

A native install must never set `rootfs_path`/`procfs_path`/`sysfs_path`.
