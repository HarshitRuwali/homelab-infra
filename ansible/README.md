# Fleet Automation

Ansible control plane for every host in the homelab. It owns the Grafana Alloy
collector config, the textfile exporters that report pending updates and disk
health, the package patching policy, the container update timers, and the
provisioned Grafana dashboards and alert rules.

**📖 Documentation: <https://harshitruwali.github.io/homelab-infra/ansible/>**

Source under [`docs/`](docs/index.md); build it locally with
[Building the docs](docs/reference/tooling.md).

## Why it is at the repository root

It started inside `monitoring/`, and moved up when it stopped being about
monitoring alone: patching, container updates and host onboarding apply to
every machine, whatever stack it runs.

It has its own dedicated docs site, separate from the monitoring stack's. The
split follows the deployment boundary: **this site covers getting things onto
hosts and keeping them current**, while the
[monitoring site](https://harshitruwali.github.io/homelab-infra/monitoring/)
covers what the resulting telemetry means.

## Quick start

```bash
brew install ansible                            # macOS
# Debian/Ubuntu: python3 -m venv ~/.venvs/ansible
#                ~/.venvs/ansible/bin/pip install ansible

cd ansible                                      # from the repository root
cp inventory/hosts.example.yml inventory/hosts.local.yml   # then edit it
ansible-playbook playbooks/preflight.yml        # read-only, changes nothing
ansible-playbook playbooks/site.yml             # everything, idempotent
```

> [!IMPORTANT]
> **`cd ansible` first, always.** `ansible.cfg` resolves `inventory`,
> `roles_path` and `vault_password_file` relative to the working directory, and
> Ansible only reads a config file from the directory it was invoked in. Run
> `ansible-playbook ansible/playbooks/site.yml` from the repository root and it
> loads no inventory, no roles and no vault password, then fails in a way that
> looks like a permissions problem.

> [!IMPORTANT]
> `site.yml` **configures** the machinery; it does not itself install packages
> or pull images. Those apply on a schedule: **03:00** for packages, **04:00**
> for containers, both with jitter. Nothing here ever reboots a machine.

New to Ansible? [Ansible concepts](docs/getting-started/ansible-basics.md)
assumes no prior knowledge and uses examples from this repository.

## Layout

```text
ansible.cfg                    inventory, roles_path and vault password, all
                               resolved relative to THIS directory
docs/                          the MkDocs site published to GitHub Pages
docs/requirements.txt          pinned MkDocs toolchain
inventory/
  hosts.example.yml            template, committed; documents the group scheme
  hosts.local.yml              real estate map, GITIGNORED
  group_vars/all/main.yml      ingest URLs, collector defaults
  group_vars/all/vault.yml     committed, ansible-vault encrypted
  group_vars/<group>/main.yml  per-group overrides
  host_vars/<host>.yml         per-host pins
playbooks/                     entry points, see the table below
roles/                         one role per thing installed on a host
```

## Playbooks

| Playbook | What it does |
|---|---|
| `site.yml` | everything, in dependency order; safe to re-run, `changed=0` on a clean fleet |
| `preflight.yml` | read-only reachability and privilege checks; run before every rollout phase |
| `onboard.yml` | one new host, from "it is in the inventory" to "visible in Grafana" |
| `collectors.yml` | the Alloy collector on every monitored host |
| `update-metrics.yml` | apt and reboot-required textfile exporter |
| `unattended-upgrades.yml` | package patching policy |
| `docker-updates.yml` | container image update script, unit and timer |
| `force-updates.yml` | install every pending package **now**; still never reboots |
| `dashboards.yml` | the committed Grafana dashboards |
| `central-alerting.yml` | the Matrix relay and Grafana unified alerting |
| `rotate-collector-password.yml` | rotate the shared collector basic-auth password |

Full reference: [Playbooks](docs/reference/playbooks.md).

## Roles

| Role | Installs |
|---|---|
| `alloy_collector` | Grafana Alloy, the collector on every monitored host |
| `update_metrics` | pending-package and reboot-required textfile exporter |
| `smart_metrics` | SMART disk health, bare metal only |
| `gpu_exporter` | NVIDIA GPU telemetry, autodetected off the host |
| `unattended_upgrades` | apt patching policy, applied by group membership only |
| `docker_updates` | container image update timer |
| `grafana_dashboards` | the committed dashboard JSON |
| `grafana_alerting` | provisioned alert rules and contact points |
| `matrix_webhook` | the local Grafana-to-Matrix relay |

## Inventory

Two axes, kept deliberately separate. **Platform then distro family**
(`lxc` → `lxc_debian`, `vm` → `vm_debian`, `pi` → `pi_debian`,
`metal` → `metal_debian`); every monitored host sits in exactly one leaf.
**Overlay groups** cut across that tree: `monitored`, `central`, `lan_guests`,
`autoupdate`, `no_autoupdate`.

`hosts.example.yml` documents the whole scheme inline, including which level a
given variable belongs to and why. Start there.

> [!CAUTION]
> `inventory/hosts.local.yml` is gitignored and must stay that way. This
> repository is public, and an inventory is a complete map of the estate:
> ingest endpoint, internal addressing, valid usernames, and which box to hit
> to blind the monitoring. `group_vars/all/vault.yml` **is** committed, but it
> is ansible-vault encrypted; the point is encrypted-at-rest in git. The vault
> password lives at `~/.config/ansible/monitorting-vault-pass`, outside this
> repository, and must be backed up separately.

## Design commitments

- **Machines never reboot themselves**, under any policy. Kernel updates
  therefore accumulate until a human acts, which is why
  `fleet-reboot-required-too-long` nags at seven days.
- **Patching is decided by inventory group membership only**, never by a
  conditional inside a role. A conditional inside a role is one typo away from
  auto-upgrading a hypervisor.
- **Capability is autodetected, policy is declared.** Whether a host *can* do
  something (Docker socket, NVIDIA GPU) is read off the host; whether it
  *should* is read off the inventory. The two are never conflated.
- **Alerting is provisioned from files.** Rules edited in the UI are not the
  source of truth, and rules deleted in the UI do not come back on their own.
- **Config is owned by Ansible**, not by the install scripts.
  `monitoring/scripts/lxc-install.sh` is a bootstrap path, guarded by a marker
  file so it can never revert what Ansible manages.

## Read next

| Section | Start at |
|---|---|
| Never used Ansible | [Getting started](docs/getting-started/index.md) |
| Set up the controller | [Controller setup](docs/fleet/setup.md) |
| Add a host | [Onboarding](docs/fleet/onboarding.md) |
| What runs at what time | [What runs when](docs/fleet/schedules.md) |
| Something broke | [Troubleshooting](docs/fleet/troubleshooting.md) |
| Every variable | [Variables](docs/reference/variables.md) |
| What the telemetry means | [Monitoring stack](https://harshitruwali.github.io/homelab-infra/monitoring/) |
