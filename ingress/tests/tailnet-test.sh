#!/usr/bin/env bash
#
# tailnet-test.sh - run the Caddyfile that render.sh writes for the example
# sites in a real Caddy, with two docker networks standing in for the LAN
# (192.0.2.0/24) and the tailnet (100.64.0.0/24), and check what each side can
# reach and how redirects are rewritten:
#
#   - a tailnet site answers a tailnet client and aborts a LAN client, also
#     when the LAN client sends X-Forwarded-For with a tailnet address, and
#     also when the TLS server name is a lan site but the Host is the tailnet
#     site;
#   - a lan site answers both;
#   - an http upstream's redirect to a .lan name is rewritten to https; one to
#     an external site, to the upstream's own address, or to a look-alike name
#     keeps http.
#
#   ./tests/tailnet-test.sh
#
# Needs docker, the caddy:2.11 and curlimages/curl images and permission to
# create two docker networks. Without them it prints why and skips. Nothing
# is published on the host and nothing outside the containers is touched.
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no()   { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$*"; SKIP=$((SKIP+1)); }
finish() { echo; printf '%d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"; (( FAIL == 0 )); }

CADDY_IMAGE=caddy:2.11
CURL_IMAGE=curlimages/curl:latest
LAN_NET=192.0.2.0/24;     CADDY_LAN=192.0.2.2;   UPSTREAM=192.0.2.10;  LAN_CLIENT=192.0.2.20
TS_NET=100.64.0.0/24;     CADDY_TS=100.64.0.2;   TS_CLIENT=100.64.0.20
id="ingress-test-$$"

echo "== Caddy enforces tailnet_only and rewrites .lan redirects ($CADDY_IMAGE) =="
if ! command -v docker >/dev/null; then
  skip "docker is not installed, so Caddy was not run against the rendered Caddyfile"; finish; exit
fi
for img in "$CADDY_IMAGE" "$CURL_IMAGE"; do
  if ! docker image inspect "$img" >/dev/null 2>&1 && ! docker pull -q "$img" >/dev/null 2>&1; then
    skip "cannot pull $img (no network?), so Caddy was not run against the rendered Caddyfile"; finish; exit
  fi
done

tmp="$(mktemp -d)"
cleanup() {
  docker rm -f "$id-caddy" "$id-up" "$id-lan" "$id-ts" >/dev/null 2>&1
  docker network rm "$id-lan" "$id-ts" >/dev/null 2>&1
  rm -rf "$tmp"
}
trap cleanup EXIT

if ! docker network create --subnet "$LAN_NET" "$id-lan" >/dev/null 2>"$tmp/net.err" \
   || ! docker network create --subnet "$TS_NET" "$id-ts" >/dev/null 2>>"$tmp/net.err"; then
  skip "cannot create the docker networks $LAN_NET and $TS_NET (in use on this host?): $(tr '\n' ' ' <"$tmp/net.err")"
  finish; exit
fi

# Render the example sites. The https ones need CA files and unreachable
# upstreams, so they are dropped; every http upstream is pointed at the one
# upstream container below.
mkdir -p "$tmp/cas" "$tmp/out"
grep -vE '^[^#]*https://' sites.example.tsv \
  | sed -E 's#http://[0-9.]+:[0-9]+#http://'"$UPSTREAM"':8080#' >"$tmp/sites.tsv"
: >"$tmp/hosts.tsv"
if ! ./render.sh --sites "$tmp/sites.tsv" --hosts "$tmp/hosts.tsv" --lan-ip "$CADDY_LAN" \
     --tailnet-ip "$CADDY_TS" --cas "$tmp/cas" --out "$tmp/out" 2>"$tmp/render.err"; then
  no "render.sh failed on the example sites: $(cat "$tmp/render.err")"; finish; exit
fi
LAN_SITE="$(sed -E 's/#.*//' "$tmp/sites.tsv" | awk '$2 == "lan" { print $1; exit }')"
TS_SITE="$(sed -E 's/#.*//' "$tmp/sites.tsv" | awk '$2 == "tailnet" { print $1; exit }')"
if [[ -z "$LAN_SITE" || -z "$TS_SITE" ]]; then
  no "the example sites need an http lan site and an http tailnet site"; finish; exit
fi

# The upstream: answers 200, and redirects to the places the rewrite must
# treat differently.
cat >"$tmp/upstream.Caddyfile" <<EOT
{
	auto_https off
}
:8080 {
	redir /ext http://example.com/x 302
	redir /own http://$UPSTREAM:8080/z 302
	redir /lan http://printer.lan/y 302
	redir /lanbare http://printer.lan 302
	redir /lanport http://printer.lan:8080/p 302
	redir /lookalike http://printer.lan.example.com/x 302
	respond "upstream ok" 200
}
EOT

if ! { docker run -d --name "$id-up" --network "$id-lan" --ip "$UPSTREAM" \
  -v "$tmp/upstream.Caddyfile:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" >/dev/null 2>"$tmp/start.err" \
  && docker create --name "$id-caddy" --network "$id-lan" --ip "$CADDY_LAN" \
       -v "$tmp/out/Caddyfile:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" >/dev/null 2>>"$tmp/start.err" \
  && docker network connect --ip "$CADDY_TS" "$id-ts" "$id-caddy" 2>>"$tmp/start.err" \
  && docker start "$id-caddy" >/dev/null 2>>"$tmp/start.err" \
  && docker run -d --name "$id-lan" --network "$id-lan" --ip "$LAN_CLIENT" --entrypoint sleep "$CURL_IMAGE" 3600 >/dev/null 2>>"$tmp/start.err" \
  && docker run -d --name "$id-ts" --network "$id-ts" --ip "$TS_CLIENT" --entrypoint sleep "$CURL_IMAGE" 3600 >/dev/null 2>>"$tmp/start.err"; }; then
  no "could not start the containers: $(cat "$tmp/start.err")"; finish; exit
fi

# fetch <client: lan|ts> <server name for SNI and --resolve> <path> [curl args...]
# Prints "<http code> <curl exit code>" and leaves the headers in $tmp/headers.
fetch() {
  local client="$1" name="$2" path="$3" addr rc code; shift 3
  if [[ "$client" == lan ]]; then addr="$CADDY_LAN"; else addr="$CADDY_TS"; fi
  code="$(docker exec "$id-$client" curl -sk -m 5 -o /dev/null -D "/tmp/headers" -w '%{http_code}' \
            --resolve "$name.lan:443:$addr" "$@" "https://$name.lan$path" 2>/dev/null)"; rc=$?
  docker exec "$id-$client" cat /tmp/headers >"$tmp/headers" 2>/dev/null || : >"$tmp/headers"
  docker exec "$id-$client" rm -f /tmp/headers >/dev/null 2>&1
  echo "$code $rc"
}
location() { awk 'tolower($1) == "location:" { sub(/\r$/, ""); print $2 }' "$tmp/headers"; }

# Wait for Caddy to have issued its certificates and be serving.
ready=0
for _ in $(seq 40); do
  [[ "$(fetch ts "$LAN_SITE" /)" == "200 0" ]] && { ready=1; break; }
  sleep 0.5
done
if (( ! ready )); then
  no "Caddy never answered 200 for $LAN_SITE.lan: $(docker logs "$id-caddy" 2>&1 | tail -5)"; finish; exit
fi

# expect <label> <client> <name> <path> <want "code rc" or REFUSED> [curl args...]
# REFUSED is what Caddy's abort looks like to curl: no HTTP status (000) and
# exit 52 (empty reply) or 56 (reset) over HTTP/1.1, or 92 (HTTP/2 stream
# error) over HTTP/2, which curl negotiates by default.
expect() {
  local label="$1" client="$2" name="$3" path="$4" want="$5" got; shift 5
  got="$(fetch "$client" "$name" "$path" "$@")"
  if [[ "$want" == REFUSED ]]; then
    case "$got" in
      "000 52"|"000 56"|"000 92") ok "$label (connection aborted: $got)" ;;
      *) no "$label: wanted the connection aborted (000 with curl exit 52, 56 or 92), got '$got'" ;;
    esac
  elif [[ "$got" == "$want" ]]; then ok "$label"
  else no "$label: wanted '$want', got '$got'"; fi
}

echo
echo "== a lan site answers the LAN and the tailnet =="
expect "$LAN_SITE.lan from the LAN network"     lan "$LAN_SITE" / "200 0"
expect "$LAN_SITE.lan from the tailnet network" ts  "$LAN_SITE" / "200 0"

echo
echo "== a tailnet site answers the tailnet only =="
expect "$TS_SITE.lan from the tailnet network"  ts  "$TS_SITE" / "200 0"
expect "$TS_SITE.lan from the LAN network is refused" lan "$TS_SITE" / REFUSED
expect "$TS_SITE.lan from the LAN network over HTTP/1.1 is refused" lan "$TS_SITE" / REFUSED --http1.1
expect "$TS_SITE.lan from the LAN network with X-Forwarded-For: 100.64.0.9 is refused" \
  lan "$TS_SITE" / REFUSED -H 'X-Forwarded-For: 100.64.0.9'
expect "$TS_SITE.lan from the LAN network with Forwarded and X-Real-IP tailnet addresses is refused" \
  lan "$TS_SITE" / REFUSED -H 'Forwarded: for=100.64.0.9' -H 'X-Real-IP: 100.64.0.9'
# SNI is the URL name (a lan site); the Host header names the tailnet site.
expect "$TS_SITE.lan from the LAN network with the SNI of $LAN_SITE.lan is refused" \
  lan "$LAN_SITE" / REFUSED -H "Host: $TS_SITE.lan"
expect "$TS_SITE.lan from the tailnet network with the SNI of $LAN_SITE.lan is answered" \
  ts "$LAN_SITE" / "200 0" -H "Host: $TS_SITE.lan"
expect "$TS_SITE.lan from the tailnet network with X-Forwarded-For: 192.0.2.99 is answered" \
  ts "$TS_SITE" / "200 0" -H 'X-Forwarded-For: 192.0.2.99'

echo
echo "== an http upstream's redirects: only .lan names are sent to https =="
check_location() { # check_location <label> <path> <want Location>
  local got code
  code="$(fetch ts "$LAN_SITE" "$2")"; got="$(location)"
  if [[ "$code" == "302 0" && "$got" == "$3" ]]; then ok "$1: $3"
  else no "$1: wanted 302 with Location '$3', got '$code' and '$got'"; fi
}
check_location "redirect to an external site stays http"         /ext       "http://example.com/x"
check_location "redirect to the upstream's own address stays http" /own     "http://$UPSTREAM:8080/z"
check_location "redirect to a look-alike of a .lan name stays http" /lookalike "http://printer.lan.example.com/x"
check_location "redirect to a .lan name becomes https"           /lan       "https://printer.lan/y"
check_location "redirect to a bare .lan name becomes https"      /lanbare   "https://printer.lan"
check_location "redirect to a .lan name with a port becomes https" /lanport "https://printer.lan:8080/p"

finish
