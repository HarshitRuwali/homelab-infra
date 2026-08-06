# Homelab Monitoring Stack

Central Grafana, Prometheus and Loki for a Proxmox and Tailscale homelab, with
Ansible-managed collectors on every host, automated OS and container updates,
and alerting into a self-hosted Matrix room.

## What it does

<div class="grid cards" markdown>

- :material-chart-line: **Collects**

    Grafana Alloy on every host ships host metrics, systemd journal logs,
    container metrics and container logs to one place.

- :material-package-down: **Patches**

    `unattended-upgrades` applies every origin nightly and **never reboots**.
    Container images update from their Compose files on a separate schedule.

- :material-bell-alert: **Alerts**

    Grafana unified alerting posts to Matrix through a local relay. Rules are
    provisioned from files, not clicked into the UI.

- :material-eye-check: **Proves it**

    Every automated action writes a metric, so "it updates itself" cannot
    quietly become "it broke itself three weeks ago".

</div>

## Start here

!!! tip "Never used Ansible?"
    Start with **[Getting started](getting-started/index.md)**. It assumes no
    prior knowledge and explains every concept using examples from this repo:
    [Ansible concepts](getting-started/ansible-basics.md) →
    [Your first run](getting-started/first-run.md) →
    [Reading the output](getting-started/reading-output.md).

    There is also a [Glossary](getting-started/glossary.md) covering the
    monitoring and systemd terms, not just the Ansible ones.

| If you want to… | Go to |
|---|---|
| Learn Ansible from scratch | [Getting started](getting-started/index.md) |
| Look up an unfamiliar word | [Glossary](getting-started/glossary.md) |
| Understand how telemetry moves | [Architecture](architecture/index.md) |
| Know why a dead host is not `up == 0` | [The push model](architecture/push-model.md) |
| Set up the Ansible controller | [Controller setup](fleet/setup.md) |
| Know what runs at what time | [What runs when](fleet/schedules.md) |
| Add a host | [Rollout order](fleet/rollout.md) |
| Understand the patching policy | [Package patching](fleet/patching.md) |
| Auto-update Docker containers | [Container updates](fleet/container-updates.md) |
| Fix something that broke | [Troubleshooting](fleet/troubleshooting.md) |
| Look up a metric | [Metrics catalogue](reference/metrics.md) |
| Build the docs | [Building the docs](reference/tooling.md) |

## The one-command version

```bash
cd ansible
ansible-playbook playbooks/site.yml
```

Safe to re-run. A clean fleet reports `changed=0` on every host.

!!! warning "This configures, it does not upgrade"
    `site.yml` installs and configures the machinery that applies updates on a
    schedule. It does not itself install packages or pull images. To force
    either one now, see [Forcing an update](fleet/schedules.md#forcing-an-update-now).
    Both restart services and deserve to be deliberate.

## Reading these docs offline

```bash
python3 -m venv .venv
.venv/bin/pip install -r docs/requirements.txt
.venv/bin/mkdocs serve    # live preview on http://127.0.0.1:8000
.venv/bin/mkdocs build    # render the static site into ./site
```

`.github/workflows/deploy-docs.yml` publishes to GitHub Pages on push to
`master`, and builds without publishing on pull requests. It is the only
workflow here, it is docs-only, and it holds no secrets. See
[Building the docs](reference/tooling.md).

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

**Alerting is provisioned from files.** Rules edited in the UI are not the
source of truth, and rules deleted in the UI do not come back on their own.
See [Alerting](monitoring/alerting.md#provisioned-rules-are-not-ui-rules).

**Config is owned by Ansible, not by the install scripts.**
`scripts/lxc-install.sh` is a bootstrap path, guarded by a marker file so it
can never revert what Ansible manages.
