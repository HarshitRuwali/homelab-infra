#!/usr/bin/env bash
#
# render-test.sh - run render.sh and assert on what it writes and what it
# refuses. The Caddyfile is checked by Caddy itself (caddy adapt, then caddy
# validate) and both dnsmasq configs by `dnsmasq --test`, each in a container.
# Nothing here touches /etc/ingress or a running service.
#
#   ./tests/render-test.sh
#
# Needs docker for the Caddy check; without docker it is skipped with a reason
# and the rest still runs. The dnsmasq check needs docker and a network to pull
# debian:13 and install dnsmasq-base (the same dnsmasq binary the dnsmasq package
# ships); it is skipped with a reason when either is missing.
#
# shellcheck disable=SC2016  # single quotes are deliberate: literal $ in patterns
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no()   { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$*"; SKIP=$((SKIP+1)); }

CADDY_IMAGE=caddy:2.11
DNSMASQ_IMAGE=debian:13
LAN=192.0.2.50
TS=100.64.0.50
# The exact line render.sh gives every http upstream (sites.md, "What an http upstream gets").
LOCREWRITE='header_down Location "^http://([a-z0-9-]+\.lan)([:/]|$)" "https://$1$2"'

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
cas="$tmp/cas"; out="$tmp/out"; mkdir -p "$cas" "$out"

# Throwaway CA certificates so the Caddyfile has real PEM files to load.
if command -v openssl >/dev/null; then
  for f in pve-root-ca.pem wazuh-root-ca.pem; do
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
      -keyout "$tmp/key.pem" -out "$cas/$f" -subj "/CN=test-$f" -days 2 2>/dev/null
  done
  REAL_PEM=1
else
  printf 'placeholder\n' >"$cas/pve-root-ca.pem"; cp "$cas/pve-root-ca.pem" "$cas/wazuh-root-ca.pem"
  REAL_PEM=0
fi

render() { # render <sites> <hosts> <outdir> [extra args replacing the defaults]
  ./render.sh --sites "$1" --hosts "$2" --lan-ip "$LAN" --tailnet-ip "$TS" \
    --cas "$cas" --out "$3"
}

# block <file> <name>: the Caddyfile block of <name>.lan
block() { awk -v n="$2.lan {" '$0 == n { on = 1 } on { print } on && /^}$/ { exit }' "$1"; }
has()   { grep -qxF -- "$2" "$1"; }
hasre() { grep -qE -- "$2" <<<"$1"; }

echo "== render.sh accepts the example files =="
if render sites.example.tsv hosts.example.tsv "$out" >"$tmp/stdout" 2>"$tmp/stderr"; then
  ok "render.sh exits 0 on sites.example.tsv and hosts.example.tsv"
else
  no "render.sh rejected the example files: $(cat "$tmp/stderr")"
fi
for f in Caddyfile hosts-lan hosts-tailnet dnsmasq-lan.conf dnsmasq-tailnet.conf; do
  if [[ -s "$out/$f" ]]; then ok "wrote $f"; else no "did not write $f"; fi
done

echo
echo "== every site in the example has a Caddy block =="
sitesfail=0
SITE_NAMES=(); TAILNET_SITES=(); LAN_SITES=()
while read -r name access _; do
  SITE_NAMES+=("$name")
  [[ "$access" == tailnet ]] && TAILNET_SITES+=("$name") || LAN_SITES+=("$name")
  [[ -n "$(block "$out/Caddyfile" "$name")" ]] || { no "no Caddyfile block for $name.lan"; sitesfail=1; }
done < <(sed -E 's/#.*//' sites.example.tsv | awk 'NF')
(( ${#TAILNET_SITES[@]} > 0 && ${#LAN_SITES[@]} > 0 )) || { no "the example needs both tailnet and lan sites"; sitesfail=1; }
(( sitesfail )) || ok "one block per site, and the example has both access values"

echo
echo "== access: tailnet sites import tailnet_only, lan sites do not =="
accfail=0
has "$out/Caddyfile" '(tailnet_only) {' || { no "Caddyfile does not define tailnet_only"; accfail=1; }
grep -qF 'remote_ip 100.64.0.0/10 fd7a:115c:a1e0::/48' "$out/Caddyfile" || { no "tailnet_only does not match the tailnet ranges"; accfail=1; }
grep -qF 'abort @outside' "$out/Caddyfile" || { no "tailnet_only does not abort outside clients"; accfail=1; }
for n in "${TAILNET_SITES[@]}"; do
  grep -qF 'import tailnet_only' <<<"$(block "$out/Caddyfile" "$n")" || { no "$n is a tailnet site but does not import tailnet_only"; accfail=1; }
done
for n in "${LAN_SITES[@]}"; do
  grep -qF 'import tailnet_only' <<<"$(block "$out/Caddyfile" "$n")" && { no "$n is a lan site but imports tailnet_only"; accfail=1; }
done
(( accfail )) || ok "tailnet_only is defined, imported by tailnet sites and by no lan site"

echo
echo "== DNS records: which address each instance answers =="
dnsfail=0
want() { # want <file> <line>
  has "$out/$1" "$2" || { no "$1 lacks the line: $2"; dnsfail=1; }
}
unwant() { # unwant <file> <line>
  has "$out/$1" "$2" && { no "$1 has the line it must not have: $2"; dnsfail=1; }
}
want hosts-lan "$LAN ingress.lan"
want hosts-tailnet "$TS ingress.lan"
for n in "${TAILNET_SITES[@]}"; do
  want hosts-lan "$TS $n.lan"; want hosts-tailnet "$TS $n.lan"
  unwant hosts-lan "$LAN $n.lan"
done
for n in "${LAN_SITES[@]}"; do
  want hosts-lan "$LAN $n.lan"; want hosts-tailnet "$TS $n.lan"
  unwant hosts-lan "$TS $n.lan"; unwant hosts-tailnet "$LAN $n.lan"
done
while read -r name lan ts; do
  want hosts-lan "$lan $name.lan"
  if [[ -z "${ts:-}" || "$ts" == - ]]; then want hosts-tailnet "$lan $name.lan"
  else want hosts-tailnet "$ts $name.lan"; unwant hosts-tailnet "$lan $name.lan"; fi
done < <(sed -E 's/#.*//' hosts.example.tsv | awk 'NF')
# One record per name per file: nothing is listed twice.
for f in hosts-lan hosts-tailnet; do
  dup="$(grep -v '^#' "$out/$f" | awk '{print $2}' | sort | uniq -d)"
  [[ -z "$dup" ]] || { no "$f lists a name twice: $dup"; dnsfail=1; }
done
(( dnsfail )) || ok "ingress, lan sites, tailnet sites and hosts map to the right address in both host files"

echo
echo "== upstream handling =="
upfail=0
for n in "${SITE_NAMES[@]}"; do
  b="$(block "$out/Caddyfile" "$n")"
  up="$(awk -v n="$n" '{sub(/#.*/, "")} $1 == n { print $3 }' sites.example.tsv)"
  tls="$(awk -v n="$n" '{sub(/#.*/, "")} $1 == n { print $4 }' sites.example.tsv)"
  if [[ "$up" == http://* ]]; then
    grep -qF "$LOCREWRITE" <<<"$b" || { no "$n: http upstream lacks the Location rewrite"; upfail=1; }
    grep -qE 'tls_trust_pool|tls_server_name|transport' <<<"$b" && { no "$n: http upstream has TLS transport settings"; upfail=1; }
  else
    ca="${tls#ca=}"; ca="${ca%%,*}"
    grep -qF "tls_trust_pool file $cas/$ca" <<<"$b" || { no "$n: https upstream lacks tls_trust_pool for $ca"; upfail=1; }
    grep -qF 'header_down' <<<"$b" && { no "$n: https upstream has the Location rewrite"; upfail=1; }
    if [[ "$tls" == *,sni=* ]]; then
      grep -qF "tls_server_name ${tls##*sni=}" <<<"$b" || { no "$n: lacks tls_server_name ${tls##*sni=}"; upfail=1; }
    else
      grep -qF 'tls_server_name' <<<"$b" && { no "$n: has tls_server_name without sni="; upfail=1; }
    fi
  fi
  grep -qF "reverse_proxy $up {" <<<"$b" || { no "$n: does not proxy to $up"; upfail=1; }
done
(( upfail )) || ok "http upstreams get the Location rewrite; https upstreams get tls_trust_pool and tls_server_name"

# An https upstream without sni= gets a trust pool and no server name.
cat >"$tmp/nosni.tsv" <<'TSV'
plain  lan  https://192.0.2.9:8006  ca=pve-root-ca.pem
TSV
: >"$tmp/empty.tsv"
render "$tmp/nosni.tsv" "$tmp/empty.tsv" "$tmp/o-nosni" 2>/dev/null
b="$(block "$tmp/o-nosni/Caddyfile" plain)"
if grep -qF "tls_trust_pool file $cas/pve-root-ca.pem" <<<"$b" && ! grep -qF tls_server_name <<<"$b"; then
  ok "an https upstream without sni= has a trust pool and no tls_server_name"
else
  no "https upstream without sni= rendered wrongly: $b"
fi
# tls may be left out for an http upstream, and trailing comments are ignored.
printf 'bare  lan  http://192.0.2.9:80   # no tls column\n' >"$tmp/bare.tsv"
if render "$tmp/bare.tsv" "$tmp/empty.tsv" "$tmp/o-bare" 2>/dev/null \
   && grep -qF "$LOCREWRITE" <<<"$(block "$tmp/o-bare/Caddyfile" bare)"; then
  ok "an http upstream may omit the tls column, and a trailing comment is ignored"
else
  no "an http upstream with no tls column was rejected or rendered wrongly"
fi

echo
echo "== dnsmasq configs =="
cfgfail=0
grep -qxF "listen-address=$LAN" "$out/dnsmasq-lan.conf" || { no "dnsmasq-lan.conf does not listen on the LAN address"; cfgfail=1; }
grep -qxF "listen-address=$TS" "$out/dnsmasq-tailnet.conf" || { no "dnsmasq-tailnet.conf does not listen on the tailnet address"; cfgfail=1; }
grep -qxF 'addn-hosts=/etc/ingress/rendered/hosts-lan' "$out/dnsmasq-lan.conf" || { no "dnsmasq-lan.conf reads the wrong host file"; cfgfail=1; }
grep -qxF 'addn-hosts=/etc/ingress/rendered/hosts-tailnet' "$out/dnsmasq-tailnet.conf" || { no "dnsmasq-tailnet.conf reads the wrong host file"; cfgfail=1; }
for f in dnsmasq-lan.conf dnsmasq-tailnet.conf; do
  for line in 'local=/lan/' 'server=1.1.1.1' 'server=1.0.0.1' 'no-resolv' 'no-hosts'; do
    grep -qxF "$line" "$out/$f" || { no "$f lacks: $line"; cfgfail=1; }
  done
done
(( cfgfail )) || ok "each instance listens on its own address, reads its own host file, owns .lan and forwards the rest"

echo
echo "== render.sh refuses bad input, exits non-zero and names the line =="
# reject <label> <exit-code> <message-fragment> <expected-location-regex> <sites-text> [hosts-text]
reject() {
  local label="$1" want_rc="$2" frag="$3" loc="$4" sites="$5" hosts="${6:-}" rc err
  printf '%s' "$sites" >"$tmp/s.tsv"; printf '%s' "$hosts" >"$tmp/h.tsv"
  rm -rf "$tmp/o-rej"
  err="$(render "$tmp/s.tsv" "$tmp/h.tsv" "$tmp/o-rej" 2>&1 >/dev/null)"; rc=$?
  if (( rc != want_rc )); then no "$label: exit code $rc, wanted $want_rc"
  elif ! grep -qF -- "$frag" <<<"$err"; then no "$label: message lacks '$frag': $err"
  elif ! grep -qE -- "$loc" <<<"$err"; then no "$label: message does not name the line ($loc): $err"
  else ok "$label"; fi
}
H1=$'# hosts\nnas 192.0.2.30 -\n'
S1=$'# sites\n\ngrafana lan http://192.0.2.29:80 -\n'
reject "duplicate site name"          1 "duplicate name 'grafana'" "s.tsv:4:" "$S1"$'grafana lan http://192.0.2.30:80 -\n'
reject "name reused in hosts.tsv"     1 "duplicate name 'grafana'" "h.tsv:2:" "$S1" $'# hosts\ngrafana 192.0.2.30 -\n'
reject "host name repeated"           1 "duplicate name 'nas'" "h.tsv:3:" "$S1" "$H1"$'nas 192.0.2.31 -\n'
reject "reserved name ingress"        1 "duplicate name 'ingress'" "s.tsv:4:" "$S1"$'ingress lan http://192.0.2.30:80 -\n'
reject "bad access value"             1 "access must be lan or tailnet" "s.tsv:4:" "$S1"$'wiki public http://192.0.2.30:80 -\n'
reject "bad name (uppercase)"         1 "bad name 'Wiki'" "s.tsv:4:" "$S1"$'Wiki lan http://192.0.2.30:80 -\n'
reject "bad name (leading hyphen)"    1 "bad name '-wiki'" "s.tsv:4:" "$S1"$'-wiki lan http://192.0.2.30:80 -\n'
reject "upstream with a path"         1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80/x -\n'
reject "upstream without a port"      1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30 -\n'
reject "upstream with a query"        1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80?x=1 -\n'
reject "upstream with braces"         1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://{env.HOST}:80 -\n'
reject "upstream with a placeholder"  1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:{http.request.uri} -\n'
reject "upstream with a quote"        1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80" -\n'
reject "upstream with userinfo"       1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://user@192.0.2.30:80 -\n'
reject "upstream with an uppercase host" 1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://Host:80 -\n'
reject "upstream with a trailing slash" 1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80/ -\n'
reject "upstream with a six-digit port" 1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:123456 -\n'
reject "upstream with port 65536"     1 "upstream port above 65535" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:65536 -\n'
reject "upstream with port 99999"     1 "upstream port above 65535" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:99999 -\n'
reject "control character in sites"   1 "control character in line" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80 -\001\n'
reject "control character in a comment" 1 "control character in line" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80 - # a\033b\n'
reject "CR in the middle of a line"   1 "control character in line" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80\r -\n'
reject "control character in hosts"   1 "control character in line" "h.tsv:3:" "$S1" "$H1"$'box 192.0.2.40 -\001\n'
reject "upstream without a scheme"    1 "bad upstream" "s.tsv:4:" "$S1"$'wiki lan 192.0.2.30:80 -\n'
reject "too many fields in sites"     1 "too many fields" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80 - extra\n'
reject "https upstream without ca="   1 "an https upstream needs tls ca=" "s.tsv:4:" "$S1"$'pve lan https://192.0.2.9:8006 -\n'
reject "https upstream, tls omitted"  1 "an https upstream needs tls ca=" "s.tsv:4:" "$S1"$'pve lan https://192.0.2.9:8006\n'
reject "https upstream, malformed tls" 1 "an https upstream needs tls ca=" "s.tsv:4:" "$S1"$'pve lan https://192.0.2.9:8006 sni=pve-host\n'
reject "http upstream with ca="       1 "tls must be - for an http upstream" "s.tsv:4:" "$S1"$'wiki lan http://192.0.2.30:80 ca=pve-root-ca.pem\n'
reject "CA file missing"              1 "CA file $cas/absent.pem not found" "s.tsv:4:" "$S1"$'pve lan https://192.0.2.9:8006 ca=absent.pem\n'
reject "bad lan_ip in hosts"          1 "lan_ip is not an IPv4 address" "h.tsv:3:" "$S1" "$H1"$'box not-an-ip -\n'
reject "short lan_ip in hosts"        1 "lan_ip is not an IPv4 address" "h.tsv:3:" "$S1" "$H1"$'box 192.0.2 -\n'
reject "bad tailnet_ip in hosts"      1 "tailnet_ip must be an IPv4 address or -" "h.tsv:3:" "$S1" "$H1"$'box 192.0.2.40 nope\n'
reject "too many fields in hosts"     1 "too many fields" "h.tsv:3:" "$S1" "$H1"$'box 192.0.2.40 - extra\n'
reject "bad host name"                1 "bad name 'Box'" "h.tsv:3:" "$S1" "$H1"$'Box 192.0.2.40 -\n'

echo
echo "== render.sh accepts what the upstream rule allows, and strips CRLF =="
acceptfail=0
for up in http://192.0.2.30:80 https://host.example:8443 http://a:1 'http://[2001:db8::1]:65535' http://my-host.lan:0080; do
  tls=-; [[ "$up" == https://* ]] && tls=ca=pve-root-ca.pem
  printf 'wiki lan %s %s\n' "$up" "$tls" >"$tmp/up.tsv"
  render "$tmp/up.tsv" "$tmp/empty.tsv" "$tmp/o-up" 2>"$tmp/up.err" \
    || { no "upstream $up was rejected: $(cat "$tmp/up.err")"; acceptfail=1; continue; }
  grep -qF "reverse_proxy $up {" "$tmp/o-up/Caddyfile" || { no "upstream $up was not proxied as written"; acceptfail=1; }
done
(( acceptfail )) || ok "host name, IPv4, bracketed IPv6, port 1 and port 65535 are accepted"
printf '# sites\r\n\r\ngrafana\tlan\thttp://192.0.2.29:80\t-\r\n' >"$tmp/crlf-s.tsv"
printf '# hosts\r\nnas 192.0.2.30 -\r\n' >"$tmp/crlf-h.tsv"
if render "$tmp/crlf-s.tsv" "$tmp/crlf-h.tsv" "$tmp/o-crlf" 2>"$tmp/crlf.err" \
   && ! grep -q $'\r' "$tmp/o-crlf"/*; then
  ok "CRLF line endings are stripped: input accepted, no CR in any output file"
else
  no "CRLF input was rejected or leaked a CR into the output: $(cat "$tmp/crlf.err")"
fi

echo
echo "== render.sh usage errors exit 2 =="
usefail=0
./render.sh --sites sites.example.tsv --hosts hosts.example.tsv --lan-ip nope --tailnet-ip "$TS" --cas "$cas" --out "$tmp/o-u1" >/dev/null 2>&1; rc=$?
(( rc == 2 )) || { no "a bad --lan-ip exited $rc, wanted 2"; usefail=1; }
./render.sh --sites sites.example.tsv --hosts hosts.example.tsv --lan-ip "$LAN" --tailnet-ip nope --cas "$cas" --out "$tmp/o-u2" >/dev/null 2>&1; rc=$?
(( rc == 2 )) || { no "a bad --tailnet-ip exited $rc, wanted 2"; usefail=1; }
./render.sh --sites sites.example.tsv >/dev/null 2>&1; rc=$?
(( rc == 2 )) || { no "missing options exited $rc, wanted 2"; usefail=1; }
./render.sh --frobnicate >/dev/null 2>&1; rc=$?
(( rc == 2 )) || { no "an unknown option exited $rc, wanted 2"; usefail=1; }
for opt in --sites --hosts --lan-ip --tailnet-ip --cas --out; do
  err="$(./render.sh "$opt" 2>&1 >/dev/null)"; rc=$?
  (( rc == 2 )) || { no "$opt with no value exited $rc, wanted 2"; usefail=1; }
  grep -qF -- "$opt needs a value" <<<"$err" || { no "$opt with no value did not say so: $err"; usefail=1; }
  grep -qF "unbound variable" <<<"$err" && { no "$opt with no value crashed on an unbound variable"; usefail=1; }
done
./render.sh --help >/dev/null 2>&1; rc=$?
(( rc == 0 )) || { no "--help exited $rc, wanted 0"; usefail=1; }
(( usefail )) || ok "bad address, missing option, option without a value and unknown option exit 2; --help exits 0"

echo
echo "== Caddy accepts the rendered Caddyfile ($CADDY_IMAGE) =="
if ! command -v docker >/dev/null; then
  skip "docker is not installed, so the Caddyfile was not checked by Caddy"
elif ! docker image inspect "$CADDY_IMAGE" >/dev/null 2>&1 && ! docker pull -q "$CADDY_IMAGE" >/dev/null 2>&1; then
  skip "cannot pull $CADDY_IMAGE (no network?), so the Caddyfile was not checked by Caddy"
else
  caddy_run() { docker run --rm -v "$tmp:$tmp:ro" "$CADDY_IMAGE" caddy "$@"; }
  if caddy_run adapt --config "$out/Caddyfile" --adapter caddyfile >"$tmp/adapted.json" 2>"$tmp/adapt.err"; then
    ok "caddy adapt accepts the Caddyfile rendered from the example files"
  else
    no "caddy adapt rejected the Caddyfile: $(head -5 "$tmp/adapt.err")"
  fi
  hostsfail=0
  for n in "${SITE_NAMES[@]}"; do
    grep -qF "\"$n.lan\"" "$tmp/adapted.json" || { no "adapted config has no host $n.lan"; hostsfail=1; }
  done
  grep -qF '"search_regexp":"^http://([a-z0-9-]+\\.lan)([:/]|$)"' "$tmp/adapted.json" || { no "adapted config lacks the .lan-only Location regex"; hostsfail=1; }
  grep -qF '"replace":"https://$1$2"' "$tmp/adapted.json" || { no "adapted config lacks the https://\$1\$2 replacement"; hostsfail=1; }
  grep -qF '"remote_ip"' "$tmp/adapted.json" || { no "adapted config has no remote_ip matcher for tailnet_only"; hostsfail=1; }
  grep -qF '"server_name":"pve-host"' "$tmp/adapted.json" || { no "adapted config lacks the upstream server_name"; hostsfail=1; }
  (( hostsfail )) || ok "the adapted config serves every site, restricts tailnet sites and sets the upstream server_name"
  # Negative control: Caddy must be able to reject what we feed it.
  printf 'broken.lan {\n\treverse_proxy\n\tnot_a_directive\n' >"$tmp/broken.Caddyfile"
  if caddy_run adapt --config "$tmp/broken.Caddyfile" --adapter caddyfile >/dev/null 2>&1; then
    no "negative control: caddy adapt accepted a broken Caddyfile, so the check above proves nothing"
  else
    ok "negative control: caddy adapt rejects a broken Caddyfile"
  fi
  if (( REAL_PEM )); then
    if caddy_run validate --config "$out/Caddyfile" --adapter caddyfile >/dev/null 2>"$tmp/validate.err"; then
      ok "caddy validate accepts the Caddyfile, loading both CA files"
    else
      no "caddy validate rejected the Caddyfile: $(tail -3 "$tmp/validate.err")"
    fi
  else
    skip "openssl is not installed, so no real PEM files for caddy validate"
  fi
fi

echo
echo "== dnsmasq accepts both rendered configs ($DNSMASQ_IMAGE) =="
if ! command -v docker >/dev/null; then
  skip "docker is not installed, so dnsmasq --test was not run"
else
  # Exit 90 from the container means the package could not be installed.
  docker run --rm -v "$out:/t:ro" "$DNSMASQ_IMAGE" sh -c '
    export DEBIAN_FRONTEND=noninteractive
    { apt-get update -qq && apt-get install -y -qq --no-install-recommends dnsmasq-base; } >/dev/null 2>&1 || exit 90
    mkdir /w
    for i in lan tailnet; do
      sed "s#/etc/ingress/rendered/#/t/#" /t/dnsmasq-$i.conf >/w/$i.conf
      if dnsmasq --test --conf-file=/w/$i.conf >/dev/null 2>&1; then echo "OK $i"; else echo "BAD $i"; fi
    done
    sed "s#/etc/ingress/rendered/#/t/#" /t/dnsmasq-lan.conf >/w/broken.conf
    echo "no-such-dnsmasq-option=1" >>/w/broken.conf
    if dnsmasq --test --conf-file=/w/broken.conf >/dev/null 2>&1; then echo "OK broken"; else echo "BAD broken"; fi
  ' >"$tmp/dnsmasq.out" 2>"$tmp/dnsmasq.err"; rc=$?
  if (( rc != 0 )); then
    skip "cannot run $DNSMASQ_IMAGE or install dnsmasq-base (rc=$rc, no network?), so dnsmasq --test was not run"
  else
    dmfail=0
    for i in lan tailnet; do
      grep -qxF "OK $i" "$tmp/dnsmasq.out" || { no "dnsmasq --test rejected dnsmasq-$i.conf"; dmfail=1; }
    done
    (( dmfail )) || ok "dnsmasq --test accepts dnsmasq-lan.conf and dnsmasq-tailnet.conf"
    if grep -qxF 'BAD broken' "$tmp/dnsmasq.out"; then
      ok "negative control: dnsmasq --test rejects a config with an unknown option"
    else
      no "negative control: dnsmasq --test accepted a broken config, so the check above proves nothing"
    fi
  fi
fi

echo
printf '%d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
(( FAIL == 0 ))
