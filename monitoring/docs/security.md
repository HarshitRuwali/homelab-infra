# Security Notes

This stack can be used on a public domain, but do not expose Grafana, Prometheus, Loki, or Alloy debug ports directly to the internet. Publish only a reverse proxy, terminate TLS there, and keep the backend service ports private.

## Public Domain Model

Recommended public layout:

```text
internet
  -> TLS reverse proxy on monitor.example.com
      /                 -> Grafana login UI
      /prometheus/*     -> Prometheus remote_write, protected by Basic Auth
      /loki/*           -> Loki write/query API, protected by Basic Auth

localhost/private only
  Grafana    127.0.0.1:3000
  Prometheus 127.0.0.1:9090
  Loki       127.0.0.1:3100
  Alloy UI   127.0.0.1:12345
```

The Docker Compose stack now binds Grafana, Prometheus, and Loki to `127.0.0.1` by default. The direct-LXC installer writes an nginx reverse proxy that exposes Grafana and protects collector ingest paths with htpasswd Basic Auth.

## Required Secrets

Set real values before running a public deployment:

```bash
GRAFANA_ADMIN_PASSWORD=<strong-grafana-password>
COLLECTOR_BASIC_AUTH_USER=collector
COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
GRAFANA_ROOT_URL=https://monitor.example.com
GRAFANA_COOKIE_SECURE=true
PUBLIC_DOMAIN=monitor.example.com
```

The lifecycle scripts reject placeholder passwords.

## TLS

Use HTTPS for the public domain. The LXC installer configures nginx on port `80`; terminate TLS either in front of it with Cloudflare, Caddy, Traefik, Nginx Proxy Manager, or another reverse proxy, or replace the generated nginx site with your own certificate-backed `443` server.

Only expose these public ports at the network edge:

```text
80/tcp   optional redirect/proxy
443/tcp  HTTPS reverse proxy
```

Do not expose these directly to the internet:

```text
3000/tcp Grafana backend
9090/tcp Prometheus backend
3100/tcp Loki backend
12345/tcp Alloy debug UI
```

## Remote Collectors

For public-domain collectors, point Alloy at the authenticated HTTPS paths:

```bash
export PROMETHEUS_REMOTE_WRITE_URL=https://monitor.example.com/prometheus/api/v1/write
export LOKI_WRITE_URL=https://monitor.example.com/loki/api/v1/push
export COLLECTOR_BASIC_AUTH_USER=collector
export COLLECTOR_BASIC_AUTH_PASSWORD=<strong-collector-password>
```

Alloy sends Basic Auth credentials to both remote-write targets. Keep the collector password different from the Grafana admin password.

## Privileged Collector

Alloy runs with privileged host access in the Docker collector mode so it can collect accurate host, Docker, cgroup, disk, and journal telemetry. That means the collector has sensitive host visibility.

Reduce risk by:

- Running it only on trusted hosts.
- Keeping Docker and host OS packages updated.
- Avoiding public exposure of the Docker socket or Alloy debug UI.
- Limiting who can edit `alloy/config.alloy`, scripts, and Compose files.

## Firewall Checklist

- Allow public traffic only to the TLS reverse proxy.
- Allow collector traffic only to the authenticated `/prometheus/*` and `/loki/*` proxy paths.
- Keep direct Prometheus and Loki ports loopback-only unless the host is protected by a VPN or private firewall.
- Prefer a VPN or private overlay network for collectors if you do not want ingestion endpoints on the public internet.
