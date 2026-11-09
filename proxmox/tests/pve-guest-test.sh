#!/usr/bin/env bash
#
# pve-guest-test.sh - run pve-guest.sh in a sandbox, off a Proxmox host.
#
# The script only runs on a hypervisor, as root, against /etc/pve. This runs a
# patched copy of it with qm, pct, pvesm, pvesh, ip, free, iptables and
# pve-firewall replaced by shims first on PATH, its firewall directory, CPU
# flags and key file pointed into a temporary directory, and a stand-in for
# PVE's Perl firewall parser (tests/fake-pve) so its own fw_check really runs.
# No hypervisor, firewall or network is contacted, and nothing outside the
# temporary directory is written. Most runs are dry runs; the --apply runs
# write only into the sandbox and call the shims, which log every command.
#
# What it asserts:
#   - the generated cluster.fw: both aliases, both groups, every DROP rule
#     logged, the SMTP rule at warning, and `firewall` still refusing to merge
#     into a cluster.fw it did not write
#   - a lease (the default): bridge vmbr5, an address in 10.10.90.0/24,
#     group lease, CPU x86-64-v2-AES, tags tenant;lease, and an address from
#     the work segment refused
#   - --uncapped: bridge vmbr2, 10.10.60.0/24, group tenant, x86-64-v3, tags
#     tenant;uncapped
#   - an address in use on either segment is refused
#   - the CPU rule: a lease takes only a model on the allowlist (the ones that
#     hide AVX), and max, host and every other name is refused; a work VM keeps
#     the old rules
#   - the per-VM firewall file: log level, ipfilter, macfilter, dhcp, radv and
#     the right GROUP line, and fw_check refusing the wrong group or a missing
#     one
#   - preflight: a missing bridge, group or lease_gw alias
#   - the lease packet-rate limit: the hookscript (compiles, is written only
#     when it differs, runs the right tc commands, passes conforming packets on
#     to PVE's byte-rate filter with drop/continue, and never exits non-zero),
#     the snippets storage step in the dry run and under --apply, and nothing
#     of it for --uncapped
#   - host.fw: the conntrack options after nf_conntrack_allow_invalid; a missing
#     one written; an older script-written one brought up to date, with its
#     diff shown; one whose header PVE dropped but whose rules are the script's
#     treated as the script's own; a really different one never touched
#   - cluster.fw: a complete one is compared with what the script writes (PVE
#     re-sorting and the datacenter switch ignored) and never rewritten, a
#     difference shown as a diff
#   - cluster.fw: [IPSET private] holds the IPv6 link-local and unique-local
#     ranges too
#   - --ip: only plain dotted quads, no leading zeros; --allow: plain ports
#   - the hookscript subcommand: dry run changes nothing, --apply installs the
#     file and the snippets setting, --vmid attaches it to a lease and refuses
#     any other VM
#   - a work VM is described as a work VM, a lease as a lease
#   - handover on both segments
#
#   ./tests/pve-guest-test.sh
#
# Needs bash, perl and coreutils. PVE_GUEST_SCRIPT=<file> tests another copy of
# the script, which is how the checks were tried against deliberate breakage.
#
# shellcheck disable=SC2016  # single quotes are deliberate: literal $ in shims
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0
ok() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

SRC="$(readlink -f "${PVE_GUEST_SCRIPT:-pve-guest.sh}")"
[[ -f "$SRC" ]] || { echo "no such script: $SRC" >&2; exit 1; }
FAKE_PERL="$PWD/tests/fake-pve"

# The script reads its configuration from the environment: a caller's own
# settings must not leak into the sandbox.
while read -r v; do unset "$v"; done < <(grep -oP '^[A-Z][A-Z0-9_]*(?==")' "$SRC")

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
BIN="$T/bin"; FW="$T/pve/firewall"; STATE="$T/state"; LOG="$T/fake.log"
mkdir -p "$BIN" "$FW" "$STATE" "$T/pve/nodes/$(hostname)" "$T/home"
: > "$LOG"

# --- the script under test, with its host paths moved into the sandbox ------
text="$(cat "$SRC"; echo x)"; text="${text%x}"
swap() { # swap <from> <to>: fail loudly when the script no longer contains it
  if [[ "$text" != *"$1"* ]]; then
    echo "sandbox setup: '$1' is not in $SRC any more; update the patches in this test" >&2
    exit 2
  fi
  text="${text//"$1"/"$2"}"
}
swap '[[ $EUID -eq 0 ]] || die "must run as root on the Proxmox host"' 'true'
swap 'FW_DIR=/etc/pve/firewall' "FW_DIR=$FW"
swap '/proc/cpuinfo' "$T/cpuinfo"
swap '/etc/pve/nodes/%s/host.fw' "$T/pve/nodes/%s/host.fw"
swap '/root/.ssh/authorized_keys' "$T/authorized_keys"
swap '/var/lib/vz/snippets/lease-limits.pl' "$T/vz/snippets/lease-limits.pl"
printf '%s' "$text" > "$T/pve-guest.sh"
# A second copy whose write_file silently skips host.fw, to see that a write
# that did not happen is noticed afterwards.
nowrite="${text/printf \'%s\' \"\$2\" > \"\$1\"/[[ \"\$1\" == */host.fw ]] || printf \'%s\' \"\$2\" > \"\$1\"}"
[[ "$nowrite" != "$text" ]] || { echo "sandbox setup: write_file no longer has its printf line; update the nowrite patch" >&2; exit 2; }
printf '%s' "$nowrite" > "$T/pve-guest-nowrite.sh"
GUEST="$T/pve-guest.sh"

echo 'flags : fpu sse sse4_2 avx avx2 avx512f aes' > "$T/cpuinfo"

# --- shims ------------------------------------------------------------------
fake() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$BIN/$1"; chmod +x "$BIN/$1"; }

# qm: the template is 9000. FAKE_GUEST is an existing VM (for handover), with
# FAKE_TAGS. A clone creates a state file so that `qm config` finds the new VM.
fake qm '
echo "qm $*" >> "$FAKE_LOG"
case "$1" in
  config)
    if [[ "$2" == 9000 ]]; then
      printf "%s\n" "name: tmpl-debian-13" "template: 1" "scsi0: local-lvm:base-9000-disk-0,size=3G"
    elif [[ "$2" == "${FAKE_GUEST:-none}" ]]; then
      printf "%s\n" "name: alice-vm" "ciuser: alice" "tags: ${FAKE_TAGS-tenant;lease}"
      [[ -z "${FAKE_HOOKSCRIPT:-}" ]] || echo "hookscript: $FAKE_HOOKSCRIPT"
    elif [[ -e "$FAKE_STATE/vm-$2" ]]; then
      printf "%s\n" "name: new" "scsi0: local-lvm:vm-$2-disk-0,size=3G"
    else exit 2; fi ;;
  status) [[ "$2" == "${FAKE_GUEST:-none}" ]] || exit 2; echo "status: ${FAKE_RUNSTATE:-stopped}" ;;
  list)   printf "%s\n" "VMID NAME STATUS" "9000 tmpl-debian-13 stopped" "100 sec-wazuh running" ;;
  set)    [[ "$*" != *--hookscript* ]] || echo "hookscript-set-saw-file: $([[ -x "$FAKE_VZ/snippets/lease-limits.pl" ]] && echo yes || echo no)" >> "$FAKE_LOG" ;;
  clone)  echo "clone-saw-hookscript: $([[ -x "$FAKE_VZ/snippets/lease-limits.pl" ]] && echo yes || echo no)" >> "$FAKE_LOG"
          touch "$FAKE_STATE/vm-$3" ;;
esac
exit 0'
fake pct 'case "$1" in status) exit 2 ;; list) echo "VMID Status Lock Name" ;; esac; exit 0'
# pvesm: FAKE_SNIPPETS=1 (the default) means `local` already offers snippets;
# `pvesm set local --content` turns it on for the rest of the run.
fake pvesm '
echo "pvesm $*" >> "$FAKE_LOG"
case "$1 $2 $3" in
  "status --storage ${FAKE_STORAGE:-local-lvm}")
    printf "%s\n" "Name Type Status Total Used Available %" "$3 lvmthin active 419430400 20971520 209715200 5.00%" ;;
  "status --content snippets")
    printf "%s\n" "Name Type Status Total Used Available %"
    if [[ "${FAKE_SNIPPETS:-1}" == 1 || -e "$FAKE_STATE/snippets" ]]; then echo "local dir ${FAKE_SNIPPETS_STATUS:-active} 98497780 2000000 91500000 2.03%"; fi ;;
  "set local --content")
    echo "pvesm-set: hookscript present: $([[ -e "$FAKE_VZ/snippets/lease-limits.pl" ]] && echo yes || echo no)" >> "$FAKE_LOG"
    touch "$FAKE_STATE/snippets" ;;
  *) exit 1 ;;
esac'
fake pvesh '
echo "pvesh $*" >> "$FAKE_LOG"
case "$*" in
  "get /cluster/nextid") echo 113 ;;
  "get /storage/local-lvm --output-format json") echo "{\"content\":\"images,rootdir\"}" ;;
  "get /storage/local --output-format json")
    [[ -z "${FAKE_NO_LOCAL:-}" ]] || exit 2
    echo "{\"content\":\"${FAKE_LOCAL_CONTENT-vztmpl,iso,backup,import}\",\"path\":\"${FAKE_LOCAL_PATH-$FAKE_VZ}\",\"type\":\"dir\"}" ;;
  "get /cluster/firewall/options --output-format json") echo "{\"enable\":${FAKE_FW_ENABLE:-1}}" ;;
  *) exit 1 ;;
esac'
fake ip '
case "$*" in
  "link show "*) for b in ${FAKE_BRIDGES:-vmbr0 vmbr1 vmbr2 vmbr5}; do [[ "$3" == "$b" ]] && exit 0; done; exit 1 ;;
  "-br link")    for b in ${FAKE_BRIDGES:-vmbr0 vmbr1 vmbr2 vmbr5}; do echo "$b UP"; done ;;
esac
exit 0'
fake free 'echo "Mem: 32000 2000 8000 100 6000 24000"'
fake iptables 'printf "%s\n" "-P INPUT ACCEPT" "-A INPUT -j ts-input"'
fake pve-firewall 'echo "local IP address: 10.10.0.109"'
# tc: logs, shows FAKE_TC_QDISC for `qdisc show`, and fails `filter add` with FAKE_TC_ADD_RC.
fake tc '
echo "tc $*" >> "$FAKE_LOG"
case "$1 $2" in
  "qdisc show") echo "${FAKE_TC_QDISC:-}" ;;
  "filter add") exit "${FAKE_TC_ADD_RC:-0}" ;;
esac
exit 0'

# --- keys -------------------------------------------------------------------
KEYS="$T/keys"; mkdir -p "$KEYS"
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIsetupkeysetupkeysetupkeysetupkey0001 owner@laptop' > "$KEYS/setup.pub"
echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItenantkeytenantkeytenantkeytenantk002 alice@laptop' > "$KEYS/alice.pub"
cp "$KEYS/setup.pub" "$T/authorized_keys"

# --- helpers ----------------------------------------------------------------
N=0; OUT=""; RC=0
VZ="$T/vz"; HOOK="$VZ/snippets/lease-limits.pl"
# run_guest <args...>: output in $OUT with colours and backslashes removed,
# exit code in $RC. Variables set in front of the call (FAKE_*, LEASE_*) reach
# the script.
run_guest() {
  OUT="$T/out.$((++N))"
  FAKE_LOG="$LOG" FAKE_STATE="$STATE" FAKE_VZ="$VZ" PATH="$BIN:$PATH" PERL5LIB="$FAKE_PERL" \
    SSH_CLIENT="100.64.1.2 5555 22" HOME="$T/home" \
    bash "$GUEST" "$@" > "$OUT.raw" 2>&1
  RC=$?
  sed -e 's/\x1b\[[0-9;]*m//g' -e 's/\\//g' "$OUT.raw" > "$OUT"
}
# fresh [cluster.fw]: empty the sandbox, then install a cluster.fw if given.
fresh() {
  rm -rf "${FW:?}"/* "${STATE:?}"/*; : > "$LOG"; rm -f "$T/pve/nodes/$(hostname)"/host.fw
  [[ -n "${KEEP_VZ:-}" ]] || rm -rf "${VZ:?}"
  [[ $# -eq 0 ]] || cp "$1" "$FW/cluster.fw"
}
has()    { grep -qF -e "$1" "$OUT"; }
nothas() { ! has "$1"; }
# Several strings at once, with the exit code: rc0_has <str...>, rcnz_has <str...>.
has_all()    { local x; for x in "$@"; do has "$x" || return 1; done; }
nothas_all() { local x; for x in "$@"; do nothas "$x" || return 1; done; }
rc0_has()    { test "$RC" -eq 0 && has_all "$@"; }
rcnz_has()   { test "$RC" -ne 0 && has_all "$@"; }
# Lines of the output (in_order) or of the fake command log (log_order), the
# first match of each pattern, must come in the order given.
in_order()  { local prev=0 n x; for x in "$@"; do n="$(grep -nF -e "$x" "$OUT" | head -1 | cut -d: -f1)"; [[ -n "$n" && "$n" -gt "$prev" ]] || return 1; prev="$n"; done; }
log_order() { local prev=0 n x; for x in "$@"; do n="$(grep -nE -e "$x" "$LOG" | head -1 | cut -d: -f1)"; [[ -n "$n" && "$n" -gt "$prev" ]] || return 1; prev="$n"; done; }
logged_all() { local x; for x in "$@"; do logged "$x" || return 1; done; }
hasre()  { grep -qE -e "$1" "$OUT"; }
fhas()   { grep -qxF -e "$2" "$1"; }          # fhas <file> <exact line>
logged() { grep -qE -e "$1" "$LOG"; }
check() { # check <description> <command...>
  local desc="$1"; shift
  if "$@"; then ok "$desc"; else no "$desc"; [[ ! -f "$OUT" ]] || sed 's/^/        | /' "$OUT" | head -30; fi
}
# The rules of one group of a cluster.fw, comments and blank lines removed.
group_rules() {
  awk -v g="$2" '/^\[/ { in_g = ($0 ~ "^\\[group " g "\\]"); next } in_g && NF && $0 !~ /^#/ { sub(/ *#.*/, ""); print }' "$1"
}
alias_lines() {
  awk '/^\[/ { in_a = ($0 ~ /^\[ALIASES\]/); next } in_a && NF && $0 !~ /^#/ { sub(/ *#.*/, ""); print }' "$1"
}
# The entries of one [IPSET name] section, comments removed.
ipset_entries() {
  awk -v n="$2" '/^\[/ { in_s = ($0 ~ "^\\[IPSET " n "\\]"); next } in_s && NF && $0 !~ /^#/ { sub(/ *#.*/, ""); print }' "$1"
}
# Lines of dry-run output that are the preview of a written file.
GROUP_RE='^ +GROUP '

TENANT_ARGS=(tenant --name alice-vm --user alice --key "$KEYS/setup.pub" --cores 2 --memory 2048 --disk 20)

# ============================================================================
echo "== firewall: the generated cluster.fw =="
fresh
run_guest firewall --apply
check "firewall --apply succeeds in the sandbox" test "$RC" -eq 0
check "it re-checks what it wrote" has "written and re-checked"
check "and says it wrote host.fw, which did not exist" has "wrote host.fw"
GEN="$T/cluster.generated"; cp "$FW/cluster.fw" "$GEN"
HOSTGEN="$T/host.generated"; cp "$T/pve/nodes/$(hostname)/host.fw" "$HOSTGEN"
check "[ALIASES] holds tenant_gw and lease_gw, and nothing else" \
  test "$(alias_lines "$GEN")" = "$(printf '%s\n' 'tenant_gw 10.10.60.1' 'lease_gw 10.10.90.1')"
check "group tenant has the five rules" \
  test "$(group_rules "$GEN" tenant)" = "$(printf '%s\n' \
    'IN ACCEPT -source tenant_gw -p tcp -dport 22' \
    'IN DROP -source +private -log info' \
    'OUT ACCEPT -dest tenant_gw' \
    'OUT DROP -p tcp -dport 25 -log warning' \
    'OUT DROP -dest +private -log info')"
check "group lease has the five rules" \
  test "$(group_rules "$GEN" lease)" = "$(printf '%s\n' \
    'IN ACCEPT -source lease_gw -p tcp -dport 22' \
    'IN DROP -source +private -log info' \
    'OUT ACCEPT -dest lease_gw' \
    'OUT DROP -p tcp -dport 25 -log warning' \
    'OUT DROP -dest +private -log info')"
for g in tenant lease; do
  drops="$(group_rules "$GEN" "$g" | grep -c ' DROP ' || true)"
  unlogged="$(group_rules "$GEN" "$g" | grep ' DROP ' | grep -cvE -- ' -log (emerg|alert|crit|err|warning|notice|info|debug)$' || true)"
  check "group $g: all $drops DROP rules carry a -log level" test "$drops" -ge 3 -a "$unlogged" -eq 0
  check "group $g: the SMTP rule logs at warning" \
    test "$(group_rules "$GEN" "$g" | grep -c -- '^OUT DROP -p tcp -dport 25 -log warning$' || true)" -eq 1
done
check "the management and private ipsets are still there" \
  test "$(grep -cE '^\[(IPSET management|IPSET private|OPTIONS)\]' "$GEN")" -eq 3
check "the datacenter firewall stays off in what it writes" fhas "$GEN" 'enable: 0'
check "[IPSET private] holds the IPv4 ranges, then the IPv6 link-local and unique-local ranges" \
  test "$(ipset_entries "$GEN" private)" = "$(printf '%s\n' 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 fe80::/10 fc00::/7)"
check "the link-local line is exact, comment included" fhas "$GEN" 'fe80::/10 # IPv6 link-local: another guest on the same bridge'
check "the unique-local line is exact, comment included" fhas "$GEN" "fc00::/7 # IPv6 unique local, which includes the tailnet's fd7a::/48"
check "[IPSET management] is unchanged" \
  test "$(ipset_entries "$GEN" management)" = "$(printf '%s\n' 10.10.0.0/24 '!10.10.0.114' 100.64.0.0/10 fd7a:115c:a1e0::/48)"
check "both groups drop +private in both directions" \
  test "$(grep -c -E '^(IN DROP -source|OUT DROP -dest) \+private -log info' "$GEN")" -eq 4
check "host.fw: the conntrack options follow nf_conntrack_allow_invalid, exactly" \
  test "$(awk '/^nf_conntrack_allow_invalid: 1$/ { f = 1 } f { print; if (++n == 6) exit }' "$HOSTGEN")" = "$(printf '%s\n' \
    'nf_conntrack_allow_invalid: 1' \
    '# A tenant can open flows faster than the default table (262144) holds them:' \
    '# a larger table and a shorter half-open timeout buy time while the per-guest' \
    "# alerts fire. The firewall's per-source limits (OPNsense) apply after this table." \
    'nf_conntrack_max: 1048576' \
    'nf_conntrack_tcp_timeout_syn_recv: 30')"
check "host.fw: they are in [OPTIONS], before [RULES]" \
  test "$(awk '/^\[/ { sec = $0 } /^nf_conntrack_(max|tcp_timeout_syn_recv):/ { print sec }' "$HOSTGEN" | sort -u)" = "[OPTIONS]"

echo
echo "== firewall: a cluster.fw it did not write is never merged into =="
fresh; printf '%s\n' '[OPTIONS]' 'enable: 1' > "$FW/cluster.fw"; cp "$FW/cluster.fw" "$T/hand.fw"
run_guest firewall --apply
check "a hand-written cluster.fw is refused" test "$RC" -ne 0
check "the refusal says it is not merging" has "not merging into it"
check "the refusal prints what to merge, with both groups" test "$(grep -c '\[group ' "$OUT")" -ge 2
check "the hand-written file is untouched" cmp -s "$FW/cluster.fw" "$T/hand.fw"
run_guest firewall
check "a dry run refuses it too" test "$RC" -ne 0

fresh; sed '/^\[group lease\]/,$d' "$GEN" > "$FW/cluster.fw"; cp "$FW/cluster.fw" "$T/old.fw"
run_guest firewall --apply
check "a cluster.fw with only the tenant group is refused, not extended" test "$RC" -ne 0
check "the refusal names the missing lease group" has "lacks the firewall group(s): lease"
check "the old file is untouched" cmp -s "$FW/cluster.fw" "$T/old.fw"

fresh "$GEN"; run_guest firewall --apply
check "a complete cluster.fw is reported as current" has "cluster.fw is current"
check "and left as it was" cmp -s "$FW/cluster.fw" "$GEN"

echo
echo "== firewall: a complete cluster.fw is compared, shown a diff, never rewritten =="
HOSTDIR="$T/pve/nodes/$(hostname)"
# cluster_try <label> <file>: install <file> as cluster.fw with a current host.fw, run firewall --apply.
cluster_try() { fresh "$2"; cp "$HOSTGEN" "$HOSTDIR/host.fw"; cp "$FW/cluster.fw" "$T/cluster.before"; run_guest firewall --apply; }
cluster_try current "$GEN"
check "a cluster.fw equal to the script's is current" rc0_has "cluster.fw is current"
check "no diff is printed for it" nothas_all "differs from what this script writes" "not rewritten"
sed 's/^enable: 0$/enable: 1/' "$GEN" > "$T/cluster.on"
cluster_try on "$T/cluster.on"
check "the datacenter switch turned on is not a difference" rc0_has "cluster.fw is current"
check "and the file is untouched" cmp -s "$FW/cluster.fw" "$T/cluster.before"
sed -e '/^#/d' -e 's/ *#.*$//' "$GEN" | awk '/^tenant_gw /{ a = $0; next } /^lease_gw /{ print; print a; next }
  /^policy_in:/{ pi = $0; next } /^policy_out:/{ print; print pi; next }
  /^10.0.0.0\/8$/{ x = $0; next } /^172.16.0.0\/12$/{ print; print x; next } { print }' > "$T/cluster.resorted"
check "the re-sorted fixture is not byte-equal to the original" test "$(cat "$T/cluster.resorted")" != "$(sed -e '/^#/d' -e 's/ *#.*$//' "$GEN")"
cluster_try resorted "$T/cluster.resorted"
check "comments gone and aliases, options and ipset entries re-ordered (what PVE does) is still current" rc0_has "cluster.fw is current"
check "and untouched" cmp -s "$FW/cluster.fw" "$T/cluster.before"

# An older install: both groups, but without the IPv6 ranges added later.
grep -v -e '^fe80::/10' -e '^fc00::/7' "$GEN" > "$T/cluster.v4only"
check "the older fixture lacks both IPv6 lines and keeps both groups" \
  test -z "$(grep -E '^(fe80|fc00)' "$T/cluster.v4only")" -a "$(grep -c '^\[group ' "$T/cluster.v4only")" -eq 2
cluster_try v4only "$T/cluster.v4only"
check "it is reported as different, with a diff" rc0_has "differs from what this script writes now" "+[IPSET private] fc00::/7" "+[IPSET private] fe80::/10"
check "each diff line names its section, so it says where to merge it" has "+[IPSET private] fe80::/10"
check "and no comment text is in the diff" nothas "IPv6 link-local: another guest"
check "it says it is not rewritten, and how to merge" has_all "is not rewritten, because it may hold your edits" "Merge the + lines by hand"
check "it is not called current" nothas "cluster.fw is current"
check "and it is really not rewritten" cmp -s "$FW/cluster.fw" "$T/cluster.before"
check "--apply still re-checks the files" has "re-checked, nothing to write"
fresh "$T/cluster.v4only"; cp "$HOSTGEN" "$HOSTDIR/host.fw"; run_guest firewall
check "a dry run shows the same diff" rc0_has "+[IPSET private] fc00::/7" "+[IPSET private] fe80::/10"

sed 's/^IN ACCEPT -source lease_gw -p tcp -dport 22/IN ACCEPT -p tcp -dport 22/' "$GEN" > "$T/cluster.edited"
cluster_try edited "$T/cluster.edited"
check "an operator's edit to a group rule is shown as a difference" rc0_has "-[group lease] IN ACCEPT -p tcp -dport 22" "+[group lease] IN ACCEPT -source lease_gw -p tcp -dport 22"
check "and the edit stays" cmp -s "$FW/cluster.fw" "$T/cluster.before"
awk '/^IN ACCEPT -source lease_gw/ { a = $0; next } /^IN DROP -source \+private -log info # the LAN, the sandbox, work VMs/ { print; print a; next } { print }' "$GEN" > "$T/cluster.order"
check "the reordered-rules fixture differs from the original" test "$(cat "$T/cluster.order")" != "$(cat "$GEN")"
cluster_try order "$T/cluster.order"
check "rules in another order are a difference, since the first match wins" rc0_has "differs from what this script writes now"
sed 's/# setup path, from OPNsense/# the way in for setup/' "$GEN" > "$T/cluster.comments"
cluster_try comments "$T/cluster.comments"
check "a changed comment alone is not a difference" rc0_has "cluster.fw is current"

# ============================================================================
echo
echo "== firewall: host.fw =="
HOSTFW="$T/pve/nodes/$(hostname)/host.fw"
# What an earlier version wrote: the current file without the conntrack options.
grep -v -e '^nf_conntrack_max' -e '^nf_conntrack_tcp_timeout_syn_recv' -e '^# A tenant can open' -e '^# a larger table' -e "^# alerts fire" "$HOSTGEN" > "$T/host.old"
check "the fixture really lacks the new options and still has the old one" \
  test -z "$(grep -E 'nf_conntrack_max|syn_recv' "$T/host.old")" -a -n "$(grep '^nf_conntrack_allow_invalid: 1' "$T/host.old")"
fresh "$GEN"; cp "$T/host.old" "$HOSTFW"
run_guest firewall
check "dry run: an older script-written host.fw is reported as one that would be updated" rc0_has "update host.fw" "host.fw would be updated"
check "the dry run shows the options it would add" has_all "nf_conntrack_max: 1048576" "nf_conntrack_tcp_timeout_syn_recv: 30"
check "and changes nothing" cmp -s "$HOSTFW" "$T/host.old"
run_guest firewall --apply
check "--apply reports 'updated host.fw'" rc0_has "updated host.fw"
check "host.fw is now exactly what the generator produces" cmp -s "$HOSTFW" "$HOSTGEN"
check "cluster.fw is untouched" cmp -s "$FW/cluster.fw" "$GEN"
check "it was re-checked" has "written and re-checked"
run_guest firewall --apply
check "a second --apply finds host.fw current" rc0_has "host.fw is current"
check "and does not report an update" nothas "updated host.fw"
{ cat "$HOSTGEN"; echo 'IN ACCEPT -p tcp -dport 9999'; } > "$HOSTFW"
run_guest firewall --apply
check "a host.fw with the header but hand-edited content is script-managed: rewritten" rc0_has "updated host.fw"
check "and its edit is gone" cmp -s "$HOSTFW" "$HOSTGEN"

fresh "$GEN"; printf '%s\n' '[OPTIONS]' 'nf_conntrack_max: 262144' '' '[RULES]' 'IN ACCEPT -p tcp -dport 22' > "$HOSTFW"; cp "$HOSTFW" "$T/host.hand"
run_guest firewall --apply
check "a hand-written host.fw (no header) is refused" rcnz_has "was not written by this script; not merging into it"
check "what to merge is printed" has_all "Merge this into" "nf_conntrack_max: 1048576"
check "it is not reported as updated" nothas "updated host.fw"
check "and is untouched" cmp -s "$HOSTFW" "$T/host.hand"
run_guest firewall
check "a dry run refuses it as well" rcnz_has "not merging into it"

echo "-- a host.fw that is missing"
HOSTDIR="$(dirname "$HOSTFW")"
fresh "$GEN"
run_guest firewall
check "dry run, cluster.fw complete and host.fw missing: host.fw would be written" rc0_has "host.fw would be written" "write host.fw"
check "the dry run shows what it would write" has "nf_conntrack_max: 1048576"
check "and writes nothing" test ! -e "$HOSTFW"
run_guest firewall --apply
check "--apply writes it and says 'wrote host.fw'" rc0_has "wrote host.fw" "written and re-checked"
check "it is exactly what the generator produces" cmp -s "$HOSTFW" "$HOSTGEN"
check "cluster.fw is untouched" cmp -s "$FW/cluster.fw" "$GEN"
check "it does not claim there was nothing to write" nothas "nothing to write"
run_guest firewall --apply
check "run again, nothing is written and it says so" rc0_has "host.fw is current" "re-checked, nothing to write"
check "and does not say it wrote or updated anything" nothas_all "wrote host.fw" "updated host.fw"
fresh "$GEN"; GUEST="$T/pve-guest-nowrite.sh" run_guest firewall --apply
check "a write that silently did not happen is caught afterwards" rcnz_has "is missing after the write"
check "and is not reported as written" nothas_all "wrote host.fw" "written and re-checked"
if [[ "$EUID" -ne 0 ]]; then
  fresh "$GEN"; chmod 555 "$HOSTDIR"
  run_guest firewall --apply
  chmod 755 "$HOSTDIR"
  check "when host.fw cannot be written, --apply fails" test "$RC" -ne 0
  check "and does not claim it was written" nothas_all "wrote host.fw" "written and re-checked"
else
  ok "SKIP (running as root, a read-only directory would not stop the write)"
fi

echo "-- a host.fw PVE rewrote: the header comment gone"
sed -e '/^#/d' -e 's/ *#.*$//' "$HOSTGEN" > "$HOSTFW"; cp "$HOSTFW" "$T/host.nocomments"
check "the fixture has no comment left and still has the options" test -z "$(grep '#' "$HOSTFW")" -a -n "$(grep '^nf_conntrack_max: 1048576' "$HOSTFW")"
run_guest firewall --apply
check "its rules are the script's, so it is current, not hand-written" rc0_has "host.fw is current" "lost its header comment"
check "it is not reported as updated or refused" nothas_all "updated host.fw" "not merging"
check "and is untouched" cmp -s "$HOSTFW" "$T/host.nocomments"
{ echo '[OPTIONS]'; echo 'nf_conntrack_tcp_timeout_syn_recv: 30'; echo 'nf_conntrack_max: 1048576'; echo 'nf_conntrack_allow_invalid: 1'; echo; sed -n '/^\[RULES\]/,$p' "$HOSTGEN" | sed -e '/^#/d' -e 's/ *#.*$//'; } > "$HOSTFW"
cp "$HOSTFW" "$T/host.resorted"
run_guest firewall --apply
check "with its options sorted the other way round it is still current" rc0_has "host.fw is current"
check "and is untouched" cmp -s "$HOSTFW" "$T/host.resorted"
sed -e '1s/.*/# Managed by proxmox\/pve-guest.sh; an older wording of the header./' "$HOSTGEN" > "$HOSTFW"; cp "$HOSTFW" "$T/host.oldhdr"
run_guest firewall --apply
check "a changed header comment alone is not a difference" rc0_has "host.fw is current"
check "and the file is untouched" cmp -s "$HOSTFW" "$T/host.oldhdr"

echo "-- a header-less host.fw that is really different is never touched"
{ cat "$T/host.nocomments"; echo 'IN ACCEPT -p tcp -dport 9999'; } > "$HOSTFW"; cp "$HOSTFW" "$T/host.extra"
run_guest firewall --apply
check "an extra rule makes it somebody's: refused" rcnz_has "was not written by this script; not merging into it"
check "what to merge is printed" has_all "Merge this into" "nf_conntrack_max: 1048576"
check "and it is untouched" cmp -s "$HOSTFW" "$T/host.extra"
awk 'BEGIN { n = 0 } /^\[RULES\]/ { r = 1; print; next } r && NF { rules[++n] = $0; next } { print }
     END { for (i = n; i >= 1; i--) print rules[i] }' "$T/host.nocomments" > "$HOSTFW"; cp "$HOSTFW" "$T/host.rev"
check "the reversed-rules fixture really differs in order only" \
  test "$(sort "$HOSTFW")" = "$(sort "$T/host.nocomments")" -a "$(cat "$HOSTFW")" != "$(cat "$T/host.nocomments")"
run_guest firewall --apply
check "the same rules in another order are a different policy: refused" rcnz_has "not merging into it"
check "and untouched" cmp -s "$HOSTFW" "$T/host.rev"

echo "-- an older script-written host.fw shows its diff before it is rewritten"
cp "$T/host.old" "$HOSTFW"
run_guest firewall
check "the dry run prints a diff with the lines it adds" has_all "- is in the file, + is what it will write" "+nf_conntrack_max: 1048576" "+nf_conntrack_tcp_timeout_syn_recv: 30"
check "the diff comes before the plan to update" in_order "- is in the file" "update host.fw (it is this script's own"
run_guest firewall --apply
check "--apply prints the diff and then updates" rc0_has "+nf_conntrack_max: 1048576" "updated host.fw"

# ============================================================================
echo
echo "== a lease: tenant, capped, the default =="
fresh "$GEN"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "the dry run succeeds" test "$RC" -eq 0
check "it is a dry run" has "DRY RUN"
check "the bridge exists" has "bridge 'vmbr5'"
check "net0 is on vmbr5, with the network cap" has "--net0 virtio,bridge=vmbr5,firewall=1,rate=12.5"
check "it is never put on vmbr2" nothas "vmbr2"
check "the CPU is x86-64-v2-AES" has "--cpu x86-64-v2-AES"
check "the tags are tenant;lease" has "--tags tenant;lease"
check "the address and gateway are on 10.10.90.0/24" has "ip=10.10.90.10/24,gw=10.10.90.1"
check "DNS is the lease gateway" has "--nameserver 10.10.90.1"
check "the VM firewall uses GROUP lease" hasre "$GROUP_RE"'lease$'
check "it does not use GROUP tenant" nothas "GROUP tenant"
check "the lease_gw alias is checked and matches" has "alias lease_gw is 10.10.90.1"
check "nothing of the work segment appears" nothas "10.10.60."
check "the segment is named in the plan" has "10.10.90.10 on vmbr5, lease segment, capped"
check "the setup path goes through OPNsense to the lease address" has "ssh -J root@10.10.0.114 alice@10.10.90.10"
check "no AVX2 warning for the default CPU" nothas "AVX2"
check "its description says Leased VM for alice" has "--description Leased VM for alice. Created by proxmox/pve-guest.sh tenant"
check "and so does the header of its firewall file" has "# Managed by proxmox/pve-guest.sh: Leased VM for alice."
check "it is never called a work VM" nothas "Work VM"
check "a dry run runs none of the mutating commands" \
  test -z "$(grep -E '^qm (clone|set|start|destroy|disk)' "$LOG")"

run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.10
check "a lease at an address of the work segment is refused" test "$RC" -ne 0
check "the refusal gives the lease range" has "--ip must be in 10.10.90.2 to 10.10.90.254"
check "the refusal points at --uncapped for the work range" has "--uncapped uses 10.10.60.x"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.1
check "the gateway address .1 is refused" test "$RC" -ne 0
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.255
check "the broadcast address .255 is refused" test "$RC" -ne 0
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --bridge vmbr2
check "--bridge is refused for a tenant" test "$RC" -ne 0
check "the refusal names both bridges" has "vmbr5, or vmbr2 with --uncapped"
for ip in 10.10.90.08 10.10.90.010 10.10.090.10 010.10.90.10 10.10.90.10.1 10.10.90 10.10.90.256 10.10.90.1e1 ' 10.10.90.10' '10.10.90.10 ' abc 10.10.90.+5 10.10.90. 10.10.90.-1 0x0a.10.90.10; do
  run_guest "${TENANT_ARGS[@]}" --ip "$ip"
  check "--ip '$ip' is refused as not a plain dotted quad" rcnz_has "is not a plain dotted-quad address"
done
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.020 --uncapped
check "a leading zero is refused for a work VM too" rcnz_has "is not a plain dotted-quad address"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.8
check "the same address written plainly is accepted" rc0_has "ip 10.10.90.8 is unused"
printf '%s\n' '[IPSET ipfilter-net0]' '10.10.90.8' > "$FW/130.fw"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.08
check "10.10.90.08 does not slip past the in-use check for 10.10.90.8" rcnz_has "is not a plain dotted-quad address"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.8
check "while 10.10.90.8 itself is refused as in use" rcnz_has "already another tenant's: 130.fw"
rm -f "$FW/130.fw"
for port in 080 0 00080 65536 99999 100000 -1 8o 80,0443 443,080; do
  run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --allow "$port"
  check "--allow $port is refused" rcnz_has "is not a port (1 to 65535, no leading zero)"
done
for port in 80 1 65535 80,443 8080,65535; do
  run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --allow "$port"
  check "--allow $port is accepted" test "$RC" -eq 0
  for one in ${port//,/ }; do check "and opens port $one in the planned firewall file" has "IN ACCEPT -p tcp -dport $one"; done
done

echo
echo "== a work VM: tenant --uncapped =="
fresh "$GEN"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped
check "the dry run succeeds" test "$RC" -eq 0
check "net0 is on vmbr2, with no network cap" has "--net0 virtio,bridge=vmbr2,firewall=1"
check "no rate is set" nothas "rate="
check "it is never put on vmbr5" nothas "vmbr5"
check "the CPU is x86-64-v3" has "--cpu x86-64-v3"
check "the tags are tenant;uncapped" has "--tags tenant;uncapped"
check "the address and gateway are on 10.10.60.0/24" has "ip=10.10.60.20/24,gw=10.10.60.1"
check "the VM firewall uses GROUP tenant" hasre "$GROUP_RE"'tenant$'
check "it does not use GROUP lease" nothas "GROUP lease"
check "the lease_gw alias is not required of a work VM" nothas "lease_gw"
check "the setup path is the work address" has "ssh -J root@10.10.0.114 alice@10.10.60.20"
check "a work VM's description says Work VM (isolated like a tenant) for alice" has "--description Work VM (isolated like a tenant) for alice. Created by proxmox/pve-guest.sh tenant"
check "and so does the header of its firewall file" has "# Managed by proxmox/pve-guest.sh: Work VM (isolated like a tenant) for alice."
check "it is not called leased" nothas "Leased VM"
check "no AVX2 warning for a work VM" nothas "AVX2"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --uncapped
check "a work VM at a lease address is refused" test "$RC" -ne 0
check "the refusal gives the work range" has "--ip must be in 10.10.60.2 to 10.10.60.254"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped --cpu x86-64-v2-AES
check "a work VM may take the lease CPU" has "--cpu x86-64-v2-AES"
CPU_TYPE=x86-64-v2-AES run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped
check "CPU_TYPE sets the work VM's CPU" has "--cpu x86-64-v2-AES"

echo
echo "== an address in use on either segment =="
fresh "$GEN"
printf '%s\n' '[OPTIONS]' 'enable: 1' '' '[IPSET ipfilter-net0] # the only source address net0 may use' '10.10.90.10' '' '[RULES]' 'GROUP lease' > "$FW/120.fw"
printf '%s\n' '[OPTIONS]' 'enable: 1' '' '[IPSET ipfilter-net0]' '10.10.60.20' '' '[RULES]' 'GROUP tenant' > "$FW/121.fw"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a lease address in use by a lease is refused" test "$RC" -ne 0
check "the refusal names the VM's firewall file" has "already another tenant's: 120.fw"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped
check "a work address in use by a work VM is refused" test "$RC" -ne 0
check "the refusal names that VM's firewall file" has "already another tenant's: 121.fw"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.11
check "a free lease address is accepted beside them" test "$RC" -eq 0
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.21 --uncapped
check "a free work address is accepted beside them" test "$RC" -eq 0

echo
echo "== the CPU rule =="
fresh "$GEN"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a lease's default is accepted and is x86-64-v2-AES" rc0_has "--cpu x86-64-v2-AES"
for m in x86-64-v2-AES x86-64-v2 kvm64 qemu64 Nehalem Westmere Penryn Conroe; do
  : > "$LOG"; run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --cpu "$m"
  check "a lease on $m is accepted" rc0_has "--cpu $m"
done
for m in max host x86-64-v3 x86-64-v4 Haswell-noTSX Skylake-Server EPYC-Rome SandyBridge IvyBridge Opteron_G3 Opteron_G4 Opteron_G5 \
         GraniteRapids SierraForest KnightsMill Dhyana custom-avx2 qemu32 Nehalem-IBRS nehalem KVM64 x86-64-v2-AES2 x86-64-v2-AE; do
  : > "$LOG"; run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --cpu "$m"
  check "a lease on $m is refused, and nothing is run" test "$RC" -ne 0 -a -z "$(grep -E '^(qm|pvesm|pvesh)' "$LOG")"
  check "the refusal says a lease may only use a model that hides AVX, and points at --uncapped" \
    has_all "a lease may only use a CPU model that hides AVX" "'$m' is not one of them" "--uncapped is for work VMs"
done
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --cpu 'x86 64;rm'
check "a CPU name with odd characters is refused" test "$RC" -ne 0
CPU_TYPE=x86-64-v3 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "CPU_TYPE does not change a lease's CPU" rc0_has "--cpu x86-64-v2-AES"
LEASE_CPU_TYPE=Nehalem run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "LEASE_CPU_TYPE may pick another allowed model" rc0_has "--cpu Nehalem"
LEASE_CPU_TYPE=x86-64-v3 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "but not one that has AVX" rcnz_has "'x86-64-v3' is not one of them"
LEASE_CPU_TYPE=max run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "and not max" rcnz_has "'max' is not one of them"

echo "-- a work VM keeps the existing rules"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped
check "its default is x86-64-v3" rc0_has "--cpu x86-64-v3"
check "with no AVX2 warning at all" nothas "AVX2"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped --cpu host
check "--cpu host is refused for a work VM" rcnz_has "never gets --cpu host"
for m in max SandyBridge Haswell custom-avx2 x86-64-v4; do
  run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped --cpu "$m"
  check "a work VM may still use $m" rc0_has "--cpu $m"
done
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --cpu host
check "--cpu host is refused for a lease, by the allowlist" rcnz_has "'host' is not one of them"

echo
echo "== LEASE_BRIDGE and LEASE_PREFIX =="
fresh
LEASE_BRIDGE=vmbr7 LEASE_PREFIX=10.10.77 run_guest firewall --apply
check "firewall --apply writes lease_gw from LEASE_PREFIX" fhas "$FW/cluster.fw" "lease_gw 10.10.77.1 # OPNsense on the lease bridge"
FAKE_BRIDGES="vmbr0 vmbr2 vmbr7" LEASE_BRIDGE=vmbr7 LEASE_PREFIX=10.10.77 run_guest "${TENANT_ARGS[@]}" --ip 10.10.77.5
check "a lease follows them: bridge, address, gateway" rc0_has "bridge=vmbr7" "ip=10.10.77.5/24,gw=10.10.77.1"
check "and the alias check uses the new gateway" has "alias lease_gw is 10.10.77.1"
FAKE_BRIDGES="vmbr0 vmbr2 vmbr7" LEASE_BRIDGE=vmbr7 LEASE_PREFIX=10.10.77 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.5
check "the default range is refused then" test "$RC" -ne 0
fresh "$GEN"
FAKE_BRIDGES="vmbr0 vmbr2 vmbr7" LEASE_BRIDGE=vmbr7 LEASE_PREFIX=10.10.77 run_guest "${TENANT_ARGS[@]}" --ip 10.10.77.5
check "an alias that disagrees with LEASE_PREFIX is warned about in a dry run" rc0_has "alias lease_gw is 10.10.90.1 in" "--apply would refuse"
FAKE_BRIDGES="vmbr0 vmbr2 vmbr7" LEASE_BRIDGE=vmbr7 LEASE_PREFIX=10.10.77 run_guest "${TENANT_ARGS[@]}" --ip 10.10.77.5 --apply
check "and refused under --apply" rcnz_has "but this segment's gateway is 10.10.77.1"
check "the refusal happens before anything is created" test -z "$(grep -E '^qm (clone|set|start)' "$LOG")"

# ============================================================================
echo
echo "== preflight =="
fresh "$GEN"
FAKE_BRIDGES="vmbr0 vmbr1 vmbr2" run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a lease needs its bridge" rcnz_has "bridge 'vmbr5' not found"
FAKE_BRIDGES="vmbr0 vmbr1 vmbr5" run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped
check "a work VM needs its bridge" rcnz_has "bridge 'vmbr2' not found"

fresh; sed '/^lease_gw /d' "$GEN" > "$FW/cluster.fw"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a missing lease_gw alias is a warning in a dry run" rc0_has "alias lease_gw is not in"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "and a refusal under --apply" rcnz_has "alias lease_gw is not in"
check "nothing was created" test -z "$(grep -E '^qm (clone|set|start)' "$LOG")"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped
check "a work VM does not care about lease_gw" test "$RC" -eq 0
check "and does not mention it" nothas "lease_gw"

fresh; sed '/^\[group lease\]/,$d' "$GEN" > "$FW/cluster.fw"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a missing lease group is a warning in a dry run" rc0_has "no firewall group(s) yet: lease"
check "which names the firewall subcommand as the remedy" has "/pve-guest.sh firewall"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "and a refusal under --apply" rcnz_has "no firewall group(s): lease"
check "that names the remedy too" has "firewall --apply"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped --apply
check "a work VM is refused for the missing lease group as well" rcnz_has "no firewall group(s): lease"
fresh "$GEN"; sed -i '/^\[group tenant\]/,/^$/d' "$FW/cluster.fw"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "a missing tenant group is reported too" rcnz_has "no firewall group(s): tenant"

fresh "$GEN"
FAKE_FW_ENABLE=0 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a datacenter firewall that is off is a dry-run warning" rc0_has "datacenter firewall is off"
FAKE_FW_ENABLE=0 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "and a refusal under --apply" rcnz_has "a tenant would be unfiltered"

# ============================================================================
echo
echo "== the VM firewall file, written under --apply =="
fresh "$GEN"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --allow 80,443 --apply
V="$FW/113.fw"
check "a lease is created under --apply" rc0_has "alice-vm created"
check "its firewall file exists" test -f "$V"
for line in 'enable: 1' 'policy_in: DROP' 'policy_out: ACCEPT' 'log_level_in: info' 'log_level_out: nolog' \
            'ipfilter: 1' 'macfilter: 1' 'dhcp: 0' 'radv: 0' '10.10.90.10' 'GROUP lease' \
            'IN ACCEPT -p tcp -dport 80' 'IN ACCEPT -p tcp -dport 443'; do
  check "113.fw has the line '$line'" fhas "$V" "$line"
done
check "113.fw has the ipfilter-net0 set" grep -q '^\[IPSET ipfilter-net0\]' "$V"
check "113.fw has no GROUP tenant" test -z "$(grep '^GROUP tenant' "$V")"
check "the commands run use vmbr5 and the lease tags and CPU" \
  logged_all '^qm set 113 --net0 virtio,bridge=vmbr5,firewall=1,rate=12.5$' \
  '^qm set 113 .*--cpu x86-64-v2-AES' '^qm set 113 .*--tags tenant;lease '
check "the lease's addressing is passed to cloud-init" \
  logged '^qm set 113 --ciuser alice --sshkeys .* --ipconfig0 ip=10.10.90.10/24,gw=10.10.90.1 --nameserver 10.10.90.1$'
check "it is protected and started" logged_all '^qm set 113 --protection 1$' '^qm start 113$'
check "its own firewall file parsed cleanly" has "tenant firewall parses cleanly"
check "the description given to qm is Leased VM for alice" logged '^qm set 113 .*--description Leased VM for alice\. Created by '
check "the first line of 113.fw says Leased VM for alice" test "$(sed -n 1p "$V")" = "# Managed by proxmox/pve-guest.sh: Leased VM for alice."

fresh "$GEN"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped --apply
check "a work VM is created under --apply" test "$RC" -eq 0
for line in 'log_level_in: info' 'log_level_out: nolog' 'ipfilter: 1' 'macfilter: 1' 'dhcp: 0' 'radv: 0' '10.10.60.20' 'GROUP tenant'; do
  check "a work VM's 113.fw has the line '$line'" fhas "$FW/113.fw" "$line"
done
check "a work VM's 113.fw has no GROUP lease" test -z "$(grep '^GROUP lease' "$FW/113.fw")"
check "the description given to qm is Work VM (isolated like a tenant) for alice" \
  logged '^qm set 113 .*--description Work VM \(isolated like a tenant\) for alice\. Created by '
check "the first line of a work VM's 113.fw says so" \
  test "$(sed -n 1p "$FW/113.fw")" = "# Managed by proxmox/pve-guest.sh: Work VM (isolated like a tenant) for alice."
check "neither says Leased" test -z "$(grep -i 'leased' "$FW/113.fw")" -a -z "$(grep -- '--description Leased' "$LOG")"
check "a work VM is on vmbr2, uncapped, x86-64-v3" \
  logged_all '^qm set 113 --net0 virtio,bridge=vmbr2,firewall=1$' '^qm set 113 .*--cpu x86-64-v3' \
  '^qm set 113 .*--tags tenant;uncapped '
cp "$FW/113.fw" "$T/work.fw"

# ============================================================================
echo
echo "== fw_check: the group a VM uses, and the groups the cluster needs =="
LEASEVM="$T/lease-vm"; WORKVM="$T/work-vm"; mkdir -p "$LEASEVM" "$WORKVM"
fresh "$GEN"; run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply; cp "$FW/113.fw" "$LEASEVM/113.fw"
cp "$T/work.fw" "$WORKVM/113.fw"
# fw_check as the script defines it, run on its own.
fwc() {
  # shellcheck disable=SC2034  # FW_GROUPS is read by the evaluated fw_check
  ( eval "$(awk '/^fw_check\(\) \{/,/^}/' "$SRC")"; FW_GROUPS=(tenant lease); PERL5LIB="$FAKE_PERL" fw_check "$@" ) > "$T/fwc.out" 2>&1
  RC=$?; OUT="$T/fwc.out"
}
fwc "$GEN" 113 "$LEASEVM" lease
check "a lease's file passes as a lease" test "$RC" -eq 0
fwc "$GEN" 113 "$LEASEVM" tenant
check "a lease's file fails as a work VM" rcnz_has "want exactly one GROUP tenant rule, has: lease"
fwc "$GEN" 113 "$WORKVM" tenant
check "a work VM's file passes as a work VM" test "$RC" -eq 0
fwc "$GEN" 113 "$WORKVM" lease
check "a work VM's file fails as a lease" rcnz_has "has: tenant"
mkdir -p "$T/both"; { cat "$LEASEVM/113.fw"; echo 'GROUP tenant'; } > "$T/both/113.fw"
fwc "$GEN" 113 "$T/both" lease
check "a file with both groups fails" rcnz_has "has: lease tenant"
mkdir -p "$T/none"; sed '/^GROUP /d' "$LEASEVM/113.fw" > "$T/none/113.fw"
fwc "$GEN" 113 "$T/none" lease
check "a file with no group fails" rcnz_has "has: none"
fwc "$GEN" 113 "$LEASEVM"
check "asking for no group is an error, not a pass" rcnz_has "no group given"
sed '/^\[group lease\]/,$d' "$GEN" > "$T/no-lease.fw"
fwc "$T/no-lease.fw" 113 "$WORKVM" tenant
check "a cluster file without the lease group fails, even for a work VM" rcnz_has "cluster: no lease group"
sed '/^\[group tenant\]/,/^$/d' "$GEN" > "$T/no-tenant.fw"
fwc "$T/no-tenant.fw" 113 "$LEASEVM" lease
check "a cluster file without the tenant group fails" rcnz_has "cluster: no tenant group"
fwc "$GEN" host "$HOSTGEN"
check "a good cluster file passes in host mode" test "$RC" -eq 0
fwc "$GEN" host "$T/no-such-host.fw"
check "a host file that does not exist fails fw_check" rcnz_has "host: $T/no-such-host.fw does not exist"
mkdir -p "$T/emptydir"; fwc "$GEN" 113 "$T/emptydir" lease
check "a VM file that does not exist fails fw_check" rcnz_has "vm 113: $T/emptydir/113.fw does not exist"
fwc "$T/no-such-cluster.fw" 113 "$LEASEVM" lease
check "a cluster file that does not exist fails fw_check" rcnz_has "cluster: $T/no-such-cluster.fw does not exist"
sed 's/-log warning/-log bogus/' "$GEN" > "$T/bad-log.fw"
fwc "$T/bad-log.fw" 113 "$LEASEVM" lease
check "a malformed rule in a group fails" rcnz_has "bad log level"

# ============================================================================
echo
echo "== lease packet-rate limit: the plan, the snippets storage =="
fresh "$GEN"
FAKE_SNIPPETS=0 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a lease dry run with no snippets storage succeeds" test "$RC" -eq 0
check "it says what --apply will run, extending the existing content list" \
  has_all "storage 'local' has content=[vztmpl,iso,backup,import], no snippets" \
          "--apply will run: pvesm set local --content vztmpl,iso,backup,import,snippets"
check "the plan has the storage change, the directory, the file, its mode, the clone, the hookscript and the start, in that order" \
  in_order "+ pvesm set local --content vztmpl,iso,backup,import,snippets" "+ mkdir -p $VZ/snippets" "+ write $HOOK" \
           "+ chmod 0755 $HOOK" "+ qm clone" "+ qm set 113 --hookscript local:snippets/lease-limits.pl" "+ qm start 113"
check "the plan shows the hookscript with the default rate" has "my \$pps = 20000;"
check "the rate is announced" has "lease packet-rate limit" 
check "a dry run runs no pvesm set and writes nothing" test -z "$(grep '^pvesm set' "$LOG")" -a ! -e "$HOOK"
check "its next steps say how to see the limit" has "tc -s filter show dev tap113i0 parent ffff:"

run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "with snippets already offered, the dry run says so" rc0_has "storage 'local' offers snippets"
check "and plans no storage change" nothas "pvesm set"
check "but still plans the hookscript" has_all "+ write $HOOK" "--hookscript local:snippets/lease-limits.pl"

FAKE_SNIPPETS=0 FAKE_LOCAL_CONTENT="vztmpl,iso,backup,import,rootdir,images" run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "the existing list is extended, never replaced" rc0_has "pvesm set local --content vztmpl,iso,backup,import,rootdir,images,snippets"
FAKE_SNIPPETS=0 FAKE_LOCAL_CONTENT="" run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "an unreadable content list is refused, not overwritten" rcnz_has "could not read the content list of storage 'local'"
FAKE_SNIPPETS=0 FAKE_LOCAL_CONTENT="vztmpl,snippets" run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "snippets listed but the storage inactive is refused" rcnz_has "lists snippets in its content but is not active"
FAKE_NO_LOCAL=1 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "no storage named local is refused, with a reason" rcnz_has "there is no storage named 'local'"
FAKE_LOCAL_PATH="/mnt/other" run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a hookscript outside the storage's snippets directory is refused" rcnz_has "keeps snippets in /mnt/other/snippets"
FAKE_LOCAL_PATH="" run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "an unreadable storage path is only a warning" rc0_has "could not read its path"

LEASE_HOOKSCRIPT="$VZ/snippets/my-limits.pl" run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "LEASE_HOOKSCRIPT moves the file and the volume id" rc0_has "+ write $VZ/snippets/my-limits.pl" "--hookscript local:snippets/my-limits.pl"
LEASE_PPS=5000 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "LEASE_PPS is honoured in the plan" rc0_has "my \$pps = 5000;" "5000 pps"
LEASE_PPS=abc run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a LEASE_PPS that is not a number is refused" rcnz_has "LEASE_PPS must be a positive integer"
LEASE_PPS=0 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a LEASE_PPS of 0 is refused" rcnz_has "LEASE_PPS must be a positive integer"

: > "$LOG"
FAKE_SNIPPETS=0 run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped
check "an --uncapped dry run succeeds" test "$RC" -eq 0
check "it shows no hookscript, no snippets step and no storage change" nothas_all "hookscript" "snippets" "pvesm" "lease-limits" "pps"
check "it does not even ask about the snippets storage" test -z "$(grep 'pvesm status --content\|get /storage/local ' "$LOG")"
FAKE_NO_LOCAL=1 LEASE_PPS=abc run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped
check "a work VM needs neither the storage 'local' nor a valid LEASE_PPS" test "$RC" -eq 0

echo
echo "== lease packet-rate limit: under --apply =="
fresh "$GEN"
FAKE_SNIPPETS=0 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "a lease with no snippets storage is created under --apply" rc0_has "alice-vm created"
check "storage local gained snippets, extending its list" logged '^pvesm set local --content vztmpl,iso,backup,import,snippets$'
check "that came before the hookscript existed, before the clone, and the hookscript is set before the start" \
  logged_all '^pvesm-set: hookscript present: no$' '^clone-saw-hookscript: yes$' \
  && log_order '^pvesm set local' '^qm clone 9000 113' '^qm set 113 --hookscript local:snippets/lease-limits.pl$' '^qm start 113$'
check "the hookscript is in the snippets directory" test -f "$HOOK"
check "it is mode 0755" test "$(stat -c %a "$HOOK")" = 755
check "it starts with a perl shebang and the managed header" \
  test "$(sed -n 1,2p "$HOOK")" = "$(printf '%s\n' '#!/usr/bin/perl' '# Managed by proxmox/pve-guest.sh. Do not edit by hand.')"
check "perl compiles it" test "$(perl -c "$HOOK" 2>&1)" = "$HOOK syntax OK"
check "the placeholder is substituted" test -z "$(grep -F __PPS__ "$HOOK")"
check "it sets 20000 pps" grep -qF 'my $pps = 20000; my $burst = $pps * 2;' "$HOOK"
check "it uses priority 5, for the delete and the add" test "$(grep -c "'prio','5'" "$HOOK")" -eq 2
check "it polices packets, not bytes" grep -qF "'police','pkts_rate',\$pps,'pkts_burst',\$burst,'conform-exceed','drop/continue'" "$HOOK"
check "conforming packets go on to PVE's byte-rate filter: drop/continue, never a plain drop" \
  test "$(grep -c "'conform-exceed','drop/continue'" "$HOOK")" -eq 1 -a -z "$(grep "'conform-exceed','drop')" "$HOOK")"
check "the VMID is matched with \\A and \\z, not ^ and $" grep -qF '/\A(\d+)\z/' "$HOOK"
check "and no ^\\d+$ pattern is left" test -z "$(grep -F '^\d+$' "$HOOK")"
check "no path in it exits non-zero" test "$(grep -oE 'exit +[^ ;]+' "$HOOK" | sort -u)" = "exit 0"
check "and it never dies" test -z "$(grep -E '\bdie\b|\bcroak\b' "$HOOK")"

# --- the hookscript run, with the sysfs tree moved and tc a shim
mkdir -p "$T/sys/tap113i0"
sed "s#/sys/class/net#$T/sys#g" "$HOOK" > "$T/hook.pl"
run_hook() { : > "$LOG"; OUT="$T/hook.out"; FAKE_LOG="$LOG" PATH="$BIN:$PATH" perl "$T/hook.pl" "$@" > "$OUT" 2>&1; RC=$?; }
run_hook 113 post-start
check "post-start: exit 0" test "$RC" -eq 0
check "post-start: adds the ingress qdisc when the tap has none" logged '^tc qdisc add dev tap113i0 ingress$'
check "post-start: removes a previous priority-5 filter first, then adds the limit" \
  log_order '^tc filter del dev tap113i0 parent ffff: prio 5$' \
            '^tc filter add dev tap113i0 parent ffff: protocol all prio 5 matchall action police pkts_rate 20000 pkts_burst 40000 conform-exceed drop/continue$'
FAKE_TC_QDISC="qdisc ingress ffff: parent ffff:fff1 ----------------" run_hook 113 post-start
check "post-start: leaves an existing ingress qdisc alone" test -z "$(grep 'qdisc add' "$LOG")"
check "post-start with an existing qdisc still sets the filter" logged ' prio 5 matchall action police pkts_rate 20000 '
for phase in pre-start post-stop pre-stop; do
  run_hook 113 "$phase"
  check "$phase: exit 0 and no tc" test "$RC" -eq 0 -a -z "$(grep '^tc ' "$LOG")"
done
run_hook
check "no arguments: exit 0 and no tc" test "$RC" -eq 0 -a -z "$(grep '^tc ' "$LOG")"
run_hook '113;touch x' post-start
check "a VMID that is not a number: exit 0 and no tc" test "$RC" -eq 0 -a -z "$(grep '^tc ' "$LOG")"
run_hook $'113\n' post-start
check "a VMID with a trailing newline: exit 0 and no tc" test "$RC" -eq 0 -a -z "$(grep '^tc ' "$LOG")"
run_hook ' 113' post-start
check "a VMID with a leading space: exit 0 and no tc" test "$RC" -eq 0 -a -z "$(grep '^tc ' "$LOG")"
run_hook 113 post-start
check "the same VMID, plain, still gets its limit" logged ' pkts_rate 20000 pkts_burst 40000 conform-exceed drop/continue$'
check "every tc call names only the validated tap" test -z "$(grep '^tc ' "$LOG" | grep -v 'tap113i0')"
FAKE_TC_ADD_RC=2 run_hook 113 post-start
check "tc failing to add the limit: still exit 0, with a message" test "$RC" -eq 0 && grep -qF "could not set the packet-rate limit on tap113i0" "$OUT"
run_hook 999 post-start
check "a tap that never appears: exit 0, with a message, and no tc" test "$RC" -eq 0 -a -z "$(grep '^tc ' "$LOG")" && grep -qF "tap999i0 not found" "$OUT"

# --- written only when it differs
hook_mdate() { date -u -d "$(stat -c %y "$HOOK")" +%F; }
fresh "$GEN"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
touch -d '2020-01-01 00:00:00' "$HOOK"
KEEP_VZ=1 fresh "$GEN"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10
check "a current hookscript is reported as current in a dry run" has "hookscript $HOOK is current (20000 pps)"
check "and nothing is planned for it" nothas_all "+ write $HOOK" "+ mkdir" "+ chmod"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "a second lease leaves it alone: same modification time" test "$(hook_mdate)" = 2020-01-01
chmod 0644 "$HOOK"; touch -d '2020-01-01 00:00:00' "$HOOK"; chmod 0644 "$HOOK"
KEEP_VZ=1 fresh "$GEN"; run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "the right content with the wrong mode is chmod-ed" test "$(stat -c %a "$HOOK")" = 755
check "and not rewritten" test "$(hook_mdate)" = 2020-01-01
echo '# edited by hand' >> "$HOOK"; touch -d '2020-01-01 00:00:00' "$HOOK"
KEEP_VZ=1 fresh "$GEN"; run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "a changed file is rewritten to the expected content" test -z "$(grep 'edited by hand' "$HOOK")"
check "and its modification time moves" test "$(hook_mdate)" != 2020-01-01
KEEP_VZ=1 fresh "$GEN"; LEASE_PPS=5000 run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "a different LEASE_PPS rewrites it with the new rate" grep -qF 'my $pps = 5000;' "$HOOK"
sed "s#/sys/class/net#$T/sys#g" "$HOOK" > "$T/hook.pl"; run_hook 113 post-start
check "and the new rate and its burst reach tc" logged ' pkts_rate 5000 pkts_burst 10000 conform-exceed drop/continue$'

fresh "$GEN"
run_guest "${TENANT_ARGS[@]}" --ip 10.10.60.20 --uncapped --apply
check "an --uncapped VM under --apply writes no hookscript" test ! -e "$HOOK"
check "and sets no hookscript on the VM" test -z "$(grep -- '--hookscript' "$LOG")"

# ============================================================================
echo
echo "== the hookscript subcommand =="
fresh "$GEN"
FAKE_SNIPPETS=0 run_guest hookscript
check "a dry run with no snippets storage succeeds" rc0_has "DRY RUN"
check "it plans the storage change, the directory, the file and its mode, in that order" \
  in_order "+ pvesm set local --content vztmpl,iso,backup,import,snippets" "+ mkdir -p $VZ/snippets" "+ write $HOOK" "+ chmod 0755 $HOOK"
check "with no --vmid it plans no qm command" nothas "qm set"
check "it says how to attach it to a lease" has "hookscript --vmid <vmid> --apply"
check "and that it was a dry run" has "This was a dry run"
check "the dry run changes nothing: no file, no pvesm set, no qm command" \
  test ! -e "$HOOK" -a -z "$(grep -E '^(pvesm set|qm (set|clone|start))' "$LOG")"
fresh "$GEN"; FAKE_SNIPPETS=0 run_guest hookscript --apply
check "--apply installs it" rc0_has "hookscript in place"
check "the storage got snippets first, before the file existed" logged_all '^pvesm set local --content vztmpl,iso,backup,import,snippets$' '^pvesm-set: hookscript present: no$'
check "the file is there, mode 0755" test -f "$HOOK" -a "$(stat -c %a "$HOOK")" = 755
check "and compiles" test "$(perl -c "$HOOK" 2>&1)" = "$HOOK syntax OK"
cp "$HOOK" "$T/hook.fromsubcommand"
fresh "$GEN"; run_guest "${TENANT_ARGS[@]}" --ip 10.10.90.10 --apply
check "it is byte for byte the file that tenant installs (the same code)" cmp -s "$HOOK" "$T/hook.fromsubcommand"
touch -d '2020-01-01 00:00:00' "$HOOK"
KEEP_VZ=1 fresh "$GEN"; run_guest hookscript --apply
check "run again, it finds the file current and leaves it alone" rc0_has "is current (20000 pps)"
check "same modification time" test "$(date -u -d "$(stat -c %y "$HOOK")" +%F)" = 2020-01-01
chmod 0644 "$HOOK"; echo '# edited' >> "$HOOK"
KEEP_VZ=1 fresh "$GEN"; run_guest hookscript --apply
check "a changed file is put back" test "$(cmp -s "$HOOK" "$T/hook.fromsubcommand" && echo same)" = same -a "$(stat -c %a "$HOOK")" = 755
LEASE_PPS=5000 run_guest hookscript
check "LEASE_PPS is honoured" rc0_has "my \$pps = 5000;"
LEASE_PPS=abc run_guest hookscript --apply
check "a LEASE_PPS that is not a number is refused" rcnz_has "LEASE_PPS must be a positive integer"
FAKE_NO_LOCAL=1 run_guest hookscript --apply
check "no storage named local is refused, as for a tenant" rcnz_has "there is no storage named 'local'"
run_guest hookscript --uncapped
check "an option for tenant only is refused" test "$RC" -ne 0
run_guest hookscript --vmid abc
check "--vmid must be a number" rcnz_has "--vmid must be a positive integer"

echo "-- with --vmid: a lease only"
fresh "$GEN"
FAKE_GUEST=114 FAKE_TAGS='tenant;lease' run_guest hookscript --vmid 114
check "a dry run for a lease names it" rc0_has "lease VM 114 (alice-vm)"
check "it plans the file before attaching it" in_order "+ write $HOOK" "+ qm set 114 --hookscript local:snippets/lease-limits.pl"
check "the dry run attaches nothing" test -z "$(grep -E '^qm set' "$LOG")" -a ! -e "$HOOK"
check "it tells the operator to restart the VM and how to check" has_all "qm shutdown 114, then qm start 114" "tc -s filter show dev tap114i0 parent ffff:"
FAKE_GUEST=114 FAKE_TAGS='tenant;lease' run_guest hookscript --vmid 114 --apply
check "--apply installs the file" rc0_has "hookscript in place"
check "and attaches it" logged '^qm set 114 --hookscript local:snippets/lease-limits.pl$'
check "the file existed when it was attached" logged '^hookscript-set-saw-file: yes$'
for tags in 'lease' 'tenant;lease' 'lease,tenant' 'tenant;x;lease'; do
  fresh "$GEN"; FAKE_GUEST=114 FAKE_TAGS="$tags" run_guest hookscript --vmid 114
  check "a VM tagged '$tags' is a lease" rc0_has "lease VM 114"
done
for tags in 'tenant;uncapped' 'tenant' '' 'leased' 'tenantlease' 'tenant;releaser' 'lease-old'; do
  fresh "$GEN"; FAKE_GUEST=114 FAKE_TAGS="$tags" run_guest hookscript --vmid 114 --apply
  check "a VM tagged '$tags' is refused, saying it is not a lease" rcnz_has "vmid 114 is not tagged lease, so it is not a lease"
  check "and nothing was installed or attached" test ! -e "$HOOK" -a -z "$(grep -E '^(pvesm set|qm set)' "$LOG")"
done
fresh "$GEN"; FAKE_GUEST=114 FAKE_TAGS='tenant' run_guest hookscript --vmid 114 --apply
check "an old lease (tagged tenant alone) is pointed at the README" has "proxmox/README.md#look-after-tenants"
fresh "$GEN"; run_guest hookscript --vmid 999 --apply
check "a vmid that is not a QEMU VM is refused" rcnz_has "vmid 999 is not a QEMU VM on this host"
check "and nothing was installed" test ! -e "$HOOK" -a -z "$(grep -E '^(pvesm set|qm set)' "$LOG")"
fresh "$GEN"; FAKE_GUEST=114 FAKE_TAGS='tenant;lease' FAKE_HOOKSCRIPT="local:snippets/other.pl" run_guest hookscript --vmid 114 --apply
check "a lease with another hookscript is refused, not overwritten" rcnz_has "already has the hookscript local:snippets/other.pl" "qm set 114 --delete hookscript"
check "and nothing was installed or attached" test ! -e "$HOOK" -a -z "$(grep -E '^(pvesm set|qm set)' "$LOG")"
fresh "$GEN"; FAKE_GUEST=114 FAKE_TAGS='tenant;lease' FAKE_HOOKSCRIPT="local:snippets/lease-limits.pl" run_guest hookscript --vmid 114 --apply
check "a lease that already uses it is told so" rc0_has "vmid 114 already uses local:snippets/lease-limits.pl"
check "and is not attached again" test -z "$(grep -E '^qm set' "$LOG")"

# ============================================================================
echo
echo "== handover works for both segments =="
fresh "$GEN"
for tags in 'tenant;lease' 'tenant;uncapped' 'tenant'; do
  FAKE_GUEST=114 FAKE_TAGS="$tags" run_guest handover --vmid 114 --key "$KEYS/alice.pub"
  check "handover accepts a VM tagged '$tags'" rc0_has "would be handed over" "qm set 114 --sshkeys"
  check "and names no segment's address" nothas_all "10.10.60." "10.10.90."
done
FAKE_GUEST=114 FAKE_TAGS='sandbox' run_guest handover --vmid 114 --key "$KEYS/alice.pub"
check "handover refuses a VM that is not a tenant" rcnz_has "is not tagged tenant"
FAKE_GUEST=114 FAKE_TAGS='tenantx;lease' run_guest handover --vmid 114 --key "$KEYS/alice.pub"
check "handover is exact about the tag" test "$RC" -ne 0
FAKE_GUEST=114 run_guest handover --vmid 114 --key "$KEYS/setup.pub"
check "handover refuses the operator's own key" rcnz_has "so it is yours"

# ============================================================================
echo
echo "== help =="
run_guest --help
check "--help exits 0" test "$RC" -eq 0
for s in LEASE_BRIDGE LEASE_PREFIX LEASE_CPU_TYPE vmbr5 10.10.90 'group lease' x86-64-v2-AES AVX2 GDS 'hookscript options'; do
  check "--help mentions $s" has "$s"
done
run_guest
check "no command prints the usage and exits 2" test "$RC" -eq 2

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
