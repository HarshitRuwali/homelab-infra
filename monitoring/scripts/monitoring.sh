#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CENTRAL_COMPOSE="$ROOT_DIR/docker-compose.yml"
COLLECTOR_COMPOSE="$ROOT_DIR/docker-compose.collector.yml"
ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.example"

CENTRAL_VOLUMES=(
  monitoring-grafana-data
  monitoring-prometheus-data
  monitoring-loki-data
  monitoring-alloy-data
)
COLLECTOR_VOLUMES=(monitoring-alloy-data)

usage() {
  cat <<'USAGE'
Usage:
  scripts/monitoring.sh central <init|validate|up|down|restart|status|logs>
  scripts/monitoring.sh collector <init|validate|up|down|restart|status|logs>

Central examples:
  cp .env.example .env
  $EDITOR .env
  scripts/monitoring.sh central up

Collector examples:
  export MONITORING_SERVER=192.168.1.10
  export MONITOR_HOSTNAME=vm-01
  scripts/monitoring.sh collector up

Actions:
  init      Create required external Docker volumes only.
  validate  Validate environment and Docker Compose config.
  up        Create volumes, validate config, and start services.
  down      Stop and remove containers/networks. External volumes remain.
  restart   Restart services.
  status    Show compose service status.
  logs      Follow compose logs.
USAGE
}

log() {
  printf '[monitoring] %s\n' "$*"
}

fail() {
  printf '[monitoring] ERROR: %s\n' "$*" >&2
  exit 1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif has_cmd docker-compose; then
    docker-compose "$@"
  else
    fail "Docker Compose is not available. Install Docker Compose v2 or docker-compose."
  fi
}

require_docker() {
  has_cmd docker || fail "Docker is not installed or not in PATH."
  docker info >/dev/null 2>&1 || fail "Docker daemon is not reachable by this user."
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

create_env_if_missing() {
  if [[ ! -f "$ENV_FILE" && -f "$ENV_EXAMPLE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    log "Created .env from .env.example. Edit .env before starting the central stack."
  fi
}

require_var() {
  local name="$1"
  local value="${!name:-}"
  [[ -n "$value" ]] || fail "$name is required. Set it in .env or export it before running this command."
}

require_central_env() {
  load_env_file
  require_var GRAFANA_ADMIN_PASSWORD
  require_var COLLECTOR_BASIC_AUTH_PASSWORD
  if [[ "${GRAFANA_ADMIN_PASSWORD}" == "replace-with-a-strong-password" || "${GRAFANA_ADMIN_PASSWORD}" == "<strong-password>" ]]; then
    fail "Set a real GRAFANA_ADMIN_PASSWORD in .env before starting the central stack."
  fi
  if [[ "${COLLECTOR_BASIC_AUTH_PASSWORD}" == "replace-with-a-strong-collector-password" || "${COLLECTOR_BASIC_AUTH_PASSWORD}" == "<strong-password>" ]]; then
    fail "Set a real COLLECTOR_BASIC_AUTH_PASSWORD in .env before starting the central stack."
  fi
}

require_collector_env() {
  load_env_file
  require_var MONITOR_HOSTNAME
  require_var COLLECTOR_BASIC_AUTH_PASSWORD

  if [[ -z "${MONITORING_SERVER:-}" && ( -z "${PROMETHEUS_REMOTE_WRITE_URL:-}" || -z "${LOKI_WRITE_URL:-}" ) ]]; then
    fail "Set MONITORING_SERVER, or set both PROMETHEUS_REMOTE_WRITE_URL and LOKI_WRITE_URL."
  fi
}

create_volumes() {
  local volume
  for volume in "$@"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      log "Volume exists: $volume"
    else
      docker volume create "$volume" >/dev/null
      log "Created volume: $volume"
    fi
  done
}

compose_file_for_mode() {
  case "$1" in
    central) printf '%s\n' "$CENTRAL_COMPOSE" ;;
    collector) printf '%s\n' "$COLLECTOR_COMPOSE" ;;
    *) fail "Unknown mode: $1" ;;
  esac
}

init_mode() {
  local mode="$1"
  require_docker
  case "$mode" in
    central)
      create_env_if_missing
      create_volumes "${CENTRAL_VOLUMES[@]}"
      ;;
    collector)
      create_volumes "${COLLECTOR_VOLUMES[@]}"
      ;;
  esac
}

validate_mode() {
  local mode="$1"
  local compose_file
  require_docker
  compose_file="$(compose_file_for_mode "$mode")"
  case "$mode" in
    central) require_central_env ;;
    collector) require_collector_env ;;
  esac
  compose -f "$compose_file" config >/dev/null
  log "Compose config is valid for $mode."
}

compose_action() {
  local mode="$1"
  local action="$2"
  local compose_file
  compose_file="$(compose_file_for_mode "$mode")"

  case "$action" in
    up)
      init_mode "$mode"
      validate_mode "$mode"
      compose -f "$compose_file" up -d
      ;;
    down)
      require_docker
      compose -f "$compose_file" down
      log "Stopped $mode stack. External volumes were kept."
      ;;
    restart)
      require_docker
      validate_mode "$mode"
      compose -f "$compose_file" restart
      ;;
    status)
      require_docker
      compose -f "$compose_file" ps
      ;;
    logs)
      require_docker
      compose -f "$compose_file" logs -f
      ;;
    *)
      fail "Unknown action: $action"
      ;;
  esac
}

main() {
  local mode="${1:-}"
  local action="${2:-}"

  if [[ -z "$mode" || -z "$action" || "$mode" == "-h" || "$mode" == "--help" ]]; then
    usage
    exit 0
  fi

  cd "$ROOT_DIR"

  case "$mode" in
    central|collector) ;;
    *) usage; fail "Mode must be 'central' or 'collector'." ;;
  esac

  case "$action" in
    init) init_mode "$mode" ;;
    validate) validate_mode "$mode" ;;
    up|down|restart|status|logs) compose_action "$mode" "$action" ;;
    *) usage; fail "Unsupported action: $action" ;;
  esac
}

main "$@"
