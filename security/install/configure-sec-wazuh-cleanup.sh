#!/usr/bin/env bash
# Nightly cleanup of the vulnerability feed updater's scratch space on sec-wazuh.
# Safe to run on an existing manager: it installs a timer and touches nothing
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
# Deletes vulnerability feed updater scratch files that have not been written
# for MIN_AGE_MIN minutes. The age floor is the safety: a download or
# extraction in progress keeps writing its files, so this never removes one
# out from under a running update. Anything deleted is re-downloaded by Wazuh
# the next time it needs it.
set -euo pipefail
TMP=/var/ossec/queue/vd_updater/tmp
MIN_AGE_MIN=${MIN_AGE_MIN:-360}

[[ -d "$TMP" ]] || { echo "no $TMP, nothing to do"; exit 0; }
before=$(du -sb "$TMP" | cut -f1)
count=$(find "$TMP" -mindepth 2 -type f -mmin +"$MIN_AGE_MIN" -print -delete | wc -l)
after=$(du -sb "$TMP" | cut -f1)
echo "removed $count file(s), freed $(( (before - after) / 1048576 )) MiB; $(( after / 1048576 )) MiB left in $TMP"
EOF
run chmod 0755 /usr/local/sbin/wazuh-vd-tmp-cleanup

write_file /etc/systemd/system/wazuh-vd-tmp-cleanup.service <<'EOF'
[Unit]
Description=Remove stale Wazuh vulnerability feed updater scratch files
Documentation=file:///usr/local/sbin/wazuh-vd-tmp-cleanup
ConditionPathIsDirectory=/var/ossec/queue/vd_updater/tmp

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
Description=Nightly Wazuh vulnerability feed scratch cleanup

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
  ok "nightly vulnerability feed scratch cleanup installed; see: journalctl -u wazuh-vd-tmp-cleanup"
fi
