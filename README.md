# Persistent VM Monitoring Stack

Self-hosted monitoring for a Proxmox host, Linux VMs, and Docker-based applications. The stack uses Grafana for dashboards, Prometheus for metrics, Loki for logs, and Grafana Alloy as the collector agent.

## Stack

- `grafana`: dashboards and Explore UI on port `3000`.
- `prometheus`: persistent metrics storage on port `9090`.
- `loki`: persistent log storage on port `3100`.
- `collector`: Grafana Alloy agent for host metrics, systemd state, Docker metrics, journal logs, and Docker logs.

Prometheus, Loki, Grafana, and Alloy state are stored in external Docker volumes. External volumes survive container restarts and are not removed by `docker compose down -v`.

## Quick Start

1. Create a local environment file:

```bash
cp .env.example .env
```

2. Edit `.env` and set at least:

```bash
GRAFANA_ADMIN_PASSWORD=<strong-password>
COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
MONITOR_HOSTNAME=<central-server-name>
```

For a public domain, also set:

```bash
GRAFANA_ROOT_URL=https://monitor.example.com
GRAFANA_COOKIE_SECURE=true
PUBLIC_DOMAIN=monitor.example.com
```

3. Start the central stack:

```bash
scripts/monitoring.sh central up
```

The script creates the external Docker volumes, validates the Compose config, and starts the services.

4. Open Grafana:

```text
http://127.0.0.1:3000
```

For a public domain, put a TLS reverse proxy in front of Grafana and expose only the proxy. The default Grafana user is `admin` unless you change `GRAFANA_ADMIN_USER`.

## Public Domain Setup

Do not expose raw Grafana, Prometheus, Loki, or Alloy ports directly to the internet. The secure public shape is:

```text
https://monitor.example.com/                    -> Grafana login UI
https://monitor.example.com/prometheus/api/v1/write -> Basic Auth collector metrics ingest
https://monitor.example.com/loki/api/v1/push         -> Basic Auth collector log ingest
```

The direct-LXC installer writes an nginx reverse proxy for those paths. For Docker Compose, the service ports bind to `127.0.0.1` by default so you can put your own TLS reverse proxy in front.

## Fleet Rollout (Ansible)

Ansible is the primary way to manage the fleet. It owns the Alloy config on
every host, deploys the apt/reboot metrics exporter, configures auto-applied
package updates (never auto-rebooting), schedules Docker image updates, and
provisions alerting.

```bash
brew install ansible                            # macOS
# or on Debian/Ubuntu:
#   python3 -m venv ~/.venvs/ansible && ~/.venvs/ansible/bin/pip install ansible

cd ansible
ansible-playbook playbooks/preflight.yml        # read-only
ansible-playbook playbooks/site.yml             # everything, idempotent
```

Runs from macOS or from a Linux box on the Proxmox LAN. Add
`-e lan_use_jump_host=false` when running from the LAN itself.

`site.yml` configures the machinery; it does not itself install packages or
pull images. Those apply on a schedule: **03:00** for packages, **04:00** for
containers, both with jitter, and nothing ever reboots a machine.

See [Fleet management](docs/fleet/index.md) for the full procedure,
[What runs when](docs/fleet/schedules.md) for the schedules, and
[Controller setup](docs/fleet/setup.md) for first-time setup.

The manual per-host instructions below still work and are useful for
bootstrapping a brand-new central LXC, but for an existing fleet prefer the
Ansible path: it is the single source of truth for collector config.

## Add VM or LXC Collectors

For Docker-based VMs, copy this repository, or at least `docker-compose.collector.yml`, `alloy/config.alloy`, and `scripts/monitoring.sh`, to each VM. Then run:

```bash
export PROMETHEUS_REMOTE_WRITE_URL=https://monitor.example.com/prometheus/api/v1/write
export LOKI_WRITE_URL=https://monitor.example.com/loki/api/v1/push
export COLLECTOR_BASIC_AUTH_USER=collector
export COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
export MONITOR_HOSTNAME=<vm-name>
export MONITOR_ROLE=vm
scripts/monitoring.sh collector up
```

For Debian/Ubuntu LXC collectors without Docker Compose, copy the repository and run the direct installer in collector mode:

```bash
export PROMETHEUS_REMOTE_WRITE_URL=https://monitor.example.com/prometheus/api/v1/write
export LOKI_WRITE_URL=https://monitor.example.com/loki/api/v1/push
export COLLECTOR_BASIC_AUTH_USER=collector
export COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
export MONITOR_HOSTNAME=<lxc-name>
export MONITOR_ROLE=lxc
scripts/lxc-install.sh collector
```

After one or two minutes, the VM or LXC should appear in the dashboard host selector. Use `scripts/lxc-update.sh collector` to update a direct LXC collector.

## Useful Commands

```bash
scripts/monitoring.sh central status
scripts/monitoring.sh central logs
scripts/monitoring.sh central down
scripts/monitoring.sh collector status
```

## Dashboards

Grafana automatically loads dashboards from `grafana/dashboards`:

- `System Overview`: CPU, memory, disk, network, uptime, and host count.
- `Services and Logs`: systemd unit state, a per-container inventory, journal logs, and container logs.
- `VM Fleet Overview`: fleet freshness, pending updates, which hosts need a reboot, top resource consumers, and warnings/errors.

See [Dashboards](docs/monitoring/dashboards.md) for what each panel is for.

## Repo Layout

```text
alloy/                         Collector pipeline config
ansible/                       Fleet automation: inventory, roles, playbooks
ansible/playbooks/site.yml     Everything, in dependency order
docs/                          MkDocs source (mkdocs.yml at the repo root)
grafana/dashboards/            Provisioned Grafana dashboards
grafana/provisioning/          Grafana datasource and dashboard provisioning
loki/                          Loki local filesystem storage config
prometheus/                    Prometheus scrape/storage config
scripts/monitoring.sh          Docker setup and lifecycle automation
scripts/lxc-install.sh         Direct LXC central/collector installer with nginx auth proxy
scripts/lxc-update.sh          Direct LXC central/collector update and config sync helper
docker-compose.yml             Central monitoring stack
docker-compose.collector.yml   Collector-only stack for each VM
```

## Docs

The full documentation is a MkDocs site, built and checked locally. The only
prerequisite is [uv](https://docs.astral.sh/uv/); it resolves everything else
from `pyproject.toml`.

```bash
uv run --group docs mkdocs serve   # live preview on http://127.0.0.1:8000
uv run --group docs mkdocs build   # render the static site into site/
```

Published to GitHub Pages by `.github/workflows/docs.yml` on push to `master`.
That workflow is docs-only: it never runs a playbook, never touches the fleet,
and uses no repository secrets. Requires Pages set to **GitHub Actions** once
in repository settings.

| Section | Start at |
|---|---|
| Architecture and the push model | [docs/architecture/](docs/architecture/index.md) |
| Fleet management, patching, container updates | [docs/fleet/](docs/fleet/index.md) |
| Collectors, dashboards, alerting | [docs/monitoring/](docs/monitoring/index.md) |
| Lifecycle, retention, runbooks | [docs/operations/](docs/operations/index.md) |
| Playbooks, metrics, variables | [docs/reference/](docs/reference/playbooks.md) |
| Security notes | [docs/security.md](docs/security.md) |

Frequently wanted pages:

- [What runs when](docs/fleet/schedules.md): every timer and how to force it
- [Troubleshooting](docs/fleet/troubleshooting.md): failure modes seen in production
- [Runbooks](docs/operations/runbooks.md): recovery steps per alert
- [Metrics catalogue](docs/reference/metrics.md): everything this repo adds
- [Building the docs](docs/reference/tooling.md): local preview and publishing
