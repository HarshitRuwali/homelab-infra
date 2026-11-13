#!/usr/bin/env bash
# Nightly cleanup on sec-wazuh: the vulnerability feed updater's scratch space,
# and gzipped alert archives older than a retention (default 90 days). Safe to run on an existing manager: it installs a timer and touches nothing
# else. Dry run by default; --apply to install.
#
# WHY: wazuh-modulesd downloads each vulnerability feed update into
# /var/ossec/queue/vd_updater/tmp and unpacks it there, and does not reliably
# clean up afterwards. On 2026-09-23 that directory had grown to 9 GB next to an
# 11 GB feed, a download then failed with "Failed writing received data to
# disk", the 25 GB root filled, wazuh-modulesd could not even write its PID
# file, and Alloy on the guest stopped reporting (Host Down in Grafana).
source "$(dirname "$0")/_common.sh"
require_root; banner

VD_TMP=/var/ossec/queue/vd_updater/tmp
if (( APPLY )); then
  [[ -d "$VD_TMP" ]] || die "$VD_TMP not found; install the Wazuh manager first"
fi

write_file /usr/local/sbin/wazuh-vd-tmp-cleanup <<'EOF'
#!/usr/bin/env bash
# Managed by security/install/configure-sec-wazuh-cleanup.sh. Do not edit here.
#
# 1. Deletes vulnerability feed updater scratch files that have not been
#    written for MIN_AGE_MIN minutes. The age floor is the safety: a download or
#    extraction in progress keeps writing its files, so this never removes one
#    out from under a running update. Anything deleted is re-downloaded by
#    Wazuh the next time it needs it.
# 2. Deletes rotated alert archives (ossec-alerts-*.json.gz and .log.gz under
#    /var/ossec/logs/alerts/<year>/<month>/) older than ALERT_RETENTION_DAYS
#    (default 90; 0 keeps them forever). Wazuh never prunes these, about
#    3.4 MB a day. The live alerts.json and alerts.log are never touched. The
#    indexer keeps the searchable copy, so this only drops the on-disk archive.
#
# Usage: wazuh-vd-tmp-cleanup [--dry-run]   (lists what it would delete)
# Environment overrides: MIN_AGE_MIN, ALERT_RETENTION_DAYS, TMP, ALERTS.
set -euo pipefail
TMP=${TMP:-/var/ossec/queue/vd_updater/tmp}
ALERTS=${ALERTS:-/var/ossec/logs/alerts}
MIN_AGE_MIN=${MIN_AGE_MIN:-360}
ALERT_RETENTION_DAYS=${ALERT_RETENTION_DAYS:-90}
DRY=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY=1 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac
[[ "$MIN_AGE_MIN" =~ ^[0-9]+$ && "$ALERT_RETENTION_DAYS" =~ ^[0-9]+$ ]] \
  || { echo "MIN_AGE_MIN and ALERT_RETENTION_DAYS must be integers" >&2; exit 2; }
if (( DRY )); then act=(-print); verb="would remove"; else act=(-print -delete); verb=removed; fi

if [[ -d "$TMP" ]]; then
  before=$(du -sb "$TMP" | cut -f1)
  count=$(find "$TMP" -mindepth 2 -type f -mmin +"$MIN_AGE_MIN" "${act[@]}" | wc -l)
  after=$(du -sb "$TMP" | cut -f1)
  echo "feed scratch: $verb $count file(s), freed $(( (before - after) / 1048576 )) MiB; $(( after / 1048576 )) MiB left in $TMP"
else
  echo "no $TMP, nothing to do for feed scratch"
fi

if (( ALERT_RETENTION_DAYS == 0 )); then
  echo "alert archives: retention disabled (ALERT_RETENTION_DAYS=0)"
elif [[ -d "$ALERTS" ]]; then
  old=$(find "$ALERTS" -mindepth 3 -type f -name 'ossec-alerts-*.gz' -mtime +"$ALERT_RETENTION_DAYS" "${act[@]}" | wc -l)
  (( DRY )) || find "$ALERTS" -mindepth 1 -type d -empty -delete
  echo "alert archives: $verb $old file(s) older than $ALERT_RETENTION_DAYS days"
else
  echo "no $ALERTS, nothing to do for alert archives"
fi
EOF
run chmod 0755 /usr/local/sbin/wazuh-vd-tmp-cleanup

write_file /etc/systemd/system/wazuh-vd-tmp-cleanup.service <<'EOF'
[Unit]
Description=Remove stale Wazuh feed scratch files and old alert archives
Documentation=file:///usr/local/sbin/wazuh-vd-tmp-cleanup
ConditionPathIsDirectory=/var/ossec/logs/alerts

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/wazuh-vd-tmp-cleanup
Nice=10
IOSchedulingClass=idle
EOF
run chmod 0644 /etc/systemd/system/wazuh-vd-tmp-cleanup.service

# Named timezone because the guest runs in UTC: 02:00 in India is quiet, and
# clear of apt-daily-upgrade at 03:15 UTC.
write_file /etc/systemd/system/wazuh-vd-tmp-cleanup.timer <<'EOF'
[Unit]
Description=Nightly Wazuh feed scratch and alert archive cleanup

[Timer]
OnCalendar=*-*-* 02:00 Asia/Kolkata
RandomizedDelaySec=15min
# A guest that was off at 02:00 cleans up on next boot instead of skipping a day.
Persistent=true

[Install]
WantedBy=timers.target
EOF
run chmod 0644 /etc/systemd/system/wazuh-vd-tmp-cleanup.timer

run systemctl daemon-reload
run systemctl enable --now wazuh-vd-tmp-cleanup.timer
if (( APPLY )); then
  systemctl list-timers wazuh-vd-tmp-cleanup.timer --no-pager | head -2
  ok "nightly feed scratch and alert archive cleanup installed; see: journalctl -u wazuh-vd-tmp-cleanup"
fi
