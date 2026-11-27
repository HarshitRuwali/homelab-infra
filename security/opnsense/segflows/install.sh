#!/bin/sh
# Install (or remove) segflows, the inter-segment traffic accounting, on an OPNsense firewall.
#
# Run it ON THE FIREWALL, as root, from this directory. The login shell there
# is csh, so start it with sh and do not execute it directly:
#
#   sh install.sh                      dry run: prints what it would do, changes nothing
#   sh install.sh --apply              install
#   sh install.sh --uninstall          dry run of the removal
#   sh install.sh --uninstall --apply  remove the script, the action and the cron job
#
# In order, with --apply:
#   1. copies segflows.py and actions_segflows.conf into place and creates the
#      ledger directory /var/db/segflows (mode 0750)
#   2. restarts configd, so it reads the new "segflows run" action
#   3. runs segflows.py once and checks /var/tmp/node_exporter/segflows.prom
#      carries the run and capture metrics. The first run reads up to a day of
#      flowd history, so it can take a few seconds; it runs BEFORE the cron job
#      exists, and the script's own lock keeps a later cron run out in any case
#   4. adds the every-5-minutes cron job (add-segflows-cron.php, run from
#      /usr/local/opnsense/mvc, which its require_once needs) and reloads cron
#
# Safe to run again: the files are overwritten with the same content and the
# cron script reports the job as already present.
#
# --uninstall removes the installed files, the cron job and segflows.prom. It
# keeps /var/db/segflows (the permanent ledger and the counter state); delete
# that directory by hand to drop the history.
#
# SEGFLOWS_ROOT prefixes every absolute path below, so the offline test can run
# this against a scratch tree. Leave it unset on the firewall.
set -eu

usage() {
    echo "usage: sh install.sh [--uninstall] [--apply]" >&2
    echo "  no argument  dry run: print what would be done, change nothing" >&2
    echo "  --apply      install, restart configd, run once and check, add the cron job" >&2
    echo "  --uninstall  with --apply: remove the script, action, cron job and segflows.prom" >&2
}

die() {
    echo "install.sh: $*" >&2
    exit 1
}

APPLY=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h | --help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

ROOT=${SEGFLOWS_ROOT:-}
HERE=$(cd "$(dirname "$0")" && pwd)
SBIN=$ROOT/usr/local/sbin
ACTIONS=$ROOT/usr/local/opnsense/service/conf/actions.d
MVC=$ROOT/usr/local/opnsense/mvc
LEDGER_DIR=$ROOT/var/db/segflows
PROM_DIR=$ROOT/var/tmp/node_exporter
NETFLOW_DIR=${SEGFLOWS_NETFLOW_DIR:-$ROOT/usr/local/opnsense/scripts/netflow}

[ -d "$ROOT/usr/local/opnsense" ] ||
    die "$ROOT/usr/local/opnsense does not exist: this is not an OPNsense firewall"
for dir in "$SBIN" "$ACTIONS" "$MVC"; do
    [ -d "$dir" ] || die "$dir does not exist"
done
for f in segflows.py actions_segflows.conf add-segflows-cron.php; do
    [ -f "$HERE/$f" ] || die "$HERE/$f is missing"
done
if [ "$APPLY" -eq 1 ] && [ -z "$ROOT" ] && [ "$(id -u)" -ne 0 ]; then
    die "run as root"
fi

# Print the command; with --apply, run it too.
run() {
    if [ "$APPLY" -eq 1 ]; then
        echo "+ $*"
        "$@"
    else
        echo "would run: $*"
    fi
}

if [ "$UNINSTALL" -eq 1 ]; then
    if [ "$APPLY" -eq 1 ]; then
        echo "+ cd $MVC && php $HERE/add-segflows-cron.php remove"
        (cd "$MVC" && php "$HERE/add-segflows-cron.php" remove) || die "add-segflows-cron.php failed"
    else
        echo "would run: cd $MVC && php $HERE/add-segflows-cron.php remove"
    fi
    run configctl template reload OPNsense/Cron
    run configctl cron restart
    run rm -f "$SBIN/segflows.py" "$ACTIONS/actions_segflows.conf" "$PROM_DIR/segflows.prom"
    run service configd restart
    if [ "$APPLY" -eq 1 ]; then
        echo "removed. The ledger and counter state are kept in $LEDGER_DIR; delete that directory to drop the history."
    else
        echo "dry run: nothing was changed. Run again with --uninstall --apply."
    fi
    exit 0
fi

run install -d -m 0750 "$LEDGER_DIR"
run install -m 0755 "$HERE/segflows.py" "$SBIN/segflows.py"
run install -m 0644 "$HERE/actions_segflows.conf" "$ACTIONS/actions_segflows.conf"
run service configd restart

# The first run comes before the cron job: it reads up to a day of history and must not
# overlap a cron tick (the script's lock covers that too).
if [ "$APPLY" -eq 1 ]; then
    echo "+ $SBIN/segflows.py"
    SEGFLOWS_DIR=$LEDGER_DIR SEGFLOWS_PROM_DIR=$PROM_DIR SEGFLOWS_NETFLOW_DIR=$NETFLOW_DIR \
        "$SBIN/segflows.py" || die "segflows.py failed"
    prom=$PROM_DIR/segflows.prom
    [ -f "$prom" ] || die "$prom was not written"
    for metric in opnsense_segflow_last_run_timestamp_seconds opnsense_segflow_lag_seconds \
        opnsense_segflow_run_duration_seconds opnsense_segflow_capture_interfaces; do
        grep -q "^$metric" "$prom" || die "$prom has no $metric, so the dashboard cannot read the run"
    done
    echo "ok: $prom carries the run, lag and capture metrics"
    echo "+ cd $MVC && php $HERE/add-segflows-cron.php apply"
    (cd "$MVC" && php "$HERE/add-segflows-cron.php" apply) || die "add-segflows-cron.php failed"
else
    echo "would run: $SBIN/segflows.py, and check $PROM_DIR/segflows.prom has the run, lag and capture metrics"
    echo "would run: cd $MVC && php $HERE/add-segflows-cron.php apply"
fi
run configctl template reload OPNsense/Cron
run configctl cron restart

if [ "$APPLY" -eq 1 ]; then
    echo "next: python3 $SBIN/segflows.py --report pairs  (reads the ledger)"
else
    echo "dry run: nothing was changed. Run again with --apply."
fi
