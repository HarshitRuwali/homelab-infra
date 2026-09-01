#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SCRIPT="$ROOT_DIR/scripts/lxc-install.sh"

CENTRAL_PACKAGES=(grafana prometheus loki alloy nginx apache2-utils)
COLLECTOR_PACKAGES=(alloy)
LXC_MODE=central
CONFIG_ONLY=0
NO_RESTART=0
SKIP_PACKAGE_UPGRADE=0

usage() {
  cat <<'USAGE'
Usage:
  scripts/lxc-update.sh [central|collector] [--config-only] [--skip-package-upgrade] [--no-restart]

Updates a direct LXC monitoring install created by scripts/lxc-install.sh.
Central mode is the default; collector mode updates an Alloy-only LXC collector.

What it does:
  - Upgrades the packages for the selected mode unless skipped.
  - Re-syncs Grafana dashboards/provisioning for central mode.
  - Re-renders configs for the selected LXC layout.
  - Restarts the selected mode's services unless --no-restart is used.

Options:
  --config-only           Do not upgrade packages; only sync config.
  --skip-package-upgrade  Same as --config-only for package handling.
  --no-restart           Write config but do not restart services.
USAGE
}

log() {
  printf '[lxc-update] %s\n' "$*"
}

fail() {
  printf '[lxc-update] ERROR: %s\n' "$*" >&2
  exit 1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  [[ "${EUID}" -eq 0 ]] || fail "Run this script as root inside the LXC."
  has_cmd apt-get || fail "apt-get is required. Use a Debian/Ubuntu LXC."
  has_cmd dpkg || fail "dpkg is required. Use a Debian/Ubuntu LXC."
  has_cmd systemctl || fail "systemctl is required. Use a systemd-based LXC."
  [[ -x "$INSTALL_SCRIPT" ]] || fail "Missing executable installer: $INSTALL_SCRIPT"
}

packages_for_mode() {
  case "$1" in
    central) printf '%s\n' "${CENTRAL_PACKAGES[@]}" ;;
    collector) printf '%s\n' "${COLLECTOR_PACKAGES[@]}" ;;
    *) fail "Unknown LXC mode: $1" ;;
  esac
}

services_for_mode() {
  case "$1" in
    central) printf '%s\n' prometheus loki grafana-server alloy nginx ;;
    collector) printf '%s\n' alloy ;;
    *) fail "Unknown LXC mode: $1" ;;
  esac
}

require_installed_packages() {
  local package
  local packages
  mapfile -t packages < <(packages_for_mode "$LXC_MODE")

  for package in "${packages[@]}"; do
    dpkg -s "$package" >/dev/null 2>&1 || fail "$package is not installed. Run scripts/lxc-install.sh $LXC_MODE first."
  done
}

upgrade_packages() {
  local packages
  mapfile -t packages < <(packages_for_mode "$LXC_MODE")

  export DEBIAN_FRONTEND=noninteractive

  require_installed_packages
  log "Upgrading monitoring packages: ${packages[*]}."
  apt-get update
  apt-get install --only-upgrade -y "${packages[@]}"
}

sync_configs() {
  local args=("$LXC_MODE" --config-only --no-start)

  log "Re-syncing LXC configs from repository for $LXC_MODE mode."
  "$INSTALL_SCRIPT" "${args[@]}"
}

restart_services() {
  local services

  if [[ "$NO_RESTART" -eq 1 ]]; then
    log "Services were not restarted because --no-restart was used."
    return
  fi

  mapfile -t services < <(services_for_mode "$LXC_MODE")
  log "Restarting monitoring services: ${services[*]}."
  systemctl restart "${services[@]}"
}

main() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      central|collector) LXC_MODE="$1" ;;
      --config-only) CONFIG_ONLY=1 ;;
      --skip-package-upgrade) SKIP_PACKAGE_UPGRADE=1 ;;
      --no-restart) NO_RESTART=1 ;;
      -h|--help) usage; exit 0 ;;
      *) usage; fail "Unknown option: $1" ;;
    esac
    shift
  done

  require_root
  cd "$ROOT_DIR"

  if [[ "$CONFIG_ONLY" -eq 0 && "$SKIP_PACKAGE_UPGRADE" -eq 0 ]]; then
    upgrade_packages
  fi

  sync_configs
  restart_services
  log "LXC $LXC_MODE update complete."
}

main "$@"
