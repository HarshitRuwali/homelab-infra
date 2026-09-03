# Homelab Monitoring Stack

Self-hosted monitoring and patch automation for a Proxmox and Tailscale
homelab. Grafana for dashboards, Prometheus for metrics, Loki for logs, Grafana
Alloy as the collector on every host, and Ansible to keep the whole fleet
configured, patched and alerting.

**📖 Documentation: <https://harshitruwali.github.io/homelab-infra/monitoring/>**

Source under [`docs/`](docs/index.md); build it locally with
[Building the docs](docs/reference/tooling.md).

## What it does

| | |
|---|---|
| **Collects** | Alloy on every host ships host metrics, systemd journal logs, container metrics and container logs to one place |
| **Patches** | `unattended-upgrades` applies every origin nightly and **never reboots**; container images update from their Compose files on a separate schedule |
| **Alerts** | Grafana unified alerting posts to Matrix through a local relay; rules are provisioned from files, not clicked into the UI |
| **Proves it** | Every automated action writes a metric, so "it updates itself" cannot quietly become "it broke itself three weeks ago" |

## Components

**Central stack**, one host:

| Service | Bind | Purpose |
|---|---|---|
| Grafana | `127.0.0.1:3000` | dashboards and unified alerting |
| Prometheus | `127.0.0.1:9090` | metrics, remote-write receiver |
| Loki | `127.0.0.1:3100` | logs |
| Alloy | `127.0.0.1:12345` | the central node's own telemetry |
| nginx | `:80` | reverse proxy, Basic Auth on ingest paths |
| matrix-webhook | `127.0.0.1:4785` | Grafana to Matrix relay |

**Every other host** runs Alloy only, pushing to the central node over HTTPS.

Two deployment shapes are supported: **direct LXC** (native systemd, via
`scripts/lxc-install.sh central`) and **Docker Compose** (`docker-compose.yml`).
State lives in external Docker volumes or `/var/lib/*` respectively, and
survives `docker compose down -v`.

## Quick start

### Fleet management, documented separately

Ansible owns the Alloy config on every host, deploys the apt and reboot metrics
exporter, configures package patching, schedules container image updates, and
provisions alerting. It lives at [`../ansible/`](../ansible/README.md) and has
its own docs site: **[Fleet Automation](https://harshitruwali.github.io/homelab-infra/ansible/)**.

```bash
cd ansible                                      # from the repository root
ansible-playbook playbooks/site.yml             # everything, idempotent
```

Install the central stack below **first**: a collector with nowhere to push is
not useful. Then hand configuration to Ansible, which owns it from that point
on.

### Central stack, first install

<details>
<summary><b>Direct LXC</b> (native systemd, what this fleet runs)</summary>

```bash
export PUBLIC_DOMAIN=monitor.example.com
export GRAFANA_ADMIN_PASSWORD=<strong-password>
export COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
scripts/lxc-install.sh central
```

Installs Grafana, Prometheus, Loki and Alloy as systemd services and writes an
nginx reverse proxy with htpasswd Basic Auth on the ingest paths. Update with
`scripts/lxc-update.sh central`.

</details>

<details>
<summary><b>Docker Compose</b></summary>

```bash
cp .env.example .env      # set GRAFANA_ADMIN_PASSWORD, COLLECTOR_BASIC_AUTH_PASSWORD,
                          # MONITOR_HOSTNAME, and PUBLIC_DOMAIN for a public deployment
scripts/monitoring.sh central up
```

Creates the external volumes, validates the config and starts the services.
Ports bind to `127.0.0.1`, so put your own TLS reverse proxy in front.

</details>

### Adding a collector by hand

Prefer `ansible-playbook playbooks/site.yml --limit <host>`; it is the single
source of truth for collector config. The manual paths are for bootstrapping a
host Ansible cannot yet reach, and are documented in
[Collectors](docs/monitoring/collectors.md).

## Public exposure

Never expose raw Grafana, Prometheus, Loki or Alloy ports to the internet.

```text
https://monitor.example.com/                         -> Grafana UI
https://monitor.example.com/prometheus/api/v1/write  -> Basic Auth metrics ingest
https://monitor.example.com/loki/api/v1/push         -> Basic Auth log ingest
```

> [!WARNING]
> The two ingest paths behave differently: `/prometheus/` **strips** its prefix
> while `/loki/` **preserves** it. So a Loki query URL is
> `/loki/api/v1/label/host/values`, and `/loki/ready` is a 404. See
> [Verification](https://harshitruwali.github.io/homelab-infra/ansible/fleet/verification/).

See [Security notes](docs/security.md).

## What is provisioned

**7 fleet dashboards** in the `Monitoring` folder, loaded from
`grafana/dashboards/fleet/`:

- **VM Fleet Overview**: fleet freshness, pending updates, which hosts need a
  reboot, top resource consumers, warnings and errors
- **Services and Logs**: systemd unit state, per-container inventory, journal
  and container logs
- **System Overview**: CPU, memory, disk, network, uptime, host count
- **Network**: throughput, packet rates, interface errors and drops, TCP
  retransmit share, conntrack usage, interface inventory
- **Disk Health**: SMART inventory, temperature, wear, bad sectors, plus
  filesystem and inode health for every host
- **Host Processes**: htop as a dashboard, per-core CPU meters, memory and swap
  meters, the process list. Only lists hosts with the opt-in process exporter
- **GPU**: nvtop as a dashboard, utilisation and VRAM graphs, clocks, throttle
  reasons, and which process is holding the VRAM

**One dashboard per host** in the `Servers` folder, loaded from
`grafana/dashboards/servers/`: CPU, memory, disk, network, systemd units,
containers, pending updates and logs, scoped to a single host instead of
filtered through `$host`. Hosts with an NVIDIA GPU also get GPU utilization,
VRAM, temperature, power and fan.

**Alert rules** in `grafana/provisioning/alerting/`: 42 committed across
resources, updates, containers, services, storage, network and GPU, plus five
more in a `rules-availability.yaml` **generated from the inventory** so adding
a host cannot leave a silent gap in down-detection. 47 rules load in total.

> [!NOTE]
> This stack is push-based, so `up` is a series each collector pushes about
> itself. When a host dies the series **vanishes** rather than going to 0, and
> a naive `up == 0` alert never fires. [The push model](docs/architecture/push-model.md)
> explains the or-chain that fixes it.

## Repo layout

Paths below are relative to this directory, `monitoring/`. Fleet automation is
a separate module one level up at [`../ansible/`](../ansible/README.md), with
its own README and its own docs site; every `cd ansible` here means from the
repository root.

```text
alloy/config.alloy             Docker-collector config (native installs use Ansible)
grafana/dashboards/fleet/      Fleet-wide dashboards (Monitoring folder)
grafana/dashboards/servers/    Per-host dashboards (Servers folder)
grafana/provisioning/          Datasources, dashboards, alerting
loki/, prometheus/             Server configs
scripts/lxc-install.sh         Direct LXC installer with nginx auth proxy
scripts/lxc-update.sh          Direct LXC update and config sync
scripts/monitoring.sh          Docker Compose lifecycle
docker-compose.yml             Central stack
docker-compose.collector.yml   Collector-only stack
docs/                          MkDocs source (mkdocs.yml in this directory)
docs/requirements.txt          Pinned MkDocs toolchain
```

> [!CAUTION]
> `../ansible/inventory/hosts.local.yml` is gitignored and must stay that way.
> This repository is public, and an inventory is a complete map of the estate:
> ingest endpoint, internal addressing, valid usernames, and which box to hit
> to blind the monitoring. See [Fleet Automation](../ansible/README.md).

## Documentation

Built with MkDocs Material, and published as one section of the repository's
GitHub Pages site.

```bash
python3 -m venv .venv
.venv/bin/pip install -r docs/requirements.txt
.venv/bin/mkdocs serve             # live preview on http://127.0.0.1:8000
.venv/bin/mkdocs build             # render the static site into site/
```

Published to GitHub Pages by the repository-root
`.github/workflows/deploy-docs.yml` on push to `master`; pull requests run the
same checks without publishing. One workflow builds all three docs sites in
this repository, so a change here rebuilds the others too. It is docs-only: it
never runs a playbook, never touches the fleet, and uses no repository secrets.

> [!IMPORTANT]
> Pages must be enabled once by hand: **Settings → Pages → Source: GitHub
> Actions**. It cannot be automated, because creating a Pages site is an
> admin-level API call that `GITHUB_TOKEN` is not permitted to make. Until
> then the **build** job fails, at `Configure GitHub Pages`, after every
> `mkdocs build --strict` has already passed.

| Section | Start at |
|---|---|
| Install the central stack | [Getting started](docs/getting-started/index.md) |
| Architecture and the push model | [Architecture](docs/architecture/index.md) |
| Fleet management, patching, container updates | [Fleet Automation](https://harshitruwali.github.io/homelab-infra/ansible/) |
| Collectors, dashboards, alerting | [Monitoring](docs/monitoring/index.md) |
| Lifecycle, retention, runbooks | [Operations](docs/operations/index.md) |
| Metrics catalogue and tooling | [Reference](docs/reference/index.md) |
| Security notes | [Security](docs/security.md) |

Frequently wanted:

- [What runs when](https://harshitruwali.github.io/homelab-infra/ansible/fleet/schedules/): every timer and how to force it
- [Troubleshooting](https://harshitruwali.github.io/homelab-infra/ansible/fleet/troubleshooting/): failure modes seen in production
- [Runbooks](docs/operations/runbooks.md): recovery steps per alert
- [Metrics catalogue](docs/reference/metrics.md): everything this repo adds
- [Glossary](https://harshitruwali.github.io/homelab-infra/ansible/getting-started/glossary/): Ansible, monitoring and systemd terms

## Design commitments

- **Machines never reboot themselves**, under any policy. Kernel updates
  therefore accumulate until a human acts, which is why
  `fleet-reboot-required-too-long` nags at seven days.
- **Nothing is held back from patching** except the Raspberry Pi kernel and
  bootloader, which are unversioned and would leave a running kernel with no
  modules on disk.
- **Patching is decided by inventory group membership only**, never a
  conditional inside a role. A conditional inside a role is one typo away from
  auto-upgrading a hypervisor.
- **Alerting is provisioned from files.** Rules edited in the UI are not the
  source of truth, and rules deleted in the UI do not come back on their own.
- **Config is owned by Ansible**, not the install scripts.
  `scripts/lxc-install.sh` is a bootstrap path, guarded by a marker file so it
  can never revert what Ansible manages.
