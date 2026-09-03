# Monitoring

What is collected, where it is displayed, and what alerts on it.

<div class="grid cards" markdown>

- :material-server-network: **[Collectors](collectors.md)**

    Installing Alloy on a host, by Ansible or by hand.

- :material-docker: **[Container metrics](container-metrics.md)**

    Why cAdvisor needs root, and how it fails silently without it.

- :material-view-dashboard: **[Dashboards](dashboards.md)**

    The seven fleet dashboards, the per-host Servers folder, and what each
    panel is for.

- :material-bell-alert: **[Alerting](alerting.md)**

    47 rules, the Matrix relay, and how to test the chain.

</div>

## Coverage at a glance

| Signal | Source | Where it lands |
|---|---|---|
| CPU, memory, disk, network, load | `prometheus.exporter.unix` | System Overview, VM Fleet Overview |
| systemd unit state | unix exporter, `systemd` collector | Services and Logs |
| Pending packages, reboot required | textfile collector | VM Fleet Overview |
| Container CPU, memory, restarts, health | `prometheus.exporter.cadvisor` | Services and Logs |
| Container update results | textfile collector | alerts only |
| systemd journal | `loki.source.journal` | Services and Logs |
| Container stdout/stderr | `loki.source.docker` | Services and Logs |

## What is deliberately not collected

- **OPNsense.** Would need the `os-node_exporter` plugin, scraped rather than
  pushed.
- **The central LXC being down.** Grafana dies with it. Needs an external
  dead-man's-switch.

## Label conventions

Every series carries these, applied by Alloy as external labels:

| Label | Source | Notes |
|---|---|---|
| `host` | `MONITOR_HOSTNAME` | **never rename**, it forks history |
| `role` | `MONITOR_ROLE` | `server`, `workstation`, `pi`, `router`, `service`, `central`, `hypervisor` |
| `job` | set by the exporter | `integrations/unix`, `integrations/cadvisor`, `alloy` |

`role` is genuinely useful in rules. `fleet-container-disappeared` excludes
`role="workstation"` because a dev box starts and destroys throwaway
containers constantly, and every one of them would otherwise fire.
