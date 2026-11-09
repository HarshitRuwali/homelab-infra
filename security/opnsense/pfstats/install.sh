#!/bin/sh
# Install the pfstats log line on an OPNsense firewall.
#
# Run it ON THE FIREWALL, as root, from this directory. The login shell there
# is csh, so start it with sh and do not execute it directly:
#
#   sh install.sh            dry run: prints what it would do, changes nothing
#   sh install.sh --apply    do it
#
# In order, with --apply:
#   1. copies pfstats-log.sh, actions_pfstats.conf and pfstats.inc into place
#   2. restarts configd, so it reads the new "pfstats log" action
#   3. adds the every-minute cron job (add-pfstats-cron.php, run from
#      /usr/local/opnsense/mvc, which its require_once needs) and reloads cron
#   4. runs the script once with PFSTATS_STDOUT=1 and checks the line carries
#      the keys the alert rules read
#
# Safe to run again: the files are overwritten with the same content and the
# cron script reports the job as already present. It does not touch the syslog
# destination; see security/docs/wiring/index.md, "pfstats".
#
# PFSTATS_ROOT prefixes every absolute path below, so the offline test can run
# this against a scratch tree. Leave it unset on the firewall.
set -eu

usage() {
    echo "usage: sh install.sh [--apply]" >&2
    echo "  no argument  dry run: print what would be done, change nothing" >&2
    echo "  --apply      install, restart configd, add the cron job, check the line" >&2
}

die() {
    echo "install.sh: $*" >&2
    exit 1
}

APPLY=0
case "${1:-}" in
    "") ;;
    --apply) APPLY=1 ;;
    -h | --help) usage; exit 0 ;;
    *) usage; exit 2 ;;
esac
[ "$#" -le 1 ] || { usage; exit 2; }

ROOT=${PFSTATS_ROOT:-}
HERE=$(cd "$(dirname "$0")" && pwd)
SBIN=$ROOT/usr/local/sbin
ACTIONS=$ROOT/usr/local/opnsense/service/conf/actions.d
PLUGINS=$ROOT/usr/local/etc/inc/plugins.inc.d
MVC=$ROOT/usr/local/opnsense/mvc

[ -d "$ROOT/usr/local/opnsense" ] ||
    die "$ROOT/usr/local/opnsense does not exist: this is not an OPNsense firewall"
for dir in "$SBIN" "$ACTIONS" "$PLUGINS" "$MVC"; do
    [ -d "$dir" ] || die "$dir does not exist"
done
for f in pfstats-log.sh actions_pfstats.conf pfstats.inc add-pfstats-cron.php; do
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

run install -m 0755 "$HERE/pfstats-log.sh" "$SBIN/pfstats-log.sh"
run install -m 0644 "$HERE/actions_pfstats.conf" "$ACTIONS/actions_pfstats.conf"
run install -m 0644 "$HERE/pfstats.inc" "$PLUGINS/pfstats.inc"
run service configd restart

if [ "$APPLY" -eq 1 ]; then
    echo "+ cd $MVC && php $HERE/add-pfstats-cron.php apply"
    (cd "$MVC" && php "$HERE/add-pfstats-cron.php" apply) || die "add-pfstats-cron.php failed"
else
    echo "would run: cd $MVC && php $HERE/add-pfstats-cron.php apply"
fi
run configctl template reload OPNsense/Cron
run configctl cron restart

if [ "$APPLY" -eq 1 ]; then
    line=$(PFSTATS_STDOUT=1 "$SBIN/pfstats-log.sh") || die "pfstats-log.sh failed"
    echo "$line"
    for key in states states_max src_limit_hits banned; do
        case " $line " in
            *" $key="*) ;;
            *) die "the line has no $key= field, so the alert rules cannot read it" ;;
        esac
    done
    echo "ok: the line carries states, states_max, src_limit_hits and banned"
    echo "next: forward the program pfstats in the remote syslog destination (security/docs/wiring/index.md)"
else
    echo "would run: PFSTATS_STDOUT=1 $SBIN/pfstats-log.sh, and check the line has states, states_max, src_limit_hits and banned"
    echo "dry run: nothing was changed. Run again with --apply."
fi
