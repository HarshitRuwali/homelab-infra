#!/usr/bin/env bash
#
# pve-guest.sh
#
# Builds a reusable VM template, and creates new VMs and containers, from the
# images ALREADY on this Proxmox host. Run it ON the host, as root. It never
# downloads anything: fetch images with `pveam download` or the web UI first,
# and `images` shows what it can use.
#
# DRY RUN BY DEFAULT. Nothing is created until you pass --apply.
#
#   ./pve-guest.sh images                          # what is cached, what is usable
#   ./pve-guest.sh template                        # VM template from the cloud image
#   ./pve-guest.sh template --apply
#   ./pve-guest.sh vm  --name app01 --cores 2 --memory 2048 --disk 16
#   ./pve-guest.sh lxc --name svc01 --memory 512 --image ubuntu-24.04-standard
#   ./pve-guest.sh vm  --name app01 --apply        # then for real
#
# Leased VMs, for other people. Treated as hostile: see proxmox/README.md.
#   ./pve-guest.sh firewall                        # once: the tenant and lease firewall groups
#   ./pve-guest.sh tenant --name alice-vm --user alice --key my.pub \
#                         --ip 10.10.90.10 --cores 2 --memory 2048 --disk 20
#   ./pve-guest.sh handover --vmid 113 --key alice.pub   # after Tailscale is up
#   ./pve-guest.sh hookscript --vmid 113           # the packet-rate hookscript, on its own
#
# template options:
#   --vmid N        template VMID                     (default 9000)
#   --image FILE    cloud image, a file name in the ISO or import dir
#                   (default: newest debian-*-genericcloud-amd64.qcow2)
#   --name NAME     template name                     (default: tmpl-<distro>)
#   --cpu MODEL     CPU model clones inherit          (default x86-64-v3)
#
# vm and lxc options:
#   --name NAME     hostname, required
#   --vmid N        default: the next free VMID
#   --cores N       default 1
#   --memory MB     default 1024
#   --disk GB       default 8
#   --bridge BR     default vmbr0
#   --template N    vm only: template VMID to clone   (default 9000)
#   --cpu MODEL     vm only: CPU model                (default x86-64-v3)
#   --image X       lxc only: template file, or a family such as
#                   ubuntu-24.04-standard             (default debian-13-standard)
#   --no-start      create it, leave it stopped
#
# tenant options (a VM on one of two segments, chosen by --uncapped; --bridge
# is refused). A lease is on the LEASE segment (bridge vmbr5, 10.10.90.0/24,
# firewall group lease). An --uncapped VM is on the WORK segment (bridge vmbr2,
# 10.10.60.0/24, firewall group tenant), so a lease never shares a layer-2
# segment with a work VM.
#   --name, --vmid, --cores, --memory, --disk, --template, --no-start
#                   as above
#   --cpu MODEL     default x86-64-v2-AES for a lease (LEASE_CPU_TYPE), which
#                   hides AVX and AVX2 from the guest, so a tenant cannot run
#                   gather-instruction attacks such as GDS (Downfall). A lease
#                   may only use a model that hides AVX: x86-64-v2-AES,
#                   x86-64-v2, kvm64, qemu64, Nehalem, Westmere, Penryn or
#                   Conroe. Anything else (max and host included) is refused:
#                   --uncapped is for work VMs. With --uncapped the default is
#                   x86-64-v3 (CPU_TYPE), and --cpu host is refused.
#   --user NAME     the tenant's login, required
#   --key FILE      SSH public key for setup, required: yours, until handover
#   --ip ADDR       its address, required: in the LEASE /24, or with --uncapped
#                   in the WORK /24
#   --allow PORTS   inbound TCP ports to open, e.g. 80,443 (they also need a
#                   port forward; SSH arrives over the tenant's tailnet)
#   --uncapped      no CPU, disk or network caps, and the WORK segment: for a
#                   VM of your own that needs the tenant's isolation but not
#                   its limits, such as a work VM. The firewall rules are the
#                   same as a lease's, in the group tenant.
#
# A lease also gets a packets-per-second ceiling on its tap (LEASE_PPS, default
# 20000), set at every start by a hookscript this script writes to
# LEASE_HOOKSCRIPT. Proxmox only takes a hookscript from a storage that lists
# snippets, so the first lease adds snippets to the content list of the
# storage `local` (with pvesm set, extending the list it has), after saying so
# in the dry run. --uncapped VMs get neither the hookscript nor the change.
#
# hookscript options (installs the lease packet-rate hookscript and, if the
# storage `local` lacks it, snippets in its content list; dry run by default):
#   --vmid N        also attach it to this VM, which must be a QEMU VM tagged
#                   lease. Without it only the file and the setting are made.
#
# handover options:
#   --vmid N        the tenant VM, required
#   --key FILE      the tenant's SSH public key, required. Refused if it is
#                   one this hypervisor's root trusts, which would be yours.
#
# Environment: STORAGE (guest disks, default local-lvm), STORAGE_TMPL (LXC
# templates, default local), SSH_PUBKEY (default ~/.ssh/id_ed25519.pub),
# CPU_TYPE (default x86-64-v3), MGMT_LAN and OPNSENSE_LAN_IP (who may manage
# the host), TENANT_* (the WORK segment and the tenant caps) and LEASE_* (the
# LEASE segment, its CPU model and its packet-rate limit: LEASE_BRIDGE,
# LEASE_PREFIX, LEASE_CPU_TYPE, LEASE_PPS, LEASE_HOOKSCRIPT). See the
# configuration block.
#
# Existing guests are never modified. A half-built guest is destroyed, so a
# rerun starts clean instead of skipping over it.

set -Eeuo pipefail

# ---------------------------------------------------------------- configuration

STORAGE="${STORAGE:-local-lvm}"
STORAGE_TMPL="${STORAGE_TMPL:-local}"
SSH_PUBKEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"

# Where cloud images can already be. The ISO dir is where the security stack's
# provisioner parks its qcow2 and where web UI uploads land; the import dir is
# PVE 9's `import` content type. The vztmpl dir holds LXC tarballs only.
IMAGE_DIRS=(/var/lib/vz/template/iso /var/lib/vz/import)
LXC_CACHE=/var/lib/vz/template/cache

TEMPLATE_VMID=9000
# The CPU model every VM gets. Unset, qm falls back to kvm64, which hides
# SSE4.2, POPCNT and AVX2: modern runtimes (Bun, so Claude Code) spin or die
# on it. x86-64-v3 is a generic model with AVX2 that still hides the exact
# host CPU, which matters for tenants. Set on clones too, since a clone of an
# older template would otherwise inherit kvm64. A lease gets LEASE_CPU_TYPE
# instead, below.
CPU_TYPE="${CPU_TYPE:-x86-64-v3}"
LXC_FAMILY=debian-13-standard
# The login cloud-init creates on every VM, as on sec-wazuh.
CI_USER="admin"

# Leased VMs. The tenant is root inside, so nothing in one may be trusted:
# no fleet key, no Ansible, no collector password, no guest agent. It sits on
# a bridge only OPNsense is on, and a firewall group lets it reach the internet
# and nothing private. There are two such segments, so a lease never shares a
# layer-2 segment with your own work VMs. Each must match an OPNsense
# interface, a /24 with the firewall at .1.
#
# WORK, for --uncapped: a VM of your own. Firewall group tenant, OPNsense's
# TENANT interface.
TENANT_BRIDGE="${TENANT_BRIDGE:-vmbr2}"
TENANT_PREFIX="${TENANT_PREFIX:-10.10.60}"
TENANT_GW="${TENANT_PREFIX}.1"
# LEASE, the default: a VM leased to somebody else. Firewall group lease,
# OPNsense's LEASE interface, static addresses only, no DHCP.
LEASE_BRIDGE="${LEASE_BRIDGE:-vmbr5}"
LEASE_PREFIX="${LEASE_PREFIX:-10.10.90}"
LEASE_GW="${LEASE_PREFIX}.1"
# The CPU model a lease gets. x86-64-v2-AES hides AVX and AVX2 from the guest,
# so a tenant cannot run gather-instruction attacks such as GDS (Downfall)
# against the other guests on this CPU: they need the AVX2 gather
# instructions. Work VMs keep CPU_TYPE. The template's own CPU model does not
# matter: every clone gets --cpu set explicitly.
LEASE_CPU_TYPE="${LEASE_CPU_TYPE:-x86-64-v2-AES}"
# The only CPU models a lease may be given: the ones that hide AVX. Any
# VEX-encoded instruction, gathers included, runs once a guest can enable AVX
# state, so hiding AVX2 alone is not enough: SandyBridge, IvyBridge and
# Opteron_G4/G5 have AVX without AVX2. `max` is the host CPU under KVM, as
# `host` is, and custom-* models are defined on the host and unknown here. It
# is a list of what is safe, not of what is dangerous, because Proxmox adds
# models every release. Exact names only. --uncapped VMs are not held to it.
LEASE_CPU_ALLOWED=(x86-64-v2-AES x86-64-v2 kvm64 qemu64 Nehalem Westmere Penryn Conroe)
lease_cpu_ok() {
  local m
  for m in "${LEASE_CPU_ALLOWED[@]}"; do
    if [[ "$1" == "$m" ]]; then return 0; fi
  done
  return 1
}
# A packets-per-second ceiling on each lease's tap, for what the guest sends.
# The NIC rate (TENANT_NET_RATE) caps bytes, so a flood of tiny packets still
# fills the host's conntrack table. A hookscript sets it at every start, since
# the tap is recreated each time; the burst is twice the rate. Only leases get
# it, never --uncapped work VMs. The hookscript must sit in the snippets
# directory of the storage `local`, which has to list snippets in its content.
LEASE_PPS="${LEASE_PPS:-20000}"
LEASE_HOOKSCRIPT="${LEASE_HOOKSCRIPT:-/var/lib/vz/snippets/lease-limits.pl}"
SNIPPETS_STORAGE=local
# Caps, so no tenant can starve the homelab: network in MB/s (12.5 is about
# 100 Mbit), disk in MB/s and IOPS each way, and half the default CPU weight,
# so your own guests win under contention and tenants get what is left.
TENANT_NET_RATE="${TENANT_NET_RATE:-12.5}"
TENANT_DISK_MBPS="${TENANT_DISK_MBPS:-100}"
TENANT_DISK_IOPS="${TENANT_DISK_IOPS:-2000}"
TENANT_CPUUNITS="${TENANT_CPUUNITS:-50}"
FW_DIR=/etc/pve/firewall

# The hypervisor's own firewall. SSH, the web UI and the consoles are allowed
# from Tailscale and the home LAN, never from OPNsense's LAN address: every
# vmbr1 guest and every tenant arrives from that one address, NATed. Keep it
# a DHCP reservation, or a new lease would let them all in.
MGMT_LAN="${MGMT_LAN:-10.10.0.0/24}"
OPNSENSE_LAN_IP="${OPNSENSE_LAN_IP:-10.10.0.114}"

# ---------------------------------------------------------------------- runtime

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
info() { printf '%s\n' "$*"; }
ok()   { printf '%s  ok%s   %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s warn%s  %s\n' "$c_yel" "$c_off" "$*"; }
die()  { printf '%s fail%s  %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -Eeuo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

APPLY=0
run() {
  if (( APPLY )); then
    "$@"
  else
    printf '%s       + %s%s\n' "$c_dim" "$(printf '%q ' "$@")" "$c_off"
  fi
}

# write_file PATH CONTENT: show it in a dry run, write it under --apply.
write_file() {
  if (( APPLY )); then
    printf '%s' "$2" > "$1"
  else
    printf '%s       + write %s:%s\n' "$c_dim" "$1" "$c_off"
    printf '%s' "$2" | sed "s/^/${c_dim}           /; s/\$/${c_off}/"
  fi
}

CMD="${1:-}"
[[ -n "$CMD" ]] || usage 2
shift
case "$CMD" in
  images|template|vm|lxc|firewall|tenant|handover|hookscript) ;;
  -h|--help|help) usage 0 ;;
  *) echo "unknown command: $CMD (images, template, vm, lxc, firewall, tenant, handover, hookscript)" >&2; exit 2 ;;
esac

NAME="" VMID="" CORES=1 MEMORY=1024 DISK=8 BRIDGE=vmbr0 IMAGE="" START=1 CPU=""
FROM_TEMPLATE="$TEMPLATE_VMID" BRIDGE_SET=0
T_USER="" T_KEY="" T_IP="" T_ALLOW="" T_UNCAPPED=0
SEG_NAME="" SEG_BRIDGE="" SEG_PREFIX="" SEG_GW="" SEG_GROUP="" SEG_ALIAS="" T_KIND=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)    APPLY=1; shift ;;
    --name)     NAME="${2:?--name needs a value}"; shift 2 ;;
    --vmid)     VMID="${2:?--vmid needs a value}"; shift 2 ;;
    --cores)    CORES="${2:?--cores needs a value}"; shift 2 ;;
    --memory)   MEMORY="${2:?--memory needs a value}"; shift 2 ;;
    --disk)     DISK="${2:?--disk needs a value}"; shift 2 ;;
    --bridge)   BRIDGE="${2:?--bridge needs a value}"; BRIDGE_SET=1; shift 2 ;;
    --user)     T_USER="${2:?--user needs a value}"; shift 2 ;;
    --key)      T_KEY="${2:?--key needs a value}"; shift 2 ;;
    --ip)       T_IP="${2:?--ip needs a value}"; shift 2 ;;
    --allow)    T_ALLOW="${2:?--allow needs a value}"; shift 2 ;;
    --uncapped) T_UNCAPPED=1; shift ;;
    --image)    IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --template) FROM_TEMPLATE="${2:?--template needs a value}"; shift 2 ;;
    --cpu)      CPU="${2:?--cpu needs a value}"; shift 2 ;;
    --no-start) START=0; shift ;;
    -h|--help)  usage 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Options that do not apply to the command are refused, not ignored: `vm
# --image ubuntu...` silently cloning Debian is the wrong answer to a typo.
case "$CMD" in
  template) [[ "$FROM_TEMPLATE" == "$TEMPLATE_VMID" ]] || die "--template is for vm, not template" ;;
  vm)       [[ -z "$IMAGE" ]] || die "vm clones a template; --image is for template and lxc" ;;
  lxc)      [[ "$FROM_TEMPLATE" == "$TEMPLATE_VMID" ]] || die "--template is for vm, not lxc" ;;
  # A tenant on vmbr0 would share a segment with the hypervisor's own
  # management address, so the bridge is not negotiable.
  tenant)   (( ! BRIDGE_SET )) || die "a tenant's bridge is fixed by its segment ($LEASE_BRIDGE, or $TENANT_BRIDGE with --uncapped); --bridge is refused"
            [[ -z "$IMAGE" ]] || die "tenant clones a template; --image is for template and lxc" ;;
esac
if [[ "$CMD" != tenant ]] && { [[ -n "$T_USER$T_IP$T_ALLOW" ]] || (( T_UNCAPPED )); }; then
  die "--user, --ip, --allow and --uncapped are for tenant only"
fi
if [[ "$CMD" != tenant && "$CMD" != handover && -n "$T_KEY" ]]; then
  die "--key is for tenant and handover; your own guests take SSH_PUBKEY"
fi
case "$CMD" in
  template|vm|tenant) ;;
  *) [[ -z "$CPU" ]] || die "--cpu is for template, vm and tenant" ;;
esac
# A lease gets the model that hides AVX (see LEASE_CPU_TYPE); everything else,
# --uncapped work VMs included, gets CPU_TYPE.
if [[ "$CMD" == tenant ]] && (( ! T_UNCAPPED )); then CPU="${CPU:-$LEASE_CPU_TYPE}"; else CPU="${CPU:-$CPU_TYPE}"; fi
[[ "$CPU" =~ ^[A-Za-z0-9._-]+$ ]] || die "--cpu '$CPU' is not a CPU model name"
if [[ "$CMD" == tenant ]] && (( ! T_UNCAPPED )) && ! lease_cpu_ok "$CPU"; then
  die "a lease may only use a CPU model that hides AVX (${LEASE_CPU_ALLOWED[*]}), and '$CPU' is not one of them. --uncapped is for work VMs, which may use other models."
fi
# Passthrough tells a hostile guest exactly what it runs on, and pins it to
# this host's microcode and errata. A named model is enough for any workload.
[[ "$CMD" != tenant || "$CPU" != host ]] || die "a tenant never gets --cpu host; use a named model such as $LEASE_CPU_TYPE (or $CPU_TYPE with --uncapped)"

is_int() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }
for _opt in cores:CORES memory:MEMORY disk:DISK template:FROM_TEMPLATE; do
  _var="${_opt#*:}"
  is_int "${!_var}" || die "--${_opt%%:*} must be a positive integer, got '${!_var}'"
done
[[ -z "$VMID" ]] || is_int "$VMID" || die "--vmid must be a positive integer, got '$VMID'"
(( MEMORY >= 128 )) || die "--memory is in MB; ${MEMORY} is too small"

# ----------------------------------------------------------------- inspection

# The x86-64-vN models need the matching instructions on the host, or the VM
# will not start. `host` and named models are left to qm to judge.
check_cpu() {
  local need=""
  case "$CPU" in
    x86-64-v2*) need=sse4_2 ;;
    x86-64-v3*) need=avx2 ;;
    x86-64-v4*) need=avx512f ;;
  esac
  if [[ -n "$need" ]] && ! grep -m1 '^flags' /proc/cpuinfo | grep -w "$need" >/dev/null; then
    die "cpu $CPU needs $need, which this host does not have; pass --cpu x86-64-v2-AES or another model"
  fi
  ok "cpu $CPU"
}

guest_exists() { qm status "$1" >/dev/null 2>&1 || pct status "$1" >/dev/null 2>&1; }

# `grep -q` is not used after a pipe anywhere here: it exits at the first
# match, the writer dies of SIGPIPE, and under pipefail a match reads as a
# miss. `>/dev/null` makes grep read to the end.

# Every guest name on the host, VMs and containers. Two guests with one
# hostname fight over the DHCP reservation and the Ansible inventory.
guest_names() {
  qm list 2>/dev/null | awk 'NR>1{print $2}'
  pct list 2>/dev/null | awk 'NR>1{print $NF}'
}

# Cloud images: the formats `import-from` reads. ISOs are installers, which
# need a person at the console, so they are listed but never used.
cloud_images() {
  local d f
  for d in "${IMAGE_DIRS[@]}"; do
    for f in "$d"/*.qcow2 "$d"/*.img "$d"/*.raw; do
      if [[ -f "$f" ]]; then printf '%s\n' "$f"; fi
    done
  done
}

lxc_templates() {
  local f
  for f in "$LXC_CACHE"/*.tar.*; do
    if [[ -f "$f" ]]; then printf '%s\n' "${f##*/}"; fi
  done
}

vm_templates() {
  local conf
  for conf in /etc/pve/qemu-server/*.conf; do
    [[ -f "$conf" ]] || continue
    if grep -q '^template: 1' "$conf"; then
      printf '%s %s\n' "$(basename "$conf" .conf)" "$(sed -n 's/^name: //p' "$conf")"
    fi
  done
}

# -------------------------------------------------------------------- preflight

need_host() {
  [[ $EUID -eq 0 ]] || die "must run as root on the Proxmox host"
  local c
  for c in qm pct pvesm pvesh; do
    command -v "$c" >/dev/null || die "$c not found. Run this ON the Proxmox host, not a guest."
  done
}

# A storage that exists but lacks the content type fails at qm/pct create,
# after the VMID is taken. `local` here holds no guest disks at all.
check_storage() {
  local st="$1" want="$2" content
  pvesm status --storage "$st" >/dev/null 2>&1 \
    || die "storage '$st' not found. Pick one from 'pvesm status' and set STORAGE / STORAGE_TMPL."
  content="$(pvesh get "/storage/$st" --output-format json 2>/dev/null \
             | sed -n 's/.*"content":"\([^"]*\)".*/\1/p')"
  if [[ -z "$content" ]]; then
    warn "storage '$st': could not read content types, continuing unchecked"
  elif [[ ",$content," != *",$want,"* ]]; then
    die "storage '$st' has content=[$content], needs $want"
  else
    ok "storage '$st' offers $want"
  fi
}

check_bridge() {
  ip link show "$1" >/dev/null 2>&1 || die "bridge '$1' not found. Bridges here: $(ip -br link | awk '/^vmbr/{print $1}' | tr '\n' ' ')"
  ok "bridge '$1'"
}

check_ssh_key() {
  if [[ -r "$SSH_PUBKEY" ]]; then
    ok "ssh key $SSH_PUBKEY ($(awk '{print $NF; exit}' "$SSH_PUBKEY"))"
  else
    die "ssh public key not readable at $SSH_PUBKEY. Set SSH_PUBKEY; on this host: $(compgen -G "$HOME/.ssh/*.pub" | tr '\n' ' ')"
  fi
}

# Resolves VMID when it was not given, and refuses one already taken.
check_vmid() {
  if [[ -z "$VMID" ]]; then
    VMID="$(pvesh get /cluster/nextid)"
    ok "vmid $VMID (next free)"
  elif guest_exists "$VMID"; then
    die "vmid $VMID already exists; this script never modifies an existing guest"
  else
    ok "vmid $VMID is free"
  fi
}

check_name() {
  [[ "$NAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
    || die "name '$NAME' is not a valid hostname: lowercase letters, digits and inner hyphens"
  if guest_names | grep -x "$NAME" >/dev/null; then
    die "a guest named '$NAME' already exists"
  fi
  ok "name '$NAME' is unused"
}

# RAM is a hard gate: a hypervisor pushed into swap degrades every guest on it.
# Disk is checked against the pool's free space, which on thin storage is what
# runs out, not the sum of allocations.
check_capacity() {
  local avail_mb avail_gb
  avail_mb="$(free -m | awk '/^Mem:/{print $7}')"
  [[ "$avail_mb" =~ ^[0-9]+$ ]] || die "could not parse available memory from 'free -m'"
  if (( MEMORY > avail_mb )); then
    die "not enough RAM: ${MEMORY} MB requested, ${avail_mb} MB available"
  elif (( MEMORY * 100 / avail_mb > 50 )); then
    warn "${MEMORY} MB is $(( MEMORY * 100 / avail_mb ))% of the ${avail_mb} MB available"
  else
    ok "RAM: ${MEMORY} MB of ${avail_mb} MB available"
  fi
  (( CORES <= $(nproc) )) || warn "${CORES} vCPU on a $(nproc) core host"

  avail_gb="$(pvesm status --storage "$STORAGE" | awk 'NR==2{print int($6/1048576)}')"
  if [[ -z "$avail_gb" ]]; then
    warn "could not read free space for storage '$STORAGE'"
  elif (( DISK > avail_gb )); then
    die "storage '$STORAGE': ${DISK} GB requested, only ${avail_gb} GB free"
  else
    ok "disk: ${DISK} GB on '$STORAGE', ${avail_gb} GB free"
  fi
}

# What the last step reports: a dry run must not say "created".
created() { if (( APPLY )); then ok "$1 created"; else ok "$1 would be created"; fi; }

banner() {
  if (( APPLY )); then
    info "MODE: ${c_red}APPLY${c_off}"
  else
    info "MODE: ${c_grn}DRY RUN${c_off}, nothing will be created. Add --apply to execute."
  fi
  info ""
}

# A build is several commands, not a transaction. Armed only after the create
# succeeded, so a VMID collision can never destroy somebody else's guest.
arm_cleanup() {
  local tool=$1 id=$2
  (( APPLY )) || return 0
  trap 'trap - ERR; warn "vmid '"$id"' failed mid-build, destroying the partial guest"; '"$tool"' destroy '"$id"' --purge || warn "cleanup failed for vmid '"$id"'; inspect it manually"' ERR
}

# ---------------------------------------------------------------------- images

cmd_images() {
  local f found=0
  info "cloud images (usable by: template)"
  while read -r f; do
    [[ -n "$f" ]] || continue
    found=1
    info "  ${f##*/}   ${c_dim}$(du -h "$f" | cut -f1), $(date -r "$f" +%F), ${f%/*}${c_off}"
  done < <(cloud_images)
  (( found )) || info "  none"

  info ""
  info "LXC templates (usable by: lxc --image)"
  found=0
  while read -r f; do
    [[ -n "$f" ]] || continue
    found=1
    info "  $f"
  done < <(lxc_templates)
  (( found )) || info "  none"

  info ""
  info "VM templates (usable by: vm --template)"
  found=0
  while read -r f; do
    [[ -n "$f" ]] || continue
    found=1
    info "  $f"
  done < <(vm_templates)
  (( found )) || info "  none yet. Build one with: $0 template"

  info ""
  info "installer ISOs (not usable here: they need an interactive install)"
  found=0
  for f in "${IMAGE_DIRS[0]}"/*.iso; do
    if [[ -f "$f" ]]; then found=1; info "  ${f##*/}"; fi
  done
  (( found )) || info "  none"
}

# -------------------------------------------------------------------- template

# Sets IMAGE_PATH rather than echoing it, so the log lines stay on screen.
IMAGE_PATH=""
resolve_cloud_image() {
  local f
  if [[ -n "$IMAGE" ]]; then
    # Read line by line: `for f in $(cloud_images)` splits a file name with a
    # space in it into two words, and --image could never match it.
    while IFS= read -r f; do
      if [[ "${f##*/}" == "$IMAGE" ]]; then IMAGE_PATH="$f"; fi
    done < <(cloud_images)
  else
    IMAGE_PATH="$(cloud_images | grep -E '/debian-[0-9]+-genericcloud-amd64\.qcow2$' | sort -V | tail -1 || true)"
  fi
  if [[ -z "$IMAGE_PATH" ]]; then
    # shellcheck disable=SC2016  # $IMAGE does expand: the quotes are literal
    die "no cloud image ${IMAGE:+named '$IMAGE' }found. On this host: $(cloud_images | sed 's#.*/##' | tr '\n' ' ')"
  fi
  ok "cloud image ${IMAGE_PATH##*/} ($(date -r "$IMAGE_PATH" +%F))"
}

cmd_template() {
  banner
  info "preflight"
  need_host
  resolve_cloud_image
  check_storage "$STORAGE" images
  check_bridge "$BRIDGE"
  check_cpu

  # debian-13-genericcloud-amd64.qcow2 -> tmpl-debian-13
  if [[ -z "$NAME" ]]; then
    NAME="tmpl-$(basename "$IMAGE_PATH" | sed -E 's/\.(qcow2|img|raw)$//; s/-(genericcloud|generic|cloudimg|server|amd64)//g')"
  fi
  [[ "$NAME" =~ ^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$ ]] || die "template name '$NAME' is not valid; pass --name"

  VMID="${VMID:-$TEMPLATE_VMID}"
  if guest_exists "$VMID"; then
    if qm config "$VMID" 2>/dev/null | grep '^template: 1' >/dev/null; then
      warn "template $VMID already exists ($(qm config "$VMID" | sed -n 's/^name: //p')), nothing to do"
      info "  To rebuild it from a newer image: qm destroy $VMID, then rerun."
      info "  Full clones do not depend on it, so that is safe."
      return 0
    fi
    die "vmid $VMID is an existing guest, not a template. Pass --vmid."
  fi
  ok "vmid $VMID is free"
  info ""

  info "VM template $NAME (vmid $VMID, from ${IMAGE_PATH##*/}, disk on $STORAGE)"
  # Only what every clone shares. Size, bridge, key and name are set per clone.
  # A serial port always, for xterm.js and `qm terminal`. The display is the
  # default VGA, so the web UI's noVNC console shows tty1, except for Debian's
  # genericcloud image, which has no graphics driver and talks on serial
  # only. Agent enabled so PVE shows the IP once the guest runs
  # qemu-guest-agent, which the images do not ship.
  local vga=std
  case "${IMAGE_PATH##*/}" in *genericcloud*) vga=serial0 ;; esac
  run qm create "$VMID" \
      --name "$NAME" \
      --cores 1 \
      --memory 1024 \
      --net0 "virtio,bridge=${BRIDGE},firewall=1" \
      --scsihw virtio-scsi-single \
      --ostype l26 \
      --cpu "$CPU" \
      --agent enabled=1 \
      --serial0 socket --vga "$vga" \
      --description "VM template from ${IMAGE_PATH##*/} dated $(date -r "$IMAGE_PATH" +%F). Built by proxmox/pve-guest.sh"
  arm_cleanup qm "$VMID"
  # Import straight into scsi0: `qm importdisk` then guessing the volume name
  # is wrong on directory storage, and importdisk is deprecated in PVE 9.
  run qm set "$VMID" --scsi0 "${STORAGE}:0,import-from=${IMAGE_PATH},discard=on,ssd=1"
  run qm set "$VMID" --ide2 "${STORAGE}:cloudinit"
  run qm set "$VMID" --boot order=scsi0
  run qm set "$VMID" --ciuser "$CI_USER" --ipconfig0 ip=dhcp
  run qm template "$VMID"
  trap - ERR
  created "template $NAME"
  info ""
  info "next: $0 vm --name <host> --template $VMID"
}

# -------------------------------------------------------------------------- vm

# Sets TMPL_NAME. A disk can only grow, so the template's own size is the floor.
TMPL_NAME=""
check_template() {
  qm config "$FROM_TEMPLATE" 2>/dev/null | grep '^template: 1' >/dev/null \
    || die "vmid $FROM_TEMPLATE is not a VM template. Build one: $0 template --apply"
  local base_gb
  TMPL_NAME="$(qm config "$FROM_TEMPLATE" | sed -n 's/^name: //p')"
  ok "template $FROM_TEMPLATE ($TMPL_NAME)"
  base_gb="$(qm config "$FROM_TEMPLATE" | sed -n 's/^scsi0:.*size=\([0-9]*\)G.*/\1/p')"
  if [[ -n "$base_gb" ]] && (( DISK < base_gb )); then
    die "--disk ${DISK} is smaller than the template's ${base_gb} GB disk"
  fi
}

cmd_vm() {
  [[ -n "$NAME" ]] || die "--name is required"
  banner
  info "preflight"
  need_host
  check_template
  local tmpl_name="$TMPL_NAME"
  check_storage "$STORAGE" images
  check_bridge "$BRIDGE"
  check_cpu
  check_ssh_key
  check_name
  check_vmid
  check_capacity
  info ""

  info "VM   $NAME (vmid $VMID, ${CORES} vCPU, ${MEMORY} MB, ${DISK} GB on $STORAGE, $BRIDGE)"
  # Full, not linked: a linked clone pins the template forever, so it could
  # never be rebuilt from a newer image while the guest exists.
  run qm clone "$FROM_TEMPLATE" "$VMID" --name "$NAME" --full 1 --storage "$STORAGE"
  arm_cleanup qm "$VMID"
  run qm set "$VMID" \
      --cores "$CORES" \
      --cpu "$CPU" \
      --memory "$MEMORY" \
      --net0 "virtio,bridge=${BRIDGE},firewall=1" \
      --onboot 1 \
      --description "$NAME. Cloned from template $FROM_TEMPLATE ($tmpl_name) by proxmox/pve-guest.sh"
  run qm set "$VMID" --ciuser "$CI_USER" --sshkeys "$SSH_PUBKEY" --ipconfig0 ip=dhcp
  run qm disk resize "$VMID" scsi0 "${DISK}G"
  (( START )) && run qm start "$VMID"
  trap - ERR
  created "$NAME"
  next_steps vm
}

# ------------------------------------------------------------------------- lxc

# Sets LXC_TEMPLATE. An exact file name wins; otherwise the newest cached
# build of the family, since pinning a patch version rots.
LXC_TEMPLATE=""
resolve_lxc_template() {
  local want="${IMAGE:-$LXC_FAMILY}"
  if lxc_templates | grep -xF "$want" >/dev/null; then
    LXC_TEMPLATE="$want"
  else
    LXC_TEMPLATE="$(lxc_templates | grep -E "^${want}_.*_amd64\.tar\." | sort -V | tail -1 || true)"
  fi
  [[ -n "$LXC_TEMPLATE" ]] \
    || die "no cached LXC template matches '$want'. Cached: $(lxc_templates | tr '\n' ' ')"
  ok "lxc template $LXC_TEMPLATE"
}

cmd_lxc() {
  [[ -n "$NAME" ]] || die "--name is required"
  banner
  info "preflight"
  need_host
  resolve_lxc_template
  check_storage "$STORAGE" rootdir
  check_storage "$STORAGE_TMPL" vztmpl
  check_bridge "$BRIDGE"
  check_ssh_key
  check_name
  check_vmid
  check_capacity
  info ""

  info "LXC  $NAME (vmid $VMID, ${CORES} vCPU, ${MEMORY} MB, ${DISK} GB on $STORAGE, $BRIDGE)"
  # nesting=1 is what the web UI sets on every unprivileged container and
  # pct create does not. Without it Debian 13's systemd cannot mount /tmp,
  # /run/lock or mqueue, boots degraded, and trips Systemd Unit Failed once
  # monitored: sec-dns came up exactly like that on 2026-09-15.
  run pct create "$VMID" "${STORAGE_TMPL}:vztmpl/${LXC_TEMPLATE}" \
      --hostname "$NAME" \
      --cores "$CORES" \
      --memory "$MEMORY" \
      --swap 512 \
      --rootfs "${STORAGE}:${DISK}" \
      --net0 "name=eth0,bridge=${BRIDGE},firewall=1,ip=dhcp" \
      --ssh-public-keys "$SSH_PUBKEY" \
      --unprivileged 1 \
      --features nesting=1 \
      --onboot 1 \
      --description "$NAME. Created from $LXC_TEMPLATE by proxmox/pve-guest.sh"
  arm_cleanup pct "$VMID"
  (( START )) && run pct start "$VMID"
  trap - ERR
  created "$NAME"
  next_steps lxc
}

# -------------------------------------------------------------------- firewall

# The datacenter firewall is what makes `firewall=1` on a NIC mean anything;
# it was off on this host, so every guest's flag was decorative, and nothing
# filtered traffic to the hypervisor itself: SSH, the web UI, rpcbind and an
# unauthenticated netdata were open to the LAN and, through OPNsense's NAT, to
# every sandbox guest.
#
# policy_in DROP at datacenter level applies to the host only. Guests are
# filtered only when their own firewall says enable: 1, and only tenants do.
CLUSTER_FW="$FW_DIR/cluster.fw"
# One firewall group per segment: tenant for WORK, lease for LEASE.
FW_GROUPS=(tenant lease)
cluster_fw_content() {
  cat <<EOF
# Managed by proxmox/pve-guest.sh; see proxmox/README.md. Two things live here:
# who may manage this hypervisor, and the tenant and lease groups that keep a
# VM on the internet and off everything else.
[OPTIONS]
enable: 0
policy_in: DROP
policy_out: ACCEPT

[ALIASES]

tenant_gw ${TENANT_GW} # OPNsense on the tenant bridge
lease_gw ${LEASE_GW} # OPNsense on the lease bridge

[IPSET management] # may reach SSH, the web UI and the consoles
${MGMT_LAN} # the home LAN, as a way in when Tailscale is down
!${OPNSENSE_LAN_IP} # except OPNsense: the sandbox and the tenants hide behind it
100.64.0.0/10 # Tailscale
fd7a:115c:a1e0::/48 # Tailscale, IPv6

[IPSET private] # every range a tenant must never reach
10.0.0.0/8
172.16.0.0/12
192.168.0.0/16
100.64.0.0/10 # Tailscale, and carrier-grade NAT
169.254.0.0/16
fe80::/10 # IPv6 link-local: another guest on the same bridge
fc00::/7 # IPv6 unique local, which includes the tailnet's fd7a::/48

[group tenant] # work VM on the tenant bridge: internet out, nothing private either way
IN ACCEPT -source tenant_gw -p tcp -dport 22 # setup path, from OPNsense
IN DROP -source +private -log info # the LAN, the sandbox, other tenants and the lease segment
OUT ACCEPT -dest tenant_gw # DNS and the default route
OUT DROP -p tcp -dport 25 -log warning # no spam from the home IP
OUT DROP -dest +private -log info # nothing private, whoever asks

[group lease] # leased VM on the lease bridge: internet out, nothing private either way
IN ACCEPT -source lease_gw -p tcp -dport 22 # setup path, from OPNsense
IN DROP -source +private -log info # the LAN, the sandbox, work VMs and other leases
OUT ACCEPT -dest lease_gw # DNS and the default route
OUT DROP -p tcp -dport 25 -log warning # no spam from the home IP
OUT DROP -dest +private -log info # nothing private, whoever asks
EOF
}

# The host's own rules. Management is PVE's built-in: SSH, 8006, 3128 and the
# VNC range from the management ipset. Everything else, rpcbind and netdata
# included, is dropped unless it arrives over Tailscale, whose own chain
# accepts tailscale0 before PVE's rules run.
host_fw_path() { printf '/etc/pve/nodes/%s/host.fw' "$(hostname)"; }
host_fw_content() {
  cat <<EOF
# Managed by proxmox/pve-guest.sh; see proxmox/README.md.
[OPTIONS]
# OPNsense routes between bridges on this host, so one flow crosses it twice
# and strict conntrack would drop the second leg as INVALID.
nf_conntrack_allow_invalid: 1
# A tenant can open flows faster than the default table (262144) holds them:
# a larger table and a shorter half-open timeout buy time while the per-guest
# alerts fire. The firewall's per-source limits (OPNsense) apply after this table.
nf_conntrack_max: 1048576
nf_conntrack_tcp_timeout_syn_recv: 30

[RULES]
IN DROP -source ${OPNSENSE_LAN_IP} # OPNsense, whatever the ipset says: the sandbox and tenants arrive NATed to it
IN ACCEPT -p udp -sport 67 -dport 68 # DHCP: this host's own address on vmbr0 is a lease
IN ACCEPT -p udp -dport 41641 # Tailscale's direct connections
IN ACCEPT -source +management -p icmp
EOF
}

# PVE skips a rule it cannot parse and carries on, so a typo in a DROP rule is
# a silent hole, and `pve-firewall compile` still exits 0. This parses with
# PVE's own parser and fails on any complaint, in dry runs too.
# The cluster file must hold every group in FW_GROUPS, and a VM must use the
# group of its own segment and no other.
#   fw_check CLUSTER_FILE [VMID DIR GROUP]   (DIR holds VMID.fw; GROUP is the
#                                            group that VM must use)
#   fw_check CLUSTER_FILE host HOST_FILE
fw_check() {
  FW_WANT="${FW_GROUPS[*]}" perl -MPVE::Firewall -e '
    use strict; use warnings;
    my ($cfile, $vmid, $vdir, $group) = @ARGV;
    my @w; local $SIG{__WARN__} = sub { push @w, $_[0] };
    my $bad = 0;
    my $rules = sub {
      my ($where, $list) = @_;
      for my $r (@{$list || []}) {
        next if !$r->{errors};
        $bad++;
        print "  $where: $_: $r->{errors}->{$_}\n" for sort keys %{$r->{errors}};
      }
    };
    my $ipsets = sub {
      my ($where, $sets) = @_;
      for my $n (sort keys %{$sets || {}}) {
        for my $e (@{$sets->{$n}}) {
          if ($e->{errors}) { $bad++; print "  $where ipset $n: bad entry $e->{cidr}\n"; }
        }
      }
    };
    if (!-f $cfile) { $bad++; print "  cluster: $cfile does not exist\n"; }
    my $c = PVE::Firewall::load_clusterfw_conf($cfile);
    $rules->("cluster", $c->{rules});
    $rules->("group $_", $c->{groups}->{$_}) for sort keys %{$c->{groups} || {}};
    $ipsets->("cluster", $c->{ipset});
    for my $g (split " ", $ENV{FW_WANT}) {
      if (!$c->{groups}->{$g} || !@{$c->{groups}->{$g}}) { $bad++; print "  cluster: no $g group\n"; }
    }
    if (defined $vmid && $vmid eq "host") {
      if (!-f $vdir) { $bad++; print "  host: $vdir does not exist\n"; }
      my $h = PVE::Firewall::load_hostfw_conf($c, $vdir);
      $rules->("host", $h->{rules});
    } elsif (defined $vmid) {
      if (!-f "$vdir/$vmid.fw") { $bad++; print "  vm $vmid: $vdir/$vmid.fw does not exist\n"; }
      my $v = PVE::Firewall::load_vmfw_conf($c, "vm", $vmid, $vdir);
      $rules->("vm $vmid", $v->{rules});
      $ipsets->("vm $vmid", $v->{ipset});
      my @used = map { $_->{action} } grep { ($_->{type} // "") eq "group" } @{$v->{rules}};
      if (!defined $group) {
        $bad++; print "  vm $vmid: no group given to check against\n";
      } elsif (@used != 1 || $used[0] ne $group) {
        $bad++; print "  vm $vmid: want exactly one GROUP $group rule, has: ", (@used ? join(" ", @used) : "none"), "\n";
      }
    }
    print "  $_" for @w;
    exit(($bad || @w) ? 1 : 0);
  ' "$@"
}

firewall_enabled() {
  pvesh get /cluster/firewall/options --output-format json 2>/dev/null | grep '"enable":1' >/dev/null
}
# Prints the groups in FW_GROUPS that $CLUSTER_FW lacks, one per line.
fw_groups_missing() {
  local g
  for g in "${FW_GROUPS[@]}"; do
    grep -i "^\[group $g\]" "$CLUSTER_FW" >/dev/null 2>&1 || printf '%s\n' "$g"
  done
}
tenant_group_present() {
  [[ -z "$(fw_groups_missing)" ]]
}

# The comparable form of a firewall file, one entry per line with its section in
# front so a diff of it says where each line goes: comments and blank lines
# gone, the sections in name order, and the entries of the sections whose order means
# nothing ([OPTIONS], [ALIASES], [IPSET ...]) sorted, so a file PVE has
# rewritten compares equal to what this script writes. The rule sections
# ([RULES], [group ...]) keep their order, since the first match wins in them.
# With FW_IGNORE_ENABLE=1 the `enable:` option is left out: it is the
# datacenter switch, written as 0 here and turned on by the operator later.
fw_normal() {
  perl -e '
    my ($sec, %e) = ("");
    while (my $l = <>) {
      chomp $l; $l =~ s/\s+#.*$//; $l =~ s/^\s+|\s+$//g;
      next if $l eq "" || $l =~ /^#/;
      if ($l =~ /^\[/) { $sec = $l; $e{$sec} ||= []; next; }
      next if $ENV{FW_IGNORE_ENABLE} && $sec eq "[OPTIONS]" && $l =~ /^enable:/;
      push @{$e{$sec}}, $l;
    }
    for my $s (sort keys %e) {
      my @l = @{$e{$s}};
      @l = sort @l if $s =~ /^\[(OPTIONS|ALIASES|IPSET )/i;
      print @l ? (map { "$s $_\n" } @l) : "$s\n";
    }
  ' "$1"
}

# `firewall` with a cluster.fw that already has both groups: say whether it is
# what this script writes now. It is never rewritten, since it may hold the
# operator's edits; a difference is shown so an older install can merge what
# was added since (the IPv6 private ranges, say).
cluster_fw_report() {
  local have want
  have="$(FW_IGNORE_ENABLE=1 fw_normal "$CLUSTER_FW")"
  want="$(FW_IGNORE_ENABLE=1 fw_normal <(cluster_fw_content))"
  if [[ "$have" == "$want" ]]; then
    ok "cluster.fw is current: $CLUSTER_FW"
    return 0
  fi
  info ""
  info "$CLUSTER_FW differs from what this script writes now. In the diff, - is in"
  info "the file and + is what the script writes; comments, blank lines, the"
  info "datacenter switch and the order of options, aliases and ipset entries are"
  info "ignored:"
  diff -u --label "$CLUSTER_FW" --label "written by this script" \
    <(printf '%s\n' "$have") <(printf '%s\n' "$want") | sed 's/^/    /' || true
  info ""
  warn "$CLUSTER_FW is not rewritten, because it may hold your edits. Merge the + lines by hand, or move the file aside and rerun."
}

# Prints the host's input chain as PVE compiles it from DIR's cluster.fw and
# host.fw: what the kernel will get, not what the files say. Compiled from a
# copy with enable: 1, since PVE compiles nothing while it is off.
fw_host_rules() {
  local dir=$1
  sed -i 's/^enable: 0$/enable: 1/' "$dir/cluster.fw"
  perl -MPVE::Firewall -e '
    my $vmdata = { testdir => $ARGV[0], qemu => {}, lxc => {} };
    my ($r, $ipsets) = PVE::Firewall::compile(undef, undef, $vmdata);
    print "    $_\n" for @{$r->{filter}->{"PVEFW-HOST-IN"} || []};
    for my $s (sort keys %$ipsets) {
      next if $s !~ /management-v4/;
      print "    $_\n" for grep { /^add/ } @{$ipsets->{$s}};
    }
  ' "$dir"
}

# The session running this must survive the switch. A Tailscale session always
# does (100.64.0.0/10, or fd7a:115c:a1e0::/48 over IPv6): Tailscale's own chain
# runs first and accepts tailscale0. A LAN session survives if it comes from
# MGMT_LAN, and never through OPNsense.
check_mgmt_access() {
  [[ "$MGMT_LAN" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.0/24$ ]] || die "MGMT_LAN must be a /24 such as 10.10.0.0/24"
  local lan="${BASH_REMATCH[1]}" src="${SSH_CLIENT%% *}"
  if iptables -S INPUT 2>/dev/null | sed -n 2p | grep -x -- '-A INPUT -j ts-input' >/dev/null; then
    ok "Tailscale's own rules run first, so access over Tailscale stays open"
  else
    warn "Tailscale's ts-input is not first in INPUT; Tailscale access relies on the management ipset"
  fi
  if [[ -z "$src" ]]; then
    warn "not an SSH session, so it cannot be checked; keep a Tailscale session open when enabling"
  elif [[ "$src" == "$OPNSENSE_LAN_IP" ]]; then
    die "this session comes through OPNsense ($src), which the host firewall drops. Connect over Tailscale."
  elif [[ "$src" =~ ^100\.([0-9]+)\. ]] && (( BASH_REMATCH[1] >= 64 && BASH_REMATCH[1] <= 127 )); then
    ok "this session ($src) is over Tailscale"
  elif [[ "${src,,}" == fd7a:115c:a1e0:* ]]; then
    ok "this session ($src) is over Tailscale (IPv6)"
  elif [[ "$src" == "$lan".* ]]; then
    ok "this session ($src) is from the management LAN"
  else
    die "this session ($src) is from neither Tailscale nor $MGMT_LAN, and would be cut off"
  fi
  # PVE adds its idea of the local network to management by itself. Here it
  # resolves the host to a stale /etc/hosts entry, so the explicit list is what
  # keeps the LAN in.
  local localip; localip="$(pve-firewall localnet 2>/dev/null | sed -n 's/^local IP address: //p')"
  if [[ -n "$localip" && "$localip" != "$lan".* ]]; then
    warn "PVE resolves this host to $localip (/etc/hosts), not to an address on $MGMT_LAN; the management list below covers it"
  fi
}

cmd_firewall() {
  banner
  info "preflight"
  need_host
  command -v pve-firewall >/dev/null || die "pve-firewall not found"

  # A hand-written cluster.fw or host.fw is somebody's policy, and rewriting
  # it could open or close things nobody reviewed, so those are never merged
  # into. host.fw is the script's own when it carries this script's header, or
  # when its rules equal what the script writes (PVE drops the comments when it
  # rewrites a file): if it is missing it is written, and if it differs from
  # what the generator now produces an older version wrote it, so it is
  # rewritten to get what later versions added.
  local hostfw host_action=""; hostfw="$(host_fw_path)"
  if tenant_group_present; then
    cluster_fw_report
  elif [[ -e "$CLUSTER_FW" ]]; then
    info ""
    info "Merge this into $CLUSTER_FW by hand, or move the file aside and rerun:"
    cluster_fw_content | sed 's/^/    /'
    die "$CLUSTER_FW exists and lacks the firewall group(s): $(fw_groups_missing | paste -sd' ' -); not merging into it"
  fi
  if [[ ! -e "$hostfw" ]]; then
    host_action=wrote
  elif [[ "$(fw_normal "$hostfw")" == "$(fw_normal <(host_fw_content))" ]]; then
    if grep '^# Managed by proxmox/pve-guest.sh' "$hostfw" >/dev/null; then
      ok "host.fw is current: $hostfw"
    else
      ok "host.fw is current: $hostfw (it has lost its header comment, as a file PVE rewrote does, but its rules are the script's)"
    fi
  elif grep '^# Managed by proxmox/pve-guest.sh' "$hostfw" >/dev/null; then
    host_action=updated
    info ""
    info "$hostfw differs from what this script writes now (- is in the file, + is what it will write):"
    diff -u --label "$hostfw" --label "written by this script" "$hostfw" <(host_fw_content) | sed 's/^/    /' || true
  else
    info ""
    info "Merge this into $hostfw by hand, or move the file aside and rerun:"
    host_fw_content | sed 's/^/    /'
    die "$hostfw exists and was not written by this script; not merging into it"
  fi

  check_mgmt_access

  # Guests that turn filtered with the datacenter switch: any whose own
  # firewall says enable: 1. Today that should be tenants and nothing else.
  local f live=()
  for f in "$FW_DIR"/*.fw; do
    [[ -f "$f" && "$f" != "$CLUSTER_FW" ]] || continue
    if grep '^enable: 1' "$f" >/dev/null; then live+=("$(basename "$f" .fw)"); fi
  done
  if (( ${#live[@]} )); then
    warn "these guests have their own firewall enabled and will be filtered once it is on: ${live[*]}"
  else
    ok "no guest has its own firewall enabled; enabling filters only the host"
  fi
  if firewall_enabled; then ok "datacenter firewall is enabled"; else warn "datacenter firewall is disabled"; fi

  local tmp; tmp="$(mktemp -d)"
  cluster_fw_content > "$tmp/cluster.fw"
  host_fw_content > "$tmp/host.fw"
  if ! fw_check "$tmp/cluster.fw" host "$tmp/host.fw"; then
    rm -rf "$tmp"; die "the rules do not parse cleanly"
  fi
  ok "rules parse cleanly with PVE's own parser"
  info ""
  info "what the host will accept once the datacenter firewall is on, as PVE"
  info "compiles it (after Tailscale's own chain, which accepts tailscale0):"
  fw_host_rules "$tmp"
  rm -rf "$tmp"
  info ""

  local wrote_cluster=0
  if ! tenant_group_present; then
    wrote_cluster=1
    info "write the rules (the datacenter firewall stays off)"
    write_file "$CLUSTER_FW" "$(cluster_fw_content)"$'\n'
  fi
  case "$host_action" in
    wrote)   info "write host.fw (it does not exist)"
             write_file "$hostfw" "$(host_fw_content)"$'\n' ;;
    updated) info "update host.fw (it is this script's own, so it is rewritten from the generator)"
             write_file "$hostfw" "$(host_fw_content)"$'\n' ;;
  esac
  if (( APPLY )); then
    [[ -f "$CLUSTER_FW" ]] || die "$CLUSTER_FW is missing after the write"
    [[ -f "$hostfw" ]] || die "$hostfw is missing after the write"
    fw_check "$CLUSTER_FW" host "$hostfw" || die "the rules as written do not parse cleanly"
    if [[ -z "$host_action" ]] && (( ! wrote_cluster )); then
      ok "re-checked, nothing to write"
    else
      ok "written and re-checked"
    fi
  fi
  case "$host_action" in
    wrote)   if (( APPLY )); then ok "wrote host.fw"; else ok "host.fw would be written"; fi ;;
    updated) if (( APPLY )); then ok "updated host.fw"; else ok "host.fw would be updated"; fi ;;
  esac
  info ""
  info "next steps, each on its own, checking in between"
  info "  1. Configure OPNsense's TENANT and LEASE interfaces: proxmox/README.md#opnsense."
  info "     Make sure $OPNSENSE_LAN_IP is a DHCP reservation on the home router."
  info "  2. Enable the datacenter firewall, from a Tailscale session, with an"
  info "     automatic undo in case you lose access:"
  info "       systemd-run --on-active=10min --unit=pve-fw-undo pvesh set /cluster/firewall/options --enable 0"
  info "       pvesh set /cluster/firewall/options --enable 1"
  info "     From a NEW session, check SSH and https://<host>:8006 over Tailscale"
  info "     and over the LAN, that the LAN reaches the internet through OPNsense,"
  info "     and that Grafana still receives from every host. Then keep it:"
  info "       systemctl stop pve-fw-undo.timer"
  info "  3. Create yourself as the first lease (and a work VM with --uncapped) and run"
  info "     the isolation checks from inside it before giving a lease to anybody."
  (( APPLY )) || { info ""; info "This was a dry run. Re-run with --apply to write."; }
}

# ---------------------------------------------------------------------- tenant

# Where a tenant lives. A lease, the default, goes on the LEASE segment;
# --uncapped, a VM of your own, goes on the WORK segment. Sets SEG_NAME,
# SEG_BRIDGE, SEG_PREFIX, SEG_GW, SEG_GROUP (the firewall group), SEG_ALIAS
# (the cluster.fw alias of the gateway) and T_KIND (what the VM is called in
# its description and its firewall file).
tenant_segment() {
  if (( T_UNCAPPED )); then
    SEG_NAME=work SEG_BRIDGE="$TENANT_BRIDGE" SEG_PREFIX="$TENANT_PREFIX" SEG_GW="$TENANT_GW"
    SEG_GROUP=tenant SEG_ALIAS=tenant_gw T_KIND="Work VM (isolated like a tenant)"
  else
    SEG_NAME=lease SEG_BRIDGE="$LEASE_BRIDGE" SEG_PREFIX="$LEASE_PREFIX" SEG_GW="$LEASE_GW"
    SEG_GROUP=lease SEG_ALIAS=lease_gw T_KIND="Leased VM"
  fi
}

# Every tenant VM's firewall file pins its address in [IPSET ipfilter-net0], on
# either segment. Prints the file of each VM that has $1 there.
ip_owners() {
  local f
  for f in "$FW_DIR"/[0-9]*.fw; do
    [[ -f "$f" ]] || continue
    if awk -v ip="$1" '/^\[/ { in_set = ($0 ~ /^\[IPSET ipfilter-net0\]/); next } in_set && $1 == ip { found = 1 } END { exit !found }' "$f"; then
      basename "$f"
    fi
  done
  return 0
}

# The segment's group names its gateway by alias. Without it in cluster.fw, or
# with another address, the setup-path and DNS rules would point at nothing.
check_gw_alias() {
  local have
  have="$(awk -v a="$SEG_ALIAS" '/^\[/ { in_al = ($0 ~ /^\[ALIASES\]/); next } in_al && $1 == a { print $2; exit }' "$CLUSTER_FW" 2>/dev/null || true)"
  if [[ -z "$have" ]]; then
    (( APPLY )) && die "alias $SEG_ALIAS is not in $CLUSTER_FW, so the $SEG_GROUP group's rules would not resolve. See: $0 firewall"
    warn "alias $SEG_ALIAS is not in $CLUSTER_FW; --apply would refuse. See: $0 firewall"
  elif [[ "$have" != "$SEG_GW" ]]; then
    (( APPLY )) && die "alias $SEG_ALIAS is $have in $CLUSTER_FW, but this segment's gateway is $SEG_GW"
    warn "alias $SEG_ALIAS is $have in $CLUSTER_FW, not $SEG_GW; --apply would refuse"
  else
    ok "alias $SEG_ALIAS is $have"
  fi
}

tenant_fw_content() {
  local port
  cat <<EOF
# Managed by proxmox/pve-guest.sh: ${T_KIND} for ${T_USER}.
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT
log_level_in: info
log_level_out: nolog
ipfilter: 1
macfilter: 1
dhcp: 0
radv: 0

[IPSET ipfilter-net0] # the only source address net0 may use
${T_IP}

[RULES]
GROUP ${SEG_GROUP}
EOF
  for port in ${T_ALLOW//,/ }; do
    printf 'IN ACCEPT -p tcp -dport %s\n' "$port"
  done
}

# The lease packet-rate hookscript, as installed. A hookscript runs on the
# host, as root, at each phase of a VM's life, so this one does the least
# possible: it acts only after a start, and every path inside it ends in
# exit 0, so a failure in it never stops a VM from starting. (A missing or
# non-executable file is another matter: PVE runs pre-start with stop-on-error.)
# __PPS__ becomes LEASE_PPS. The filter goes at priority 5, ahead of the
# `basic police` byte-rate filter PVE adds at 50 to a rate-limited tap, and
# the earlier one is deleted first so a restart never stacks a second copy.
# conform-exceed is drop/continue: a packet within the rate must go on to the
# filter at 50, because the default for a conforming packet, ok, ends
# classification at 5 and the byte cap would never see it. Both limits apply.
hookscript_content() {
  sed "s/__PPS__/${LEASE_PPS}/" <<'EOF'
#!/usr/bin/perl
# Managed by proxmox/pve-guest.sh. Do not edit by hand.
# Packets-per-second ceiling on a leased VM's tap (what the guest sends), applied at every start.
use strict; use warnings;
my ($arg, $phase) = @ARGV;
exit 0 if !defined $arg || !defined $phase || $phase ne 'post-start';
my ($vmid) = $arg =~ /\A(\d+)\z/;
exit 0 if !defined $vmid;
my $pps = __PPS__; my $burst = $pps * 2; my $tap = "tap${vmid}i0";
for (1 .. 20) { last if -d "/sys/class/net/$tap"; select(undef, undef, undef, 0.25); }
if (!-d "/sys/class/net/$tap") { print "lease-limits: $tap not found, no packet-rate limit set\n"; exit 0; }
my $q = `tc qdisc show dev $tap 2>/dev/null` // '';
system('tc','qdisc','add','dev',$tap,'ingress') if $q !~ /\bingress\b/;
system('tc','filter','del','dev',$tap,'parent','ffff:','prio','5');   # a previous start may have left it
system('tc','filter','add','dev',$tap,'parent','ffff:','protocol','all','prio','5','matchall','action','police','pkts_rate',$pps,'pkts_burst',$burst,'conform-exceed','drop/continue') == 0
  or print "lease-limits: could not set the packet-rate limit on $tap\n";
exit 0;   # never fail a VM start
EOF
}

# Proxmox takes a hookscript only from a storage whose content list includes
# snippets, and `local` here lists vztmpl, iso, backup and import. Sets
# SNIPPETS_CONTENT to the list `local` must be given, or leaves it empty when
# `local` already offers snippets. The existing list is read and extended,
# never replaced. The hookscript's directory must be where `local` keeps
# snippets, or the volume id handed to qm would name another file.
SNIPPETS_CONTENT=""
check_snippets() {
  local st="$SNIPPETS_STORAGE" json content path
  json="$(pvesh get "/storage/$st" --output-format json 2>/dev/null)" \
    || die "there is no storage named '$st', which the lease packet-rate hookscript needs (it lives in the snippets directory of '$st'). Pick another LEASE_HOOKSCRIPT or create the storage."
  content="$(sed -n 's/.*"content":"\([^"]*\)".*/\1/p' <<<"$json")"
  path="$(sed -n 's/.*"path":"\([^"]*\)".*/\1/p' <<<"$json")"
  if [[ -z "$path" ]]; then
    warn "storage '$st': could not read its path, so $LEASE_HOOKSCRIPT is unchecked"
  elif [[ "$(dirname "$LEASE_HOOKSCRIPT")" != "$path/snippets" ]]; then
    die "LEASE_HOOKSCRIPT is in $(dirname "$LEASE_HOOKSCRIPT"), but storage '$st' keeps snippets in $path/snippets"
  fi
  if pvesm status --content snippets 2>/dev/null | awk -v s="$st" 'NR>1 && $1 == s && $3 == "active" { f = 1 } END { exit !f }'; then
    ok "storage '$st' offers snippets"
  elif [[ -z "$content" ]]; then
    die "could not read the content list of storage '$st'; not guessing at it"
  elif [[ ",$content," == *",snippets,"* ]]; then
    die "storage '$st' lists snippets in its content but is not active"
  else
    SNIPPETS_CONTENT="$content,snippets"
    warn "storage '$st' has content=[$content], no snippets; --apply will run: pvesm set $st --content $SNIPPETS_CONTENT"
  fi
}

# Writes the hookscript, only when what is there differs, so a lease created
# later does not touch the file the running leases were started from. Every
# lease shares it: a new LEASE_PPS reaches the others at their next start.
install_hookscript() {
  local want have
  want="$(hookscript_content)"
  have=""
  if [[ -f "$LEASE_HOOKSCRIPT" ]]; then have="$(cat "$LEASE_HOOKSCRIPT")"; fi
  if [[ "$have" == "$want" ]]; then
    ok "hookscript $LEASE_HOOKSCRIPT is current (${LEASE_PPS} pps)"
    if [[ "$(stat -c %a "$LEASE_HOOKSCRIPT")" != 755 ]]; then run chmod 0755 "$LEASE_HOOKSCRIPT"; fi
    return 0
  fi
  run mkdir -p "$(dirname "$LEASE_HOOKSCRIPT")"
  write_file "$LEASE_HOOKSCRIPT" "$want"$'\n'
  run chmod 0755 "$LEASE_HOOKSCRIPT"
}

# The host-wide half of a lease's packet-rate limit, in the order PVE needs:
# the storage must list snippets before the file goes in. `tenant` and
# `hookscript` both call this, so they do exactly the same.
install_lease_hookscript() {
  info "lease packet-rate limit: ${LEASE_PPS} pps, burst $(( LEASE_PPS * 2 )), set at every start by $LEASE_HOOKSCRIPT"
  if [[ -n "$SNIPPETS_CONTENT" ]]; then run pvesm set "$SNIPPETS_STORAGE" --content "$SNIPPETS_CONTENT"; fi
  install_hookscript
}

# A public key, not a private one: whatever this is goes to cloud-init.
check_key_file() {
  [[ -n "$T_KEY" ]] || die "--key is required"
  [[ -r "$T_KEY" ]] || die "--key $T_KEY is not readable"
  grep -E '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com) ' "$T_KEY" >/dev/null \
    || die "--key $T_KEY does not look like an SSH public key"
}

# The hypervisor's root trusting a key is the best available sign it is yours.
key_is_operators() {
  grep -F "$(awk '{print $2; exit}' "$T_KEY")" /root/.ssh/authorized_keys >/dev/null 2>&1
}

check_tenant() {
  [[ "$T_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] \
    || die "--user '$T_USER' is not a valid login name"
  case "$T_USER" in root|admin|debian) die "--user '$T_USER' is reserved; use the tenant's own name" ;; esac

  check_key_file
  if key_is_operators; then
    ok "setup key $T_KEY ($(awk '{print $NF; exit}' "$T_KEY")) is yours; hand it over before the tenant gets it"
  else
    warn "--key is not one this host trusts, so you will not be able to set this VM up. Use yours, then handover."
  fi

  # Four plain decimal numbers 0 to 255, no leading zeros: bash reads 08 as a
  # bad octal in arithmetic and 010 as 8, and neither would match the address
  # the firewall file pins.
  local o bad=0 octets=() last other owners
  if [[ "$T_IP" =~ ^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$ ]]; then
    IFS=. read -r -a octets <<<"$T_IP"
    for o in "${octets[@]}"; do (( o <= 255 )) || bad=1; done
  else
    bad=1
  fi
  (( ! bad )) || die "--ip '$T_IP' is not a plain dotted-quad address: four decimal numbers from 0 to 255, none with a leading zero"
  last="${T_IP##*.}"
  if ! [[ "$T_IP" == "${SEG_PREFIX}.${last}" ]] || (( last < 2 || last > 254 )); then
    if (( T_UNCAPPED )); then other="a lease uses ${LEASE_PREFIX}.x"; else other="--uncapped uses ${TENANT_PREFIX}.x"; fi
    die "--ip must be in ${SEG_PREFIX}.2 to ${SEG_PREFIX}.254, the $SEG_NAME segment on $SEG_BRIDGE ($other)"
  fi
  owners="$(ip_owners "$T_IP")"
  if [[ -n "$owners" ]]; then
    die "$T_IP is already another tenant's: $(tr '\n' ' ' <<<"$owners")"
  fi
  ok "ip $T_IP is unused by other tenants"

  local port
  for port in ${T_ALLOW//,/ }; do
    # No leading zero: `-dport 080` is not what was meant, and a rule PVE
    # reads differently from the one asked for is worse than a refusal.
    if ! [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then die "--allow: '$port' is not a port (1 to 65535, no leading zero)"; fi
  done

  # Never create a tenant that is unfiltered, even for a moment. A dry run
  # says so and carries on so the plan is still visible. Both groups are
  # required, not only this segment's: one cluster.fw serves both.
  local missing; missing="$(fw_groups_missing | paste -sd' ' -)"
  if [[ -n "$missing" ]]; then
    (( APPLY )) && die "no firewall group(s): $missing. Run: $0 firewall --apply"
    warn "no firewall group(s) yet: $missing; --apply would refuse. Run: $0 firewall"
  elif ! firewall_enabled; then
    (( APPLY )) && die "the datacenter firewall is off, so a tenant would be unfiltered. See: $0 firewall"
    warn "datacenter firewall is off; --apply would refuse"
  else
    ok "firewall groups ${FW_GROUPS[*]} present and datacenter firewall on"
  fi
  if [[ "$SEG_NAME" == lease && -z "$missing" ]]; then check_gw_alias; fi
}

cmd_tenant() {
  [[ -n "$NAME" ]] || die "--name is required"
  [[ -n "$T_USER" ]] || die "--user is required"
  [[ -n "$T_IP" ]] || die "--ip is required"
  tenant_segment
  BRIDGE="$SEG_BRIDGE"
  if (( ! T_UNCAPPED )); then is_int "$LEASE_PPS" || die "LEASE_PPS must be a positive integer, got '$LEASE_PPS'"; fi
  banner
  info "preflight"
  need_host
  check_template
  check_storage "$STORAGE" images
  check_bridge "$BRIDGE"
  check_cpu
  check_tenant
  if (( ! T_UNCAPPED )); then check_snippets; fi
  check_name
  check_vmid
  check_capacity

  # The tenant's rules, parsed against the firewall groups as they are, or as
  # `firewall` would write them when this is a dry run ahead of that step.
  local tmp; tmp="$(mktemp -d)"
  if tenant_group_present; then cp "$CLUSTER_FW" "$tmp/cluster.fw"; else cluster_fw_content > "$tmp/cluster.fw"; fi
  tenant_fw_content > "$tmp/$VMID.fw"
  fw_check "$tmp/cluster.fw" "$VMID" "$tmp" "$SEG_GROUP" || { rm -rf "$tmp"; die "the tenant's firewall does not parse cleanly"; }
  rm -rf "$tmp"
  ok "tenant firewall parses cleanly with PVE's own parser"
  info ""

  # Uncapped drops the CPU, disk and network limits and nothing else: the
  # firewall, the reserved RAM and the agent stay as for any tenant.
  local limits=",mbps_rd=${TENANT_DISK_MBPS},mbps_wr=${TENANT_DISK_MBPS},iops_rd=${TENANT_DISK_IOPS},iops_wr=${TENANT_DISK_IOPS}"
  # Every tenant is tagged tenant, which is what `handover` and the README's
  # grep look for; the second tag says which segment it is on.
  local cpuunits=(--cpuunits "$TENANT_CPUUNITS") rate=",rate=${TENANT_NET_RATE}" tags="tenant;lease" caps=capped
  if (( T_UNCAPPED )); then limits="" cpuunits=() rate="" tags="tenant;uncapped" caps=uncapped; fi
  info "TENANT $NAME for $T_USER (vmid $VMID, ${CORES} vCPU, ${MEMORY} MB, ${DISK} GB, $T_IP on $BRIDGE, $SEG_NAME segment, $caps)"
  # Host-wide setup first, before any guest exists, so a failure here leaves
  # nothing half-built: the storage must list snippets before the hookscript is
  # installed, and the hookscript must exist before `qm set --hookscript`.
  if (( ! T_UNCAPPED )); then install_lease_hookscript; fi
  run qm clone "$FROM_TEMPLATE" "$VMID" --name "$NAME" --full 1 --storage "$STORAGE"
  if (( APPLY )); then
    trap 'trap - ERR; warn "vmid '"$VMID"' failed mid-build, destroying the partial guest"; qm destroy '"$VMID"' --purge || warn "cleanup failed for vmid '"$VMID"'; inspect it manually"; rm -f '"$FW_DIR/$VMID.fw"'' ERR
  fi
  # balloon 0: the tenant's RAM is reserved, never reclaimed from under it or
  # overcommitted against yours. Agent off: nothing on the host parses what a
  # hostile guest says, and the IP is static so nothing needs asking.
  run qm set "$VMID" \
      --cores "$CORES" \
      --cpu "$CPU" \
      --memory "$MEMORY" \
      --balloon 0 \
      "${cpuunits[@]}" \
      --agent enabled=0 \
      --onboot 1 \
      --tags "$tags" \
      --description "${T_KIND} for ${T_USER}. Created by proxmox/pve-guest.sh tenant from template $FROM_TEMPLATE ($TMPL_NAME)"
  run qm set "$VMID" --net0 "virtio,bridge=${BRIDGE},firewall=1${rate}"
  # Set before the first start, since the hookscript acts after each start.
  if (( ! T_UNCAPPED )); then run qm set "$VMID" --hookscript "${SNIPPETS_STORAGE}:snippets/$(basename "$LEASE_HOOKSCRIPT")"; fi
  # nameserver set explicitly: left empty, cloud-init hands the guest the
  # hypervisor's own resolvers, which are on the LAN.
  run qm set "$VMID" --ciuser "$T_USER" --sshkeys "$T_KEY" \
      --ipconfig0 "ip=${T_IP}/24,gw=${SEG_GW}" --nameserver "$SEG_GW"
  run qm disk resize "$VMID" scsi0 "${DISK}G"
  local vol="<scsi0 volume>"
  if (( APPLY )); then vol="$(qm config "$VMID" | sed -n 's/^scsi0: \([^,]*\),.*/\1/p')"; fi
  run qm set "$VMID" --scsi0 "${vol},discard=on,ssd=1${limits}"
  # The firewall exists before the first boot, so there is no unfiltered window.
  write_file "$FW_DIR/$VMID.fw" "$(tenant_fw_content)"$'\n'
  if (( APPLY )); then
    # `false`, not die: exit does not fire the ERR trap, and this guest must
    # not survive with rules that did not load.
    fw_check "$CLUSTER_FW" "$VMID" "$FW_DIR" "$SEG_GROUP" || { warn "$FW_DIR/$VMID.fw as written does not parse cleanly"; false; }
  fi
  (( START )) && run qm start "$VMID"
  trap - ERR
  # Last, because it also stops the cleanup above from destroying it.
  run qm set "$VMID" --protection 1
  created "$NAME"
  next_steps tenant
}

# ------------------------------------------------------------------ hookscript

# Installs the lease packet-rate hookscript without creating a VM: for a host
# where the file or the snippets setting is missing (a lease then fails to
# start, since PVE runs the hookscript's pre-start phase with stop-on-error),
# and for attaching it to a lease that was made without it. With --vmid it also
# attaches it, to a QEMU VM tagged lease and to no other: a work VM never gets
# the limit, and an old lease tagged only tenant is not known to be on the
# lease segment.
cmd_hookscript() {
  is_int "$LEASE_PPS" || die "LEASE_PPS must be a positive integer, got '$LEASE_PPS'"
  banner
  info "preflight"
  need_host
  local volid conf="" cur=""
  volid="${SNIPPETS_STORAGE}:snippets/$(basename "$LEASE_HOOKSCRIPT")"
  if [[ -n "$VMID" ]]; then
    conf="$(qm config "$VMID" 2>/dev/null)" \
      || die "vmid $VMID is not a QEMU VM on this host; a lease is a VM, and containers are never leased"
    grep -E '^tags: (.*[;, ])?lease([;, ].*)?$' <<<"$conf" >/dev/null \
      || die "vmid $VMID is not tagged lease, so it is not a lease: work VMs never get the limit, and a VM made before the LEASE segment existed is tagged tenant alone. See proxmox/README.md#look-after-tenants"
    cur="$(sed -n 's/^hookscript: //p' <<<"$conf")"
    if [[ -n "$cur" && "$cur" != "$volid" ]]; then
      die "vmid $VMID already has the hookscript $cur; detach it first with: qm set $VMID --delete hookscript"
    fi
    ok "lease VM $VMID ($(sed -n 's/^name: //p' <<<"$conf"))"
  fi
  check_snippets
  info ""

  info "HOOKSCRIPT ${volid}${VMID:+, for vmid $VMID}"
  install_lease_hookscript
  if [[ -n "$VMID" ]]; then
    if [[ "$cur" == "$volid" ]]; then ok "vmid $VMID already uses $volid"; else run qm set "$VMID" --hookscript "$volid"; fi
  fi
  if (( APPLY )); then ok "hookscript in place"; else ok "hookscript would be put in place"; fi
  info ""
  info "next steps"
  if [[ -n "$VMID" ]]; then
    info "  1. The limit is set when the VM starts: qm shutdown $VMID, then qm start $VMID."
    info "  2. Check it: tc -s filter show dev tap${VMID}i0 parent ffff:  (must show pkts_rate)"
  else
    info "  Attach it to a lease with: $0 hookscript --vmid <vmid> --apply"
  fi
  (( APPLY )) || { info ""; info "This was a dry run. Re-run with --apply to install."; }
}

# -------------------------------------------------------------------- handover

# Swaps the key in the VM's cloud-init config to the tenant's. Removing yours
# from authorized_keys alone is not enough: the config would still hold it,
# and cloud-init re-adds it whenever that config is regenerated.
cmd_handover() {
  [[ -n "$VMID" ]] || die "--vmid is required"
  banner
  info "preflight"
  need_host
  qm config "$VMID" >/dev/null 2>&1 || die "vmid $VMID is not a VM on this host"
  qm config "$VMID" | grep -E '^tags: (.*[;,])?tenant([;,].*)?$' >/dev/null \
    || die "vmid $VMID is not tagged tenant; handover is only for leased VMs"
  check_key_file
  key_is_operators && die "--key is trusted by this hypervisor's root, so it is yours, not the tenant's"
  local user who running=0
  user="$(qm config "$VMID" | sed -n 's/^ciuser: //p')"
  who="$(awk '{print $NF; exit}' "$T_KEY")"
  qm status "$VMID" | grep 'status: running' >/dev/null && running=1
  ok "tenant VM $VMID ($(qm config "$VMID" | sed -n 's/^name: //p')), login $user"
  ok "tenant key $T_KEY ($who)"
  info ""

  info "HANDOVER vmid $VMID to $who"
  run qm set "$VMID" --sshkeys "$T_KEY"
  # To cloud-init a changed config is a new instance: on the next boot it adds
  # this key and regenerates the SSH host keys once. Rebooting now means the
  # tenant never meets a changed host key after their first login.
  if (( running )); then run qm reboot "$VMID"; fi
  if (( APPLY )); then ok "handed over"; else ok "would be handed over"; fi
  info ""
  info "next steps"
  info "  1. Once it is back, the tenant connects over their tailnet:"
  info "       ssh ${user}@<its name in their tailnet>"
  info "     and 'tailscale status' in the VM shows their account, not yours."
  info "  2. Then remove your key, leaving only theirs:"
  info "       printf '%s\\n' '$(head -1 "$T_KEY")' > ~/.ssh/authorized_keys"
  info "  3. The firewall's setup path from OPNsense stays, and without your key"
  info "     it leads nowhere."
  (( APPLY )) || { info ""; info "This was a dry run. Re-run with --apply to hand over."; }
}

# ------------------------------------------------------------------------ main

next_steps() {
  local kind=$1 mac=""
  if [[ $kind == tenant ]]; then
    info ""
    info "next steps"
    info "  1. Reach it from OPNsense, the only host it accepts SSH from:"
    info "       ssh -J root@${OPNSENSE_LAN_IP} ${T_USER}@${T_IP}"
    info "  2. In the VM: curl -fsSL https://tailscale.com/install.sh | sh"
    info "       sudo tailscale up --hostname $NAME"
    info "     Send the login URL it prints to the tenant, and have them log in"
    info "     with THEIR account. Never yours: your tailnet routes"
    info "     10.10.0.0/24, and one login would hand them your network."
    info "  3. $0 handover --vmid $VMID --key <their key>.pub"
    info "  4. It is deliberately not in Ansible, Wazuh or Grafana's host list:"
    info "     the tenant is root inside and could read or forge anything there."
    info "  5. To end the lease: qm set $VMID --protection 0 && qm stop $VMID"
    info "     && qm destroy $VMID --purge"
    if (( ! T_UNCAPPED )); then
      info "  6. Once it runs, its packet-rate limit shows as pkts_rate and a dropped"
      info "     count in: tc -s filter show dev tap${VMID}i0 parent ffff:"
    fi
    if (( T_UNCAPPED )); then
      info "  If this is your own VM, not a lease: skip 2 and 3, and join your"
      info "  tailnet with --advertise-tags=tag:work, a tag your ACLs let reach"
      info "  nothing. See 'A work VM of your own' in proxmox/README.md."
    fi
    (( APPLY )) || { info ""; info "This was a dry run. Re-run with --apply to create."; }
    return
  fi
  if (( APPLY )); then
    if [[ $kind == vm ]]; then
      mac="$(qm config "$VMID" | sed -n 's/^net0: virtio=\([^,]*\),.*/\1/p')"
    else
      mac="$(pct config "$VMID" | sed -n 's/^net0: .*hwaddr=\([^,]*\),.*/\1/p')"
    fi
  fi
  info ""
  info "next steps"
  info "  1. DHCP reservation for ${mac:-its MAC} on whatever serves DHCP on $BRIDGE"
  info "     (vmbr0: the home router; vmbr1: OPNsense). Leases move."
  if [[ $kind == vm ]]; then
    info "  2. ssh $CI_USER@<ip>, then: sudo apt-get install -y qemu-guest-agent"
    info "     so Proxmox can report its IP and shut it down cleanly."
  else
    info "  2. ssh root@<ip>"
  fi
  info "  3. Add it to ansible/inventory/hosts.local.yml, then from ansible/:"
  info "     ansible-playbook playbooks/onboard.yml --limit $NAME"
  (( APPLY )) || { info ""; info "This was a dry run. Re-run with --apply to create."; }
}

case "$CMD" in
  images)   need_host; cmd_images ;;
  template) cmd_template ;;
  vm)       cmd_vm ;;
  lxc)      cmd_lxc ;;
  firewall) cmd_firewall ;;
  tenant)   cmd_tenant ;;
  handover) cmd_handover ;;
  hookscript) cmd_hookscript ;;
esac
