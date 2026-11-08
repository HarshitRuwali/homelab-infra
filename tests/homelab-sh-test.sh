#!/usr/bin/env bash
# Behavioural tests for homelab.sh. A stub `ssh` runs the remote side locally
# with a minimal PATH of stubs, against a fake checkout whose module installers
# only log their arguments.
#
# Run from the repository root:  bash tests/homelab-sh-test.sh
# Exit 0 = all pass. Nothing touches a real host, package manager or docker.
# shellcheck disable=SC2016,SC2329  # quotes are for stubs; helpers are called through check
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$REPO/homelab.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILS=0

check() { # check <label> <command...>
  local label="$1"; shift
  if "$@"; then echo "  [ ok ] $label"; else echo "  [FAIL] $label"; FAILS=$((FAILS + 1)); fi
}
has() { grep -qF -- "$2" <<<"$1"; }
lacks() { ! grep -qF -- "$2" <<<"$1"; }
logged() { grep -qF -- "$1" "$LOG"; }
not_logged() { ! grep -qF -- "$1" "$LOG"; }

# --- the remote side: a minimal PATH with stubs ------------------------------------
STUBS="$TMP/stubs"; MINBIN="$TMP/minbin"; EXTRA="$TMP/extra"
mkdir -p "$STUBS" "$MINBIN" "$EXTRA"
for t in bash sh cat cp mv rm mkdir ls find sed sort cmp tar gzip gunzip base64 dirname tr awk grep mktemp head env \
         install printf echo true false test chmod; do
  p="$(command -v "$t" 2>/dev/null)" && [ -n "$p" ] && [ -x "$p" ] && ln -sf "$p" "$MINBIN/$t"
done
mkstub() { printf '#!/usr/bin/env bash\necho "%s $*" >> "$LOG"\n%s\n' "$1" "$2" > "$STUBS/$1"; chmod +x "$STUBS/$1"; }
mkstub id 'case "$1" in -u) echo "${STUB_UID:-0}" ;; -un) echo tester ;; esac'
mkstub sudo '[ -z "${STUB_NO_SUDO:-}" ] || exit 1; [ "$1" = -n ] && shift; [ "$1" = true ] && exit 0; exec "$@"'
mkstub apt-get '[ "$1" = install ] || exit 0; for p in "$@"; do case "$p" in util-linux) printf "#!/bin/sh\nexit 0\n" > "$EXTRA/flock"; chmod +x "$EXTRA/flock" ;; esac; done'
mkstub docker 'case "$1 ${2:-}" in "compose version") [ -z "${STUB_NO_COMPOSE:-}" ] ;; "compose config") [ -z "${STUB_BAD_ENV:-}" ] ;; *) exit 0 ;; esac'
mkstub systemctl 'exit 0'
mkstub loginctl 'echo yes'
mkstub curl 'exit 0'
# ssh: skip options, take the host, run the command as "the host".
cat > "$STUBS/ssh" <<'EOF'
#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do case "$1" in -o|-i) shift 2 ;; -*) shift ;; *) break ;; esac; done
host="$1"; shift
echo "ssh $host" >> "$LOG"
[ "$host" != unreachable ] || exit 255
mkdir -p "$TMP/rtmp"
PATH="$STUBS:$EXTRA:$MINBIN" HOME="$TMP/rhome" TMPDIR="$TMP/rtmp" exec bash -c "$*"
EOF
chmod +x "$STUBS/ssh"
# The control side: the real PATH plus the ssh stub (and ansible-playbook).
mkdir -p "$TMP/local"
ln -sf "$STUBS/ssh" "$TMP/local/ssh"
printf '#!/usr/bin/env bash\necho "ansible-playbook $* (in $PWD)" >> "$LOG"\n' > "$TMP/local/ansible-playbook"
chmod +x "$TMP/local/ansible-playbook"

# --- a fake checkout: homelab.sh with modules whose installers log their args -------
FAKE="$TMP/repo"
fake_module_installer() { # <path> : a script that logs "<path> args"
  mkdir -p "$FAKE/$(dirname "$1")"
  printf '#!/usr/bin/env bash\necho "installer %s [$*] in $PWD" >> "$LOG"\nexit "${STUB_INSTALL_RC:-0}"\n' "$1" > "$FAKE/$1"
  chmod +x "$FAKE/$1"
}
fake_checkout() {
  rm -rf "$FAKE"; mkdir -p "$FAKE"
  cp "$HELPER" "$FAKE/homelab.sh"
  for f in media/install.sh s3-backup/install.sh ingress/install.sh aibox-model-queue/install.sh \
           security/install/install-sec-dns.sh proxmox/pve-guest.sh; do fake_module_installer "$f"; done
  mkdir -p "$FAKE/memory/memory-service/data" "$FAKE/ansible/inventory"
  echo "services: {}  # repo" > "$FAKE/memory/memory-service/docker-compose.yml"
  echo "POSTGRES_PASSWORD=" > "$FAKE/memory/memory-service/.env.example"
  touch "$FAKE/memory/memory-service/data/.gitignore"
}

LOG="$TMP/log"
DEST="$TMP/rdest"
run() { # run [VAR=value...] -- args... : sets OUT and RC
  local envs=()
  while [ "$1" != -- ]; do envs+=("$1"); shift; done
  shift
  : > "$LOG"
  OUT="$(env "${envs[@]+"${envs[@]}"}" PATH="$TMP/local:$PATH" LOG="$LOG" TMP="$TMP" STUBS="$STUBS" EXTRA="$EXTRA" \
    MINBIN="$MINBIN" bash "$FAKE/homelab.sh" "$@" 2>&1)"
  RC=$?
}
reset_host() { rm -rf "$DEST" "$DEST".* "$TMP/rhome" "$TMP/rtmp" "${EXTRA:?}"/*; mkdir -p "$TMP/rhome"; }
no_temp_dirs() { ! find "$TMP/rtmp" -maxdepth 1 -name 'homelab.*' 2>/dev/null | grep -q .; }

fake_checkout

echo "usage and list"
run -- ; check "no command exits 2" test "$RC" -eq 2
run -- frobnicate; check "unknown command exits 2" test "$RC" -eq 2
run -- install nosuch box; check "unknown module exits 2" test "$RC" -eq 2
run -- install media; check "missing HOST exits 2" test "$RC" -eq 2
run -- install monitoring box; check "a module with roles needs --role" test "$RC" -eq 2
check "the roles are named" has "$OUT" "central collector"
run -- install media box --role x; check "--role on a module without roles exits 2" test "$RC" -eq 2
run -- check media box --apply; check "--apply outside install exits 2" test "$RC" -eq 2
run -- install media box --force; check "--force without --apply exits 2" test "$RC" -eq 2
run -- status media box -- --x; check "MODULE_ARGS outside install exit 2" test "$RC" -eq 2
check "usage errors contact no host" not_logged "ssh "
run -- list
check "list exits 0" test "$RC" -eq 0
for m in vault-viewer aibox-model-queue ingress media s3-backup memory monitoring security proxmox ansible; do
  check "list names $m" has "$OUT" "$m"
done
for r in "memory --role storage" "monitoring --role central" "security --role wazuh" "security --role provision"; do
  check "list names $r" has "$OUT" "$r"
done

echo "every setup doc named by an adapter exists in the real checkout"
while read -r doc; do
  check "SETUP $doc exists" test -f "$REPO/$doc"
done < <(grep -oE 'SETUP="[^"$]+"' "$HELPER" | sed 's/SETUP="//; s/"$//' | sort -u)
while read -r path; do
  check "SHIP $path exists" test -e "$REPO/$path"
done < <(grep -oE 'SHIP="[^"$]+"' "$HELPER" | sed 's/SHIP="//; s/"$//' | sort -u)

echo "check: prerequisites"
reset_host
run -- check s3-backup box
check "missing flock: exits 1" test "$RC" -eq 1
check "missing flock is reported with its package" has "$OUT" "missing flock apt util-linux"
check "docker present is ok" has "$OUT" "ok docker"
check "check installs nothing" not_logged "apt-get"
run -- check ingress box
check "ingress without caddy exits 1" test "$RC" -eq 1
check "caddy is a by-hand prerequisite with its doc" has "$OUT" "set these up by hand first: caddy"
check "the setup doc is named" has "$OUT" "ingress/docs/setup.md"
run STUB_NO_COMPOSE=1 -- check media box
check "compose missing is reported" has "$OUT" "missing docker-compose"

echo "install: dry run"
reset_host; printf '#!/bin/sh\nexit 0\n' > "$EXTRA/flock"; chmod +x "$EXTRA/flock"
run -- install s3-backup box --dest "$DEST"
check "dry run exits 0" test "$RC" -eq 0
check "dry run says DEST would be created" has "$OUT" "does not exist yet"
check "the module's own dry run ran (--check)" logged "installer s3-backup/install.sh [--check]"
check "dry run did not create DEST" test ! -e "$DEST"
check "dry run left no temporary directory" no_temp_dirs
for c in qm pct pvesm pvesh; do printf '#!/bin/sh\nexit 0\n' > "$EXTRA/$c"; chmod +x "$EXTRA/$c"; done
run -- install proxmox box --dest "$DEST"
check "a module without a real dry run prints the plan" has "$OUT" "installs pve-guest.sh as /root/pve-guest.sh"
check "proxmox dry run does not run the CLI" not_logged "installer proxmox"

echo "install --apply: packages, placement, arguments"
reset_host
run -- install s3-backup box --dest "$DEST" --apply -- --secrets aws
check "apply exits 0" test "$RC" -eq 0
check "missing apt package installed" logged "apt-get install -y -qq --no-install-recommends util-linux"
check "module placed in DEST" test -f "$DEST/install.sh"
check "installer got exactly the module args" logged "installer s3-backup/install.sh [--secrets aws] in $DEST"
check "status ran after install" has "$OUT" "-- status"
reset_host; printf '#!/bin/sh\nexit 0\n' > "$EXTRA/flock"; chmod +x "$EXTRA/flock"
run -- install s3-backup box --dest "$DEST" --apply
check "no module args: the installer gets none" logged "installer s3-backup/install.sh [] in $DEST"
echo "local" > "$DEST/marker"
run -- install s3-backup box --dest "$DEST" --apply
check "second apply keeps the previous copy" test -f "$DEST.prev/marker"
check "and replaces DEST" test ! -e "$DEST/marker"
check "no .new directory left" test ! -e "$DEST.new"
run STUB_INSTALL_RC=3 -- install s3-backup box --dest "$DEST" --apply
check "failed installer exits 1" test "$RC" -eq 1
check "failed installer names the previous copy" has "$OUT" "$DEST.prev"
reset_host
run -- install ingress box --dest "$DEST" --apply
check "a by-hand prerequisite stops --apply" test "$RC" -eq 1
check "nothing placed when prerequisites are missing" test ! -e "$DEST"

echo "root through sudo"
reset_host; printf '#!/bin/sh\nexit 0\n' > "$EXTRA/flock"; chmod +x "$EXTRA/flock"
run STUB_UID=1000 -- install s3-backup box --dest "$DEST"
check "non-root user: runs through sudo" has "$OUT" "as root (sudo)"
check "sudo was used" logged "sudo -n bash -c"
run STUB_UID=1000 STUB_NO_SUDO=1 -- install s3-backup box --dest "$DEST"
check "no root and no sudo: exits 1" test "$RC" -eq 1
check "no root and no sudo: says why" has "$OUT" "passwordless sudo"

echo "memory: local files kept, compose file guarded"
reset_host; mkdir -p "$DEST/data/postgres"
echo "POSTGRES_PASSWORD=secret" > "$DEST/.env"; echo "rows" > "$DEST/data/postgres/db"
echo "services: {}  # host" > "$DEST/docker-compose.yml"
run -- install memory box --role storage --dest "$DEST"
check "dry run marks the compose file as guarded" has "$OUT" "guarded"
check "dry run validated with the host's .env" logged "docker compose config -q"
run -- install memory box --role storage --dest "$DEST" --apply
check "a differing compose file stops --apply" test "$RC" -eq 1
check "the host's compose file is untouched" grep -q "# host" "$DEST/docker-compose.yml"
check "no .new directory left after the stop" test ! -e "$DEST.new"
run -- install memory box --role storage --dest "$DEST" --apply --force
check "--force replaces it" grep -q "# repo" "$DEST/docker-compose.yml"
check ".env kept" grep -q "secret" "$DEST/.env"
check "data kept" test -f "$DEST/data/postgres/db"
check "compose up ran in DEST" logged "docker compose up -d"
run STUB_BAD_ENV=1 -- install memory box --role storage --dest "$DEST"
check "an invalid .env fails the dry run" test "$RC" -eq 1

echo "vault-viewer hands over to its deploy.sh"
mkdir -p "$FAKE/vault-viewer"
printf '#!/usr/bin/env bash\necho "deploy.sh $*" >> "$LOG"\n' > "$FAKE/vault-viewer/deploy.sh"
chmod +x "$FAKE/vault-viewer/deploy.sh"; touch "$TMP/key"
run -- install vault-viewer box -i "$TMP/key" -- --bind 10.1.2.3 /v
check "dry run hand-over" logged "deploy.sh -i $TMP/key --dry-run box -- --bind 10.1.2.3 /v"
run -- install vault-viewer box --apply
check "apply hand-over has no --dry-run and no --" logged "deploy.sh box"
check "apply hand-over passes no stray arguments" not_logged "deploy.sh box --"

echo "controller-side modules"
run -- install ansible web1
check "no inventory: exits 1" test "$RC" -eq 1
touch "$FAKE/ansible/inventory/hosts.local.yml"
run -- install ansible web1
check "ansible dry run runs preflight for the host" logged "ansible-playbook playbooks/preflight.yml --limit web1 (in $FAKE/ansible)"
run -- install monitoring web1 --role collector --apply
check "collector apply runs onboard.yml for the host" logged "ansible-playbook playbooks/onboard.yml --limit web1"
run -- install monitoring web1 --role collector
check "collector dry run uses --check --diff" logged "playbooks/onboard.yml --limit web1 --check --diff"
check "controller modules use no ssh" not_logged "ssh "

echo "unreachable host"
run -- check media unreachable
check "unreachable host exits 1" test "$RC" -eq 1
check "unreachable host is named" has "$OUT" "cannot reach unreachable"

echo
if [ "$FAILS" -eq 0 ]; then echo "all checks passed"; exit 0; fi
echo "$FAILS check(s) failed"; exit 1
