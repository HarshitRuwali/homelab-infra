#!/usr/bin/env bash
# Tests for proxmox/pve-guest.sh capacity logic and the template default, with
# fake qm/pct/pvesm/pvesh/free/nproc on PATH and fake guest config files.
# Nothing touches a real host. The script insists on root, so run as root in a
# container:
#
#   docker run --rm -v "$PWD":/repo:ro debian:13 bash /repo/proxmox/tests/test_pve_guest.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../pve-guest.sh"
[[ $EUID -eq 0 ]] || { echo "run as root (see header)"; exit 2; }

W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/qemu" "$W/lxc" "$W/cache"
touch "$W/cache/debian-13-standard_13.1-1_amd64.tar.zst"
mkdir -p "$W/iso" "$W/noconf" "$W/nofake"
printf "      VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n" > "$W/nofake/qm.list"; : > "$W/nofake/pct.list"
touch "$W/iso/debian-13-genericcloud-amd64.qcow2" "$W/iso/ubuntu-26.04-server-cloudimg-amd64.img" "$W/iso/ubuntu-24.04-server-cloudimg-amd64.img"
export IMAGE_DIRS_LIST="$W/iso"
export PATH="$W/bin:$PATH" QEMU_CONF_DIR="$W/qemu" LXC_CONF_DIR="$W/lxc" LXC_CACHE="$W/cache"
export SSH_PUBKEY="$W/key.pub"; echo "ssh-ed25519 AAAA test" > "$SSH_PUBKEY"

# Fake host: 40 threads, 257000 MB total, 120000 MB available.
cat > "$W/bin/nproc" <<'X'
#!/bin/sh
echo "${FAKE_THREADS:-40}"
X
cat > "$W/bin/free" <<'X'
#!/bin/sh
echo "              total        used        free      shared  buff/cache   available"
echo "Mem:         ${FAKE_TOTAL_MB:-257000}       1000       1000          0       1000      ${FAKE_AVAIL_MB:-120000}"
X
cat > "$W/bin/qm" <<'X'
#!/bin/sh
case "$1" in
  list) cat "$FAKE_DIR/qm.list" ;;
  status) grep -q "^ *$2 " "$FAKE_DIR/qm.list" ;;
  config) f="$QEMU_CONF_DIR/$2.conf"; [ -f "$f" ] && cat "$f" ;;
  *) echo "qm $*" >> "$FAKE_DIR/mutations"; exit 1 ;;
esac
X
cat > "$W/bin/pct" <<'X'
#!/bin/sh
case "$1" in
  list) cat "$FAKE_DIR/pct.list" ;;
  status) grep -q "^$2 " "$FAKE_DIR/pct.list" ;;
  *) echo "pct $*" >> "$FAKE_DIR/mutations"; exit 1 ;;
esac
X
cat > "$W/bin/pvesm" <<'X'
#!/bin/sh
printf 'Name Type Status Total Used Available %%\n%s dir active 900000000 100000000 800000000 11%%\n' "$3"
X
cat > "$W/bin/pvesh" <<'X'
#!/bin/sh
case "$2" in
  /cluster/nextid) echo 500 ;;
  /storage/*) echo '{"content":"images,rootdir,vztmpl"}' ;;
  *) echo "pvesh $*" >> "$FAKE_DIR/mutations"; exit 1 ;;
esac
X
cat > "$W/bin/ip" <<'X'
#!/bin/sh
exit 0
X
chmod +x "$W"/bin/*
export FAKE_DIR="$W"

cat > "$W/qm.list" <<'X'
      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID
       202 big                  running    32768             130.00 1
       203 dev                  running    32768             100.00 2
       301 off                  stopped    32768             160.00 0
      9001 tmpl-ubuntu          stopped     1024               3.50 0
X
cat > "$W/pct.list" <<'X'
VMID       Status     Lock         Name
102        running                 ingress
111        running                 dns
X
cat > "$W/qemu/202.conf" <<'X'
name: big
cores: 16
memory: 32768
[snap1]
sockets: 2
cores: 99
memory: 99999
X
cat > "$W/qemu/203.conf" <<'X'
name: dev
cores: 10
sockets: 2
memory: 32768
X
cat > "$W/qemu/301.conf" <<'X'
name: off
cores: 12
memory: 32768
X
cat > "$W/qemu/9001.conf" <<'X'
name: tmpl-ubuntu
template: 1
cores: 1
memory: 1024
scsi0: local-lvm:base-9001-disk-0,size=4G
X
printf 'hostname: ingress\ncores: 1\nmemory: 256\n' > "$W/lxc/102.conf"
printf 'hostname: dns\ncores: 2\nmemory: 512\n' > "$W/lxc/111.conf"
# running: 16 + 20 + 1 + 2 = 39 vCPU; 32768*2 + 256 + 512 = 66304 MB

fails=0 n=0
check() { # name, expected exit, grep pattern, then command...
  local name="$1" want="$2" pat="$3"; shift 3
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  n=$((n+1))
  if [[ $rc -ne $want ]] || ! grep -E -- "$pat" <<<"$out" >/dev/null; then
    fails=$((fails+1)); echo "FAIL $name (rc=$rc want $want, pattern '$pat')"; printf '%s\n' "${out//$'\n'/$'\n'    }"
  else
    echo "ok   $name"
  fi
}

check "default template is 9001 in help" 0 'default 9001' "$SCRIPT" --help
check "vm without --template reaches the 9001 template" 0 'template 9001 \(tmpl-ubuntu\)' "$SCRIPT" vm --name app01
check "missing template names --template" 1 'Pass --template <vmid> .*9001\(tmpl-ubuntu\)' "$SCRIPT" vm --name app01 --template 9000
# The template default and the vm default must name the same guest: Ubuntu at 9001.
# "noconf" is a host with no template yet, so the plan is not "already exists".
tpl_plan="$(FAKE_DIR="$W/nofake" QEMU_CONF_DIR="$W/noconf" "$SCRIPT" template 2>&1)"
tpl_vmid="$(sed -n 's/.*VM template [^ ]* (vmid \([0-9]*\), from .*/\1/p' <<<"$tpl_plan")"
vm_tmpl="$("$SCRIPT" vm --name app01 2>&1 | sed -n 's/.*template \([0-9]*\) (.*/\1/p' | head -1)"
n=$((n+1))
if [[ -n "$tpl_vmid" && "$tpl_vmid" == "$vm_tmpl" ]]; then echo "ok   template default VMID ($tpl_vmid) equals the vm default ($vm_tmpl)"
else fails=$((fails+1)); echo "FAIL template default VMID '$tpl_vmid' vs vm default '$vm_tmpl'"; fi
n=$((n+1))
if grep -q 'ubuntu-26.04-server-cloudimg-amd64.img' <<<"$tpl_plan" && grep -q 'tmpl-ubuntu-26.04' <<<"$tpl_plan"; then echo "ok   template default image is the newest Ubuntu cloud image"
else fails=$((fails+1)); echo "FAIL template default image is not Ubuntu 26.04"; printf '%s\n' "$tpl_plan"; fi
check "template --image debian at --vmid 9000 stays possible" 0 'tmpl-debian-13 \(vmid 9000' env FAKE_DIR="$W/nofake" QEMU_CONF_DIR="$W/noconf" "$SCRIPT" template --image debian-13-genericcloud-amd64.qcow2 --vmid 9000
check "capacity lists guests and totals" 0 'running guests: 39 vCPU on 40 threads \(0.97x\), 66304 MB' "$SCRIPT" capacity
check "capacity skips templates and snapshot sections" 0 '^vm +202 +big +running +16 +32768$' "$SCRIPT" capacity
check "capacity shows stopped guest" 0 '^vm +301 +off +stopped +12 +32768$' "$SCRIPT" capacity
check "capacity counts sockets x cores" 0 '^vm +203 +dev +running +20 +32768$' "$SCRIPT" capacity
check "vm within limits: no vCPU warning" 0 'vCPU: 40 committed' "$SCRIPT" vm --name app01
check "vCPU overcommit warns" 0 'vCPU overcommit: 84 vCPU on 40 threads, more than 2.0x' "$SCRIPT" vm --name app01 --cores 45
check "vCPU ratio is configurable" 0 'vCPU: 84 committed' env VCPU_RATIO_MAX=3 "$SCRIPT" vm --name app01 --cores 45
check "RAM over commit limit is refused" 1 'RAM committed: running guests hold 66304 MB' env RAM_RESERVE_MB=200000 "$SCRIPT" vm --name app01
check "--force lets it through with a warning" 0 'continuing because of --force' env RAM_RESERVE_MB=200000 "$SCRIPT" vm --name app01 --force
check "lxc is refused the same way" 1 'RAM committed' env RAM_RESERVE_MB=200000 "$SCRIPT" lxc --name svc01
check "--force refused on capacity" 1 '--force is for vm, lxc and tenant' "$SCRIPT" capacity --force
check "dry run default" 0 'DRY RUN' "$SCRIPT" vm --name app01

n=$((n+1))
if [[ -e "$W/mutations" ]]; then fails=$((fails+1)); echo "FAIL a mutating command ran:"; sed 's/^/    /' "$W/mutations"; else echo "ok   no mutating qm/pct/pvesh call in any run"; fi

echo "$((n-fails))/$n passed"
exit $(( fails > 0 ))
