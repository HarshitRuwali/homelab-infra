#!/usr/bin/env bash
#
# install-test.sh - run install.sh itself, in a throwaway debian:13 container
# with no network, against the real Caddy binary (taken from caddy:2.11) and
# stand-ins for systemctl, dnsmasq, tailscale, dig, curl, ip and openssl that
# log what they are asked and fail on demand. It checks:
#
#   - preflight requires curl, openssl, ip and runuser, and refuses a CA file
#     the caddy user cannot read;
#   - caddy validate runs as the caddy user with HOME and the XDG directories
#     inside the scratch directory, and nothing is written outside it;
#   - a dry run changes nothing and prints every action;
#   - Caddy is reloaded before the DNS instances are restarted;
#   - a failed Caddy reload, or a failed DNS restart, puts the previous files
#     back and exits non-zero;
#   - dnsmasq.service is disabled only when the unit exists;
#   - the https check targets a lan site from 127.0.0.1, or a tailnet site from
#     the tailnet address when there is no lan site.
#
#   ./tests/install-test.sh
#
# Needs docker with debian:13 and caddy:2.11 (and openssl on the host for a
# throwaway CA file); without them it prints why and skips. Nothing on the
# host is touched: install.sh runs as root inside the container only.
# shellcheck disable=SC2016,SC2329  # literal $ in patterns; helpers are passed to check by name
set -uo pipefail

# ---------------------------------------------------------------- inside ----
if [[ "${1:-}" == --inside ]]; then
  PASS=0; FAIL=0
  ok() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
  no() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
  check() { local label="$1"; shift; if "$@"; then ok "$label"; else no "$label"; fi; }

  mkdir -p /stubs /log; chmod 0755 /stubs; chmod 1777 /log
  cp /t/caddy-real /stubs/caddy-real; chmod 0755 /stubs/caddy-real
  stub() { printf '#!/bin/sh\n%s\n' "$2" >"/stubs/$1"; chmod 0755 "/stubs/$1"; }
  stub caddy 'if [ "$1" = validate ]; then
  echo "caddy-validate user=$(id -un) HOME=$HOME XDG_DATA_HOME=${XDG_DATA_HOME:-} XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-}" >>/log/calls
  /stubs/caddy-real "$@"; rc=$?
  base="${XDG_DATA_HOME:-$HOME/.local/share}"
  echo "caddy-validate-wrote $(find "$base" -type f 2>/dev/null | wc -l) rc=$rc" >>/log/calls
  exit $rc
fi
exec /stubs/caddy-real "$@"'
  stub dnsmasq 'case "$1" in --version) echo "Dnsmasq version 2.91  Copyright (c) 2000-2025 Simon Kelley" ;; esac
echo "dnsmasq $*" >>/log/calls'
  stub tailscale 'echo 100.64.0.50'
  stub ip 'echo "1.1.1.1 via 192.0.2.1 dev eth0 src 192.0.2.50 uid 0"'
  stub dig 'echo 192.0.2.50'
  stub curl 'echo "curl $*" >>/log/calls; printf 200'
  stub openssl 'echo "sha256 Fingerprint=AA:BB:CC"'
  stub systemctl 'echo "systemctl $*" >>/log/calls
case "$*" in
  "cat dnsmasq.service") [ -e /stubs/has-dnsmasq-unit ]; exit $? ;;
  "reload-or-restart caddy.service") grep -q failcaddy.lan /etc/caddy/Caddyfile && exit 1 ;;
  "restart ingress-dns@tailnet.service") grep -q faildns.lan /etc/ingress/rendered/hosts-tailnet 2>/dev/null && exit 1 ;;
esac
exit 0'
  export PATH="/stubs:$PATH"
  useradd --system --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy

  OLD="# previous content"
  fresh() { # fresh [oldfiles]: reset every path install.sh touches
    rm -rf /etc/ingress /etc/caddy /var/lib/caddy /root/.local /log/calls /tmp/tmp.*
    mkdir -p /etc/ingress/cas /etc/caddy; install -d -o caddy -g caddy /var/lib/caddy
    cp /t/pve-root-ca.pem /etc/ingress/cas/; chmod 0644 /etc/ingress/cas/pve-root-ca.pem
    printf '%s\n' 'grafana  lan  http://192.0.2.29:80  -' \
                  'pve      lan  https://192.0.2.9:8006  ca=pve-root-ca.pem' \
                  'qb       tailnet  http://100.64.0.10:8081  -' >/etc/ingress/sites.tsv
    printf '%s\n' 'nas 192.0.2.30 -' >/etc/ingress/hosts.tsv
    printf '%s\n' "$OLD" >/etc/caddy/Caddyfile
    if [[ "${1:-}" == oldfiles ]]; then
      mkdir -p /etc/ingress/rendered
      for f in hosts-lan hosts-tailnet dnsmasq-lan.conf dnsmasq-tailnet.conf; do printf '%s %s\n' "$OLD" "$f" >"/etc/ingress/rendered/$f"; done
    fi
    : >/log/calls; chmod 0666 /log/calls
  }
  clean() { sed 's/\x1b\[[0-9;]*m//g' "$1"; }
  # inst [args]: run install.sh, output (ANSI removed) in /tmp/out.txt, status in $rc
  inst() { /ingress/install.sh "$@" >/tmp/raw.txt 2>&1; rc=$?; clean /tmp/raw.txt >/tmp/out.txt; }
  out_has()   { grep -qF -- "$1" /tmp/out.txt; }
  calls_has() { grep -qE -- "$1" /log/calls; }
  nth() { grep -nE -- "$2" "$1" | sed -n "${3:-1}{s/:.*//;p}"; }       # nth <file> <regex> [n]: its line number
  before() { local a b; a="$(nth "$1" "$2")"; b="$(nth "$1" "$3")"; [[ -n "$a" && -n "$b" ]] && (( a < b )); }
  count() { grep -cE -- "$2" "$1"; }
  same() { [[ "$(cat "$1")" == "$2" ]]; }
  nothing_left_in_tmp() { ! compgen -G '/tmp/tmp.*' >/dev/null; }

  echo "== preflight =="
  for b in curl openssl ip; do
    fresh; mv "/stubs/$b" "/stubs/$b.off"; inst
    check "a missing $b stops the preflight" bash -c "(( $rc == 1 )) && grep -qF '$b is not installed' /tmp/out.txt"
    mv "/stubs/$b.off" "/stubs/$b"
  done
  fresh; mv /usr/sbin/runuser /usr/sbin/runuser.off; inst
  check "a missing runuser stops the preflight" bash -c "(( $rc == 1 )) && grep -qF 'runuser is not installed' /tmp/out.txt"
  mv /usr/sbin/runuser.off /usr/sbin/runuser

  fresh; chmod 0600 /etc/ingress/cas/pve-root-ca.pem; inst
  check "a CA file the caddy user cannot read fails the preflight" bash -c "(( $rc == 1 ))"
  check "  the message names the file and suggests chmod 0644" bash -c "grep -qF '/etc/ingress/cas/pve-root-ca.pem' /tmp/out.txt && grep -qF 'not readable by the caddy user: chmod 0644' /tmp/out.txt"
  check "  nothing was rendered or validated" bash -c "! grep -q caddy-validate /log/calls && [[ ! -e /etc/ingress/rendered ]]"
  check "  it is root-readable, so a check made as root would have passed" test -r /etc/ingress/cas/pve-root-ca.pem
  fresh; chmod 0700 /etc/ingress/cas; inst
  check "a cas directory the caddy user cannot enter fails the preflight" bash -c "(( $rc == 1 )) && grep -qF 'not readable by the caddy user' /tmp/out.txt"

  echo
  echo "== dry run =="
  fresh; inst
  check "exits 0 and says it is a dry run" bash -c "(( $rc == 0 )) && grep -qF 'DRY RUN' /tmp/out.txt && grep -qF 'This was a dry run' /tmp/out.txt"
  check "the CA files are checked as the caddy user" out_has "every file in /etc/ingress/cas is readable by the caddy user"
  check "caddy validate ran as the caddy user" calls_has '^caddy-validate user=caddy '
  check "  with HOME and both XDG directories inside the scratch directory" calls_has '^caddy-validate user=caddy HOME=/tmp/tmp\.[^/]+/caddy-home XDG_DATA_HOME=/tmp/tmp\.[^/]+/caddy-home/data XDG_CONFIG_HOME=/tmp/tmp\.[^/]+/caddy-home/config$'
  check "  and wrote its state there (so the redirect is what keeps it out of /root)" calls_has '^caddy-validate-wrote [1-9][0-9]* rc=0$'
  check "nothing under /root/.local or /var/lib/caddy" bash -c "[[ ! -e /root/.local ]] && [[ -z \"\$(ls -A /var/lib/caddy)\" ]]"
  check "the scratch directory is gone" nothing_left_in_tmp
  check "the live Caddyfile is unchanged" same /etc/caddy/Caddyfile "$OLD"
  check "no rendered files, no root copy, no unit installed" bash -c "[[ ! -e /etc/ingress/rendered && ! -e /etc/ingress/ca-root.crt && ! -e /etc/systemd/system/ingress-dns@.service ]]"
  check "no service was touched" bash -c "! grep -qE '^systemctl (enable|disable|restart|reload|daemon)' /log/calls"
  check "every action is printed: backup of the live Caddyfile" out_has "+ cp -p /etc/caddy/Caddyfile "
  check "  install of the Caddyfile and the unit" bash -c "grep -qF '+ install -m 0644 /tmp/' /tmp/out.txt && grep -qF '/etc/caddy/Caddyfile' /tmp/out.txt && grep -qF '/etc/systemd/system/ingress-dns@.service' /tmp/out.txt"
  check "  Caddy reload is printed before the DNS restarts" before /tmp/out.txt '\+ systemctl reload-or-restart caddy.service' '\+ systemctl restart ingress-dns@lan.service'
  check "  and the lan restart before the tailnet restart" before /tmp/out.txt '\+ systemctl restart ingress-dns@lan.service' '\+ systemctl restart ingress-dns@tailnet.service'
  check "with no dnsmasq.service unit, nothing is disabled" bash -c "grep -qF 'no dnsmasq.service unit' /tmp/out.txt && ! grep -qF 'disable --now dnsmasq.service' /tmp/out.txt"
  fresh; touch /stubs/has-dnsmasq-unit; inst
  check "with a dnsmasq.service unit, the dry run prints disabling it" out_has "+ systemctl disable --now dnsmasq.service"
  rm -f /stubs/has-dnsmasq-unit

  fresh; userdel caddy; inst
  check "without a caddy user it warns and validates as root" bash -c "(( $rc == 0 )) && grep -qF \"there is no 'caddy' user\" /tmp/out.txt && grep -q '^caddy-validate user=root HOME=/tmp/tmp\\.[^/]*/caddy-home ' /log/calls"
  check "  and still writes nothing under /root/.local" test ! -e /root/.local
  useradd --system --home-dir /var/lib/caddy --shell /usr/sbin/nologin caddy

  echo
  echo "== apply =="
  mkdir_root() { mkdir -p /var/lib/caddy/.local/share/caddy/pki/authorities/local; echo cert >/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt; }
  fresh oldfiles; mkdir_root; inst --apply
  check "exits 0 and reaches done" bash -c "(( $rc == 0 )) && tail -1 /tmp/out.txt | grep -qx done"
  check "the new files are live" bash -c "grep -qF 'grafana.lan {' /etc/caddy/Caddyfile && grep -qF 'grafana.lan' /etc/ingress/rendered/hosts-lan && ! grep -qF '$OLD' /etc/ingress/rendered/hosts-tailnet"
  check "the root certificate was copied" test -s /etc/ingress/ca-root.crt
  check "Caddy is reloaded before either DNS instance is restarted" before /log/calls '^systemctl reload-or-restart caddy.service' '^systemctl restart ingress-dns@lan.service'
  check "  and the tailnet instance after the lan one" before /log/calls '^systemctl restart ingress-dns@lan.service' '^systemctl restart ingress-dns@tailnet.service'
  check "no rollback on success: one reload, one restart each" bash -c "[[ \$(grep -cE '^systemctl reload-or-restart caddy.service' /log/calls) == 1 ]] && [[ \$(grep -cE '^systemctl restart ingress-dns@' /log/calls) == 2 ]]"
  check "the https check targets the first lan site from 127.0.0.1" calls_has '^curl .*--resolve grafana\.lan:443:127\.0\.0\.1 https://grafana\.lan/'
  check "the scratch directory is gone" nothing_left_in_tmp
  check "no dnsmasq.service unit, so no disable call" bash -c "! grep -qE '^systemctl disable' /log/calls"
  fresh; touch /stubs/has-dnsmasq-unit; mkdir_root; inst --apply
  check "with the unit present it is disabled, before the daemon-reload" before /log/calls '^systemctl disable --now dnsmasq.service' '^systemctl daemon-reload'
  rm -f /stubs/has-dnsmasq-unit

  fresh; mkdir_root; printf '%s\n' 'qb       tailnet  http://100.64.0.10:8081  -' 'lm tailnet http://100.64.0.22:8080 -' >/etc/ingress/sites.tsv
  inst --apply
  check "with only tailnet sites, the first is checked from the tailnet address" bash -c "(( $rc == 0 )) && grep -qE '^curl .*--resolve qb\\.lan:443:100\\.64\\.0\\.50 https://qb\\.lan/' /log/calls"

  echo
  echo "== a service that refuses the new files =="
  printf '%s\n' 'grafana  lan  http://192.0.2.29:80  -' 'failcaddy lan http://192.0.2.31:80 -' >/tmp/sites.failcaddy
  fresh oldfiles; mkdir_root; cp /tmp/sites.failcaddy /etc/ingress/sites.tsv; inst --apply
  check "a failed Caddy reload exits 1 and says the files were restored" bash -c "(( $rc == 1 )) && grep -qF 'Caddy refused the new Caddyfile; restored the previous files' /tmp/out.txt"
  check "  the Caddyfile is the previous one" same /etc/caddy/Caddyfile "$OLD"
  check "  the four rendered files are the previous ones" bash -c "for f in hosts-lan hosts-tailnet dnsmasq-lan.conf dnsmasq-tailnet.conf; do [[ \$(cat /etc/ingress/rendered/\$f) == '$OLD '\$f ]] || exit 1; done"
  check "  Caddy was reloaded again after the restore" bash -c "[[ \$(grep -cE '^systemctl reload-or-restart caddy.service' /log/calls) == 2 ]]"
  check "  the DNS instances were never restarted" bash -c "! grep -qE '^systemctl restart ingress-dns@' /log/calls"
  check "  there is no root copy and no verify output" bash -c "[[ ! -e /etc/ingress/ca-root.crt ]] && ! grep -qx done /tmp/out.txt"
  fresh; mkdir_root; cp /tmp/sites.failcaddy /etc/ingress/sites.tsv; inst --apply
  check "on a first install a failed reload removes the files that were not there before" bash -c "(( $rc == 1 )) && [[ ! -e /etc/ingress/rendered/hosts-lan && ! -e /etc/ingress/rendered/dnsmasq-tailnet.conf ]] && grep -qxF '$OLD' /etc/caddy/Caddyfile"

  printf '%s\n' 'grafana  lan  http://192.0.2.29:80  -' 'faildns lan http://192.0.2.31:80 -' >/tmp/sites.faildns
  fresh oldfiles; mkdir_root; cp /tmp/sites.faildns /etc/ingress/sites.tsv; inst --apply
  check "a failed DNS restart exits 1 and says the files were restored" bash -c "(( $rc == 1 )) && grep -qF 'ingress-dns@tailnet failed with the new files; restored the previous files' /tmp/out.txt"
  check "  the Caddyfile and the four rendered files are the previous ones" bash -c "[[ \$(cat /etc/caddy/Caddyfile) == '$OLD' ]] && for f in hosts-lan hosts-tailnet dnsmasq-lan.conf dnsmasq-tailnet.conf; do [[ \$(cat /etc/ingress/rendered/\$f) == '$OLD '\$f ]] || exit 1; done"
  check "  Caddy was reloaded again with the previous Caddyfile" bash -c "[[ \$(grep -cE '^systemctl reload-or-restart caddy.service' /log/calls) == 2 ]]"
  check "  both DNS instances were restarted again with the previous files" bash -c "[[ \$(grep -cE '^systemctl restart ingress-dns@lan.service' /log/calls) == 2 && \$(grep -cE '^systemctl restart ingress-dns@tailnet.service' /log/calls) == 2 ]]"
  check "  the failing restart came after the Caddy reload" before /log/calls '^systemctl reload-or-restart caddy.service' '^systemctl restart ingress-dns@tailnet.service'
  check "  the scratch directory is gone" nothing_left_in_tmp

  echo
  printf '%d passed, %d failed, 0 skipped\n' "$PASS" "$FAIL"
  (( FAIL == 0 ))
  exit
fi

# ------------------------------------------------------------------- host ----
self="$(readlink -f "${BASH_SOURCE[0]}")"
cd "$(dirname "$self")/.." || exit 1
echo "== install.sh in a container with the real Caddy and stand-in services =="
skipall() { printf '  \033[33mSKIP\033[0m %s\n\n0 passed, 0 failed, 1 skipped\n' "$*"; exit 0; }
command -v docker >/dev/null || skipall "docker is not installed, so install.sh was not run"
command -v openssl >/dev/null || skipall "openssl is not installed, so there is no throwaway CA file for the run"
for img in caddy:2.11 debian:13; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null 2>&1 \
    || skipall "cannot pull $img (no network?), so install.sh was not run"
done
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
docker run --rm --entrypoint cat caddy:2.11 /usr/bin/caddy >"$tmp/caddy-real" 2>/dev/null \
  || skipall "cannot read the caddy binary out of caddy:2.11"
chmod 0755 "$tmp/caddy-real"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "$tmp/key.pem" \
  -out "$tmp/pve-root-ca.pem" -subj "/CN=test-pve" -days 2 2>/dev/null || skipall "openssl could not make a CA file"
chmod 0644 "$tmp/pve-root-ca.pem"
cp "$self" "$tmp/install-test.sh" || exit 1
docker run --rm --network none -v "$PWD:/ingress:ro" -v "$tmp:/t:ro" debian:13 bash /t/install-test.sh --inside
