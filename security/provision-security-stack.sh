#!/usr/bin/env bash
#
# provision-security-stack.sh
#
# Creates the four security-stack guests on Proxmox VE described in
# docs/. Run this ON the Proxmox host.
#
# DRY RUN BY DEFAULT. Nothing is created until you pass --apply.
#
#   ./provision-security-stack.sh                 # show what would happen
#   ./provision-security-stack.sh --apply         # actually create
#   ./provision-security-stack.sh --only sec-dns  # one guest
#   ./provision-security-stack.sh --apply --only sec-dns,sec-crowdsec
#
# Idempotent: a guest whose VMID already exists is skipped, never modified.
# No secrets are embedded. LXC guests get your SSH public key, no root password.

set -Eeuo pipefail

# ---------------------------------------------------------------- configuration

# Storage IDs as they appear in `pvesm status`, NOT mount paths. A storage is
# only usable here if it advertises the right content type: guest disks need
# `images` (VMs) and `rootdir` (LXC), templates need `vztmpl`. A backup target
# declared as `content backup` passes a name check and then fails at
# `qm create`, halfway through provisioning, so preflight checks content too.
#
# Both tiers default to the same storage because a stock Proxmox install has
# exactly one that accepts guest disks. Point STORAGE_HDD at a second pool only
# once you have confirmed it offers images and rootdir.
STORAGE_SSD="${STORAGE_SSD:-local-lvm}"
STORAGE_HDD="${STORAGE_HDD:-local-lvm}"
STORAGE_TMPL="${STORAGE_TMPL:-local}"

BRIDGE="${BRIDGE:-vmbr0}"
# The firewall's LAN side, where the sandbox workloads live. Only sec-scan goes
# here. A scanner ORIGINATES connections, the inverse of the agent tools, so on
# BRIDGE it has no route into this segment at all. From here it reaches this
# segment directly and BRIDGE through the firewall's outbound NAT. See
# docs/architecture/index.md.
BRIDGE_SANDBOX="${BRIDGE_SANDBOX:-vmbr1}"
SSH_PUBKEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"

# Leave LXC_TEMPLATE empty to resolve the newest build of LXC_TEMPLATE_FAMILY,
# preferring one already in the local cache. Pinning a patch version rots:
# upstream drops old builds from the appliance index and `pveam download` then
# fails on a name that was correct when this file was written.
LXC_TEMPLATE_FAMILY="${LXC_TEMPLATE_FAMILY:-debian-13-standard}"
LXC_TEMPLATE="${LXC_TEMPLATE:-}"
CLOUD_IMAGE_URL="${CLOUD_IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2}"
# The ISO dir, not template/cache: that one is the vztmpl directory and PVE's
# vztmpl lister only globs tar.*, so a qcow2 parked there is invisible to the
# UI and to `pvesm list`.
CLOUD_IMAGE_CACHE="${CLOUD_IMAGE_CACHE:-/var/lib/vz/template/iso}"

# guest spec: name:type:vmid:cores:memMB:diskGB:storage_tier:network
# Sizes come from docs/reference/index.md, at 30 day retention. network is
# "trusted" (BRIDGE) or "sandbox" (BRIDGE_SANDBOX).
GUESTS=(
  "sec-wazuh:vm:200:4:8192:40:ssd:trusted"
  "sec-scan:vm:201:2:6144:40:hdd:sandbox"
  "sec-crowdsec:lxc:210:1:1024:8:ssd:trusted"
  "sec-dns:lxc:211:1:512:8:ssd:trusted"
)
# ---------------------------------------------------------------------- runtime

APPLY=0
ONLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --only)  ONLY="${2:?--only needs a comma separated list}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# An unrecognised name in --only matched no guest, selected() filtered
# everything out, and the script printed a green "RAM fits: 0 MB" and exited 0
# having created nothing. A staged rollout is where a typo is most likely, and
# exit 0 is the wrong answer to one.
if [[ -n "$ONLY" ]]; then
  for _n in ${ONLY//,/ }; do
    printf '%s\n' "${GUESTS[@]}" | cut -d: -f1 | grep -qx "$_n" \
      || { echo "--only: no such guest '$_n'. Known: $(printf '%s\n' "${GUESTS[@]}" | cut -d: -f1 | tr '\n' ' ')" >&2; exit 2; }
  done
fi

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
info() { printf '%s\n' "$*"; }
ok()   { printf '%s  ok%s   %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s warn%s  %s\n' "$c_yel" "$c_off" "$*"; }
die()  { printf '%s fail%s  %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

# run: echo in dry-run, execute under --apply
run() {
  if (( APPLY )); then
    "$@"
  else
    printf '%s       + %s%s\n' "$c_dim" "$(printf '%q ' "$@")" "$c_off"
  fi
}

selected() {
  [[ -z "$ONLY" ]] && return 0
  [[ ",$ONLY," == *",$1,"* ]]
}

# -------------------------------------------------------------------- preflight

# A storage that exists but advertises the wrong content type is the expensive
# failure: `pvesm status` says it is fine, then `qm create` rejects it after
# earlier guests already exist. Verify content up front.
check_storage() {
  local st="$1" want="$2" content c missing=""

  if ! pvesm status --storage "$st" >/dev/null 2>&1; then
    info ""
    info "Available storage:"
    pvesm status | sed 's/^/    /'
    die "storage '$st' not found. Set STORAGE_SSD / STORAGE_HDD / STORAGE_TMPL and rerun."
  fi

  # pvesm status has no --output-format; the content list lives in the API.
  content="$(pvesh get "/storage/$st" --output-format json 2>/dev/null \
             | sed -n 's/.*"content":"\([^"]*\)".*/\1/p')"

  if [[ -z "$content" ]]; then
    warn "storage '$st': could not read content types, continuing unchecked"
    return
  fi

  for c in ${want//,/ }; do
    [[ ",$content," == *",$c,"* ]] || missing="$missing $c"
  done

  if [[ -n "$missing" ]]; then
    die "storage '$st' has content=[$content] but needs:$missing. Pick a different storage, or add the content type under Datacenter > Storage."
  fi

  ok "storage '$st' content=[$content]"
}

preflight() {
  info "preflight"

  [[ $EUID -eq 0 ]] || die "must run as root on the Proxmox host"

  command -v pvesm >/dev/null || die "pvesm not found. Run this ON the Proxmox host, not a guest."
  command -v pct   >/dev/null || die "pct not found"
  command -v qm    >/dev/null || die "qm not found"
  ok "running on a Proxmox host as root"

  local st
  check_storage "$STORAGE_SSD"  images,rootdir
  check_storage "$STORAGE_HDD"  images,rootdir
  check_storage "$STORAGE_TMPL" vztmpl

  # Every bridge a selected guest attaches to, not only BRIDGE: sec-scan sits
  # on a different one from the rest.
  local spec name vmid net br conflicts=0
  declare -A bridges=()
  for spec in "${GUESTS[@]}"; do
    IFS=: read -r name _ _ _ _ _ _ net <<<"$spec"
    selected "$name" || continue
    br="$(bridge_for "$net")"
    bridges["$br"]=1
  done
  for br in "${!bridges[@]}"; do
    if ip link show "$br" >/dev/null 2>&1; then
      ok "bridge '$br' exists"
    else
      die "bridge '$br' not found"
    fi
  done

  if [[ -r "$SSH_PUBKEY" ]]; then
    ok "ssh public key $SSH_PUBKEY"
  else
    die "ssh public key not readable at $SSH_PUBKEY. Set SSH_PUBKEY=/path/to/key.pub"
  fi

  # VMID collisions
  for spec in "${GUESTS[@]}"; do
    IFS=: read -r name _ vmid _ _ _ _ _ <<<"$spec"
    selected "$name" || continue
    if qm status "$vmid" >/dev/null 2>&1 || pct status "$vmid" >/dev/null 2>&1; then
      warn "VMID $vmid already exists ($name will be SKIPPED)"
      conflicts=1
    fi
  done
  (( conflicts )) && warn "existing guests are never modified by this script"

  # Capacity. RAM is a hard gate, not advice: a hypervisor pushed into swap
  # degrades every guest on it, so a stack that "just fits" is a stack that
  # takes the estate down with it. CPU overcommit is normal and only warns.
  # Keyed on the resolved storage ID, not the tier name. Both tiers default to
  # one pool, and keying on the tier asked "does 56 GB fit?" and "does 40 GB
  # fit?" as two separate questions of the same pool, never "does 96 GB fit?".
  local tot_c=0 tot_m=0 cores mem disk tier sid
  declare -A store_disk=()
  for spec in "${GUESTS[@]}"; do
    IFS=: read -r name _ _ cores mem disk tier _ <<<"$spec"
    selected "$name" || continue
    tot_c=$(( tot_c + cores ))
    tot_m=$(( tot_m + mem ))
    sid="$(storage_for "$tier")"
    store_disk["$sid"]=$(( ${store_disk["$sid"]:-0} + disk ))
  done

  local host_cpu avail_mb
  host_cpu="$(nproc)"
  avail_mb="$(free -m | awk '/^Mem:/{print $7}')"
  [[ "$avail_mb" =~ ^[0-9]+$ ]] || die "could not parse available memory from 'free -m'"

  info "requested: ${tot_c} vCPU, ${tot_m} MB RAM"
  info "host now:  ${host_cpu} cores, ${avail_mb} MB RAM available"

  if (( tot_m > avail_mb )); then
    die "not enough RAM: ${tot_m} MB requested, ${avail_mb} MB available. Free memory, shrink GUESTS, or stage the rollout with --only."
  elif (( tot_m * 100 / avail_mb > 70 )); then
    warn "this consumes $(( tot_m * 100 / avail_mb ))% of available RAM. Stage it with --only."
  else
    ok "RAM fits: ${tot_m} MB of ${avail_mb} MB available"
  fi

  if (( tot_c > host_cpu )); then
    warn "${tot_c} vCPU requested on ${host_cpu} cores (overcommit is normal, but it is now on the record)"
  fi

  local avail_gb
  for st in "${!store_disk[@]}"; do
    avail_gb="$(pvesm status --storage "$st" | awk 'NR==2{print int($6/1048576)}')"
    if [[ -z "$avail_gb" ]]; then
      warn "could not read free space for storage '$st'"
    elif (( store_disk["$st"] > avail_gb )); then
      die "storage '$st': ${store_disk[$st]} GB requested, only ${avail_gb} GB free"
    else
      ok "storage '$st': ${store_disk[$st]} GB requested, ${avail_gb} GB free"
    fi
  done

  info ""
}

# ------------------------------------------------------------------- templates

ensure_lxc_template() {
  local cache="/var/lib/vz/template/cache"

  # Prefer a build already on disk: no network, and no silent version bump
  # between a dry run and the apply that follows it.
  if [[ -z "$LXC_TEMPLATE" ]]; then
    local f cached=()
    for f in "$cache/${LXC_TEMPLATE_FAMILY}"_*_amd64.tar.*; do
      if [[ -f "$f" ]]; then cached+=("${f##*/}"); fi
    done
    if (( ${#cached[@]} > 0 )); then
      LXC_TEMPLATE="$(printf '%s\n' "${cached[@]}" | sort -V | tail -1)"
    fi
  fi

  if [[ -n "$LXC_TEMPLATE" && -f "$cache/$LXC_TEMPLATE" ]]; then
    ok "lxc template present: $LXC_TEMPLATE"
    return
  fi

  run pveam update
  local avail
  # grep exits 1 on no match, and `set -o pipefail` would kill the script here
  # with a bare exit 1 instead of the diagnosis below. Contain it.
  avail="$(pveam available --section system 2>/dev/null \
    | awk '{print $2}' \
    | { grep -E "^${LXC_TEMPLATE_FAMILY}_.*_amd64\.tar\.(zst|gz|xz)$" || true; } \
    | sort -V | tail -1)"

  [[ -n "$avail" ]] || die "no '${LXC_TEMPLATE_FAMILY}' template in the appliance index. Set LXC_TEMPLATE to an exact filename from 'pveam available'."

  LXC_TEMPLATE="$avail"
  warn "lxc template not cached, will download: $LXC_TEMPLATE"
  run pveam download "$STORAGE_TMPL" "$LXC_TEMPLATE"
}

# Sets CLOUD_IMAGE_PATH rather than echoing it: this function logs, and a
# command substitution would capture those log lines into the path.
CLOUD_IMAGE_PATH=""
ensure_cloud_image() {
  local img
  img="$CLOUD_IMAGE_CACHE/$(basename "$CLOUD_IMAGE_URL")"
  if [[ -f "$img" ]]; then
    ok "cloud image present: $(basename "$img")"
  else
    warn "cloud image missing, will download"
    run mkdir -p "$CLOUD_IMAGE_CACHE"
    # Download to .part, then rename. Writing straight to $img means an
    # interrupted 339 MB transfer leaves a truncated file that the -f test
    # above accepts forever after, so the NEXT run says "cloud image present"
    # and then dies inside qm with "Image is not in qcow2 format".
    run wget -q -O "${img}.part" "$CLOUD_IMAGE_URL"
    run mv -f "${img}.part" "$img"
  fi
  CLOUD_IMAGE_PATH="$img"
}

storage_for() {
  case "$1" in
    ssd) printf '%s' "$STORAGE_SSD" ;;
    hdd) printf '%s' "$STORAGE_HDD" ;;
    *) die "unknown storage tier: $1" ;;
  esac
}

bridge_for() {
  case "$1" in
    trusted) printf '%s' "$BRIDGE" ;;
    sandbox) printf '%s' "$BRIDGE_SANDBOX" ;;
    *) die "unknown network: $1" ;;
  esac
}

# ---------------------------------------------------------------------- create

create_lxc() {
  local name=$1 vmid=$2 cores=$3 mem=$4 disk=$5 tier=$6 net=$7
  local store; store=$(storage_for "$tier")
  local bridge; bridge=$(bridge_for "$net")

  info "LXC  $name (vmid $vmid, ${cores} vCPU, ${mem} MB, ${disk} GB on $store, $bridge)"

  # A build is several commands, not a transaction. If one fails, `set -e`
  # aborts with the VMID already created, and the next run's "already exists"
  # check then SKIPS it and reports success over a half-built guest, forever.
  # The header promises "never modified", which is what makes that dangerous.
  #
  # nesting=1 is what the Proxmox UI sets on every unprivileged container, and
  # pct create does not. Without it the Debian 13 template's systemd 257 cannot
  # mount /tmp, /run/lock or the mqueue filesystem, so the guest boots
  # "degraded" and trips the fleet's Systemd Unit Failed alert once monitored.
  # sec-dns came up exactly like that on 2026-09-15.
  run pct create "$vmid" "${STORAGE_TMPL}:vztmpl/${LXC_TEMPLATE}" \
      --hostname "$name" \
      --cores "$cores" \
      --memory "$mem" \
      --swap 512 \
      --rootfs "${store}:${disk}" \
      --net0 "name=eth0,bridge=${bridge},firewall=1,ip=dhcp" \
      --ssh-public-keys "$SSH_PUBKEY" \
      --unprivileged 1 \
      --features nesting=1 \
      --onboot 1 \
      --description "security stack: $name. Managed by provision-security-stack.sh"

  # Arm cleanup only after create succeeds: a concurrent VMID collision must
  # never cause us to destroy a guest created by somebody else.
  if (( APPLY )); then
    trap 'trap - ERR; warn "$name failed mid-build, destroying the partial guest"; pct destroy "$vmid" --purge || warn "cleanup failed for VMID $vmid; inspect it manually"' ERR
  fi
  run pct start "$vmid"
  trap - ERR
  ok "$name created"
}

create_vm() {
  local name=$1 vmid=$2 cores=$3 mem=$4 disk=$5 tier=$6 net=$7
  local store; store=$(storage_for "$tier")
  local bridge; bridge=$(bridge_for "$net")
  local img="$CLOUD_IMAGE_PATH"

  info "VM   $name (vmid $vmid, ${cores} vCPU, ${mem} MB, ${disk} GB on $store, $bridge)"

  # A build is several commands, not a transaction. If one fails, `set -e`
  # aborts with the VMID already created, and the next run's "already exists"
  # check then SKIPS it and reports success over a half-built guest, forever.
  # The header promises "never modified", which is what makes that dangerous.
  run qm create "$vmid" \
      --name "$name" \
      --cores "$cores" \
      --memory "$mem" \
      --net0 "virtio,bridge=${bridge},firewall=1" \
      --scsihw virtio-scsi-single \
      --ostype l26 \
      --agent enabled=1 \
      --onboot 1 \
      --description "security stack: $name. Managed by provision-security-stack.sh"

  if (( APPLY )); then
    trap 'trap - ERR; warn "$name failed mid-build, destroying the partial guest"; qm destroy "$vmid" --purge || warn "cleanup failed for VMID $vmid; inspect it manually"' ERR
  fi
  # Import straight into scsi0. The old path was `qm importdisk` followed by
  # `qm set --scsi0 ${store}:vm-${vmid}-disk-0`, which GUESSES the volume name,
  # because qm disk import prints the volid but returns nothing scriptable.
  # The guess holds on LVM-thin and ZFS and is WRONG on a directory storage,
  # which allocates ${vmid}/vm-${vmid}-disk-0.qcow2. Point STORAGE_HDD at a dir
  # pool and this failed with "volume does not exist" AFTER qm create had
  # already made the guest. `qm importdisk` is also a deprecated PVE 9 alias.
  run qm set "$vmid" --scsi0 "${store}:0,import-from=${img},discard=on,ssd=1"
  run qm disk resize "$vmid" scsi0 "${disk}G"

  # cloud-init: ssh key only, no password, DHCP
  run qm set "$vmid" --ide2 "${store}:cloudinit"
  run qm set "$vmid" --boot "order=scsi0"
  run qm set "$vmid" --serial0 socket --vga serial0
  run qm set "$vmid" --ciuser admin --sshkeys "$SSH_PUBKEY" --ipconfig0 ip=dhcp

  run qm start "$vmid"
  trap - ERR
  ok "$name created"
}

# ------------------------------------------------------------------------ main

main() {
  if (( APPLY )); then
    info "MODE: ${c_red}APPLY${c_off}, guests will be created"
  else
    info "MODE: ${c_grn}DRY RUN${c_off}, nothing will be created. Add --apply to execute."
  fi
  [[ -n "$ONLY" ]] && info "restricted to: $ONLY"
  info ""

  preflight

  local need_lxc=0 need_vm=0 spec name type vmid cores mem disk tier net
  for spec in "${GUESTS[@]}"; do
    IFS=: read -r name type _ _ _ _ _ _ <<<"$spec"
    selected "$name" || continue
    [[ $type == lxc ]] && need_lxc=1
    [[ $type == vm  ]] && need_vm=1
  done
  (( need_lxc )) && ensure_lxc_template
  (( need_vm ))  && ensure_cloud_image
  info ""

  for spec in "${GUESTS[@]}"; do
    IFS=: read -r name type vmid cores mem disk tier net <<<"$spec"
    selected "$name" || continue

    if qm status "$vmid" >/dev/null 2>&1 || pct status "$vmid" >/dev/null 2>&1; then
      warn "skip $name, vmid $vmid already exists"
      continue
    fi

    case "$type" in
      lxc) create_lxc "$name" "$vmid" "$cores" "$mem" "$disk" "$tier" "$net" ;;
      vm)  create_vm  "$name" "$vmid" "$cores" "$mem" "$disk" "$tier" "$net" ;;
      *)   die "unknown guest type: $type" ;;
    esac
    info ""
  done

  info "next steps"
  info "  1. Give every guest a DHCP reservation. Leases move; see"
  info "     docs/operations/index.md. A guest on ${BRIDGE_SANDBOX} gets its lease"
  info "     from whatever serves DHCP there, usually the firewall, not your router."
  info "  2. Run install/install-<service>.sh inside each guest. Guests on"
  info "     ${BRIDGE_SANDBOX} sit behind the firewall; reach them through your jump host."
  info "  3. Add the firewall allow rules from docs/reference/index.md ABOVE any"
  info "     inter-VLAN block rules, in the same change."
  info "  4. Add the guests to Ansible's monitored platform groups and security_guests."
  info "     Follow docs/getting-started/index.md#host-monitoring-in-grafana to"
  info "     onboard Alloy, verify telemetry and load the Servers dashboards."
  (( APPLY )) || info ""
  (( APPLY )) || info "This was a dry run. Re-run with --apply to create."
}

main "$@"
