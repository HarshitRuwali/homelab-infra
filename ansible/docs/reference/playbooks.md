# Playbooks and roles

A reference list. For what a playbook or a role *is*, see
[Ansible concepts](../getting-started/ansible-basics.md).

## Playbooks

| Playbook | Targets | Restarts | Use when |
|---|---|---|---|
| `site.yml` | all | services, if a role changed | everyday; runs everything in order |
| `preflight.yml` | `monitored` | **nothing** | before a first rollout, or to debug access |
| `onboard.yml` | one host, `--limit` required | `alloy` | adding a new host; installs the collector and proves data landed centrally |
| `collectors.yml` | `monitored` | `alloy`, `systemd-journald` | Alloy config, container metrics, journal caps |
| `update-metrics.yml` | `monitored` | nothing meaningful | the apt/reboot exporter, plus SMART and Proxmox guest names where `group_vars/metal` enables them |
| `dashboards.yml` | `central` | only if the provider config changed | Grafana dashboards; prunes ones deleted from git |
| `central-alerting.yml` | `central` | **`grafana-server`** | alert rules, Matrix relay |
| `unattended-upgrades.yml` | `autoupdate` | nothing | patching policy |
| `docker-updates.yml` | `autoupdate` | containers, only with `run_now` | container update schedule |
| `rotate-collector-password.yml` | `central` | `nginx`, central `alloy` | rotating collector auth |
| `desk-appliance.yml` | `desk_appliances` | `docker` (started on first install, reloaded for `live-restore`); udev rules reloaded; `systemd-logind` told to re-read its config; `btusb` reloaded when its DKMS build changes (Bluetooth connections drop and return); never a running session | an always-on X11 desk appliance; not part of `site.yml` |
| `tailscale-node.yml` | `tailscale_nodes` | nothing: `tailscaled` is never restarted by this role unless `tailscale_node_manage_upgrades` is true | a host on the tailnet under a tag, with services published by `tailscale serve`; not part of `site.yml` |
| `image-registry.yml` | `image_registries` | the registry container, only when its configuration changed; during a garbage collection | the private container image registry; not part of `site.yml` |
| `force-updates.yml` | `monitored`, except `desk_appliances` | **services, via apt** | installing pending updates NOW; never reboots |

### Common invocations

```bash
cd ansible                                                 # from the repository root

ansible-playbook playbooks/site.yml                        # everything
ansible-playbook playbooks/site.yml --limit rpi5           # one host
ansible-playbook playbooks/site.yml --limit pi             # one group
ansible-playbook playbooks/site.yml --limit 'lan_guests,!matrix'
ansible-playbook playbooks/site.yml --check --diff         # dry run
ansible-playbook playbooks/site.yml -e lan_use_jump_host=false   # from the LAN
```

!!! danger "`central-alerting.yml` restarts Grafana"
    Grafana reads provisioning **only at startup**, so the restart is the
    point. There is a short gap in evaluation and dashboards.

## Roles

### `alloy_collector`

Installs Alloy from the Grafana APT repo and owns `/etc/alloy/config.alloy`
on every native install.

| Does | Note |
|---|---|
| Adds the Grafana APT repo and installs `alloy` | `state: present`, never `latest` |
| Grants `alloy` the groups it needs | `systemd-journal`, `adm`, `docker` |
| Runs Alloy as root **on Docker hosts** | drop-in, for [cAdvisor](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/container-metrics/) |
| Templates the config | `validate: alloy fmt %s`: a bad config never lands |
| Writes `/etc/alloy/.ansible-managed` | stops `lxc-install.sh` reverting it |
| Caps the journal where set | `journald_system_max_use` |
| Waits for `/-/ready` | proof of life, not just "started" |

### `update_metrics`

The apt and reboot textfile exporter, plus its timer.

Installs `needrestart` in **non-interactive list mode**
(`$nrconf{restart} = 'l'`). Its default APT hook prompts during upgrades,
which would hang an unattended run indefinitely.

The service carries `[Install] WantedBy=multi-user.target` so a reboot
refreshes metrics immediately rather than waiting out the timer jitter.

### `unattended_upgrades`

Applied by **group membership only**. Ends with a validation gate:
`unattended-upgrade --dry-run --debug`, asserting no `Traceback` and no
blacklisted package appears.

### `docker_updates`

Compose-based container updates. Checks for a Docker socket (capability gate)
and for Compose v2, then installs the script, unit and timer.

The `docker_update_run_now` variable triggers a supervised run and asserts the
result.

### `matrix_webhook`

The Grafana-to-Matrix relay: a system user, a venv, a systemd unit and a smoke
test. Refuses to deploy with placeholder credentials.

### `desk_appliance`

Baseline for an always-on desk appliance, applied by membership of
`desk_appliances` only: LightDM autologin into a named X11 session, no
blanking or suspend, PipeWire, a serial udev symlink, a DKMS-patched `btusb`
for a Bluetooth adapter the kernel does not list yet, Docker, and security
updates with an automatic reboot window. Tagged per concern: `desktop`,
`power`, `keyring` (opt-in), `audio`, `serial`, `bluetooth` (opt-in),
`docker`, `updates`.

!!! warning "The one host class that reboots itself"
    It sets `Automatic-Reboot "true"`, so it fails early on a host in
    `autoupdate` or `no_autoupdate`, whose role asserts the opposite. On Kali
    Rolling the update layer is off unless `desk_appliance_updates_enabled` is
    set.

Runs on Ubuntu 26.04 and Kali Rolling; any other distribution fails the play
at the start.

Variables and behaviour are documented in the role itself:
`roles/desk_appliance/README.md`.

### `tailscale_node`

Applied by membership of `tailscale_nodes` only: Tailscale from its own apt
repository (key pinned by checksum), `tailscaled` enabled, a join under a tag
with an auth key from the vault (only while the node is not on the tailnet),
and `tailscale serve --bg --https=...` for each entry in
`tailscale_node_serve`. Tagged per concern: `install`, `join`, `serve`. Never
enables Funnel, and fails if any Funnel is on.

!!! danger "Never put the tailnet router in `tailscale_nodes`"
    The exit node and subnet router carries the estate's traffic, and
    restarting `tailscaled` there drops it. The join step refuses a node that
    already routes, but group membership is the control.

Variables, the steps only the owner can do (tags, auth keys, LXC TUN
passthrough) and behaviour are documented in the role itself:
`roles/tailscale_node/README.md`.

### `image_registry`

Applied by membership of `image_registries` only: Docker CE if the host has
none, a Compose project running `registry:3` on `127.0.0.1`, one htpasswd
credential from the vault, and a weekly garbage collection timer that stops the
registry for its run. Tagged per concern: `docker`, `config`, `gc`, `service`.
Reached from the tailnet through `tailscale_node` on the same host, which
supplies the HTTPS listener and certificate.

Variables and behaviour are documented in the role itself:
`roles/image_registry/README.md`.

### `grafana_alerting`

Copies the committed alerting files, **templates** `rules-availability.yaml`
from the inventory, injects secrets into Grafana's `EnvironmentFile`,
restarts Grafana, then asserts one known uid per rules file.

!!! info "Why one assert per file"
    Grafana aborts alerting provisioning for **all** files when any single one
    fails to parse, without failing startup. Probing one uid per file turns a
    silent total failure into a red play.

## Ad-hoc commands

```bash
# what is the fleet made of
ansible-inventory --graph
ansible-inventory --host rpi5

# reachability
ansible monitored -m ping

# force a metrics refresh
ansible monitored -m systemd -a 'name=fleet-update-metrics.service state=started' --become

# check a timer everywhere
ansible monitored -m shell -a 'systemctl list-timers <unit> --no-pager' --become

# assert the never-reboot policy
ansible autoupdate -m shell \
  -a 'apt-config dump | grep "Unattended-Upgrade::Automatic-Reboot "' --become
```

!!! bug "Jinja eats Go template braces"
    `docker ps --format '{{.Names}}'` fails in `-m shell` because Ansible
    templates the argument first. Put the command in a file and use
    `-m script` instead.

## Wazuh agents

`wazuh-agents.yml` targets `wazuh_agents`. Before running it:

1. Configure the manager with `security/install/configure-sec-wazuh.sh`; its
   `WAZUH_AGENT_GROUP` must match `wazuh_agent_group` (both default to `homelab`).
2. Add that manager to the `wazuh_manager` inventory group, or set
   `wazuh_manager_inventory_host` to an inventory host reachable by Ansible.
   The role verifies the group directory on this host before installing agents.
3. Set `wazuh_manager_address` and `wazuh_manager_ca_src` in private inventory.
   The latter is a controller-side PEM file from your trusted CA distribution.
4. Set `vault_wazuh_enrollment_password` in Ansible Vault to the manager's
   enrollment password (at least 20 characters, no whitespace).

The role installs CA/password files as root:wazuh `0640` without logging the
password, enables server verification during enrollment, and configures journald
collection for SSH, sudo and system logs. Add application file inputs through
`wazuh_log_sources` (`location` and `log_format`). This role owns the complete
`ossec.conf`; express local log inputs in that variable rather than editing the
managed file. Manager-shared configuration should be reviewed separately.

After rollout, check `/var/ossec/bin/agent_control -l` on the manager and verify
a known SSH or sudo event in Wazuh. A running agent alone does not prove either
enrollment or log collection. Existing agents may continue using their existing
client keys; the new credentials secure future enrollment as well.

## Collector password handling

The rotation play supplies the new password to `htpasswd -iB` over stdin. It is
hidden from Ansible output and never placed in process arguments. Rotation still
changes the central endpoint first; rerun `collectors.yml` for the fleet promptly
in the same maintenance window so remote collectors stop receiving 401 responses.
