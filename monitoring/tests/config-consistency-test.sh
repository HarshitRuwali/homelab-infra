#!/usr/bin/env bash
# Guards the log-volume, retention, pinning and refresh settings against drift
# between the installer, the Compose files, the docs and the dashboards.
# Run from anywhere: monitoring/tests/config-consistency-test.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
fails=0

check() {
  local desc="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'ok   %s\n' "$desc"
  else
    printf 'FAIL %s\n' "$desc"
    fails=$((fails + 1))
  fi
}

has() { grep -qE -- "$1" "$2"; }
lacks() { ! grep -qE -- "$1" "$2"; }

# Grafana telemetry and log level: installer and Compose agree.
for key in ANALYTICS_REPORTING_ENABLED ANALYTICS_CHECK_FOR_UPDATES ANALYTICS_CHECK_FOR_PLUGIN_UPDATES; do
  check "installer disables GF_$key" has "GF_$key \"false\"" scripts/lxc-install.sh
  check "compose disables GF_$key" has "GF_$key: \"false\"" docker-compose.yml
done
check "installer sets Grafana log level warn" has 'GF_LOG_LEVEL "warn"' scripts/lxc-install.sh
check "compose sets Grafana log level warn" has 'GF_LOG_LEVEL: warn' docker-compose.yml

# Loki log level in both configs.
check "installer Loki log_level warn" has '^  log_level: warn' scripts/lxc-install.sh
check "compose Loki log_level warn" has '^  log_level: warn' loki/loki-config.yml

# Loki retention is 720h everywhere it is stated.
check "installer default retention 720h" has 'LOKI_RETENTION_PERIOD:-720h' scripts/lxc-install.sh
check "installer usage names Loki retention" has 'LOKI_RETENTION_PERIOD +default: 720h' scripts/lxc-install.sh
check "loki-config retention 720h" has 'retention_period: 720h' loki/loki-config.yml
check ".env.example retention 720h" has '^LOKI_RETENTION_PERIOD=720h' .env.example
check "retention doc names 720h" has '720h' docs/operations/retention.md
check "no stale 168h retention in docs" lacks 'retention.*168h|168h.*\(7d\)' docs/operations/retention.md

# Prometheus scrapes Loki and Grafana, dropping histogram buckets.
for f in scripts/lxc-install.sh prometheus/prometheus.yml; do
  check "$f scrapes loki" has 'job_name: loki' "$f"
  check "$f scrapes grafana" has 'job_name: grafana' "$f"
  check "$f drops buckets" has "regex: '\\.\\+_bucket'" "$f"
done

# Images are pinned.
check "compose has no :latest" lacks ':latest' docker-compose.yml
check "collector compose has no :latest" lacks ':latest' docker-compose.collector.yml

# No dashboard refreshes faster than 30s.
check "no dashboard refreshes at 10s or faster" \
  bash -c '! grep -rE "^  \"refresh\": \"([0-9]|10)s\"" grafana/dashboards'

# Links into the install page must not use anchors that do not exist.
check "getting-started has no install.md anchors" lacks 'install\.md#' docs/getting-started/index.md

# Prometheus config is valid (needs docker; skipped if unavailable). Both the
# Compose file and the config the installer writes into the LXC are checked.
if command -v docker >/dev/null 2>&1 && docker image inspect prom/prometheus:v2.53.3 >/dev/null 2>&1; then
  check "promtool accepts prometheus.yml" \
    docker run --rm --entrypoint promtool -v "$ROOT/prometheus:/p:ro" prom/prometheus:v2.53.3 check config /p/prometheus.yml
  promdir="$(mktemp -d)"
  chmod 0755 "$promdir"
  sed -n "/prometheus\.yml <<'EOF'/,/^EOF\$/p" scripts/lxc-install.sh | sed '1d;$d' > "$promdir/prometheus.yml"
  chmod 0644 "$promdir/prometheus.yml"
  check "installer heredoc is not empty" test -s "$promdir/prometheus.yml"
  check "promtool accepts the installer's prometheus.yml" \
    docker run --rm --entrypoint promtool -v "$promdir:/p:ro" prom/prometheus:v2.53.3 check config /p/prometheus.yml
  rm -rf "$promdir"
else
  echo "skip promtool check (docker image prom/prometheus:v2.53.3 not present)"
fi

if [[ "$fails" -ne 0 ]]; then
  printf '%s check(s) failed\n' "$fails"
  exit 1
fi
echo "all checks passed"
