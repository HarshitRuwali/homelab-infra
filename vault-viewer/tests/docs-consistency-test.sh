#!/usr/bin/env bash
#
# docs-consistency-test.sh - catch drift between the docs, the installer and
# the program. Every command shown to a user must exist, accept the flags
# shown, and be referred to by a path that is really there.
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

# Flags of other tools that the docs legitimately show (docker run, docker
# build, tailscale whois, systemctl, and argparse's built-in --help).
OTHER_FLAGS=(--network --read-only --tmpfs --user --cap-drop --security-opt
             --memory --pids-limit --pull --json --now --help)
is_other_flag() { printf '%s\n' "${OTHER_FLAGS[@]}" | grep -qxF -- "$1"; }

in_install_parser()   { grep -qE -e "(^|[|[:space:]])$1([|)])" install.sh; }
in_artifacts_parser() { grep -qE -e "add_argument\(\"$1\"" artifacts.py; }

# One logical command per line: join backslash continuations, and join the
# indented continuation lines of a usage block to the line that opened it.
logical_lines() {
  awk '
    /^```/ { infence = !infence; prev = ""; next }
    {
      line = $0
      if (cont) { sub(/^[ \t]+/, "", line); buf = buf " " line }
      else if (infence && buf != "" && line ~ /^[ \t]+/ && buf ~ /(install\.sh|artifacts)/) {
        sub(/^[ \t]+/, "", line); buf = buf " " line
      } else { if (buf != "") print buf; buf = line }
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

for need in install.sh artifacts.py mkdocs.yml; do
  [[ -f "$need" ]] || { echo "missing $need, cannot check"; exit 1; }
done

# check_flags <label> <file>: every flag shown on a line with install.sh is in
# install.sh's parser; every flag shown on a line with artifacts or artifacts.py
# is in artifacts.py's argparse (or is argparse's own --help). Flags of other
# tools (OTHER_FLAGS) are not accepted after either command.
flagfail=0
check_flags() {
  local label="$1" file="$2" line tok cmd flag
  while IFS= read -r line; do
    cmd=""
    for tok in $line; do
      tok="${tok//\`/}"
      case "$tok" in
        install.sh|*/install.sh)                  cmd=install;   break ;;
        artifacts|artifacts.py|*/artifacts|*/artifacts.py) cmd=artifacts; break ;;
      esac
    done
    [[ -n "$cmd" ]] || continue
    for tok in $line; do
      [[ "$tok" == *--?* ]] || continue
      flag="$(grep -oE -- '--[a-z][a-z-]*' <<<"$tok" | head -1)"
      [[ -n "$flag" ]] || continue
      case "$cmd" in
        install)   in_install_parser   "$flag" || { no "$label shows 'install.sh $flag' but install.sh has no such flag"; flagfail=1; } ;;
        artifacts) in_artifacts_parser "$flag" || [[ "$flag" == --help ]] \
                     || { no "$label shows 'artifacts $flag' but artifacts.py has no such flag"; flagfail=1; } ;;
      esac
    done
  done < <(logical_lines "$file")
}

echo "== flags shown with a command are accepted by that command =="
for f in "${SURFACES[@]}"; do check_flags "$f" "$f"; done

# The root README carries a vault-viewer snippet too; check just that block.
ROOT_README="../README.md"
snippet="$(mktemp)"; trap 'rm -f "$snippet"' EXIT
if [[ -f "$ROOT_README" ]]; then
  awk '/<summary>.*Serve a vault in the browser/ { on = 1 } on { print } on && /<\/details>/ { exit }' "$ROOT_README" > "$snippet"
  if grep -qF 'install.sh' "$snippet"; then
    check_flags "root README.md (vault-viewer snippet)" "$snippet"
  else
    no "root README.md has no 'Serve a vault in the browser' snippet showing install.sh"; flagfail=1
  fi
fi
(( flagfail )) || ok "every flag shown with install.sh or artifacts is in its parser"

echo
echo "== every --flag in the docs belongs to some tool we know =="
anyfail=0
while read -r flag; do
  in_install_parser "$flag" && continue
  in_artifacts_parser "$flag" && continue
  is_other_flag "$flag" && continue
  no "docs mention '$flag', which is in neither parser nor the known-other-tools list"; anyfail=1
done < <(grep -ohE -- '--[a-z][a-z-]*' "${SURFACES[@]}" | sort -u)
(( anyfail )) || ok "no unknown flag appears anywhere in README or docs"

echo
echo "== no references to files that do not exist =="
pathfail=0
while read -r path; do
  [[ "$path" == */ ]] && continue
  [[ -e "$path" ]] || { no "referenced but missing: $path"; pathfail=1; }
done < <(grep -ohE '(^|[^/[:alnum:]_.~$}-])(artifacts\.py|install\.sh|mkdocs\.yml|(systemd|tests|docs)/[A-Za-z0-9._/-]+)' "${SURFACES[@]}" \
         | sed -E 's/^[^a-z]//; s/[.,)]+$//' | sort -u)
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
echo "== the PDF image name matches the code =="
imgfail=0
code_image="$(sed -nE 's/^PDF_IMAGE = "([^"]+)".*/\1/p' artifacts.py)"
inst_image="$(sed -nE 's/^PDF_IMAGE="([^"]+)".*/\1/p' install.sh)"
[[ -n "$code_image" ]] || { no "could not read PDF_IMAGE from artifacts.py"; imgfail=1; }
[[ "$inst_image" == "$code_image" ]] || { no "install.sh PDF_IMAGE '$inst_image' != artifacts.py '$code_image'"; imgfail=1; }
grep -qF -- "$code_image" docs/pdf-export.md || { no "docs/pdf-export.md does not show '$code_image'"; imgfail=1; }
while read -r name; do
  [[ "$name" == "$code_image" ]] || { no "docs show image '$name', code uses '$code_image'"; imgfail=1; }
done < <(grep -ohE 'artifacts-pdf:[A-Za-z0-9._-]+' "${SURFACES[@]}" | sort -u)
(( imgfail )) || ok "docs, install.sh and artifacts.py agree on '$code_image'"

echo
echo "== defaults shown in the docs match the code =="
deffail=0
code_port="$(sed -nE 's/.*"ARTIFACTS_PORT", ([0-9]+)\).*/\1/p' artifacts.py)"
inst_port="$(sed -nE 's/^PORT=([0-9]+).*/\1/p' install.sh)"
doc_port="$(grep -E '^\| `--port N`' docs/index.md | grep -oE 'Defaults to [0-9]+' | grep -oE '[0-9]+')"
[[ -n "$code_port" && "$code_port" == "$inst_port" && "$code_port" == "$doc_port" ]] \
  || { no "port default differs: artifacts.py='$code_port' install.sh='$inst_port' docs='$doc_port'"; deffail=1; }
unit_name="$(sed -nE 's/^UNIT_NAME="([^"]+)".*/\1/p' install.sh)"
[[ -f "systemd/$unit_name" ]] || { no "install.sh UNIT_NAME '$unit_name' has no file in systemd/"; deffail=1; }
grep -qF -- "$unit_name" docs/operations.md || { no "docs/operations.md never names $unit_name"; deffail=1; }
bin_default="$(sed -nE 's/^BIN="\$\{ARTIFACTS_BIN:-([^}]+)\}".*/\1/p' install.sh)"
grep -qF -- 'ARTIFACTS_BIN:-$HOME/.local/bin/artifacts' docs/index.md && [[ "$bin_default" == '$HOME/.local/bin/artifacts' ]] \
  || { no "install path default in docs/index.md differs from install.sh ('$bin_default')"; deffail=1; }
(( deffail )) || ok "port, unit name and install path defaults agree"

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
  < <(grep -nP "$DASH_RE" "${SURFACES[@]}" mkdocs.yml tests/docs-consistency-test.sh || true)
(( textfail )) || ok "no hardcoded test counts, no em dashes"

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
