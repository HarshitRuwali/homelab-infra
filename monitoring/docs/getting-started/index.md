# Getting started

Standing up the central stack: one host running Grafana, Prometheus, Loki and
Alloy, behind a reverse proxy that authenticates the ingest paths.

!!! tip "This is the server side only"
    Getting a collector onto each of your other hosts is the fleet control
    plane's job, and it has its own site:
    **[Fleet Automation](https://harshitruwali.github.io/homelab-infra/ansible/)**.
    Install the central stack first, because a collector with nowhere to push
    is not useful.

## Pick a deployment shape

| Shape | When | Guide |
|---|---|---|
| **Direct LXC** | a Debian or Ubuntu LXC on Proxmox, native systemd units. What this fleet actually runs. | [Central stack install](install.md#direct-lxc) |
| **Docker Compose** | anywhere with Docker, and the quicker way to try it. | [Central stack install](install.md#docker-compose) |

Both end in the same place: services bound to `127.0.0.1`, state that survives
a teardown, and configuration that Ansible subsequently owns.

## Prerequisites

- A host that will stay up. Everything else reports **to** this one, so when it
  is down you are blind, and nothing else in the fleet notices.
- **Disk sized for retention.** Prometheus writes roughly 28 MB per host per
  day, so a 12-host fleet is about 2.4 GB at the default `7d`, and 10 GB at
  `30d`. See [Retention](../operations/retention.md).
- A **TLS reverse proxy** if you will expose it. Nothing here binds to a public
  interface, and nothing here terminates TLS for you on the Compose path.
- Two strong passwords: `GRAFANA_ADMIN_PASSWORD` and
  `COLLECTOR_BASIC_AUTH_PASSWORD`. The second is shared by every collector in
  the fleet.

!!! danger "Never expose the raw ports"
    Grafana, Prometheus, Loki and Alloy have no meaningful authentication on
    their own ports, and Prometheus's remote-write receiver will accept
    anything that reaches it. Everything public goes through nginx with Basic
    Auth. See [Security](../security.md).

## What comes up

| Service | Bind | Purpose |
|---|---|---|
| Grafana | `127.0.0.1:3000` | dashboards and unified alerting |
| Prometheus | `127.0.0.1:9090` | metrics, and the remote-write receiver |
| Loki | `127.0.0.1:3100` | logs |
| Alloy | `127.0.0.1:12345` | this node's own telemetry |
| nginx | `:80` | reverse proxy, Basic Auth on the ingest paths |
| matrix-webhook | `127.0.0.1:4785` | the Grafana to Matrix relay |

!!! note "The two ingest paths do not behave the same way"
    `/prometheus/` **strips** its prefix while `/loki/` **preserves** it. So a
    Loki query URL is `/loki/api/v1/label/host/values`, and `/loki/ready` is a
    404 rather than a health check. This trips up every first verification
    attempt.

## Next

1. [Central stack install](install.md), the LXC or Compose path end to end.
2. [Architecture](../architecture/index.md), what is actually moving and where.
3. [The push model](../architecture/push-model.md), why a dead host is not
   `up == 0` and what to do about it.
4. [Collectors](../monitoring/collectors.md), what Alloy ships once hosts start
   reporting.
