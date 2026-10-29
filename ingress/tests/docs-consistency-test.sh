#!/usr/bin/env bash
#
# docs-consistency-test.sh - catch drift between the docs, the scripts and the
# example data. Every command shown to a user must exist and accept the flags
# shown, every error message render.sh can print must be documented, every
# path named must be there, and nothing in the module may hold an address
# outside the documentation ranges.
#
#   ./tests/docs-consistency-test.sh
#
# shellcheck disable=SC2016  # single quotes are deliberate: literal $ in patterns
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0
ok() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

# Globbed, not listed: a doc added later is checked automatically.
SURFACES=(README.md docs/*.md)
# Everything the module ships as text, for the address and dash rules.
TEXTFILES=(README.md docs/*.md mkdocs.yml .gitignore sites.example.tsv hosts.example.tsv
           render.sh install.sh verify-target.sh systemd/* tests/*.sh)

# Flags of other tools that the docs legitimately show: tailscale up, curl,
# gpg, systemctl, dnsmasq and Nextcloud's occ. install.sh, render.sh and
# pve-guest.sh are checked against their own parsers.
OTHER_FLAGS=(--hostname --advertise-tags --accept-dns --accept-routes
             --cacert --resolve --value --dearmor --now --test)
is_other_flag() { printf '%s\n' "${OTHER_FLAGS[@]}" | grep -qxF -- "$1"; }

# parser_flags <script>: the --flags its option loop accepts.
parser_flags() { sed -n '/^while (($#))/,/^done/p' "$1" | grep -oE -- '--[a-z][a-z-]*' | sort -u; }
in_parser() { grep -qxF -- "$2" < <(parser_flags "$1"); }

PVE=../proxmox/pve-guest.sh
# pve-guest.sh keeps its parser in a `case` inside a while loop; read its cases.
in_pve_parser() { grep -qE -e "(^|[|[:space:]])$1([|)])" "$PVE"; }

# One logical command per line: join backslash continuations.
logical_lines() {
  awk '
    /^```/ { prev = ""; next }
    {
      line = $0
      if (cont) { sub(/^[ \t]+/, "", line); buf = buf " " line }
      else { if (buf != "") print buf; buf = line }
      cont = ($0 ~ /\\[ \t]*$/)
      if (cont) sub(/\\[ \t]*$/, "", buf)
    }
    END { if (buf != "") print buf }
  ' "$1"
}

# Heading text of a Markdown file, ignoring comment lines inside code fences.
headings() {
  awk '/^```/ { f = !f; next } !f && /^#{1,6} / { sub(/^#+ +/, ""); print }' "$1"
}
# Python-Markdown toc slug: drop punctuation, lowercase, runs of space and
# hyphen become one hyphen.
slug() { tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_ -]//g; s/^ +| +$//g; s/[ -]+/-/g'; }

for need in install.sh render.sh verify-target.sh mkdocs.yml sites.example.tsv hosts.example.tsv systemd/ingress-dns@.service "$PVE"; do
  [[ -f "$need" ]] || { echo "missing $need, cannot check"; exit 1; }
done

echo "== flags shown with a command are accepted by that command =="
flagfail=0
while IFS= read -r line; do
  cmd=""
  for tok in $line; do
    tok="${tok//\`/}"
    case "$tok" in
      install.sh|*/install.sh) cmd=install; break ;;
      render.sh|*/render.sh)   cmd=render;  break ;;
      pve-guest.sh|*/pve-guest.sh) cmd=pve; break ;;
    esac
  done
  [[ -n "$cmd" ]] || continue
  for tok in $line; do
    [[ "$tok" == *--?* ]] || continue
    flag="$(grep -oE -- '--[a-z][a-z-]*' <<<"$tok" | head -1)"
    [[ -n "$flag" ]] || continue
    [[ "$flag" == --help ]] && continue
    case "$cmd" in
      install) in_parser install.sh "$flag" || { no "docs show 'install.sh $flag' but install.sh has no such flag"; flagfail=1; } ;;
      render)  in_parser render.sh "$flag"  || { no "docs show 'render.sh $flag' but render.sh has no such flag"; flagfail=1; } ;;
      pve)     in_pve_parser "$flag"        || { no "docs show 'pve-guest.sh $flag' but pve-guest.sh has no such flag"; flagfail=1; } ;;
    esac
  done
done < <(for f in "${SURFACES[@]}"; do logical_lines "$f"; done)
(( flagfail )) || ok "every flag shown with install.sh, render.sh or pve-guest.sh is in that script's parser"

echo
echo "== every --flag in the docs belongs to a tool we know =="
anyfail=0
while read -r flag; do
  in_parser install.sh "$flag" && continue
  in_parser render.sh "$flag" && continue
  in_pve_parser "$flag" && continue
  is_other_flag "$flag" && continue
  [[ "$flag" == --help ]] && continue
  no "docs mention '$flag', which is in no parser nor the known-other-tools list"; anyfail=1
done < <(grep -ohE -- '--[a-z][a-z-]*' "${SURFACES[@]}" | sort -u)
(( anyfail )) || ok "no unknown flag appears anywhere in README or docs"

echo
echo "== every install.sh and render.sh flag is documented =="
docfail=0
while read -r flag; do
  [[ "$flag" == --help ]] && continue
  grep -qF -- "$flag" docs/setup.md || { no "install.sh accepts '$flag' but docs/setup.md never shows it"; docfail=1; }
done < <(parser_flags install.sh)
while read -r flag; do
  [[ "$flag" == --help ]] && continue
  grep -qF -- "$flag" docs/operations.md || { no "render.sh accepts '$flag' but docs/operations.md never shows it"; docfail=1; }
done < <(parser_flags render.sh)
(( docfail )) || ok "docs/setup.md shows every install.sh flag, docs/operations.md every render.sh flag"

echo
echo "== no references to files that do not exist =="
pathfail=0
while read -r path; do
  [[ "$path" == */ ]] && continue
  base="."; target="$path"
  case "$path" in
    pve-guest.sh) base=".."; target="proxmox/pve-guest.sh" ;;
    proxmox/*)    base=".." ;;
  esac
  [[ -e "$base/$target" ]] || { no "referenced but missing: $path"; pathfail=1; }
done < <(sed -E 's#(^|[ `(])\./#\1#g' "${SURFACES[@]}" \
         | grep -ohP '(?<![/[:alnum:]_.~$}-])(render\.sh|install\.sh|verify-target\.sh|pve-guest\.sh|mkdocs\.yml|sites\.example\.tsv|hosts\.example\.tsv|proxmox/[A-Za-z0-9._/-]+|(systemd|tests|docs)/[A-Za-z0-9._@/-]+)' \
         | sed -E 's/[.,):]+$//' | sort -u)
(( pathfail )) || ok "every repo path named in README and docs exists"

echo
echo "== markdown links and anchors resolve =="
linkfail=0
for f in "${SURFACES[@]}"; do
  dir="$(dirname "$f")"
  while read -r target; do
    [[ "$target" =~ ^(https?|mailto): ]] && continue
    if [[ "$target" == \#* ]]; then file=""; anchor="${target#\#}"
    else
      file="${target%%#*}"
      if [[ "$target" == *#* ]]; then anchor="${target#*#}"; else anchor=""; fi
    fi
    if [[ -n "$file" ]]; then
      [[ -e "$dir/$file" ]] || { no "$f: broken link -> $file"; linkfail=1; continue; }
      tgt="$dir/$file"
    else
      tgt="$f"
    fi
    if [[ -n "$anchor" ]]; then
      if ! headings "$tgt" | slug | grep -qxF -- "$anchor"; then
        no "$f: broken anchor -> ${file}#${anchor}"; linkfail=1
      fi
    fi
  done < <(grep -ohE '\]\([^)]+\)' "$f" | sed -E 's/^\]\(//; s/\)$//')
done
(( linkfail )) || ok "every relative link and anchor resolves"

echo
echo "== every docs page is in the mkdocs nav =="
navfail=0
for page in docs/*.md; do
  base="$(basename "$page")"
  sed -n '/^nav:/,$p' mkdocs.yml | grep -qE ":[[:space:]]+${base}[[:space:]]*$" \
    || { no "$page is not in the nav of mkdocs.yml"; navfail=1; }
done
while read -r target; do
  [[ -f "docs/$target" ]] || { no "mkdocs.yml nav names docs/$target, which does not exist"; navfail=1; }
done < <(sed -n '/^nav:/,$p' mkdocs.yml | grep -oE '[A-Za-z0-9_./-]+\.md')
(( navfail )) || ok "nav and docs/*.md agree"

echo
echo "== every error render.sh can print is documented =="
msgfail=0; nmsg=0
while read -r msg; do
  msg="$(sed -E 's/[[:space:]]+$//' <<<"$msg")"
  [[ -n "$msg" ]] || continue
  nmsg=$((nmsg+1))
  grep -qF -- "$msg" docs/sites.md || { no "render.sh can print '$msg', which docs/sites.md never shows"; msgfail=1; }
done < <(sed -nE "s/.*fail \"[$][A-Z]+:[$]n: ([^\"$']*).*/\1/p" render.sh | sort -u)
(( nmsg > 0 )) || { no "found no fail messages in render.sh, so the check proves nothing"; msgfail=1; }
(( msgfail )) || ok "docs/sites.md shows every validation message render.sh prints"

echo
echo "== constants in the docs match the scripts =="
constfail=0
ranges="$(sed -nE 's/.*remote_ip ([0-9./]+) ([0-9a-f:/]+).*/\1 \2/p' render.sh | head -1)"
[[ -n "$ranges" ]] || { no "could not read the tailnet ranges from render.sh"; constfail=1; }
for r in $ranges; do
  grep -qF -- "$r" docs/sites.md || { no "docs/sites.md never names the tailnet range $r from render.sh"; constfail=1; }
done
while read -r s; do
  grep -qF -- "${s#server=}" docs/dns.md || { no "docs/dns.md never names the resolver ${s#server=} from render.sh"; constfail=1; }
done < <(grep -oE '^server=[0-9.]+' render.sh | sort -u)
etc="$(sed -nE 's/^ETC=(\/[^ ]+).*/\1/p' install.sh)"
if [[ -z "$etc" ]] || ! grep -qF -- "$etc/sites.tsv" docs/setup.md; then
  no "install.sh ETC '$etc' is not what docs/setup.md uses"; constfail=1
fi
root_copy="$(sed -nE 's/.*install -m 0644 "\$root" "\$ETC\/([^"]+)".*/\1/p' install.sh)"
if [[ -z "$root_copy" ]] || ! grep -qF -- "$etc/$root_copy" docs/certificates.md; then
  no "docs/certificates.md does not name the root copy '$etc/$root_copy' that install.sh writes"; constfail=1
fi
unit="$(sed -nE 's#.*"\$HERE/systemd/([^"]+)".*#\1#p' install.sh | head -1)"
[[ -f "systemd/$unit" ]] || { no "install.sh installs systemd/$unit, which does not exist"; constfail=1; }
for i in $(sed -nE 's/^for i in ([a-z ]+); do$/\1/p' install.sh | tr ' ' '\n' | sort -u); do
  grep -qF -- "ingress-dns@$i" docs/dns.md || { no "install.sh starts ingress-dns@$i, which docs/dns.md never names"; constfail=1; }
done
rewrite="$(sed -nE "s/^location_rewrite='(.*)'\$/\1/p" render.sh)"
if [[ -z "$rewrite" ]] || ! grep -qF -- "$rewrite" docs/sites.md; then
  no "docs/sites.md does not show the Location rewrite render.sh writes: '$rewrite'"; constfail=1
fi
upstream_re="$(sed -nE "s/^upstream_re='(.*)'\$/\1/p" render.sh)"
[[ -n "$upstream_re" ]] || { no "could not read upstream_re from render.sh"; constfail=1; }
max_port="$(sed -nE 's/.*<= ([0-9]+) \)\).*upstream port above.*/\1/p' render.sh)"
if [[ -z "$max_port" ]] || ! grep -qF -- "$max_port" docs/sites.md; then
  no "docs/sites.md does not name the port limit '$max_port' that render.sh enforces"; constfail=1
fi
(( constfail )) || ok "tailnet ranges, resolvers, /etc/ingress paths, unit and instance names, the Location rewrite and the port limit agree"

echo
echo "== every script and test is listed, every installer failure is documented =="
listfail=0
for f in *.sh; do
  grep -qF -- "\`$f\`" docs/index.md || { no "docs/index.md does not list \`$f\`"; listfail=1; }
  grep -qE -- "^$f " README.md || { no "README.md Layout does not list $f"; listfail=1; }
done
for f in tests/*-test.sh; do
  grep -qF -- "$f" README.md || { no "README.md Tests does not run $f"; listfail=1; }
  grep -qF -- "$(basename "$f")" tests/run.sh || { no "tests/run.sh does not run $(basename "$f")"; listfail=1; }
done
# Failure messages install.sh can print that a person must act on: each must
# still exist in install.sh and be shown in docs/operations.md.
for frag in 'not readable by the caddy user' 'Caddy refused the new Caddyfile' 'failed with the new files' 'the rendered Caddyfile does not validate' 'not on the tailnet yet'; do
  grep -qF -- "$frag" install.sh || { no "install.sh no longer prints '$frag'"; listfail=1; }
  grep -qF -- "$frag" docs/operations.md || { no "docs/operations.md never shows the install.sh message '$frag'"; listfail=1; }
done
(( listfail )) || ok "every script is in the README layout and docs/index.md, every test is in the README and run.sh, installer failures are in operations.md"

echo
echo "== the module is registered =="
regfail=0
grep -qF '"component": "ingress"' ../release-please-config.json || { no "release-please-config.json has no ingress package"; regfail=1; }
grep -qF 'ingress/README.md' ../README.md || { no "the root README Modules table does not link ingress/README.md"; regfail=1; }
for needle in 'ingress/mkdocs.yml' 'ingress/docs/**' 'ingress/docs/requirements.txt'; do
  grep -qF -- "$needle" ../.github/workflows/deploy-docs.yml || { no "deploy-docs.yml never mentions $needle"; regfail=1; }
done
grep -qF 'href="ingress/"' ../docs-landing/index.html || { no "docs-landing/index.html has no card for ingress/"; regfail=1; }
(( regfail )) || ok "release config, root README, docs workflow and landing page all name ingress"

echo
echo "== real site data cannot be committed =="
gitfail=0
if command -v git >/dev/null && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  for f in sites.tsv hosts.tsv; do
    git check-ignore -q "$f" || { no ".gitignore does not ignore $f"; gitfail=1; }
  done
  for f in sites.example.tsv hosts.example.tsv; do
    git check-ignore -q "$f" && { no ".gitignore ignores $f, which must be committed"; gitfail=1; }
  done
  (( gitfail )) || ok "sites.tsv and hosts.tsv are ignored; the .example.tsv files are not"
else
  echo "  skip: not inside a git work tree"
fi

echo
echo "== no address outside the documentation ranges =="
# Allowed: loopback, RFC 5737 (192.0.2/24, 198.51.100/24, 203.0.113/24), the
# first /24 of 100.64.0.0/10 for tailnet examples, and the two public
# resolvers the module forwards to. A private-range address (10.x, 172.16-31.x,
# 192.168.x), a real-looking tailnet address (100.x elsewhere) or a Tailscale
# IPv6 node address all fail.
addrfail=0
OKADDR='^(127(\.[0-9]{1,3}){3}|192\.0\.2\.[0-9]+|198\.51\.100\.[0-9]+|203\.0\.113\.[0-9]+|100\.64\.0\.[0-9]+|1\.1\.1\.1|1\.0\.0\.1)$'
while IFS=: read -r file line addr; do
  grep -qE "$OKADDR" <<<"$addr" || { no "$file:$line: address $addr is outside the documentation ranges"; addrfail=1; }
done < <(grep -noE '[0-9]{1,3}(\.[0-9]{1,3}){3}' "${TEXTFILES[@]}" | awk -F: '{ print $1 ":" $2 ":" $3 }')
while IFS= read -r hit; do
  no "Tailscale IPv6 node address: $hit"; addrfail=1
done < <(grep -nE 'fd7a:115c:a1e0:[0-9a-f]' "${TEXTFILES[@]}" || true)
(( addrfail )) || ok "every address in the module is loopback, documentation-range or a named public resolver"

echo
echo "== no counts restated in prose, no em dashes =="
textfail=0
# A number that lives in tool output drifts the moment a test is added.
COUNT_RE='(^|[^[:alnum:]_])[0-9]+ (assertion|check|test)s?\b'
# Built from pieces so the script does not match its own pattern.
DASH_RE='\x{2014}|&mdas''h;'
while read -r hit; do no "hardcoded count in prose: $hit"; textfail=1; done \
  < <(grep -nE "$COUNT_RE" "${SURFACES[@]}" || true)
while read -r hit; do no "em dash: $hit"; textfail=1; done \
  < <(grep -nP "$DASH_RE" "${TEXTFILES[@]}" || true)
(( textfail )) || ok "no hardcoded test counts, no em dashes"

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
