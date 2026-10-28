#!/usr/bin/env bash
#
# docs-consistency-test.sh - catch drift between the docs, the installer and
# the compose file. Every command shown to a user must exist, accept the flags
# shown, and be referred to by a path that is really there; the settings and
# images named in compose.yaml must match .env.example and follow :latest.
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

# Flags of other tools that the docs legitimately show (tailscale serve,
# tar, docker compose logs and argparse-style --help on install.sh).
OTHER_FLAGS=(--bg --tcp --exclude --tail --help)
is_other_flag() { printf '%s\n' "${OTHER_FLAGS[@]}" | grep -qxF -- "$1"; }

in_install_parser() { grep -qE -e "(^|[|[:space:]])$1([|)])" install.sh; }

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

for need in install.sh compose.yaml .env.example mkdocs.yml; do
  [[ -f "$need" ]] || { echo "missing $need, cannot check"; exit 1; }
done

echo "== flags shown with install.sh are accepted by its parser =="
flagfail=0
while IFS= read -r line; do
  grep -qE '(^|[^[:alnum:]_.-])(\./)?install\.sh' <<<"$line" || continue
  for tok in $line; do
    [[ "$tok" == *--?* ]] || continue
    flag="$(grep -oE -- '--[a-z][a-z-]*' <<<"$tok" | head -1)"
    [[ -n "$flag" ]] || continue
    in_install_parser "$flag" || { no "docs show 'install.sh $flag' but install.sh has no such flag"; flagfail=1; }
  done
done < <(for f in "${SURFACES[@]}"; do logical_lines "$f"; done)
(( flagfail )) || ok "every flag shown with install.sh is in its parser"

echo
echo "== every --flag in the docs belongs to a tool we know =="
anyfail=0
while read -r flag; do
  in_install_parser "$flag" && continue
  is_other_flag "$flag" && continue
  no "docs mention '$flag', which is not in install.sh's parser nor the known-other-tools list"; anyfail=1
done < <(grep -ohE -- '--[a-z][a-z-]*' "${SURFACES[@]}" | sort -u)
(( anyfail )) || ok "no unknown flag appears anywhere in README or docs"

echo
echo "== every install.sh flag is documented =="
docfail=0
while read -r flag; do
  grep -qF -- "$flag" docs/setup.md || { no "install.sh accepts '$flag' but docs/setup.md never shows it"; docfail=1; }
done < <(sed -n '/^while (($#))/,/^done/p' install.sh | grep -oE -- '--[a-z][a-z-]*' | sort -u)
(( docfail )) || ok "docs/setup.md shows every flag install.sh accepts"

echo
echo "== no references to files that do not exist =="
pathfail=0
while read -r path; do
  [[ "$path" == */ ]] && continue
  base="."; [[ "$path" == ansible/* ]] && base=".."
  [[ -e "$base/$path" ]] || { no "referenced but missing: $path"; pathfail=1; }
done < <(sed -E 's#(^|[ `(])\./#\1#g' "${SURFACES[@]}" \
         | grep -ohP '(?<![/[:alnum:]_.~$}-])(compose\.yaml|install\.sh|\.env\.example|mkdocs\.yml|(tests|docs|ansible)/[A-Za-z0-9._/-]+)' \
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
echo "== compose.yaml and .env.example name the same settings =="
envfail=0
env_keys="$(grep -oE '^[A-Z][A-Z0-9_]*=' .env.example | tr -d '=' | sort -u)"
# ${VAR} references in compose.yaml. {{PORT}} placeholders are gluetun's, not compose's.
compose_keys="$(grep -oE '\$\{[A-Z][A-Z0-9_]*' compose.yaml | tr -d '${' | sort -u)"
while read -r k; do
  [[ -n "$k" ]] || continue
  grep -qxF "$k" <<<"$env_keys" || { no "compose.yaml uses \${$k}, which .env.example does not define"; envfail=1; }
done <<<"$compose_keys"
while read -r k; do
  [[ -n "$k" ]] || continue
  grep -qxF "$k" <<<"$compose_keys" || { no ".env.example defines $k, which compose.yaml never uses"; envfail=1; }
done <<<"$env_keys"
# Each setting is documented, with the default the example holds.
while read -r k; do
  [[ -n "$k" ]] || continue
  default="$(sed -n "s/^$k=//p" .env.example | head -1)"
  row="$(grep -E "^\|.*\`$k\`" docs/setup.md | head -1)"
  if [[ -z "$row" ]]; then no "docs/setup.md has no settings row for $k"; envfail=1
  elif ! grep -qF -- "\`$default\`" <<<"$row"; then no "docs/setup.md row for $k does not show the default \`$default\`"; envfail=1; fi
done <<<"$env_keys"
(( envfail )) || ok "compose.yaml, .env.example and the docs agree on every setting and default"

echo
echo "== every image follows :latest (the nightly updater keeps it current) =="
imgfail=0
while read -r image; do
  tag="${image##*:}"
  if [[ "$image" != *:* || "$tag" == */* ]]; then no "image has no tag: $image"; imgfail=1
  elif [[ "$tag" != latest ]]; then no "image is pinned to $tag, not :latest: $image"; imgfail=1; fi
done < <(sed -nE 's/^[[:space:]]*image:[[:space:]]*([^[:space:]#]+).*/\1/p' compose.yaml)
grep -qE '^[[:space:]]*image:' compose.yaml || { no "no image found in compose.yaml"; imgfail=1; }
(( imgfail )) || ok "every image in compose.yaml follows :latest"

echo
echo "== services and secrets in compose.yaml are documented =="
svcfail=0
while read -r svc; do
  grep -qF -- "\`$svc\`" docs/index.md || { no "docs/index.md never names the service \`$svc\`"; svcfail=1; }
done < <(awk '/^services:/ { on = 1; next } on && /^[^ #]/ { on = 0 } on && /^  [a-z][a-z0-9-]*:/ { sub(/^  /, ""); sub(/:.*/, ""); print }' compose.yaml)
while read -r secret; do
  grep -qF -- "$secret" docs/secrets.md || { no "compose.yaml reads secrets/$secret, which docs/secrets.md never names"; svcfail=1; }
  grep -qF -- "$secret" install.sh || { no "compose.yaml reads secrets/$secret, which install.sh never checks"; svcfail=1; }
done < <(grep -oE 'secrets/[A-Za-z0-9_.-]+' compose.yaml | sed 's#secrets/##' | sort -u)
(( svcfail )) || ok "every service and secret file is named in the docs"

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
  < <(grep -nP "$DASH_RE" "${SURFACES[@]}" mkdocs.yml compose.yaml install.sh .env.example tests/*.sh || true)
(( textfail )) || ok "no hardcoded test counts, no em dashes"

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
