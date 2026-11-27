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

- :material-router-network: **[Routers and firewall](routers.md)**

    The TP-Link router, OPNsense and the ER605: the network map, what each
    device reports, and what is still planned.

- :material-bell-alert: **[Alerting](alerting.md)**

    The rules, the Matrix relay, and how to test the chain.

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
| Per-client router traffic, connections, router log (remote site) | `tplink-router-poll`, textfile and journal | Router TPLink Blr |
| Household internet usage, PPPoE reconnects and ER605 health (home) | `er605-poll` over SNMPv3, textfile and container log | Home ISP |
| OPNsense host, interfaces, firewall, gateways, services | node_exporter plugin and `opnsense-exporter`, scraped by the central Alloy | Prometheus jobs `opnsense-node`, `opnsense-api`; see [Routers and firewall](routers.md) |

## What is deliberately not collected

- **Per-client usage on the home LAN.** The ER605 forwards most traffic in
  hardware past its own counters; a port mirror is planned. See
  [Routers and firewall](routers.md#er605-home-router).
- **The central LXC being down.** Grafana dies with it. Needs an external
  dead-man's-switch.

## Label conventions

Every series carries these, applied by Alloy as external labels:

| Label | Source | Notes |
|---|---|---|
| `host` | `MONITOR_HOSTNAME` | **never rename**, it forks history |
| `role` | `MONITOR_ROLE` | `server`, `workstation`, `pi`, `router`, `service`, `central`, `hypervisor`; `firewall` for OPNsense, set on its scrape targets and syslog |
| `job` | set by the exporter | `integrations/unix`, `integrations/cadvisor`, `alloy` |

`role` is genuinely useful in rules. Two exclude `role="workstation"`, which
today means `ubuntu-dev`:

- `fleet-container-disappeared`, because a dev box starts and destroys
  throwaway containers constantly, and every one of them would otherwise fire.
- `fleet-docker-update-failed`, because half-built compose projects fail
  `up -d` and a crashing dev container left `restarting` at 04:00 sets the same
  flag. Neither is news on a workstation.

In both cases the metrics are still collected and still on the dashboards. It
is the page that is suppressed, not the signal.
