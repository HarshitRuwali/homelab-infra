# VM and LXC Collector Rollout

!!! tip "Prefer Ansible"
    For an existing fleet use `ansible-playbook playbooks/site.yml --limit <host>`.
    It is the single source of truth for collector config. The manual paths
    below are for bootstrapping a host Ansible cannot yet reach.

Use `docker-compose.collector.yml` on Docker-based VMs. Use `scripts/lxc-install.sh collector` when you want Alloy installed directly inside a Debian/Ubuntu LXC.

## Requirements

- Network access from the collector to the authenticated public HTTPS ingest paths, or private access to central ports `9090` and `3100` on a trusted LAN/VPN.
- Docker and Docker Compose plugin installed for the Docker collector path.
- A root shell inside a systemd-based Debian/Ubuntu LXC for the direct LXC collector path.
- Docker socket access only when the collector host also runs Docker containers.

## Docker Install

Copy these files to the VM:

```text
docker-compose.collector.yml
alloy/config.alloy
scripts/monitoring.sh
```

Start the collector:

```bash
export PROMETHEUS_REMOTE_WRITE_URL=https://monitor.example.com/prometheus/api/v1/write
export LOKI_WRITE_URL=https://monitor.example.com/loki/api/v1/push
export COLLECTOR_BASIC_AUTH_USER=collector
export COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
export MONITOR_HOSTNAME=<stable-vm-name>
export MONITOR_ROLE=vm
scripts/monitoring.sh collector up
```

The script creates the external `monitoring-alloy-data` Docker volume, validates config, and starts Alloy. If you are not using public HTTPS ingest paths, you can still use `MONITORING_SERVER=<central-server-ip-or-dns>` for private LAN deployments.

## Direct LXC Install

Copy the repository to the LXC and run the direct installer in collector mode:

```bash
export PROMETHEUS_REMOTE_WRITE_URL=https://monitor.example.com/prometheus/api/v1/write
export LOKI_WRITE_URL=https://monitor.example.com/loki/api/v1/push
export COLLECTOR_BASIC_AUTH_USER=collector
export COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
export MONITOR_HOSTNAME=<stable-lxc-name>
export MONITOR_ROLE=lxc
scripts/lxc-install.sh collector
```

The installer adds the Grafana APT repository, installs Alloy, writes `/etc/alloy/config.alloy`, and enables the `alloy` systemd service. For updates, run:

```bash
scripts/lxc-update.sh collector
```

Use a stable `MONITOR_HOSTNAME`; changing it creates a new host identity in Prometheus and Loki.

## Verify

On a Docker collector:

```bash
scripts/monitoring.sh collector status
docker logs monitoring-collector --tail 100
```

On a direct LXC collector:

```bash
systemctl status alloy
journalctl -u alloy -n 100 --no-pager
```

On the central server:

```bash
curl 'http://localhost:9090/api/v1/query' --data-urlencode 'query=node_uname_info'
curl http://127.0.0.1:3100/ready
curl -u collector:<collector-password> https://monitor.example.com/loki/api/v1/labels
```

In Grafana, open any dashboard and select the VM from the `host` variable.

## Notes

- VMs and LXCs without Docker still report host metrics and journal logs; Docker panels remain empty for those hosts.
- If journald persistent storage is disabled, Alloy can still read runtime logs from `/run/log/journal` when available.
- Collector debug UI is intentionally not published in `docker-compose.collector.yml`.
