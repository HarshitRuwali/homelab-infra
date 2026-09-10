# shellcheck shell=bash
# Shared helpers for the install-*.sh scripts. Sourced, not executed.
set -euo pipefail

APPLY=${APPLY:-0}
[[ "${1:-}" == "--apply" ]] && APPLY=1

c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
ok()   { printf '%s  ok%s   %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s warn%s  %s\n' "$c_yel" "$c_off" "$*"; }
die()  { printf '%s fail%s  %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
run()  { if (( APPLY )); then "$@"; else printf '%s       + %s%s\n' "$c_dim" "$*" "$c_off"; fi; }

require_root() { [[ $EUID -eq 0 ]] || die "run as root inside the guest"; }
banner() {
  if (( APPLY )); then printf 'MODE: %sAPPLY%s\n\n' "$c_red" "$c_off"
  else printf 'MODE: %sDRY RUN%s, add --apply to execute\n\n' "$c_grn" "$c_off"; fi
}
