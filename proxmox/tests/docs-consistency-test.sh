#!/usr/bin/env bash
#
# docs-consistency-test.sh - catch drift between README.md, the usage block at
# the top of pve-guest.sh and the script's own argument parser and
# configuration block. It reads text only: nothing is run, no host is needed.
#
#   - every --flag the README shows with ./pve-guest.sh, or names on its own in
#     inline code, is accepted by the parser, and every parser flag appears in
#     the README
#   - the usage block and the parser name the same flags
#   - every subcommand the README runs exists, and every subcommand is in the
#     README and the usage block
#   - every environment variable in the README's table is in the script's
#     configuration block with the same default, and the reverse; every
#     variable the README names elsewhere exists; the usage block covers each
#   - the --ip in a README example is in the segment its flags select
#   - README anchors resolve, as do the anchors the script prints
#   - what the script enforces is what the README says: the lease CPU
#     allowlist (README and usage block), the conform action of the packet-rate
#     filter, every range in [IPSET private], and every fleet_pve_* metric the
#     README names (against the role that writes it)
#   - the README's by-hand steps match the script: the hookscript path in the
#     re-run command, the pre-start warning and its recovery, the messages it
#     quotes for cluster.fw and host.fw, and the lease CPU check that greps for
#     avx
#   - claims about the live firewall that no code backs are guarded by hand:
#     phrases that were wrong and must not come back, and the corrections that
#     must stay (DNS, logging gaps, connection limits, the TENANT rules, the
#     second NIC, where Loki's retention comes from)
#   - every test script is in the README, and every one it names exists
#   - no em dash in the script, the README or the tests
#
#   ./tests/docs-consistency-test.sh
#
# PVE_GUEST_SCRIPT=<file> and PVE_GUEST_README=<file> check other copies, which
# is how the checks were tried against deliberate breakage.
#
# shellcheck disable=SC2016  # single quotes are deliberate: literal $ in patterns
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0
ok() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

SCRIPT="${PVE_GUEST_SCRIPT:-pve-guest.sh}"
README="${PVE_GUEST_README:-README.md}"
for need in "$SCRIPT" "$README"; do
  [[ -f "$need" ]] || { echo "missing $need, cannot check"; exit 1; }
done

# Flags of other tools that the README names on their own in inline code.
OTHER_FLAGS=(--protection --net0)
# No `grep -q` after a pipe: it exits at the first match, the writer dies of
# SIGPIPE, and under pipefail a match reads as a miss. `>/dev/null` makes grep
# read to the end.
# Variables of other modules that the README names (the retention setting of Loki).
OTHER_VARS=(LOKI_RETENTION_PERIOD)
is_other_flag() { printf '%s\n' "${OTHER_FLAGS[@]}" | grep -xF -- "$1" >/dev/null; }
in_list() { local needle="$1"; shift; printf '%s\n' "$@" | grep -xF -- "$needle" >/dev/null; }

# The README without its fenced code blocks, and only its fenced blocks, with
# a backslash continuation joined into one logical line.
prose()  { awk '/^ *```/ { f = !f; next } !f' "$README"; }
blocks() {
  awk '
    /^ *```/ { f = !f; if (buf != "") print buf; buf = ""; cont = 0; next }
    f {
      line = $0
      if (cont) { sub(/^[ \t]+/, "", line); buf = buf " " line }
      else { if (buf != "") print buf; buf = line }
      cont = ($0 ~ /\\[ \t]*$/)
      if (cont) sub(/\\[ \t]*$/, "", buf)
    }
    END { if (buf != "") print buf }
  ' "$README"
}
# The usage block: what --help prints, without the comment markers.
header() { sed -n '2,/^set -Eeuo/p' "$SCRIPT" | sed '$d'; }
headings() { awk '/^ *```/ { f = !f; next } !f && /^#{1,6} / { sub(/^#+ +/, ""); print }' "$README"; }
# GitHub's heading anchors: lowercase, punctuation dropped, spaces to hyphens.
slug() { tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_ -]//g; s/^ +| +$//g; s/ /-/g'; }

mapfile -t PARSER_FLAGS < <(grep -oP '^\s+(?:-h\|)?\K--[a-z][a-z0-9-]*(?=\))' "$SCRIPT" | sort -u)
mapfile -t SUBCMDS < <(grep -oP '^\s+\Kimages\|[a-z|]+(?=\) ;;)' "$SCRIPT" | tr '|' '\n')
mapfile -t CONF_VARS < <(sed -nE 's/^([A-Z][A-Z0-9_]*)="\$\{\1:-.*$/\1/p' "$SCRIPT")
(( ${#PARSER_FLAGS[@]} > 10 )) || { echo "could not read the parser's flags from $SCRIPT"; exit 1; }
(( ${#SUBCMDS[@]} >= 7 ))      || { echo "could not read the subcommands from $SCRIPT"; exit 1; }
(( ${#CONF_VARS[@]} > 10 ))    || { echo "could not read the configuration block of $SCRIPT"; exit 1; }

echo "== flags =="
bad=0
while IFS= read -r flag; do
  in_list "$flag" "${PARSER_FLAGS[@]}" || { no "the README shows pve-guest.sh $flag, which the parser does not accept"; bad=1; }
done < <(blocks | grep -E '(^|[ /])pve-guest\.sh ' | grep -oE -- '--[a-z][a-z0-9-]*' | sort -u)
(( bad )) || ok "every flag shown with ./pve-guest.sh in the README is in the parser"

bad=0
while IFS= read -r flag; do
  in_list "$flag" "${PARSER_FLAGS[@]}" && continue
  is_other_flag "$flag" && continue
  no "the README names '$flag' in inline code: not in the parser, not a known flag of another tool"; bad=1
done < <(prose | grep -oP '`\K--[a-z][a-z0-9-]*' | sort -u)
(( bad )) || ok "every flag the README names on its own is in the parser or another tool's list"

bad=0
for flag in "${PARSER_FLAGS[@]}"; do
  grep -qF -- "$flag" "$README" || { no "the parser accepts $flag, which the README never mentions"; bad=1; }
done
(( bad )) || ok "every parser flag appears in the README"

mapfile -t HEADER_FLAGS < <(header | grep -oE -- '--[a-z][a-z0-9-]*' | sort -u)
bad=0
for flag in "${HEADER_FLAGS[@]}"; do
  in_list "$flag" "${PARSER_FLAGS[@]}" || { no "the usage block documents $flag, which the parser does not accept"; bad=1; }
done
(( bad )) || ok "every flag in the usage block is in the parser"
bad=0
for flag in "${PARSER_FLAGS[@]}"; do
  [[ "$flag" != --help ]] || continue
  in_list "$flag" "${HEADER_FLAGS[@]}" || { no "the parser accepts $flag, which the usage block does not document"; bad=1; }
done
(( bad )) || ok "every parser flag (but --help itself) is in the usage block"

echo
echo "== subcommands =="
bad=0
while IFS= read -r cmd; do
  in_list "$cmd" "${SUBCMDS[@]}" || { no "the README runs 'pve-guest.sh $cmd', which is not a subcommand"; bad=1; }
done < <({ prose; blocks; } | grep -oP 'pve-guest\.sh \K[a-z][a-z-]*(?=[ `]|$)' | sort -u)
(( bad )) || ok "every subcommand the README runs exists"
bad=0
for cmd in "${SUBCMDS[@]}"; do
  grep -qE "pve-guest\.sh $cmd( |\`|$)" "$README" || { no "subcommand '$cmd' is never run in the README"; bad=1; }
  header | grep -E '\./pve-guest\.sh '"$cmd"'( |$)' >/dev/null || { no "subcommand '$cmd' has no ./pve-guest.sh $cmd line in the usage block"; bad=1; }
done
(( bad )) || ok "every subcommand is in the README and the usage block"

echo
echo "== environment variables =="
default_of() { sed -nE "s/^$1=\"\\\$\\{$1:-(.*)\\}\"\$/\\1/p" "$SCRIPT"; }
# The README's Environment table: | `NAME` | `default` | purpose |
table="$(awk '/^### Environment/ { t = 1; next } /^#{1,3} / { t = 0 } t && /^\| `/' "$README")"
mapfile -t README_VARS < <(printf '%s\n' "$table" | awk -F'|' '{ gsub(/[` ]/, "", $2); print $2 }')
bad=0
for v in "${CONF_VARS[@]}"; do
  in_list "$v" "${README_VARS[@]}" || { no "$v is in the configuration block but not in the README's Environment table"; bad=1; }
done
for v in "${README_VARS[@]}"; do
  in_list "$v" "${CONF_VARS[@]}" || { no "the README's Environment table lists $v, which the script does not read"; bad=1; }
done
(( bad )) || ok "the Environment table and the configuration block list the same variables"

bad=0
for v in "${CONF_VARS[@]}"; do
  want="$(default_of "$v" | sed 's#\$HOME#~#; s#\${TENANT_PREFIX}#X#')"
  have="$(printf '%s\n' "$table" | awk -F'|' -v v="$v" '{ n = $2; gsub(/[` ]/, "", n); if (n == v) { d = $3; gsub(/[` ]/, "", d); print d } }')"
  [[ "$have" == "$want" ]] || { no "default of $v: script '$want', README '$have'"; bad=1; }
done
(( bad )) || ok "every default in the table equals the script's"

bad=0
while IFS= read -r tok; do
  if in_list "$tok" "${OTHER_VARS[@]}"; then
    :
  elif [[ "$tok" == *'*' ]]; then
    printf '%s\n' "${CONF_VARS[@]}" | grep "^${tok%\*}" >/dev/null || { no "the README names $tok, which matches no variable of the script"; bad=1; }
  else
    in_list "$tok" "${CONF_VARS[@]}" || { no "the README names $tok, which the script does not read"; bad=1; }
  fi
done < <({ prose | grep -oP '`\K[A-Z][A-Z0-9]*(_[A-Z0-9]+)+\*?(?=`)'
           blocks | grep -oP '(^|[ ;])(export )?\K[A-Z][A-Z0-9_]{2,}(?==)'; } | sort -u)
(( bad )) || ok "every variable the README names outside the table exists"

bad=0
for v in "${CONF_VARS[@]}"; do
  [[ "$v" == CI_USER ]] && continue
  header | grep -E "(^|[^A-Z_])($v|${v%%_*}_\*)([^A-Z_]|$)" >/dev/null || { no "the usage block does not cover $v"; bad=1; }
done
(( bad )) || ok "every configuration variable is covered by the usage block, by name or as PREFIX_*"

echo
echo "== examples =="
prefix_of() { default_of "$1"; }
lease_prefix="$(prefix_of LEASE_PREFIX)"; work_prefix="$(prefix_of TENANT_PREFIX)"
bad=0; seen=0
while IFS= read -r line; do
  ip="$(grep -oE -- '--ip [0-9.]+' <<<"$line" | cut -d' ' -f2)"
  [[ -n "$ip" ]] || continue
  seen=$((seen+1))
  if [[ "$line" == *--uncapped* ]]; then want="$work_prefix"; else want="$lease_prefix"; fi
  [[ "$ip" == "$want".* ]] || { no "README example '$line' has --ip $ip, outside $want.0/24"; bad=1; }
done < <(blocks | grep -E 'pve-guest\.sh tenant ')
(( seen >= 2 )) || { no "expected at least two tenant examples with --ip in the README, found $seen"; bad=1; }
(( bad )) || ok "each tenant example's --ip is in the segment its flags select ($seen examples)"

bad=0
want_vol="local:snippets/$(basename "$(default_of LEASE_HOOKSCRIPT)")"
seen=0
while IFS= read -r vol; do
  seen=$((seen+1))
  [[ "$vol" == "$want_vol" ]] || { no "the README names the hookscript volume $vol, but the script's default file gives $want_vol"; bad=1; }
done < <(grep -oP 'local:snippets/[A-Za-z0-9._-]+' "$README" | sort -u)
(( seen >= 1 )) || { no "the README never names the hookscript volume $want_vol"; bad=1; }
(( bad )) || ok "the hookscript volume id in the README is the one the script sets ($want_vol)"

bad=0; seen=0
while IFS= read -r opt; do
  seen=$((seen+1))
  grep -qF "$opt" "$README" || { no "host.fw gets '$opt' but the README does not state it"; bad=1; }
done < <(sed -n '/^host_fw_content() {/,/^}/p' "$SCRIPT" | grep -oE '^nf_conntrack_[a-z_]+: [0-9]+')
(( seen >= 3 )) || { no "expected at least three nf_conntrack options in host_fw_content, found $seen"; bad=1; }
(( bad )) || ok "every conntrack option the script writes to host.fw is stated in the README with its value ($seen options)"

echo
echo "== what the script enforces is what the README says =="
mapfile -t ALLOWED < <(sed -n 's/^LEASE_CPU_ALLOWED=(\(.*\))$/\1/p' "$SCRIPT" | tr ' ' '\n')
(( ${#ALLOWED[@]} >= 5 )) || { echo "could not read LEASE_CPU_ALLOWED from $SCRIPT"; exit 1; }
bad=0
for m in "${ALLOWED[@]}"; do
  grep -qF "\`$m\`" "$README" || { no "the README never names the allowed lease CPU model \`$m\`"; bad=1; }
  header | sed -E 's/\.( |$)/ /g' | grep -E "(^|[^A-Za-z0-9._-])$m([^A-Za-z0-9._-]|$)" >/dev/null || { no "the usage block never names the allowed lease CPU model $m"; bad=1; }
done
in_list "$(default_of LEASE_CPU_TYPE)" "${ALLOWED[@]}" || { no "LEASE_CPU_TYPE's default is not on the allowlist"; bad=1; }
(( bad )) || ok "every lease CPU model on the allowlist is in the README and the usage block, and the default is on it (${#ALLOWED[@]} models)"

bad=0
conform="$(grep -oP "'conform-exceed','\K[^']+" "$SCRIPT" | sort -u)"
[[ -n "$conform" ]] || { no "could not read the hookscript's conform-exceed action"; bad=1; }
grep -qF "conform-exceed $conform" "$README" || { no "the README does not state the hookscript's conform-exceed action ($conform)"; bad=1; }
(( bad )) || ok "the README states the hookscript's conform-exceed action ($conform)"

bad=0; seen=0
while IFS= read -r range; do
  seen=$((seen+1))
  grep -qF "\`$range\`" "$README" || { no "[IPSET private] holds $range, which the README does not list"; bad=1; }
done < <(sed -n '/^cluster_fw_content() {/,/^}/p' "$SCRIPT" | awk '/^\[IPSET private\]/ { f = 1; next } f && /^$/ { exit } f { sub(/ *#.*/, ""); print }')
(( seen >= 7 )) || { no "expected at least seven ranges in [IPSET private], found $seen"; bad=1; }
(( bad )) || ok "every range in [IPSET private] is listed in the README ($seen ranges)"

bad=0; role=../ansible/roles/pve_guest_metrics/templates/fleet-pve-guests.j2
if [[ -f "$role" ]]; then
  while IFS= read -r metric; do
    grep -qF "$metric" "$role" || { no "the README names $metric, which $role does not write"; bad=1; }
  done < <(grep -oE 'fleet_pve_[a-z_]+' "$README" | sort -u)
  (( bad )) || ok "every fleet_pve_* metric the README names is written by the pve_guest_metrics role"
else
  ok "the pve_guest_metrics role is not in this checkout, metric names not cross-checked"
fi

bad=0
hook_path="$(default_of LEASE_HOOKSCRIPT)"
grep -qF "$hook_path <vmid> post-start" "$README" || { no "the README does not show re-running the hookscript by hand as: $hook_path <vmid> post-start"; bad=1; }
grep -qF "fleet_pve_tenant_packet_limit_active" "$README" || { no "the README never names fleet_pve_tenant_packet_limit_active"; bad=1; }
grep -qF "stop-on-error" "$README" || { no "the README does not say PVE runs pre-start with stop-on-error"; bad=1; }
grep -qF "qm set <vmid> --delete hookscript" "$README" || { no "the README does not show how to detach a hookscript"; bad=1; }
for msg in "cluster.fw is current" "wrote host.fw" "updated host.fw"; do
  if ! grep -qF "$msg" "$README" || ! grep -qF "\"$msg" "$SCRIPT"; then no "the README quotes '$msg' but the script does not print it (or the README never says it)"; bad=1; fi
done
blocks | grep -E 'grep .*/proc/cpuinfo' | grep -E 'grep -c avx /proc/cpuinfo' >/dev/null || { no "the README's lease CPU check must be: grep -c avx /proc/cpuinfo"; bad=1; }
if blocks | grep -E 'grep .*/proc/cpuinfo' | grep 'avx2' >/dev/null; then no "a README check greps /proc/cpuinfo for avx2, which misses AVX"; bad=1; fi
(( bad )) || ok "the README's by-hand steps and quoted messages match the script"

echo
echo "-- claims about the live firewall: wrong phrases stay out, corrections stay in"
bad=0
FORBIDDEN=(
  "DNS always goes through the firewall's resolver"
  "logs every new connection from the VM"
  "Every new flow and every DNS query is logged"
  "and every DNS lookup, and keeps"
  "limits each VM to 10000 open connections"
  "who talked to whom"
  "monitoring/loki/loki-config.yml"
  "  5. pass → any"
  "every new flow of the TENANT and LEASE interfaces"
  "can never stop a VM from starting"
  "so it can never stop"
  "rerun the script's \`tenant\` dry run"
)
for phrase in "${FORBIDDEN[@]}"; do
  if grep -qF -- "$phrase" "$README"; then no "the README says '$phrase', which was found to be wrong"; bad=1; fi
done
REQUIRED=(
  "DNS over QUIC (UDP 853)"
  "| 111 | block | UDP | any | 853 | yes |"
  "DNS over HTTPS"
  "inside a tenant's own Tailscale tunnel"
  "one line a second per"
  "not logged on the hypervisor"
  "rate-limited on the way into Loki"
  "up to 20000 in"
  "at most 50000 states per source and"
  "2000 new connections per 10 s per source"
  "(seq 490)"
  "(seq 500)"
  "LOKI_RETENTION_PERIOD"
  "monitoring/docs/operations/retention.md"
  "no packet ceiling, no IP pinning and no metrics"
  "Lease Packet Limit Missing"
  "tags 'tenant;lease' --cpu x86-64-v2-AES"
  "not a lease in this sense"
  "stop-on-error"
)
for phrase in "${REQUIRED[@]}"; do
  grep -qF -- "$phrase" "$README" || { no "the README no longer says '$phrase'"; bad=1; }
done
(( bad )) || ok "no stale claim is back, and every correction is still in (${#FORBIDDEN[@]} phrases out, ${#REQUIRED[@]} in)"

echo
echo "== links =="
mapfile -t ANCHORS < <(headings | slug)
bad=0
while IFS= read -r a; do
  in_list "$a" "${ANCHORS[@]}" || { no "README link to #$a has no such heading"; bad=1; }
done < <(grep -oP '\]\(#\K[^)]+' "$README" | sort -u)
(( bad )) || ok "every #anchor link in the README resolves"
bad=0
while IFS= read -r a; do
  in_list "$a" "${ANCHORS[@]}" || { no "the script points at proxmox/README.md#$a, which is not a heading"; bad=1; }
done < <(grep -oP 'proxmox/README\.md#\K[a-z0-9-]+' "$SCRIPT" | sort -u)
(( bad )) || ok "every README anchor the script prints resolves"
bad=0
while IFS= read -r link; do
  target="${link%%#*}"; frag=""; [[ "$link" == *'#'* ]] && frag="${link#*#}"
  [[ "$target" == http* || -z "$target" ]] && continue
  if [[ ! -e "$target" ]]; then no "README links to $target, which does not exist"; bad=1; continue; fi
  if [[ -n "$frag" && "$target" == *.md ]]; then
    awk '/^ *```/ { f = !f; next } !f && /^#{1,6} / { sub(/^#+ +/, ""); print }' "$target" | slug | grep -xF -- "$frag" >/dev/null \
      || { no "README links to $target#$frag, which has no such heading"; bad=1; }
  fi
done < <(grep -oP '\]\(\K[^)\s]+(?=\))' "$README" | sort -u)
(( bad )) || ok "every relative link in the README, and its heading anchor, resolves"

echo
echo "== tests are documented =="
bad=0
for f in tests/*.sh; do
  grep -qF "tests/${f##*/}" "$README" || { no "$f is not named in the README"; bad=1; }
done
while IFS= read -r f; do
  [[ -f "$f" ]] || { no "the README names $f, which does not exist"; bad=1; }
done < <(grep -oP '(\./)?\Ktests/[A-Za-z0-9._-]+\.sh' "$README" | sort -u)
(( bad )) || ok "every test script is in the README, and each one it names exists"

echo
echo "== host hardening survives a reboot =="
# A net.netfilter.* key in /etc/sysctl.d is skipped at boot unless nf_conntrack
# is already loaded, so a README that shows one must also show the
# modules-load.d file that loads it.
if grep '^net\.netfilter\.' "$README" >/dev/null; then
  if grep -F '/etc/modules-load.d/nf_conntrack.conf' "$README" >/dev/null \
     && grep -x 'nf_conntrack' "$README" >/dev/null; then
    ok "the README's net.netfilter sysctl comes with the modules-load.d file that makes it apply at boot"
  else
    no "the README sets a net.netfilter sysctl without loading nf_conntrack at boot (modules-load.d)"
  fi
else
  ok "the README sets no net.netfilter sysctl, so nothing needs the module at boot"
fi

echo
echo "== style =="
if grep -nP '\x{2014}|&mdas[h];' "$SCRIPT" "$README" tests/*.sh tests/fake-pve/PVE/Firewall.pm; then
  no "an em dash is in the script, the README or the tests"
else
  ok "no em dash in the script, the README or the tests"
fi

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
