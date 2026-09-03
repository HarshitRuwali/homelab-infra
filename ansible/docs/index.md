# Fleet Automation

Ansible control plane for every machine in the homelab. It owns the collector
config, the exporters that report pending updates and disk health, the package
patching policy, the container update timers, host onboarding, and the
provisioned Grafana dashboards and alert rules.

## What it does

<div class="grid cards" markdown>

- :material-server-network: **Configures every host**

    One playbook installs and configures the Grafana Alloy collector, the
    update-metrics exporter, and SMART and GPU telemetry where the hardware
    supports it. Capability is autodetected off the host.

- :material-package-down: **Patches on a schedule**

    `unattended-upgrades` applies every origin nightly at 03:00 and **never
    reboots**. Container images update from their Compose files at 04:00, both
    with jitter.

- :material-account-check: **Onboards a host in one run**

    From "it exists in the inventory" to "its metrics and logs are visible in
    Grafana", with the verification built into the same playbook.

- :material-file-lock: **Keeps the estate map out of git**

    The inventory is the one file that describes your real addressing, so it
    is gitignored. The vault is committed, but encrypted.

</div>

## Start here

!!! tip "Never used Ansible?"
    **[Getting started](getting-started/index.md)** assumes no prior knowledge
    and explains every concept using examples from this repository:
    [Ansible concepts](getting-started/ansible-basics.md) →
    [Your first run](getting-started/first-run.md) →
    [Reading the output](getting-started/reading-output.md).

    There is also a [Glossary](getting-started/glossary.md) covering the
    monitoring and systemd terms, not just the Ansible ones.

| If you want to… | Go to |
|---|---|
| Learn Ansible from scratch | [Getting started](getting-started/index.md) |
| Look up an unfamiliar word | [Glossary](getting-started/glossary.md) |
| Set up the controller | [Controller setup](fleet/setup.md) |
| Add a host | [Onboarding a new host](fleet/onboarding.md) |
| Roll out to a fleet from scratch | [Rollout order](fleet/rollout.md) |
| Know what runs at what time | [What runs when](fleet/schedules.md) |
| Understand the patching policy | [Package patching](fleet/patching.md) |
| Auto-update Docker containers | [Container updates](fleet/container-updates.md) |
| Check it actually worked | [Verification](fleet/verification.md) |
| Fix something that broke | [Troubleshooting](fleet/troubleshooting.md) |
| Look up a playbook or variable | [Reference](reference/index.md) |
| Build the docs | [Building the docs](reference/tooling.md) |

## The one-command version

```bash
cd ansible                 # from the repository root
ansible-playbook playbooks/site.yml
```

Safe to re-run. A clean fleet reports `changed=0` on every host.

!!! info "Where these commands run from"
    Everything on this site runs from `ansible/`, at the repository root. And
    `cd` you must: `ansible.cfg` resolves `inventory` and `roles_path` relative
    to the working directory, and Ansible reads a config file **only** from the
    directory it was invoked in. Run `ansible-playbook ansible/playbooks/site.yml`
    from the repository root and it loads no inventory, no roles and no vault
    password, then fails in a way that looks like a permissions problem.

!!! warning "This configures, it does not upgrade"
    `site.yml` installs and configures the machinery that applies updates on a
    schedule. It does not itself install packages or pull images. To force
    either one now, see [Forcing an update](fleet/schedules.md#forcing-an-update-now).
    Both restart services and deserve to be deliberate.

## What this configures, and where it lands

This stack is the control plane. What it deploys reports into the **monitoring
stack**, which is documented separately:

| This site | The monitoring site |
|---|---|
| deploying the Alloy collector | [what Alloy collects](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/collectors/) |
| provisioning alert rules | [the rule catalogue](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/alerting/) |
| provisioning dashboards | [what each panel shows](https://harshitruwali.github.io/homelab-infra/monitoring/monitoring/dashboards/) |
| proving a host reports | [why a dead host is not `up == 0`](https://harshitruwali.github.io/homelab-infra/monitoring/architecture/push-model/) |

## Reading these docs offline

```bash
cd ansible                         # from the repository root
python3 -m venv .venv
.venv/bin/pip install -r docs/requirements.txt
.venv/bin/mkdocs serve             # live preview on http://127.0.0.1:8000
.venv/bin/mkdocs build             # render the static site into ./site
```

See [Building the docs](reference/tooling.md).

## Design commitments

These are the decisions everything else follows from. Each one is load-bearing
and each one has bitten this fleet at least once.

**Machines never reboot themselves.** Not on any host, under any policy.
Kernel updates therefore accumulate until a human acts, which is why
`fleet-reboot-required-too-long` exists to nag at seven days. Without that
alert, "never auto-reboot" is just a way to silently run unpatched kernels.

**Nothing is held back from patching** except the Raspberry Pi kernel and
bootloader. Those are unversioned packages that overwrite `/boot/firmware` and
`/lib/modules/$(uname -r)` in place, so applying them without the reboot this
policy forbids leaves a running kernel with no modules on disk.

**Patching is decided by inventory group membership only**, never by a
conditional inside a role. A conditional inside a role is one typo away from
auto-upgrading a hypervisor.

**Capability is autodetected, policy is declared.** Whether a host *can* do
something, a Docker socket or an NVIDIA GPU, is read off the host. Whether it
*should* is read off the inventory. Conflating the two is how a capability gate
quietly becomes a policy decision nobody made.

**Alerting is provisioned from files.** Rules edited in the UI are not the
source of truth, and rules deleted in the UI do not come back on their own.

**Config is owned by Ansible, not by the install scripts.**
`monitoring/scripts/lxc-install.sh` is a bootstrap path, guarded by a marker
file so it can never revert what Ansible manages.
