# Operations

## Lifecycle Script

Use `scripts/monitoring.sh` for normal setup and lifecycle commands. It creates required external Docker volumes, validates environment variables, and runs the correct Compose file.

```bash
scripts/monitoring.sh central init
scripts/monitoring.sh central validate
scripts/monitoring.sh central up
scripts/monitoring.sh central status
scripts/monitoring.sh central logs
scripts/monitoring.sh central down
```

For a VM collector, use the `collector` mode:

```bash
export PROMETHEUS_REMOTE_WRITE_URL=https://monitor.example.com/prometheus/api/v1/write
export LOKI_WRITE_URL=https://monitor.example.com/loki/api/v1/push
export COLLECTOR_BASIC_AUTH_USER=collector
export COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
export MONITOR_HOSTNAME=<vm-name>
scripts/monitoring.sh collector up
```

## Start and Stop Manually

The script is preferred, but the equivalent central commands are:

```bash
docker volume create monitoring-grafana-data
docker volume create monitoring-prometheus-data
docker volume create monitoring-loki-data
docker volume create monitoring-alloy-data
docker compose up -d
docker compose ps
docker compose down
```

Use `docker compose down` to stop containers. The central stack stores monitoring history in external Docker volumes, so even `docker compose down -v` will not delete Prometheus, Loki, Grafana, or Alloy data.

To intentionally delete monitoring history:

```bash
docker compose down
docker volume rm monitoring-grafana-data monitoring-prometheus-data monitoring-loki-data monitoring-alloy-data
```

## Logs

```bash
scripts/monitoring.sh central logs
```

Or directly with Compose:

```bash
docker compose logs -f grafana
docker compose logs -f prometheus
docker compose logs -f loki
docker compose logs -f collector
```

## Health Checks

```bash
curl http://127.0.0.1:3000/api/health
curl http://127.0.0.1:9090/-/ready
curl http://127.0.0.1:3100/ready
curl http://127.0.0.1:12345/-/ready
curl -u collector:<collector-password> https://monitor.example.com/prometheus/-/ready
curl -u collector:<collector-password> https://monitor.example.com/loki/api/v1/labels
```

The Docker Compose stack binds Grafana, Prometheus, Loki, and the Alloy debug UI to `127.0.0.1` by default. Put a TLS reverse proxy in front for public-domain access.

## Query Smoke Tests

```bash
curl 'http://127.0.0.1:9090/api/v1/query' --data-urlencode 'query=node_uname_info'
curl 'http://127.0.0.1:9090/api/v1/query' --data-urlencode 'query=up{job=~"integrations/unix|host-unix"}'
```

In Grafana Explore, use Loki queries like:

```logql
{source="journal"}
{source="docker"}
{host="main-server"} |~ "(?i)(error|failed|fatal|panic)"
```

## Retention

- Prometheus: set `PROMETHEUS_RETENTION` in `.env`, for example `30d` or `90d`.
- Loki: edit `limits_config.retention_period` in `loki/loki-config.yml`.

Restart the stack after retention config changes:

```bash
scripts/monitoring.sh central restart
```
