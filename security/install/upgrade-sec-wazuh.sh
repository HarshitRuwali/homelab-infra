#!/usr/bin/env bash
# Upgrades the Wazuh all-in-one on sec-wazuh in the order Wazuh documents:
# indexer, then manager, then dashboard. Run inside the sec-wazuh VM.
#
#   ./upgrade-sec-wazuh.sh                       # plan against apt's candidate
#   ./upgrade-sec-wazuh.sh --apply
#   WAZUH_VERSION=4.14.8-1 ./upgrade-sec-wazuh.sh --apply
#
# Why not `apt upgrade`, or the fleet's force-updates.yml: that moves all three
# packages at once in whatever order apt picks, with Filebeat still writing
# into an indexer that is being replaced. A broken indexer is the slowest
# thing in this stack to recover, so it goes first, alone, with shard
# allocation held and writers stopped. Stops at the first failure.
source "$(dirname "$0")/_common.sh"
require_root; banner

C=/etc/wazuh-indexer/certs
TEMPLATE=/etc/filebeat/wazuh-template.json
# The admin client certificate the installer leaves on an all-in-one node:
# no password needed, and nothing to paste into a shell.
api() { curl -sS -m30 --cacert "$C/root-ca.pem" --cert "$C/admin.pem" --key "$C/admin-key.pem" "$@"; }
health() { api "https://127.0.0.1:9200/_cluster/health" | grep -oE '"status":"[a-z]+"' | cut -d'"' -f4; }
apt_up() {
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade \
    -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$1=$V"
}
allocation() {
  run api -X PUT "https://127.0.0.1:9200/_cluster/settings" -H 'Content-Type: application/json' \
    -d "{\"persistent\":{\"cluster.routing.allocation.enable\":\"$1\"}}"
  (( APPLY )) && echo
  return 0
}

# ------------------------------------------------------------------ preflight

for f in root-ca.pem admin.pem admin-key.pem; do
  [[ -r "$C/$f" ]] || die "$C/$f missing: is this the all-in-one sec-wazuh node?"
done
run apt-get update -qq
V="${WAZUH_VERSION:-$(apt-cache policy wazuh-manager | awk '/Candidate:/{print $2}')}"
[[ -n "$V" && "$V" != "(none)" ]] || die "no candidate version for wazuh-manager"
for p in wazuh-indexer wazuh-manager wazuh-dashboard; do
  cur="$(dpkg-query -W -f='${Version}' "$p" 2>/dev/null)" || die "$p is not installed"
  dpkg --compare-versions "$cur" le "$V" || die "$p is $cur, newer than $V; refusing to downgrade"
  ok "$p $cur -> $V"
done
# install-sec-wazuh.sh holds these so nothing upgrades Wazuh by accident.
# Release the hold for this run only, and put it back on the way out, failure
# included, so the guard outlives the upgrade.
mapfile -t HELD < <(apt-mark showhold | grep -xE 'wazuh-(indexer|manager|dashboard)|filebeat' || true)
if (( ${#HELD[@]} )); then
  ok "held: ${HELD[*]}; released for this run, held again afterwards"
  if (( APPLY )); then trap 'apt-mark hold "${HELD[@]}" >/dev/null && echo "held again: ${HELD[*]}"' EXIT; fi
fi
before="$(health)" || die "the indexer does not answer on 9200"
[[ "$before" == green || "$before" == yellow ]] || die "cluster is $before before starting; fix that first"
ok "cluster $before (yellow is normal on one node: replicas have nowhere to go)"
echo

# ---------------------------------------------------------------- the upgrade

if (( ${#HELD[@]} )); then
  echo "release the hold"
  run apt-mark unhold "${HELD[@]}"
fi

echo "backup"
run tar czf "/root/wazuh-pre-$V-$(date +%F-%H%M).tgz" /etc/wazuh-indexer /etc/wazuh-dashboard /etc/filebeat /var/ossec/etc

echo "stop the writers: filebeat, dashboard"
run systemctl stop filebeat wazuh-dashboard

echo "indexer: primaries only, flush, upgrade"
allocation primaries
run api -X POST "https://127.0.0.1:9200/_flush" -o /dev/null
run systemctl stop wazuh-indexer
apt_up wazuh-indexer
run systemctl daemon-reload
run systemctl start wazuh-indexer
if (( APPLY )); then
  for _ in $(seq 1 60); do
    s="$(health 2>/dev/null || true)"
    [[ "$s" == green || "$s" == yellow ]] && break
    sleep 5
  done
  [[ "$s" == green || "$s" == yellow ]] || die "the indexer did not come back; allocation is still primaries-only"
  ok "indexer back, cluster $s"
fi
allocation all

echo "manager"
apt_up wazuh-manager
run systemctl daemon-reload
run systemctl restart wazuh-manager

# The Filebeat index template is versioned with Wazuh. Replace it only when
# this release changed it, and keep the old one next to it.
echo "filebeat template"
t="$(mktemp)"; tmpl_changed=0
if curl -fsS -m30 -o "$t" "https://raw.githubusercontent.com/wazuh/wazuh/v${V%-*}/extensions/elasticsearch/7.x/wazuh-template.json"; then
  if cmp -s "$t" "$TEMPLATE"; then ok "template unchanged in ${V%-*}"
  else tmpl_changed=1; warn "template changed in ${V%-*}; it will be replaced"
       run cp "$TEMPLATE" "$TEMPLATE.pre-$V"
       run install -m 0644 "$t" "$TEMPLATE"
  fi
else
  warn "could not fetch the ${V%-*} template; keeping the current one"
fi

echo "dashboard"
apt_up wazuh-dashboard
run systemctl daemon-reload

echo "start the writers"
run systemctl start filebeat wazuh-dashboard
if (( tmpl_changed )); then
  run filebeat setup --index-management -E output.logstash.enabled=false
fi
rm -f "$t"

(( APPLY )) || { echo; echo "This was a dry run. Re-run with --apply."; exit 0; }

# ------------------------------------------------------------------- verify

echo
sleep 20
dpkg-query -W wazuh-indexer wazuh-manager wazuh-dashboard
systemctl is-active wazuh-indexer wazuh-manager wazuh-dashboard filebeat | paste -sd' '
filebeat test output 2>&1 | tail -2
api "https://127.0.0.1:9200/_cluster/health?pretty" | grep -E '"status"|active_shards"|unassigned_shards"'
curl -sk -m10 -o /dev/null -w 'dashboard https: %{http_code}\n' https://127.0.0.1/
ok "Wazuh $V"
