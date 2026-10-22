#!/usr/bin/env bash
# Render the Caddyfile and both dnsmasq instances from sites.tsv and hosts.tsv.
#
#   render.sh --sites FILE --hosts FILE --lan-ip ADDR --tailnet-ip ADDR \
#             --cas DIR --out DIR
#
# Pure: reads its inputs, writes five files into --out, touches nothing else.
# install.sh calls it; tests call it with the example files. Exits non-zero,
# naming the line, on any malformed or duplicate entry.
set -euo pipefail

usage() {
  cat <<'USAGE'
usage: render.sh --sites FILE --hosts FILE --lan-ip ADDR --tailnet-ip ADDR --cas DIR --out DIR
       render.sh -h | --help

Writes into DIR: Caddyfile, hosts-lan, hosts-tailnet, dnsmasq-lan.conf,
dnsmasq-tailnet.conf. --cas is where ca=<file> entries are looked up.
USAGE
}

missing_value() { echo "render.sh: $1 needs a value" >&2; usage >&2; exit 2; }

SITES="" HOSTS="" LAN_IP="" TS_IP="" CAS="" OUT=""
while (($#)); do
  case "$1" in
    --sites) (($# >= 2)) || missing_value "$1"; SITES="$2"; shift ;;
    --hosts) (($# >= 2)) || missing_value "$1"; HOSTS="$2"; shift ;;
    --lan-ip) (($# >= 2)) || missing_value "$1"; LAN_IP="$2"; shift ;;
    --tailnet-ip) (($# >= 2)) || missing_value "$1"; TS_IP="$2"; shift ;;
    --cas) (($# >= 2)) || missing_value "$1"; CAS="$2"; shift ;;
    --out) (($# >= 2)) || missing_value "$1"; OUT="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "render.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
for pair in SITES:sites HOSTS:hosts LAN_IP:lan-ip TS_IP:tailnet-ip CAS:cas OUT:out; do
  v="${pair%%:*}"
  [[ -n "${!v}" ]] || { echo "render.sh: missing --${pair#*:}" >&2; usage >&2; exit 2; }
done
ipv4='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
# scheme, a lowercase host name, IPv4 address or bracketed IPv6 address, and a
# required port; no path, query, userinfo or anything Caddy would expand.
upstream_re='^https?://([a-z0-9]([a-z0-9.-]*[a-z0-9])?|\[[0-9a-fA-F:]+\]):[0-9]{1,5}$'
[[ "$LAN_IP" =~ $ipv4 ]] || { echo "render.sh: --lan-ip is not an IPv4 address" >&2; exit 2; }
[[ "$TS_IP" =~ $ipv4 ]] || { echo "render.sh: --tailnet-ip is not an IPv4 address" >&2; exit 2; }
mkdir -p "$OUT"
# Caddy header_down with a regex: find, then replace ($1 and $2 are the
# captured .lan host and the character after it).
# shellcheck disable=SC2016  # $1 and $2 are for Caddy, not for the shell
location_rewrite='header_down Location "^http://([a-z0-9-]+\.lan)([:/]|$)" "https://$1$2"'

fail() { echo "render.sh: $1" >&2; exit 1; }
declare -A SEEN=()

caddy="$OUT/Caddyfile"; hl="$OUT/hosts-lan"; ht="$OUT/hosts-tailnet"
cat >"$caddy" <<'EOF'
# Rendered by ingress/render.sh from /etc/ingress/sites.tsv. Do not edit:
# change sites.tsv and re-run install.sh --apply.
{
	# Certificates come from Caddy's own CA; no public CA can issue for .lan.
	# Devices trust its root once (docs/certificates.md).
	local_certs
	skip_install_trust
}

# Tailnet-only sites: refuse anything that did not arrive from a tailnet
# address. Their names also resolve to the tailnet address only.
(tailnet_only) {
	@outside not remote_ip 100.64.0.0/10 fd7a:115c:a1e0::/48
	abort @outside
}
EOF
printf '# Rendered by ingress/render.sh. Answers for LAN clients.\n%s ingress.lan\n' "$LAN_IP" >"$hl"
printf '# Rendered by ingress/render.sh. Answers for tailnet clients.\n%s ingress.lan\n' "$TS_IP" >"$ht"
SEEN[ingress]=1

n=0
while IFS= read -r line || [[ -n "$line" ]]; do
  n=$((n+1))
  line="${line%$'\r'}"
  [[ "${line//$'\t'/}" != *[[:cntrl:]]* ]] || fail "$SITES:$n: control character in line"
  line="${line%%#*}"; [[ -z "${line//[[:space:]]/}" ]] && continue
  read -r name access upstream tls extra <<<"$line"
  [[ -z "${extra:-}" ]] || fail "$SITES:$n: too many fields"
  [[ "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || fail "$SITES:$n: bad name '$name'"
  [[ -z "${SEEN[$name]:-}" ]] || fail "$SITES:$n: duplicate name '$name'"
  SEEN[$name]=1
  [[ "$access" == lan || "$access" == tailnet ]] || fail "$SITES:$n: access must be lan or tailnet"
  [[ "$upstream" =~ $upstream_re ]] || fail "$SITES:$n: bad upstream '$upstream'"
  (( 10#${upstream##*:} <= 65535 )) || fail "$SITES:$n: upstream port above 65535"
  tls="${tls:--}"
  {
    printf '\n%s.lan {\n' "$name"
    [[ "$access" == tailnet ]] && printf '\timport tailnet_only\n'
    printf '\treverse_proxy %s {\n' "$upstream"
    if [[ "$upstream" == https://* ]]; then
      [[ "$tls" =~ ^ca=([A-Za-z0-9._-]+)(,sni=([A-Za-z0-9.-]+))?$ ]] \
        || fail "$SITES:$n: an https upstream needs tls ca=<file>[,sni=<name>]"
      ca="${BASH_REMATCH[1]}"; sni="${BASH_REMATCH[3]:-}"
      [[ -f "$CAS/$ca" ]] || fail "$SITES:$n: CA file $CAS/$ca not found"
      printf '\t\ttransport http {\n\t\t\ttls_trust_pool file %s\n' "$CAS/$ca"
      [[ -n "$sni" ]] && printf '\t\t\ttls_server_name %s\n' "$sni"
      printf '\t\t}\n'
    else
      [[ "$tls" == - ]] || fail "$SITES:$n: tls must be - for an http upstream"
      # An http upstream builds its own redirects with http://; send the
      # browser back to https instead of through a second redirect. Only a
      # redirect to a .lan name is rewritten: one to an external site or to the
      # upstream's own address keeps its scheme.
      printf '\t\t%s\n' "$location_rewrite"
    fi
    printf '\t}\n}\n'
  } >>"$caddy"
  if [[ "$access" == lan ]]; then
    printf '%s %s.lan\n' "$LAN_IP" "$name" >>"$hl"
  else
    printf '%s %s.lan\n' "$TS_IP" "$name" >>"$hl"
  fi
  printf '%s %s.lan\n' "$TS_IP" "$name" >>"$ht"
done <"$SITES"

n=0
while IFS= read -r line || [[ -n "$line" ]]; do
  n=$((n+1))
  line="${line%$'\r'}"
  [[ "${line//$'\t'/}" != *[[:cntrl:]]* ]] || fail "$HOSTS:$n: control character in line"
  line="${line%%#*}"; [[ -z "${line//[[:space:]]/}" ]] && continue
  read -r name lan ts extra <<<"$line"
  [[ -z "${extra:-}" ]] || fail "$HOSTS:$n: too many fields"
  [[ "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || fail "$HOSTS:$n: bad name '$name'"
  [[ -z "${SEEN[$name]:-}" ]] || fail "$HOSTS:$n: duplicate name '$name'"
  SEEN[$name]=1
  [[ "$lan" =~ $ipv4 ]] || fail "$HOSTS:$n: lan_ip is not an IPv4 address"
  ts="${ts:--}"
  [[ "$ts" == - || "$ts" =~ $ipv4 ]] || fail "$HOSTS:$n: tailnet_ip must be an IPv4 address or -"
  printf '%s %s.lan\n' "$lan" "$name" >>"$hl"
  if [[ "$ts" == - ]]; then printf '%s %s.lan\n' "$lan" "$name" >>"$ht"; else printf '%s %s.lan\n' "$ts" "$name" >>"$ht"; fi
done <"$HOSTS"

dnsmasq_conf() { # dnsmasq_conf <listen-address> <hosts-file>
  cat <<EOF
# Rendered by ingress/render.sh. One of two instances; see docs/dns.md.
listen-address=$1
bind-dynamic
no-resolv
no-hosts
domain-needed
bogus-priv
# Authoritative for .lan: a name not listed is NXDOMAIN, never forwarded.
local=/lan/
addn-hosts=$2
# Everything else. This instance is the LAN's fallback resolver, so it must
# not depend on AdGuard, which may be the thing that is down.
server=1.1.1.1
server=1.0.0.1
cache-size=1000
EOF
}
dnsmasq_conf "$LAN_IP" "/etc/ingress/rendered/hosts-lan" >"$OUT/dnsmasq-lan.conf"
dnsmasq_conf "$TS_IP" "/etc/ingress/rendered/hosts-tailnet" >"$OUT/dnsmasq-tailnet.conf"
