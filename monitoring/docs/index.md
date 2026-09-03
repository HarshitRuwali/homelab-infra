# Homelab Monitoring Stack

Central Grafana, Prometheus and Loki for a Proxmox and Tailscale homelab.
Metrics, logs, dashboards and alerting into a self-hosted Matrix room, fed by a
Grafana Alloy collector on every host.

## What it does

<div class="grid cards" markdown>

- :material-chart-line: **Collects**

    Grafana Alloy on every host ships host metrics, systemd journal logs,
    container metrics and container logs to one place.

- :material-view-dashboard: **Displays**

    Seven fleet-wide dashboards plus one per host, provisioned from files in
    git rather than clicked together in the UI.

- :material-bell-alert: **Alerts**

    Grafana unified alerting posts to Matrix through a local relay. 47 rules,
    also provisioned from files.

- :material-eye-check: **Proves it**

    Every automated action writes a metric, so "it updates itself" cannot
    quietly become "it broke itself three weeks ago".

</div>

## Start here

!!! tip "Standing this up for the first time?"
    **[Getting started](getting-started/index.md)** installs the central stack
    on one host, either natively in an LXC or with Docker Compose, and puts a
    reverse proxy with Basic Auth in front of the ingest paths.

| If you want to… | Go to |
|---|---|
| Install the central stack | [Central stack install](getting-started/install.md) |
| Understand how telemetry moves | [Architecture](architecture/index.md) |
| Know why a dead host is not `up == 0` | [The push model](architecture/push-model.md) |
| Add a collector to a host | [Collectors](monitoring/collectors.md) |
| Know why cAdvisor reports nothing | [Container metrics](monitoring/container-metrics.md) |
| See what a panel means | [Dashboards](monitoring/dashboards.md) |
| Look up an alert rule | [Alerting](monitoring/alerting.md) |
| Run the stack day to day | [Operations](operations/index.md) |
| Recover from an alert | [Runbooks](operations/runbooks.md) |
| Know how long data is kept | [Retention](operations/retention.md) |
| Look up a metric | [Metrics catalogue](reference/metrics.md) |
| Understand what is exposed publicly | [Security](security.md) |
| Build the docs | [Building the docs](reference/tooling.md) |

## The one-command version

```bash
scripts/monitoring.sh central up      # from monitoring/
```

Creates the external volumes, validates the config and starts Grafana,
Prometheus, Loki, Alloy and the Matrix relay. Ports bind to `127.0.0.1`, so put
your own TLS reverse proxy in front. See
[Central stack install](getting-started/install.md).

!!! info "Every path on this site is relative to `monitoring/`"
    `scripts/`, `grafana/`, `alloy/`, `loki/`, `prometheus/`,
    `docker-compose.yml`. Where a page needs to name something in the Ansible
    tree it writes it as `../ansible/...`, because that tree sits at the
    repository root, one level up.

!!! tip "The collectors are deployed by Ansible, and documented separately"
    This site covers **what is collected and what it means**. Getting Alloy
    onto a host, patching, container updates and onboarding are the fleet
    control plane, which has its own site:
    **[Fleet Automation](https://harshitruwali.github.io/homelab-infra/ansible/)**.

## Where the boundary sits

The two stacks are deployed independently and documented independently. Roughly:

| This site | [The fleet site](https://harshitruwali.github.io/homelab-infra/ansible/) |
|---|---|
| what Alloy collects, and the label conventions | [installing Alloy on a host](https://harshitruwali.github.io/homelab-infra/ansible/fleet/onboarding/) |
| the rule catalogue and what each alert means | [provisioning those rules](https://harshitruwali.github.io/homelab-infra/ansible/reference/playbooks/) |
| what each dashboard panel shows | [deploying the dashboards](https://harshitruwali.github.io/homelab-infra/ansible/reference/playbooks/) |
| running Grafana, Prometheus and Loki | [patching the hosts they run on](https://harshitruwali.github.io/homelab-infra/ansible/fleet/patching/) |
| the monitoring glossary terms | [the full glossary](https://harshitruwali.github.io/homelab-infra/ansible/getting-started/glossary/) |

## Reading these docs offline

```bash
cd monitoring                      # from the repository root
python3 -m venv .venv
.venv/bin/pip install -r docs/requirements.txt
.venv/bin/mkdocs serve             # live preview on http://127.0.0.1:8000
.venv/bin/mkdocs build             # render the static site into ./site
```

The repository-root `.github/workflows/deploy-docs.yml` publishes to GitHub
Pages on push to `master`, and builds without publishing on pull requests. It
is the only workflow in the repository, it builds all four docs sites, it is
docs-only, and it holds no secrets. See
[Building the docs](reference/tooling.md).

## Design commitments

These are the decisions everything else follows from. Each one is load-bearing
and each one has bitten this stack at least once.

**A dead host must alert, and `up == 0` will not do it.** This stack is
push-based, so `up` is a series each collector pushes about itself. When a host
dies the series **vanishes** rather than going to zero, and a naive `up == 0`
alert never fires. See [The push model](architecture/push-model.md).

**Alerting is provisioned from files.** Rules edited in the UI are not the
source of truth, and rules deleted in the UI do not come back on their own.
See [Alerting](monitoring/alerting.md#provisioned-rules-are-not-ui-rules).

**Dashboards are provisioned from files too.** They live in
`grafana/dashboards/` and are deployed by a playbook, so a panel edited in the
browser is lost on the next run. That is the intended direction: git is the
source, the UI is the view.

**Nothing binds to a public interface.** Grafana, Prometheus, Loki and Alloy
all listen on `127.0.0.1`, and everything public goes through nginx with Basic
Auth on the ingest paths. See [Security](security.md).

**Config is owned by Ansible, not by the install scripts.**
`scripts/lxc-install.sh` is a bootstrap path, guarded by a marker file so it
can never revert what Ansible manages.
