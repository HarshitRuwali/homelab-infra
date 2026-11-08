#!/usr/bin/env bash
# Install, update and check this repository's modules on the hosts they run on,
# over SSH, from a checkout. Dry run by default.
# shellcheck disable=SC2016,SC2001  # single quotes: that text runs on the remote host; sed indents output
set -euo pipefail

ROOT="${HOMELAB_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

usage() {
  cat <<'USAGE'
usage: homelab.sh list
       homelab.sh check   MODULE HOST [--role ROLE] [-i FILE] [--dest DIR]
       homelab.sh install MODULE HOST [--role ROLE] [-i FILE] [--dest DIR] [--apply [--force]] [-- MODULE_ARGS...]
       homelab.sh status  MODULE HOST [--role ROLE] [-i FILE] [--dest DIR]
       homelab.sh -h | --help

Install or update a module of this repository on the host it runs on, over
SSH, from this checkout. HOST is an ssh destination (alias, name or
user@host); for a controller-side module it is an Ansible inventory host.

  list      every module: where it runs, as whom, its prerequisites
  check     check the host's prerequisites; changes nothing
  install   check prerequisites, then run the module's own installer.
            Dry run by default: the module is copied to a temporary
            directory on the host, the files that would change are listed,
            and the module's own dry run runs there (modules without one
            print the command instead). Nothing on the host changes.
            With --apply: missing prerequisites that are plain apt packages
            are installed (others are named with their setup doc), the
            module is placed in DIR (the previous copy kept as DIR.prev,
            local files such as .env and data kept), and its installer runs.
  status    run the module's own health check on the host

  --role ROLE    for modules with roles: memory all|storage|api,
                 monitoring central|collector, security dns|crowdsec|wazuh|scan|provision
  -i FILE        ssh identity file
  --dest DIR     where the module lives on the host (see `list` for defaults)
  --apply        make the changes (install only)
  --force        with --apply: replace guarded files (a memory compose file)
                 that differ on the host; review the dry run first
  MODULE_ARGS    passed to the module's installer, e.g. -- --secrets aws

vault-viewer is handed to vault-viewer/deploy.sh when the checkout has it.
Exit status: 0 ok, 1 a check or step failed, 2 usage error.
USAGE
}
die_usage() { echo "homelab.sh: $1" >&2; usage >&2; exit 2; }

# --- module adapters ------------------------------------------------------------
# spec MODULE ROLE sets:
#   RUNAS     user | root | controller
#   SHIP      paths under ROOT to copy to the host (relative); DEST receives them
#   DEST      default directory on the host ($HOME is the remote user's home)
#   KEEP      paths under DEST kept across updates (local config, secrets, data)
#   NEEDS     prerequisites, space separated: cmd:NAME[=APT_PKG], or a special
#             check (compose, tun, systemd-user, aiohttp), or file:PATH
#   SETUP     the doc that explains prerequisites that are not apt packages
#   DRY       remote shell run in the module copy for a dry run ("" = none)
#   APPLY     remote shell run in DEST to install
#   STATUS    remote shell for status
#   WHERE     one line for `list`
#   GUARD     files in DEST that --apply will not replace when they differ,
#             unless --force (they may point at data elsewhere)
spec() {
  local m="$1" role="${2:-}"
  RUNAS=root; SHIP="$m"; DEST="/opt/homelab-infra/$m"; KEEP=""; NEEDS=""; SETUP="$m/README.md"
  DRY=""; APPLY=""; STATUS=""; WHERE=""; ROLES=""; GUARD=""
  case "$m" in
    vault-viewer)
      RUNAS=user; DEST='$HOME/homelab-infra/vault-viewer'; SETUP="vault-viewer/docs/index.md"
      NEEDS="cmd:python3=python3 systemd-user cmd:curl=curl"
      DRY='bash ./install.sh --dry-run "$@"'
      APPLY='bash ./install.sh "$@"'
      STATUS='systemctl --user is-active artifacts.service && systemctl --user status artifacts.service --no-pager | head -n 5'
      WHERE="any host with a tailnet address; installer args: --bind ADDR [--tailnet-user LOGIN] ROOT..." ;;
    aibox-model-queue)
      RUNAS=user; DEST='$HOME/homelab-infra/aibox-model-queue'
      NEEDS="cmd:curl=curl systemd-user aiohttp"
      DRY=''
      APPLY='bash ./install.sh "$@"'
      STATUS='curl -fsS "http://127.0.0.1:${MODEL_PROXY_PORT:-8099}/_proxy/status"'
      WHERE="the host whose agents call the GPU box (runs in place from DEST); optional arg: UPSTREAM_URL" ;;
    ingress)
      SETUP="ingress/docs/setup.md"
      NEEDS="cmd:caddy cmd:tailscale cmd:dnsmasq=dnsmasq cmd:dig=bind9-dnsutils cmd:curl=curl cmd:openssl=openssl
             cmd:ip=iproute2 cmd:runuser=util-linux tun file:/etc/ingress/sites.tsv file:/etc/ingress/hosts.tsv"
      DRY='bash ./install.sh'
      APPLY='bash ./install.sh --apply'
      STATUS='systemctl is-active caddy ingress-dns@lan ingress-dns@tailnet'
      WHERE="the Debian 13 ingress LXC on the LAN (runs in place from DEST)" ;;
    media)
      SETUP="media/docs/setup.md"
      NEEDS="cmd:docker compose tun cmd:findmnt=util-linux cmd:ss=iproute2"
      DRY='bash ./install.sh "$@"'
      APPLY='bash ./install.sh --apply "$@"'
      STATUS='docker compose -p media ps'
      WHERE="the Docker host with the media share (copies itself to /opt/media); args: [--no-tailscale-serve]" ;;
    s3-backup)
      SETUP="s3-backup/docs/setup.md"
      NEEDS="cmd:docker cmd:flock=util-linux"
      DRY='bash ./install.sh --check || true'
      APPLY='bash ./install.sh "$@"'
      STATUS="systemctl list-timers 's3-backup*' --no-pager"
      WHERE="the server with the backup HDD (copies itself to /opt/s3-backup); args: [--secrets file|aws]" ;;
    memory)
      ROLES="all storage api"; GUARD="docker-compose.yml"
      SETUP="memory/docs/getting-started/split-deployment.md"
      NEEDS='cmd:docker compose file:$DEST/.env'
      STATUS='docker compose ps'
      case "$role" in
        all)
          SETUP="memory/docs/getting-started/one-machine.md"
          KEEP=".env memory-service/data api-service/logs"
          DRY='docker compose config -q && echo "compose file is valid with the host'"'"'s .env"'
          APPLY='mkdir -p memory-service/data/qdrant memory-service/data/postgres memory-service/data/redis api-service/logs && docker compose up -d --build'
          WHERE="all: one Docker host runs the whole stack (in place from DEST; .env and data stay)" ;;
        storage)
          SHIP="memory/memory-service"; DEST="/opt/homelab-infra/memory-service"; KEEP=".env data"
          DRY='docker compose config -q && echo "compose file is valid with the host'"'"'s .env"'
          APPLY='mkdir -p data/qdrant data/postgres data/redis && docker compose up -d'
          WHERE="storage: the memory host (PostgreSQL, Qdrant, Redis; in place from DEST; .env and data stay)" ;;
        api)
          SHIP="memory/api-service"; DEST="/opt/homelab-infra/memory-api"; KEEP=".env logs"
          DRY='docker compose config -q && echo "compose file is valid with the host'"'"'s .env"'
          APPLY='mkdir -p logs && docker compose up -d --build'
          WHERE="api: the API host (FastAPI; in place from DEST; .env stays)" ;;
        *) WHERE="all: one host; storage + api: the split deployment" ;;
      esac ;;
    monitoring)
      ROLES="central collector"
      case "$role" in
        central)
          SHIP="monitoring"; KEEP=".env"; SETUP="monitoring/docs/getting-started/install.md"
          NEEDS='cmd:apt-get cmd:systemctl file:$DEST/.env'
          DRY='echo "no dry run in the module; --apply runs: scripts/lxc-update.sh central when Prometheus is installed, else scripts/lxc-install.sh central"'
          APPLY='if command -v prometheus >/dev/null 2>&1; then bash scripts/lxc-update.sh central "$@"; else bash scripts/lxc-install.sh central "$@"; fi'
          STATUS='systemctl is-active prometheus loki grafana-server alloy nginx'
          WHERE="central: the monitoring LXC (runs in place from DEST, .env stays)" ;;
        collector)
          RUNAS=controller; SHIP="ansible"; SETUP="ansible/README.md"
          DRY='ansible-playbook playbooks/onboard.yml --limit "$HOST" --check --diff'
          APPLY='ansible-playbook playbooks/onboard.yml --limit "$HOST"'
          STATUS='ansible-playbook playbooks/preflight.yml --limit "$HOST"'
          WHERE="collector: from this machine with Ansible; HOST is the inventory name" ;;
        *) WHERE="central: monitoring LXC; collector: every other host, through Ansible" ;;
      esac ;;
    security)
      ROLES="dns crowdsec wazuh scan provision"
      SETUP="security/docs/getting-started/index.md"
      case "$role" in
        provision)
          SHIP="security/provision-security-stack.sh"; DEST="/opt/homelab-infra/security-provision"
          NEEDS="cmd:pvesm cmd:pct cmd:qm"
          DRY='env -u APPLY bash ./provision-security-stack.sh "$@"'
          APPLY='env -u APPLY bash ./provision-security-stack.sh --apply "$@"'
          STATUS='pct list; qm list'
          WHERE="provision: the Proxmox host; creates the four security guests" ;;
        dns|crowdsec|wazuh|scan)
          SHIP="security/install"; DEST="/opt/homelab-infra/security-install"
          NEEDS="cmd:curl=curl"
          [ "$role" = crowdsec ] && NEEDS+=" file:/root/security-bootstrap/crowdsec.crt file:/root/security-bootstrap/crowdsec.key file:/root/security-bootstrap/ca.crt"
          [ "$role" = wazuh ] && NEEDS+=" file:/root/security-bootstrap/wazuh-enrollment.password file:/root/security-bootstrap/wazuh.crt file:/root/security-bootstrap/wazuh.key"
          DRY="env -u APPLY bash ./install-sec-$role.sh"
          APPLY="env -u APPLY bash ./install-sec-$role.sh --apply"
          case "$role" in
            dns) STATUS='systemctl is-active AdGuardHome' ;;
            crowdsec) STATUS='systemctl is-active crowdsec && cscli lapi status' ;;
            wazuh) STATUS='systemctl is-active wazuh-manager wazuh-indexer wazuh-dashboard' ;;
            scan) STATUS='docker compose -f /opt/greenbone/compose.yaml ps' ;;
          esac
          WHERE="$role: inside the sec-$role guest" ;;
        *) WHERE="provision: the Proxmox host; dns|crowdsec|wazuh|scan: inside each security guest" ;;
      esac ;;
    proxmox)
      SHIP="proxmox/pve-guest.sh"; DEST="/opt/homelab-infra/proxmox"
      NEEDS="cmd:qm cmd:pct cmd:pvesm cmd:pvesh"
      DRY='echo "--apply installs pve-guest.sh as /root/pve-guest.sh"'
      APPLY='install -m 0755 ./pve-guest.sh /root/pve-guest.sh && echo "installed /root/pve-guest.sh; run it with --help"'
      STATUS='/root/pve-guest.sh --help >/dev/null && echo "/root/pve-guest.sh is installed" && cmp -s ./pve-guest.sh /root/pve-guest.sh && echo "it matches the copy in DEST"'
      WHERE="the Proxmox host, as root (an operator CLI, not a service)" ;;
    ansible)
      RUNAS=controller; SETUP="ansible/README.md"
      DRY='ansible-playbook playbooks/preflight.yml --limit "$HOST"'
      APPLY='ansible-playbook playbooks/site.yml --limit "$HOST"'
      STATUS='ansible-playbook playbooks/preflight.yml --limit "$HOST"'
      WHERE="this machine (the controller); HOST is the inventory name; --apply runs site.yml for it" ;;
    *) return 1 ;;
  esac
}
squeeze() { tr -s ' \n\t' ' ' <<<"$1" | sed 's/^ //; s/ $//'; }
MODULES="vault-viewer aibox-model-queue ingress media s3-backup memory monitoring security proxmox ansible"

cmd_list() {
  local m r
  printf '%-27s %-10s %s\n' MODULE "RUNS AS" "WHERE"
  for m in $MODULES; do
    spec "$m" ""
    if [ -n "$ROLES" ]; then
      for r in $ROLES; do
        spec "$m" "$r"
        printf '%-27s %-10s %s\n' "$m --role $r" "$RUNAS" "$WHERE"
        [ -z "$NEEDS" ] || printf '%-27s %-10s needs: %s\n' "" "" "$(squeeze "$NEEDS")"
      done
    else
      printf '%-27s %-10s %s\n' "$m" "$RUNAS" "$WHERE"
      [ -z "$NEEDS" ] || printf '%-27s %-10s needs: %s\n' "" "" "$(squeeze "$NEEDS")"
    fi
  done
  echo
  echo "Defaults for --dest: /opt/homelab-infra/<module> (root), ~/homelab-infra/<module> (user)."
}

# --- arguments ------------------------------------------------------------------
[ "$#" -gt 0 ] || die_usage "a command is required"
CMD="$1"; shift
case "$CMD" in
  -h|--help) usage; exit 0 ;;
  list) [ "$#" -eq 0 ] || die_usage "list takes no arguments"; cmd_list; exit 0 ;;
  check|install|status) ;;
  *) die_usage "unknown command: $CMD" ;;
esac
MODULE=""; HOST=""; ROLE=""; APPLY_MODE=0; FORCE=0; DEST_ARG=""; MODULE_ARGS=()
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15)
while [ "$#" -gt 0 ]; do
  case "$1" in
    --role) [ "$#" -ge 2 ] || die_usage "--role needs a value"; ROLE="$2"; shift 2 ;;
    -i) [ "$#" -ge 2 ] || die_usage "-i needs a file"; [ -r "$2" ] || die_usage "cannot read identity file: $2"
        SSH+=(-i "$2"); shift 2 ;;
    --dest) [ "$#" -ge 2 ] || die_usage "--dest needs a directory"; DEST_ARG="$2"; shift 2 ;;
    --apply) APPLY_MODE=1; shift ;;
    --force) FORCE=1; shift ;;
    --) shift; MODULE_ARGS=("$@"); break ;;
    -*) die_usage "unknown option: $1" ;;
    *) if [ -z "$MODULE" ]; then MODULE="$1"; elif [ -z "$HOST" ]; then HOST="$1"; else die_usage "unexpected argument: $1"; fi
       shift ;;
  esac
done
[ -n "$MODULE" ] && [ -n "$HOST" ] || die_usage "$CMD needs MODULE and HOST"
spec "$MODULE" "$ROLE" || die_usage "unknown module: $MODULE (see homelab.sh list)"
if [ -n "$ROLES" ]; then
  [[ " $ROLES " == *" $ROLE "* ]] || die_usage "$MODULE needs --role, one of: $ROLES"
elif [ -n "$ROLE" ]; then
  die_usage "$MODULE has no roles"
fi
[ "$APPLY_MODE" -eq 0 ] || [ "$CMD" = install ] || die_usage "--apply only applies to install"
[ "$FORCE" -eq 0 ] || [ "$APPLY_MODE" -eq 1 ] || die_usage "--force only applies with --apply"
[ "${#MODULE_ARGS[@]}" -eq 0 ] || [ "$CMD" = install ] || die_usage "MODULE_ARGS only apply to install"
[ -z "$DEST_ARG" ] || DEST="$DEST_ARG"
for p in $SHIP; do [ -e "$ROOT/$p" ] || { echo "homelab.sh: $ROOT/$p is missing from this checkout" >&2; exit 1; }; done

# --- vault-viewer: its own deploy helper, when the checkout has one ---------------
if [ "$MODULE" = vault-viewer ] && [ "$CMD" = install ] && [ -x "$ROOT/vault-viewer/deploy.sh" ]; then
  args=()
  for ((i = 1; i < ${#SSH[@]}; i++)); do [ "${SSH[i]}" = -i ] && args+=(-i "${SSH[i+1]}"); done
  [ "$APPLY_MODE" -eq 1 ] || args+=(--dry-run)
  args+=("$HOST")
  [ "${#MODULE_ARGS[@]}" -eq 0 ] || args+=(-- "${MODULE_ARGS[@]}")
  echo "vault-viewer: handing over to vault-viewer/deploy.sh"
  exec "$ROOT/vault-viewer/deploy.sh" "${args[@]}"
fi

# --- controller-side modules (Ansible) --------------------------------------------
if [ "$RUNAS" = controller ]; then
  cd "$ROOT/ansible"
  command -v ansible-playbook >/dev/null 2>&1 || { echo "ansible-playbook is not installed here; see $SETUP" >&2; exit 1; }
  [ -f inventory/hosts.local.yml ] || { echo "ansible/inventory/hosts.local.yml is missing; see $SETUP" >&2; exit 1; }
  case "$CMD" in
    check) echo "controller ok: ansible-playbook and the inventory are present"; exec env HOST="$HOST" bash -c "$STATUS" ;;
    status) exec env HOST="$HOST" bash -c "$STATUS" ;;
    install)
      if [ "$APPLY_MODE" -eq 1 ]; then run="$APPLY"; else run="$DRY"; echo "dry run (Ansible --check where the playbook supports it)"; fi
      exec env HOST="$HOST" bash -c "$run" bash "${MODULE_ARGS[@]+"${MODULE_ARGS[@]}"}" ;;
  esac
fi

# --- remote execution ---------------------------------------------------------------
# A script reaches the host base64-encoded inside the ssh command, so the remote
# login shell only runs bash, and stdin stays free for data. Root modules run
# through `sudo -n` when the ssh user is not root.
CTL="$(mktemp -d)"
SSH+=(-o ControlMaster=auto -o "ControlPath=$CTL/%C" -o ControlPersist=120)
SUDO=""
remote_cmd() { printf '%sbash -c "$(echo %s | base64 -d)"' "$SUDO" "$(printf '%s' "$1" | base64 | tr -d '\n')"; }
remote_in() { "${SSH[@]}" "$HOST" "$(remote_cmd "$1")"; }
remote() { remote_in "$@" </dev/null; }
assign() { printf '%s=%q\n' "$@"; }
QARGS=""
for a in "${MODULE_ARGS[@]+"${MODULE_ARGS[@]}"}"; do QARGS+=" $(printf '%q' "$a")"; done
SETARGS="set --$QARGS"
TMP_REMOTE=""
cleanup() {
  [ -z "$TMP_REMOTE" ] || remote "$(assign T "$TMP_REMOTE")"$'\nrm -rf "$T"' >/dev/null 2>&1 || true
  rm -rf "$CTL"
}
trap cleanup EXIT

# 1. Who are we there, and can we become root?
who="$("${SSH[@]}" "$HOST" 'echo "uid=$(id -u)"; sudo -n true 2>/dev/null && echo sudo=yes || echo sudo=no; command -v apt-get >/dev/null 2>&1 && echo apt=yes || echo apt=no' </dev/null)" \
  || { echo "cannot reach $HOST over ssh" >&2; exit 1; }
uid="$(sed -n 's/^uid=//p' <<<"$who")"; can_sudo="$(sed -n 's/^sudo=//p' <<<"$who")"; has_apt="$(sed -n 's/^apt=//p' <<<"$who")"
if [ "$RUNAS" = root ] && [ "$uid" != 0 ]; then
  [ "$can_sudo" = yes ] || { echo "$MODULE runs as root: log in as root or give the ssh user passwordless sudo" >&2; exit 1; }
  SUDO="sudo -n "
fi
if [ "$RUNAS" = user ] && [ "$uid" = 0 ]; then
  echo "note: $MODULE is a per-user service; it will be installed for root on $HOST"
fi
DEST_R="$(remote "$(assign D "$DEST")"$'\neval "echo $D"')"   # expand $HOME on the host

# 2. Prerequisites. Prints one line per need: ok|missing NAME [apt PKG].
read -r -d '' NEEDS_SCRIPT <<'EOF' || true
for n in $NEEDS; do
  n="${n//\$DEST/$DEST}"
  case "$n" in
    cmd:*) c="${n#cmd:}"; pkg="${c#*=}"; c="${c%%=*}"; [ "$pkg" != "${n#cmd:}" ] || pkg=""
           if command -v "$c" >/dev/null 2>&1; then echo "ok $c"; else echo "missing $c ${pkg:+apt $pkg}"; fi ;;
    compose) if docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1; then echo "ok docker-compose"
             else echo "missing docker-compose"; fi ;;
    tun) [ -c /dev/net/tun ] && echo "ok /dev/net/tun" || echo "missing /dev/net/tun" ;;
    systemd-user) systemctl --user show-environment >/dev/null 2>&1 && echo "ok systemd-user-session" || echo "missing systemd-user-session"
                  [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" = yes ] && echo "ok linger" \
                    || echo "warn linger (the service stops at logout: loginctl enable-linger $(id -un))" ;;
    aiohttp) py="${MODEL_QUEUE_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
             if [ -x "$py" ] && "$py" -c 'import aiohttp' 2>/dev/null; then echo "ok aiohttp ($py)"; else echo "missing aiohttp-in-$py"; fi ;;
    file:*) f="${n#file:}"; [ -e "$f" ] && echo "ok $f" || echo "missing $f" ;;
  esac
done
EOF
needs_report() { remote "$(assign NEEDS "$(squeeze "$NEEDS")" DEST "$DEST_R")"$'\n'"$NEEDS_SCRIPT"; }

check_needs() {  # prints the report; returns 1 while something required is missing
  local report apt_pkgs manual
  report="$(needs_report)" || { echo "   the prerequisite check failed on $HOST" >&2; return 1; }
  sed 's/^/   /' <<<"$report"
  apt_pkgs="$(sed -n 's/^missing .* apt \(.*\)$/\1/p' <<<"$report" | sort -u | tr '\n' ' ')"
  manual="$(grep '^missing ' <<<"$report" | grep -v ' apt ' | awk '{print $2}' | tr '\n' ' ' || true)"
  if [ -n "$apt_pkgs" ]; then
    if [ "$CMD" = install ] && [ "$APPLY_MODE" -eq 1 ] && [ -z "$manual" ]; then
      [ "$has_apt" = yes ] || { echo "   missing packages, and $HOST has no apt-get: $apt_pkgs"; return 1; }
      [ "$uid" = 0 ] || [ "$can_sudo" = yes ] || { echo "   cannot install packages: no root and no passwordless sudo"; return 1; }
      echo "   installing: $apt_pkgs"
      local was="$SUDO"
      [ "$uid" = 0 ] || SUDO="sudo -n "
      if ! remote "$(assign PKGS "$apt_pkgs")"$'\nexport DEBIAN_FRONTEND=noninteractive\napt-get update -qq && apt-get install -y -qq --no-install-recommends $PKGS' >/dev/null; then
        SUDO="$was"; echo "   apt-get install failed"; return 1
      fi
      SUDO="$was"
      apt_pkgs=""
    else
      echo "   install --apply installs these packages: $apt_pkgs"
    fi
  fi
  [ -z "$manual" ] || echo "   set these up by hand first: $manual(see $SETUP)"
  [ -z "$apt_pkgs$manual" ]
}

# Ship SHIP into directory $1 on the host (created; tar over ssh).
ship() {
  local base="$1" p list=()
  for p in $SHIP; do list+=("$p"); done
  tar -C "$ROOT" -czf - "${list[@]}" | remote_in "$(assign B "$base")"$'\nset -e\nrm -rf "$B" "$B.tmp"\nmkdir -p "$B.tmp"\ntar -xzf - -C "$B.tmp"'
  # Flatten: DEST holds the module's contents, not ROOT-relative paths.
  remote "$(assign B "$base" SHIP "$(squeeze "$SHIP")")"'
set -e
mkdir -p "$B"
for p in $SHIP; do
  if [ -d "$B.tmp/$p" ]; then cp -a "$B.tmp/$p/." "$B/"; else cp -a "$B.tmp/$p" "$B/"; fi
done
rm -rf "$B.tmp"'
}

if [ -n "$SUDO" ]; then AS="root (sudo)"; elif [ "$uid" = 0 ]; then AS="root"; else AS="uid $uid"; fi
echo "== $MODULE${ROLE:+ ($ROLE)} on $HOST, as $AS, in $DEST_R"
case "$CMD" in
  check) echo "-- prerequisites"; check_needs; exit $? ;;
  status) echo "-- status"; remote "$(assign D "$DEST_R")"$'\ncd "$D" 2>/dev/null || cd /\n'"$STATUS"; exit $? ;;
esac
echo "-- prerequisites"

check_needs || { echo "stopped: prerequisites are missing"; exit 1; }

if [ "$APPLY_MODE" -eq 0 ]; then
  echo "-- dry run (nothing on $HOST changes)"
  TMP_REMOTE="$(remote 'mktemp -d "${TMPDIR:-/tmp}/homelab.XXXXXX"')"
  tmp="$TMP_REMOTE"
  ship "$tmp/new"
  remote "$(assign N "$tmp/new" D "$DEST_R" KEEP "$KEEP" GUARD "$GUARD")"'
if [ ! -d "$D" ]; then echo "   $D does not exist yet: --apply creates it"; exit 0; fi
new=0; changed=0
while IFS= read -r f; do
  if [ ! -e "$D/$f" ]; then new=$((new + 1)); [ "$new" -gt 10 ] || echo "   new:     $f"
  elif ! cmp -s "$N/$f" "$D/$f"; then changed=$((changed + 1)); echo "   changed: $f"
    for g in $GUARD; do [ "$f" != "$g" ] || echo "            ^ guarded: --apply stops on this unless --force (it may point at data elsewhere)"; done
  fi
done < <(cd "$N" && find . -type f | sed "s|^\./||" | sort)
[ "$new" -le 10 ] || echo "   ... and $((new - 10)) more new files"
[ $((new + changed)) -gt 0 ] || echo "   $D already matches this checkout"
# The dry run sees the host'"'"'s local config files (not data directories).
for k in $KEEP; do [ -f "$D/$k" ] && cp -p "$D/$k" "$N/$k"; done
exit 0'
  if [ -n "$DRY" ]; then
    echo "-- the module's own dry run"
    remote "$(assign N "$tmp/new" HOST "$HOST")"$'\ncd "$N"\n'"$SETARGS"$'\n'"$DRY" || { echo "the module's dry run failed"; exit 1; }
  else
    echo "-- this module has no dry run; --apply would run in $DEST_R: $APPLY${QARGS:+ (args:$QARGS)}"
  fi
  echo "dry run done: re-run with --apply to install"
  exit 0
fi

echo "-- placing the module in $DEST_R"
ship "$DEST_R.new"
if [ -n "$GUARD" ]; then
  differs="$(remote "$(assign D "$DEST_R" GUARD "$GUARD")"'
for g in $GUARD; do [ -e "$D/$g" ] && ! cmp -s "$D/$g" "$D.new/$g" && echo "$g"; done; true')"
  if [ -n "$differs" ] && [ "$FORCE" -eq 0 ]; then
    remote "$(assign D "$DEST_R")"$'\nrm -rf "$D.new"'
    echo "stopped: on $HOST, $(echo "$differs" | tr '\n' ' ')in $DEST_R differs from this checkout and may point at"
    echo "         data elsewhere. Compare them (the dry run lists it), then re-run with --apply --force."
    exit 1
  fi
fi
remote "$(assign D "$DEST_R" KEEP "$KEEP")"'
set -e
for k in $KEEP; do
  if [ -e "$D/$k" ]; then mkdir -p "$(dirname "$D.new/$k")"; rm -rf "${D:?}.new/$k"; cp -a "$D/$k" "$D.new/$k"; fi
done
if [ -d "$D" ]; then rm -rf "$D.prev"; mv "$D" "$D.prev"; echo "   previous copy: $D.prev"; fi
mv "$D.new" "$D"'
echo "-- running the module's installer"
if ! remote "$(assign D "$DEST_R" HOST "$HOST")"$'\ncd "$D"\n'"$SETARGS"$'\n'"$APPLY"; then
  echo "FAILED: the installer failed on $HOST. The previous copy is $DEST_R.prev; the module's own"
  echo "        rollback (if it has one) has run. Fix the cause and re-run."
  exit 1
fi
if [ -n "$STATUS" ]; then
  echo "-- status"
  remote "$(assign D "$DEST_R")"$'\ncd "$D"\n'"$STATUS" | sed 's/^/   /' || { echo "FAILED: the status check failed after install"; exit 1; }
fi
echo "done: $MODULE${ROLE:+ ($ROLE)} installed on $HOST"
