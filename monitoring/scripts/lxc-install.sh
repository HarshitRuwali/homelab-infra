#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

CENTRAL_PACKAGES=(grafana prometheus loki alloy nginx apache2-utils)
COLLECTOR_PACKAGES=(alloy)
LXC_MODE=central
CONFIG_ONLY=0
NO_START=0

usage() {
  cat <<'USAGE'
Usage:
  scripts/lxc-install.sh [central|collector] [--config-only] [--no-start]

Installs the central monitoring stack directly into a Debian/Ubuntu LXC:
  - Grafana from the Grafana APT repository
  - Loki from the Grafana APT repository
  - Alloy from the Grafana APT repository
  - Prometheus from the distro APT repository

Installs a collector-only LXC when run with the collector mode:
  - Alloy from the Grafana APT repository
  - Host metrics, systemd journal logs, and Docker telemetry when Docker exists
  - Remote write/push to the central Prometheus and Loki ingest endpoints

Required:
  Run as root inside a systemd-based LXC.
  Set required secrets in .env or export them before running.

Useful environment variables:
  MONITORING_SERVER        collector mode fallback for central host/IP
  GRAFANA_ADMIN_USER       default: admin
  GRAFANA_ADMIN_PASSWORD   required for central mode
  GRAFANA_ROOT_URL         default: http://localhost:3000
  PROMETHEUS_RETENTION     default: 7d
  MONITOR_HOSTNAME         default: current hostname
  MONITOR_ROLE             default: central-lxc in central mode, lxc in collector mode
  PROMETHEUS_REMOTE_WRITE_URL collector mode explicit metrics ingest URL
  LOKI_WRITE_URL           collector mode explicit logs ingest URL
  PUBLIC_DOMAIN            default: _
  MONITORING_LISTEN_ADDRESS default: 127.0.0.1
  COLLECTOR_BASIC_AUTH_USER default: collector
  COLLECTOR_BASIC_AUTH_PASSWORD required

Options:
  --config-only  Only render configs/dashboards and systemd overrides.
  --no-start     Do not enable or restart services after writing config.
USAGE
}

log() {
  printf '[lxc-install] %s\n' "$*"
}

fail() {
  printf '[lxc-install] ERROR: %s\n' "$*" >&2
  exit 1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "Run this script as root inside the LXC."
  has_cmd apt-get || fail "apt-get is required. Use a Debian/Ubuntu LXC."
  has_cmd systemctl || fail "systemctl is required. Use a systemd-based LXC."
}

load_env_file() {
  local line key value

  [[ -f "$ENV_FILE" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      value="${value%$'\r'}"

      if [[ "$value" == \"*\" && "$value" == *\" ]]; then
        value="${value:1:${#value}-2}"
      elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
        value="${value:1:${#value}-2}"
      fi

      if [[ -z "${!key+x}" ]]; then
        export "$key=$value"
      fi
    fi
  done < "$ENV_FILE"
}

require_var() {
  local name="$1"
  local value="${!name:-}"
  [[ -n "$value" ]] || fail "$name is required. Set it in .env or export it before running this command."
}

reject_placeholder_collector_password() {
  require_var COLLECTOR_BASIC_AUTH_PASSWORD
  if [[ "${COLLECTOR_BASIC_AUTH_PASSWORD}" == "replace-with-a-strong-collector-password" || "${COLLECTOR_BASIC_AUTH_PASSWORD}" == "<strong-password>" ]]; then
    fail "Set a real COLLECTOR_BASIC_AUTH_PASSWORD before installing the LXC stack."
  fi
}

validate_central_env() {
  require_var GRAFANA_ADMIN_PASSWORD
  reject_placeholder_collector_password

  if [[ "${GRAFANA_ADMIN_PASSWORD}" == "replace-with-a-strong-password" || "${GRAFANA_ADMIN_PASSWORD}" == "<strong-password>" ]]; then
    fail "Set a real GRAFANA_ADMIN_PASSWORD before installing the central LXC stack."
  fi
}

validate_collector_env() {
  reject_placeholder_collector_password

  if [[ -z "${MONITORING_SERVER:-}" && ( -z "${PROMETHEUS_REMOTE_WRITE_URL:-}" || -z "${LOKI_WRITE_URL:-}" ) ]]; then
    fail "Set MONITORING_SERVER, or set both PROMETHEUS_REMOTE_WRITE_URL and LOKI_WRITE_URL for collector mode."
  fi
}

collector_prometheus_remote_write_url() {
  if [[ -n "${PROMETHEUS_REMOTE_WRITE_URL:-}" ]]; then
    printf '%s\n' "$PROMETHEUS_REMOTE_WRITE_URL"
  else
    printf 'http://%s:9090/api/v1/write\n' "$MONITORING_SERVER"
  fi
}

collector_loki_write_url() {
  if [[ -n "${LOKI_WRITE_URL:-}" ]]; then
    printf '%s\n' "$LOKI_WRITE_URL"
  else
    printf 'http://%s:3100/loki/api/v1/push\n' "$MONITORING_SERVER"
  fi
}

write_env_assignment() {
  local file="$1"
  local key="$2"
  local value="$3"

  [[ "$value" != *$'\n'* ]] || fail "$key cannot contain newlines."
  # Use printf with %s to safely write the value without corrupting special chars
  printf '%s="%s"\n' "$key" "$value" >> "$file"
}

install_packages() {
  local packages=("$@")

  export DEBIAN_FRONTEND=noninteractive

  log "Installing APT prerequisites."
  apt-get update
  apt-get install -y apt-transport-https ca-certificates curl wget gnupg

  log "Adding Grafana APT repository."
  install -d -m 0755 /etc/apt/keyrings
  wget -q -O /etc/apt/keyrings/grafana.asc https://apt.grafana.com/gpg-full.key
  chmod 0644 /etc/apt/keyrings/grafana.asc
  printf 'deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main\n' > /etc/apt/sources.list.d/grafana.list

  log "Installing monitoring packages: ${packages[*]}."
  apt-get update
  apt-get install -y "${packages[@]}"
}

write_prometheus_config() {
  local retention="${PROMETHEUS_RETENTION:-7d}"
  local listen_addr="${MONITORING_LISTEN_ADDRESS:-127.0.0.1}"

  log "Writing Prometheus config."
  install -d -m 0755 /etc/prometheus /var/lib/prometheus
  cat > /etc/prometheus/prometheus.yml <<'EOF'
global:
  scrape_interval: 15s
  evaluation_interval: 15s

alerting:
  alertmanagers: []

rule_files: []

# Collectors can replay queued samples after a brief ingest outage. The window
# would also hide two hosts sending identical series, so
# fleet-prometheus-out-of-order alerts when out-of-order samples keep arriving.
storage:
  tsdb:
    out_of_order_time_window: 30m

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets:
          - localhost:9090
EOF

  install -d -m 0755 /etc/systemd/system/prometheus.service.d
  cat > /etc/systemd/system/prometheus.service.d/monitoring.conf <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/var/lib/prometheus --storage.tsdb.retention.time=${retention} --web.enable-remote-write-receiver --web.enable-lifecycle --web.listen-address=${listen_addr}:9090
EOF

  if id -u prometheus >/dev/null 2>&1; then
    chown -R prometheus:prometheus /var/lib/prometheus /etc/prometheus
  fi
}

write_loki_config() {
  local listen_addr="${MONITORING_LISTEN_ADDRESS:-127.0.0.1}"

  log "Writing Loki config."
  install -d -m 0755 /etc/loki /var/lib/loki/chunks /var/lib/loki/rules /var/lib/loki/compactor
  cat > /etc/loki/loki-config.yml <<EOF
auth_enabled: false

server:
  http_listen_address: ${listen_addr}
  http_listen_port: 3100
  grpc_listen_port: 9096

common:
  instance_addr: 127.0.0.1
  path_prefix: /var/lib/loki
  storage:
    filesystem:
      chunks_directory: /var/lib/loki/chunks
      rules_directory: /var/lib/loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

query_range:
  results_cache:
    cache:
      embedded_cache:
        enabled: true
        max_size_mb: 100

schema_config:
  configs:
    - from: 2024-04-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  retention_period: ${LOKI_RETENTION_PERIOD:-168h}
  allow_structured_metadata: true
  volume_enabled: true
  # Defaults are 4MB/s ingest and 3MB/s per stream. Onboarding a whole fleet
  # at once means every collector backfills its journal simultaneously on
  # first start, which blows straight through those and 429s. Raise them
  # BEFORE rolling collectors out, not after it looks broken.
  ingestion_rate_mb: 16
  ingestion_burst_size_mb: 32
  per_stream_rate_limit: 8MB
  per_stream_rate_limit_burst: 16MB
  max_global_streams_per_user: 10000
  reject_old_samples: true
  reject_old_samples_max_age: 168h

compactor:
  working_directory: /var/lib/loki/compactor
  retention_enabled: true
  delete_request_store: filesystem

ruler:
  storage:
    type: local
    local:
      directory: /var/lib/loki/rules
EOF

  install -d -m 0755 /etc/systemd/system/loki.service.d
  cat > /etc/systemd/system/loki.service.d/monitoring.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/loki -config.file=/etc/loki/loki-config.yml
EOF

  groupadd --system loki 2>/dev/null || true
  if id -u loki >/dev/null 2>&1; then
    usermod -g loki loki 2>/dev/null || true
  else
    log "Creating loki user."
    useradd --system --gid loki --no-create-home --shell /usr/sbin/nologin loki 2>/dev/null || true
  fi
  chown -R loki:loki /var/lib/loki /etc/loki
}

write_grafana_config() {
  local env_file="/etc/default/grafana-monitoring"
  local listen_addr="${MONITORING_LISTEN_ADDRESS:-127.0.0.1}"
  local dashboard
  local dashboard_dir

  log "Writing Grafana provisioning config."
  # fleet/ and servers/ are the two provider paths in
  # grafana/provisioning/dashboards/dashboards.yml, and they are deliberately
  # disjoint siblings: Grafana scans each path recursively, so a provider at
  # the parent would double-provision everything below it.
  install -d -m 0755 \
    /etc/grafana/provisioning/datasources \
    /etc/grafana/provisioning/dashboards \
    /var/lib/grafana/dashboards \
    /var/lib/grafana/dashboards/fleet \
    /var/lib/grafana/dashboards/servers \
    /etc/systemd/system/grafana-server.service.d

  cat > /etc/grafana/provisioning/datasources/datasources.yml <<'EOF'
apiVersion: 1

datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: true
    editable: true
    jsonData:
      timeInterval: 15s
      queryTimeout: 60s

  - name: Loki
    uid: loki
    type: loki
    access: proxy
    url: http://localhost:3100
    editable: true
    jsonData:
      maxLines: 1000
EOF

  install -m 0644 "$ROOT_DIR/grafana/provisioning/dashboards/dashboards.yml" /etc/grafana/provisioning/dashboards/dashboards.yml

  shopt -s nullglob
  for dashboard_dir in fleet servers; do
    for dashboard in "$ROOT_DIR/grafana/dashboards/$dashboard_dir"/*.json; do
      install -m 0644 "$dashboard" \
        "/var/lib/grafana/dashboards/$dashboard_dir/$(basename "$dashboard")"
    done
  done
  shopt -u nullglob

  : > "$env_file"
  chmod 0600 "$env_file"
  write_env_assignment "$env_file" GF_SECURITY_ADMIN_USER "${GRAFANA_ADMIN_USER:-admin}"
  write_env_assignment "$env_file" GF_SECURITY_ADMIN_PASSWORD "${GRAFANA_ADMIN_PASSWORD}"
  write_env_assignment "$env_file" GF_SERVER_ROOT_URL "${GRAFANA_ROOT_URL:-http://localhost:3000}"
  write_env_assignment "$env_file" GF_SERVER_HTTP_ADDR "$listen_addr"
  write_env_assignment "$env_file" GF_AUTH_ANONYMOUS_ENABLED "false"
  write_env_assignment "$env_file" GF_USERS_ALLOW_SIGN_UP "false"
  write_env_assignment "$env_file" GF_SECURITY_COOKIE_SECURE "${GRAFANA_COOKIE_SECURE:-true}"
  write_env_assignment "$env_file" GF_SECURITY_COOKIE_SAMESITE "strict"
  write_env_assignment "$env_file" GF_SECURITY_DISABLE_GRAVATAR "true"
  write_env_assignment "$env_file" GF_SNAPSHOTS_EXTERNAL_ENABLED "false"

  # Consumed by $__env{} in grafana/provisioning/alerting/contactpoints.yaml.
  # Kept out of git by living only in this 0600 file.
  write_env_assignment "$env_file" MATRIX_WEBHOOK_API_KEY "${MATRIX_WEBHOOK_API_KEY:-}"
  write_env_assignment "$env_file" MATRIX_ALERT_ROOM_ID "${MATRIX_ALERT_ROOM_ID:-}"

  cat > /etc/systemd/system/grafana-server.service.d/monitoring.conf <<'EOF'
[Service]
EnvironmentFile=-/etc/default/grafana-monitoring
EOF

  if id -u grafana >/dev/null 2>&1; then
    chown -R grafana:grafana /var/lib/grafana/dashboards
  fi

  log "Verifying Grafana admin credentials."
  local admin_user admin_pass
  admin_user="$(grep '^GF_SECURITY_ADMIN_USER=' "$env_file" | cut -d= -f2 | tr -d '"' | tr -d ' ')"
  admin_pass="$(grep '^GF_SECURITY_ADMIN_PASSWORD=' "$env_file" | cut -d= -f2 | tr -d '"' | tr -d ' ')"
  if [[ -z "$admin_user" || -z "$admin_pass" ]]; then
    fail "Grafana admin credentials not found in $env_file. Check that GRAFANA_ADMIN_USER and GRAFANA_ADMIN_PASSWORD were set."
  fi
  log "Grafana admin user: '$admin_user'"
  log "Grafana admin password: '***'"
  log "Login URL: ${GRAFANA_ROOT_URL:-http://localhost:3000}"
}

write_grafana_alerting_config() {
  local file
  local found=0

  log "Writing Grafana unified alerting provisioning."
  install -d -m 0755 /etc/grafana/provisioning/alerting

  shopt -s nullglob
  for file in "$ROOT_DIR"/grafana/provisioning/alerting/*.yaml \
              "$ROOT_DIR"/grafana/provisioning/alerting/*.yml; do
    install -m 0644 "$file" "/etc/grafana/provisioning/alerting/$(basename "$file")"
    found=1
  done
  shopt -u nullglob

  if [[ "$found" -eq 0 ]]; then
    log "No alerting provisioning files found in the repo; skipping."
  fi

  # rules-availability.yaml is intentionally absent from the repo: it is
  # rendered from the Ansible inventory by roles/grafana_alerting, because
  # the host-down expression needs a per-host constant floor for every
  # machine in the fleet. See that role's template for why.
  if [[ ! -e /etc/grafana/provisioning/alerting/rules-availability.yaml ]]; then
    log "NOTE: host-down alerting is not installed yet. Run:"
    log "      ansible-playbook playbooks/central-alerting.yml"
  fi

  if id -u grafana >/dev/null 2>&1; then
    chown -R root:grafana /etc/grafana/provisioning/alerting
  fi
}

write_alloy_config() {
  local mode="$1"
  local env_file="/etc/default/alloy"
  local host_name="${MONITOR_HOSTNAME:-$(hostname -s)}"
  local host_role
  local prometheus_url
  local loki_url

  case "$mode" in
    central)
      host_role="${MONITOR_ROLE:-central-lxc}"
      prometheus_url="http://127.0.0.1:9090/api/v1/write"
      loki_url="http://127.0.0.1:3100/loki/api/v1/push"
      ;;
    collector)
      host_role="${MONITOR_ROLE:-lxc}"
      prometheus_url="$(collector_prometheus_remote_write_url)"
      loki_url="$(collector_loki_write_url)"
      ;;
    *)
      fail "Unknown LXC mode: $mode"
      ;;
  esac

  # Ansible (ansible/roles/alloy_collector) is the single source of truth for
  # every native Alloy config in the fleet. Without this guard,
  # `lxc-update.sh central` -> sync_configs() -> lxc-install.sh --config-only
  # would silently revert an Ansible-managed config back to the heredoc below,
  # dropping the textfile collector and the per-host tuning with it.
  if [[ -e /etc/alloy/.ansible-managed ]]; then
    log "Alloy config is Ansible-managed (/etc/alloy/.ansible-managed present); skipping."
    log "To change it: ansible-playbook playbooks/collectors.yml --limit \$(hostname -s)"
    return 0
  fi

  log "Writing Alloy config for $mode mode."
  install -d -m 0755 /etc/alloy /var/lib/alloy /var/lib/node_exporter/textfile_collector

  cat > /etc/alloy/config.alloy <<'EOF'
logging {
  level  = "info"
  format = "logfmt"
}

prometheus.remote_write "central" {
  external_labels = {
    host = sys.env("MONITOR_HOSTNAME"),
    role = sys.env("MONITOR_ROLE"),
  }

  endpoint {
    name = "central-prometheus"
    url  = sys.env("PROMETHEUS_REMOTE_WRITE_URL")

    basic_auth {
      username = sys.env("COLLECTOR_BASIC_AUTH_USER")
      password = sys.env("COLLECTOR_BASIC_AUTH_PASSWORD")
    }
  }
}

prometheus.exporter.unix "host" {
  enable_collectors = ["systemd", "processes"]

  // Kept in parity with ansible/roles/alloy_collector/templates/config.alloy.j2
  // so a fresh bootstrap is not missing update metrics. Ansible owns this
  // file once /etc/alloy/.ansible-managed exists.
  textfile {
    directory = "/var/lib/node_exporter/textfile_collector"
  }

  filesystem {
    mount_points_exclude = "^/(dev|proc|run/credentials/.+|sys|var/lib/.+)($|/)"
  }

  systemd {
    enable_restarts = true
    start_time      = true
    task_metrics    = true
    unit_exclude    = ".+\\.(automount|device|mount|scope|slice)"
  }
}

prometheus.scrape "host" {
  job_name        = "host-unix"
  targets         = prometheus.exporter.unix.host.targets
  scrape_interval = "15s"
  forward_to      = [prometheus.remote_write.central.receiver]
}

prometheus.scrape "alloy" {
  job_name   = "alloy"
  targets    = [{"__address__" = "127.0.0.1:12345"}]
  forward_to = [prometheus.relabel.alloy_host.receiver]
}

prometheus.relabel "alloy_host" {
  forward_to = [prometheus.remote_write.central.receiver]

  // Keep the write destination before overwriting `host`, so the loki_write_*
  // metrics still show which ingest URL each collector is using.
  rule {
    source_labels = ["host"]
    regex         = "(.+)"
    target_label  = "destination_host"
  }

  rule {
    target_label = "host"
    replacement  = sys.env("MONITOR_HOSTNAME")
  }
}

loki.write "central" {
  external_labels = {
    host = sys.env("MONITOR_HOSTNAME"),
    role = sys.env("MONITOR_ROLE"),
  }

  endpoint {
    name = "central-loki"
    url  = sys.env("LOKI_WRITE_URL")

    basic_auth {
      username = sys.env("COLLECTOR_BASIC_AUTH_USER")
      password = sys.env("COLLECTOR_BASIC_AUTH_PASSWORD")
    }
  }
}

loki.relabel "journal" {
  forward_to = []

  rule {
    source_labels = ["__journal__systemd_unit"]
    target_label  = "unit"
  }

  rule {
    source_labels = ["__journal_priority_keyword"]
    target_label  = "level"
  }
}

loki.source.journal "system" {
  max_age       = "12h"
  labels        = {job = "systemd-journal", source = "journal"}
  relabel_rules = loki.relabel.journal.rules
  forward_to    = [loki.write.central.receiver]
}
EOF

  if [[ -S /var/run/docker.sock ]]; then
    cat >> /etc/alloy/config.alloy <<'EOF'

prometheus.exporter.cadvisor "containers" {
  docker_host      = "unix:///var/run/docker.sock"
  docker_only      = true
  storage_duration = "5m"
}

prometheus.scrape "containers" {
  job_name        = "container-cadvisor"
  targets         = prometheus.exporter.cadvisor.containers.targets
  scrape_interval = "15s"
  forward_to      = [prometheus.remote_write.central.receiver]
}

discovery.docker "containers" {
  host = "unix:///var/run/docker.sock"
}

discovery.relabel "docker_logs" {
  targets = discovery.docker.containers.targets

  rule {
    source_labels = ["__meta_docker_container_name"]
    regex         = "/(.*)"
    target_label  = "container"
  }

  rule {
    source_labels = ["__meta_docker_container_id"]
    target_label  = "container_id"
  }

  rule {
    source_labels = ["__meta_docker_container_label_com_docker_compose_service"]
    target_label  = "compose_service"
  }

  rule {
    target_label = "source"
    replacement  = "docker"
  }
}

loki.source.docker "containers" {
  host       = "unix:///var/run/docker.sock"
  labels     = {job = "docker"}
  targets    = discovery.relabel.docker_logs.output
  forward_to = [loki.write.central.receiver]
}
EOF
  fi

  : > "$env_file"
  chmod 0600 "$env_file"
  write_env_assignment "$env_file" CONFIG_FILE "/etc/alloy/config.alloy"
  write_env_assignment "$env_file" CUSTOM_ARGS "--server.http.listen-addr=127.0.0.1:12345"
  write_env_assignment "$env_file" MONITOR_HOSTNAME "$host_name"
  write_env_assignment "$env_file" MONITOR_ROLE "$host_role"
  write_env_assignment "$env_file" PROMETHEUS_REMOTE_WRITE_URL "$prometheus_url"
  write_env_assignment "$env_file" LOKI_WRITE_URL "$loki_url"
  write_env_assignment "$env_file" COLLECTOR_BASIC_AUTH_USER "${COLLECTOR_BASIC_AUTH_USER:-collector}"
  write_env_assignment "$env_file" COLLECTOR_BASIC_AUTH_PASSWORD "$COLLECTOR_BASIC_AUTH_PASSWORD"

  if id -u alloy >/dev/null 2>&1; then
    chown -R alloy:alloy /var/lib/alloy /etc/alloy
    getent group systemd-journal >/dev/null 2>&1 && usermod -aG systemd-journal alloy || true
    getent group adm >/dev/null 2>&1 && usermod -aG adm alloy || true
    getent group docker >/dev/null 2>&1 && usermod -aG docker alloy || true
  fi
}


write_nginx_config() {
  local domain="${PUBLIC_DOMAIN:-_}"
  local listen_addr="${MONITORING_LISTEN_ADDRESS:-127.0.0.1}"
  local auth_user="${COLLECTOR_BASIC_AUTH_USER:-collector}"
  local htpasswd_file="/etc/nginx/monitoring-collectors.htpasswd"

  log "Writing nginx public reverse proxy config."
  has_cmd htpasswd || fail "htpasswd is required. Install apache2-utils or run scripts/lxc-install.sh without --config-only first."
  has_cmd nginx || fail "nginx is required. Install nginx or run scripts/lxc-install.sh without --config-only first."
  install -d -m 0755 /etc/nginx/sites-available /etc/nginx/sites-enabled
  htpasswd -bcB "$htpasswd_file" "$auth_user" "$COLLECTOR_BASIC_AUTH_PASSWORD" >/dev/null
  chmod 0640 "$htpasswd_file"
  chown root:www-data "$htpasswd_file" 2>/dev/null || chown root:root "$htpasswd_file"

  cat > /etc/nginx/sites-available/monitoring.conf <<EOF
server {
  listen 80;
  server_name ${domain};

  client_max_body_size 50m;

  add_header X-Content-Type-Options "nosniff" always;
  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header Referrer-Policy "strict-origin-when-cross-origin" always;

  location /api/live/ {
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_pass http://${listen_addr}:3000;
  }

  location /prometheus/ {
    auth_basic "collector metrics ingest";
    auth_basic_user_file ${htpasswd_file};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_pass http://${listen_addr}:9090/;
  }

  location /loki/ {
    auth_basic "collector logs ingest";
    auth_basic_user_file ${htpasswd_file};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_pass http://${listen_addr}:3100;
  }

  location / {
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_pass http://${listen_addr}:3000;
  }
}
EOF

  # Private HTTPS for collectors on the LAN (monitoring_ingest_base_url).
  # Port 80 above is for the Cloudflare tunnel, which connects over
  # localhost; LAN collectors use this listener so their Basic Auth
  # credential is never sent in cleartext. The certificate is signed by the
  # homelab CA and must list every private address collectors use.
  local tls_dir="/etc/nginx/tls"
  if [[ -f "$tls_dir/monitoring-ingest.crt" && -f "$tls_dir/monitoring-ingest.key" ]]; then
    log "Adding the private HTTPS ingest listener."
    cat >> /etc/nginx/sites-available/monitoring.conf <<EOF

server {
  listen 443 ssl;
  server_name _;

  ssl_certificate     ${tls_dir}/monitoring-ingest.crt;
  ssl_certificate_key ${tls_dir}/monitoring-ingest.key;
  ssl_protocols       TLSv1.2 TLSv1.3;

  client_max_body_size 50m;

  location /prometheus/ {
    auth_basic "collector metrics ingest";
    auth_basic_user_file ${htpasswd_file};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_pass http://${listen_addr}:9090/;
  }

  location /loki/ {
    auth_basic "collector logs ingest";
    auth_basic_user_file ${htpasswd_file};
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_pass http://${listen_addr}:3100;
  }

  location / {
    return 404;
  }
}
EOF
  fi

  rm -f /etc/nginx/sites-enabled/default
  ln -sfn /etc/nginx/sites-available/monitoring.conf /etc/nginx/sites-enabled/monitoring.conf
  nginx -t
}

services_for_mode() {
  case "$1" in
    central) printf '%s\n' prometheus loki grafana-server alloy nginx ;;
    collector) printf '%s\n' alloy ;;
    *) fail "Unknown LXC mode: $1" ;;
  esac
}

packages_for_mode() {
  case "$1" in
    central) printf '%s\n' "${CENTRAL_PACKAGES[@]}" ;;
    collector) printf '%s\n' "${COLLECTOR_PACKAGES[@]}" ;;
    *) fail "Unknown LXC mode: $1" ;;
  esac
}

enable_and_restart_services() {
  local services
  mapfile -t services < <(services_for_mode "$LXC_MODE")

  log "Enabling and restarting services: ${services[*]}."
  systemctl daemon-reload
  systemctl enable "${services[@]}"
  systemctl restart "${services[@]}"
}

render_configs() {
  load_env_file

  case "$LXC_MODE" in
    central)
      validate_central_env
      write_prometheus_config
      write_loki_config
      write_grafana_config
      write_grafana_alerting_config
      write_alloy_config central
      write_nginx_config
      ;;
    collector)
      validate_collector_env
      write_alloy_config collector
      ;;
    *)
      fail "Unknown LXC mode: $LXC_MODE"
      ;;
  esac
}

main() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      central|collector) LXC_MODE="$1" ;;
      --config-only) CONFIG_ONLY=1 ;;
      --no-start) NO_START=1 ;;
      -h|--help) usage; exit 0 ;;
      *) usage; fail "Unknown option: $1" ;;
    esac
    shift
  done

  require_root
  cd "$ROOT_DIR"

  if [[ "$CONFIG_ONLY" -eq 0 ]]; then
    local packages
    mapfile -t packages < <(packages_for_mode "$LXC_MODE")
    install_packages "${packages[@]}"
  fi

  render_configs

  if [[ "$NO_START" -eq 0 ]]; then
    enable_and_restart_services
  else
    systemctl daemon-reload
    log "Configs written. Services were not started because --no-start was used."
  fi

  case "$LXC_MODE" in
    central)
      log "LXC monitoring stack is ready. nginx listens on port 80 and proxies Grafana plus authenticated collector ingest paths."
      ;;
    collector)
      log "LXC collector is ready. Alloy is configured to push metrics and logs to the central ingest endpoints."
      ;;
  esac
}

main "$@"
