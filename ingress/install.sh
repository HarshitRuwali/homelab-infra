#!/usr/bin/env bash
# Install or update the .lan names on the ingress host: Caddy (HTTPS reverse
# proxy with its own CA) and two dnsmasq instances (LAN and tailnet answers).
# Dry run by default: prints every action and changes nothing.
#
# Idempotent. Edit /etc/ingress/sites.tsv or hosts.tsv, then re-run with
# --apply. Everything is rendered and validated before anything live is
# replaced, so a bad edit never takes the running config down; if a service
# still refuses the new files, the previous ones are put back.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETC=/etc/ingress
APPLY=0

usage() {
  cat <<'USAGE'
usage: install.sh [--apply]
       install.sh -h | --help

Render /etc/ingress/sites.tsv and hosts.tsv into the Caddyfile and the two
dnsmasq configs, validate them, install them, reload Caddy and restart the
two DNS instances. If a service fails, the previous files are restored.

  --apply     do it; without this flag nothing is changed
  -h, --help  show this help

Run on the ingress host as root. It must already be on the tailnet
(`tailscale ip -4` answers). Upstream CA certificates go in /etc/ingress/cas.
USAGE
}

while (($#)); do
  case "$1" in
    --apply) APPLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '%s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*" >&2; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
die()  { printf '  \033[31mfail\033[0m %s\n' "$*" >&2; exit 1; }
run()  { printf '       + %s\n' "$*"; if (( APPLY )); then "$@"; fi; }

say "MODE: $( (( APPLY )) && echo APPLY || echo 'DRY RUN, add --apply to execute')"
say "preflight"
(( EUID == 0 )) || die "run as root"
for b in caddy dnsmasq tailscale dig curl openssl ip runuser; do
  command -v "$b" >/dev/null || die "$b is not installed (apt install caddy dnsmasq tailscale bind9-dnsutils curl openssl iproute2 util-linux)"
done
ok "caddy $(caddy version | awk '{print $1}'), $(dnsmasq --version | head -1 | awk '{print $1, $3}')"
for f in sites.tsv hosts.tsv; do
  [[ -f "$ETC/$f" ]] || die "$ETC/$f is missing: start from $HERE/${f%.tsv}.example.tsv"
done
LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
[[ -n "$LAN_IP" ]] || die "cannot work out the LAN address"
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
[[ -n "$TS_IP" ]] || die "not on the tailnet yet: run tailscale up first"
ok "LAN $LAN_IP, tailnet $TS_IP"

# Caddy runs as the caddy user, so a file only root can read passes a check
# made as root and then breaks Caddy at its next start. Check as that user.
CADDY_USER=caddy
if ! id -u "$CADDY_USER" >/dev/null 2>&1; then
  CADDY_USER=""
  warn "there is no 'caddy' user: checking as root, so a file Caddy cannot read will not be caught"
fi
as_caddy() { if [[ -n "$CADDY_USER" ]]; then runuser -u "$CADDY_USER" -- "$@"; else "$@"; fi; }
if [[ -n "$CADDY_USER" && -d "$ETC/cas" ]]; then
  unreadable=()
  for f in "$ETC"/cas/*; do
    [[ -f "$f" ]] || continue
    as_caddy test -r "$f" || unreadable+=("$f")
  done
  if (( ${#unreadable[@]} )); then
    printf '       %s\n' "${unreadable[@]}" >&2
    die "the files above are not readable by the caddy user: chmod 0644 each (CA certificates are public)"
  fi
  ok "every file in $ETC/cas is readable by the caddy user"
fi

say "render and validate"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
"$HERE/render.sh" --sites "$ETC/sites.tsv" --hosts "$ETC/hosts.tsv" \
  --lan-ip "$LAN_IP" --tailnet-ip "$TS_IP" --cas "$ETC/cas" --out "$work"
# caddy validate sets up its local CA and writes private keys under $HOME, even
# in a dry run: point HOME and the XDG directories into the scratch directory,
# owned by the user that runs the check, so nothing outside it is written and
# the trap removes it.
vhome="$work/caddy-home"
chmod 0755 "$work"; chmod 0644 "$work/Caddyfile"
if [[ -n "$CADDY_USER" ]]; then install -d -o "$CADDY_USER" -g "$CADDY_USER" -m 0700 "$vhome"
else install -d -m 0700 "$vhome"; fi
as_caddy env HOME="$vhome" XDG_DATA_HOME="$vhome/data" XDG_CONFIG_HOME="$vhome/config" \
    caddy validate --config "$work/Caddyfile" --adapter caddyfile >/dev/null 2>"$work/caddy.err" \
  || { cat "$work/caddy.err" >&2; die "the rendered Caddyfile does not validate"; }
ok "Caddyfile validates${CADDY_USER:+ as the $CADDY_USER user}"
for i in lan tailnet; do
  sed "s#/etc/ingress/rendered/#$work/#" "$work/dnsmasq-$i.conf" >"$work/test-$i.conf"
  dnsmasq --test --conf-file="$work/test-$i.conf" >/dev/null 2>&1 || die "dnsmasq-$i.conf does not validate"
done
ok "both dnsmasq configs validate"

# The five live files this run replaces. They are copied into the scratch
# directory first, so a service that refuses the new ones gets the old back.
LIVE=("$ETC/rendered/hosts-lan" "$ETC/rendered/hosts-tailnet"
      "$ETC/rendered/dnsmasq-lan.conf" "$ETC/rendered/dnsmasq-tailnet.conf"
      /etc/caddy/Caddyfile)
mkdir -p "$work/backup"
restore_files() { # put the backed-up files back; remove one that did not exist before
  local k
  for k in "${!LIVE[@]}"; do
    if [[ -e "$work/backup/$k" ]]; then cp -p "$work/backup/$k" "${LIVE[$k]}"; else rm -f "${LIVE[$k]}"; fi
  done
}
rollback() { # rollback caddy | dns
  local j
  warn "restoring the previous files"
  restore_files
  systemctl reload-or-restart caddy.service || warn "could not reload Caddy with the previous Caddyfile"
  if [[ "$1" == dns ]]; then
    for j in lan tailnet; do
      systemctl restart ingress-dns@"$j".service || warn "could not restart ingress-dns@$j with the previous files"
    done
  fi
}

say "install"
for k in "${!LIVE[@]}"; do
  if [[ -e "${LIVE[$k]}" ]]; then run cp -p "${LIVE[$k]}" "$work/backup/$k"; fi
done
run install -d -m 0755 "$ETC/rendered"
for f in hosts-lan hosts-tailnet dnsmasq-lan.conf dnsmasq-tailnet.conf; do
  run install -m 0644 "$work/$f" "$ETC/rendered/$f"
done
run install -m 0644 "$work/Caddyfile" /etc/caddy/Caddyfile
run install -m 0644 "$HERE/systemd/ingress-dns@.service" /etc/systemd/system/ingress-dns@.service

say "services"
# The packaged dnsmasq listens on every address and would hold port 53. A
# host with only dnsmasq-base has no such unit.
if systemctl cat dnsmasq.service >/dev/null 2>&1; then
  run systemctl disable --now dnsmasq.service
else
  say "       (no dnsmasq.service unit, nothing to disable)"
fi
run systemctl daemon-reload
# Caddy first: it is the file most likely to be refused, and the DNS instances
# are left alone until it has accepted the new one.
run systemctl enable caddy.service
run systemctl reload-or-restart caddy.service \
  || { rollback caddy; die "Caddy refused the new Caddyfile; restored the previous files and reloaded (journalctl -u caddy)"; }
for i in lan tailnet; do
  { run systemctl enable ingress-dns@"$i".service && run systemctl restart ingress-dns@"$i".service; } \
    || { rollback dns; die "ingress-dns@$i failed with the new files; restored the previous files and restarted (journalctl -u ingress-dns@$i)"; }
done

(( APPLY )) || { say ""; say "This was a dry run. Re-run with --apply."; exit 0; }

say "verify"
root=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
for _ in $(seq 30); do [[ -s "$root" ]] && break; sleep 1; done
[[ -s "$root" ]] || die "Caddy has not created its root certificate"
install -m 0644 "$root" "$ETC/ca-root.crt"
ok "root certificate at $ETC/ca-root.crt ($(openssl x509 -in "$root" -noout -fingerprint -sha256 | cut -d= -f2))"
first="$(awk '!/^[[:space:]]*(#|$)/{print $1; exit}' "$ETC/sites.tsv")"
for pair in "lan:$LAN_IP" "tailnet:$TS_IP"; do
  ans="$(dig +short +time=2 +tries=1 "@${pair#*:}" "$first.lan" A || true)"
  if [[ -n "$ans" ]]; then ok "${pair%%:*} DNS: $first.lan -> $ans"; else die "${pair%%:*} DNS does not answer $first.lan"; fi
done
ext="$(dig +short +time=3 +tries=1 "@$LAN_IP" example.com A | head -1 || true)"
if [[ -n "$ext" ]]; then ok "forwards other names (example.com -> $ext)"; else die "does not forward names outside .lan"; fi
# A lan site answers any client, so check the first one from this host. With
# only tailnet sites, check the first from the tailnet address instead.
target="$("$HERE/verify-target.sh" "$ETC/sites.tsv" "$TS_IP")" || die "sites.tsv has no site to check over https"
vname="${target%% *}"; vaddr="${target#* }"
code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$ETC/ca-root.crt" \
          --resolve "$vname.lan:443:$vaddr" "https://$vname.lan/" || true)"
if [[ "$code" =~ ^[234] ]]; then ok "https://$vname.lan/ answers $code with a trusted certificate (connecting to $vaddr)"
else die "https://$vname.lan/ answered '$code' (connecting to $vaddr)"; fi
say "done"
