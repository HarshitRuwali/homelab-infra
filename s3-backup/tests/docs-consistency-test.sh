#!/usr/bin/env bash
#
# docs-consistency-test.sh - catch drift between the docs, the scripts and the
# installer's output. Every command shown to a user must exist, accept the
# flags shown, and be referred to by a path that is really there.
#
#   ./tests/docs-consistency-test.sh
#
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1

PASS=0; FAIL=0
ok() { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
no() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

# Globbed, not listed: a doc added later is checked automatically.
SURFACES=(README.md install.sh config/backup.env.example docs/*.md)

mapfile -t COMMANDS < <(find bin -maxdepth 1 -type f -name 's3-backup*' -printf '%f\n' | sort)
is_command() { printf '%s\n' "${COMMANDS[@]}" | grep -qxF -- "$1"; }

# s3-backup-* names that are deliberately not commands.
NOT_COMMANDS=(
  s3-backup-automation   # the repository
  s3-backup-runner       # the container image
  s3-backup-drill        # the systemd unit basename
  s3-backup-canary       # the canary marker filename
  s3-backup-homelab      # the S3 IAM user
  s3-backup-bootstrap    # the bootstrap IAM user
  s3-backup-setup        # tmpdir prefix under /run
)
is_known_noncommand() { printf '%s\n' "${NOT_COMMANDS[@]}" | grep -qxF -- "$1"; }

# Join backslash continuations so a wrapped command reads as a single line.
flatten() { sed -e ':a' -e '/\\$/{N;s/\\\n//;ta' -e '}' "$1"; }

# The user-facing part of install.sh, not its implementation.
installer_output() { sed -n '/^cat <<NEXT/,/^NEXT$/p' install.sh; }

echo "== every documented command exists =="
unknown=0
for f in "${SURFACES[@]}"; do
  while read -r tok; do
    case "$tok" in *.service|*.timer|*.prom) continue ;; esac
    is_command "$tok" && continue
    is_known_noncommand "$tok" && continue
    no "$f names '$tok', which is not a command in bin/"; unknown=1
  done < <(grep -ohE '\bs3-backup[a-z-]*\b' "$f" | sort -u)
done
(( unknown )) || ok "every s3-backup* name in the docs is a real command"

echo
echo "== every documented flag is accepted by that command =="
flagfail=0
for f in "${SURFACES[@]}"; do
  while IFS= read -r line; do
    cmd=""
    for tok in $line; do is_command "$tok" && { cmd="$tok"; break; }; done
    [[ -n "$cmd" ]] || continue
    for tok in $line; do
      [[ "$tok" == --?* ]] || continue
      flag="${tok%%=*}"; flag="${flag%%,}"; flag="${flag%%.}"; flag="${flag%%\`}"
      [[ "$flag" =~ ^--[a-z][a-z-]*$ ]] || continue
      if ! grep -qE -e "(^|\||[[:space:]])${flag}\)" "bin/$cmd"; then
        no "$f shows '$cmd $flag' but bin/$cmd has no such flag"; flagfail=1
      fi
    done
  done < <(flatten "$f")
done
(( flagfail )) || ok "every flag shown with an s3-backup* command is in its parser"

echo
echo "== install.sh flags shown in the docs exist =="
instfail=0
while read -r flag; do
  [[ "$flag" =~ ^--[a-z][a-z-]*$ ]] || continue
  if ! grep -qE -e "(^|\||[[:space:]])${flag}\)" install.sh; then
    no "docs show 'install.sh $flag' but install.sh has no such flag"; instfail=1
  fi
done < <(grep -ohE 'install\.sh +--[a-z-]+' "${SURFACES[@]}" | grep -ohE -- '--[a-z-]+' | sort -u)
(( instfail )) || ok "every install.sh flag shown in the docs is in its parser"

echo
echo "== no references to files that do not exist =="
pathfail=0
while read -r path; do
  # Skip absolute-path fragments (/etc/systemd/system/) and shebang tails.
  [[ "$path" == bin/env ]] && continue
  [[ "$path" == */ ]] && continue
  [[ -e "$path" ]] || { no "referenced but missing: $path"; pathfail=1; }
done < <(grep -ohE '(^|[^/[:alnum:]])(aws|bin|docs|docker|systemd|config|tests)/[A-Za-z0-9._/-]+' "${SURFACES[@]}" \
         | sed -E 's/^[^a-z]*//; s/[.,)]+$//' | sort -u)
(( pathfail )) || ok "every repo path named in the docs exists"

echo
echo "== markdown links and anchors resolve =="
linkfail=0
slug() { tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9 -]//g; s/ /-/g'; }
for f in README.md docs/*.md; do
  dir="$(dirname "$f")"
  while read -r target; do
    [[ "$target" =~ ^https?: ]] && continue
    [[ "$target" =~ ^\# ]] && { file=""; anchor="${target#\#}"; } || {
      file="${target%%#*}"
      if [[ "$target" == *#* ]]; then anchor="${target#*#}"; else anchor=""; fi
    }
    if [[ -n "$file" ]]; then
      [[ -e "$dir/$file" ]] || { no "$f: broken link -> $file"; linkfail=1; continue; }
      tgt="$dir/$file"
    else
      tgt="$f"
    fi
    if [[ -n "$anchor" ]]; then
      if ! grep -E '^#{1,6} ' "$tgt" | sed -E 's/^#+ //' | slug | grep -qxF -- "$anchor"; then
        no "$f: broken anchor -> ${file}#${anchor}"; linkfail=1
      fi
    fi
  done < <(grep -ohE '\]\([^)]+\)' "$f" | sed -E 's/^\]\(//; s/\)$//')
done
(( linkfail )) || ok "every relative link and anchor resolves"

echo
echo "== every config key the code reads is in the template =="
keyfail=0
while read -r key; do
  grep -qE -e "^#? ?${key}=" config/backup.env.example \
    || { no "config/backup.env.example does not document $key"; keyfail=1; }
done < <(grep -ohE '\$\{[A-Z][A-Z0-9_]+:=' bin/lib/common.sh | grep -ohE '[A-Z][A-Z0-9_]+' | sort -u)
(( keyfail )) || ok "every key with a default in common.sh appears in the template"

echo
echo "== no counts restated in prose =="
# A number that lives in tool output drifts the moment a test is added.
if grep -rnE '[0-9]+ (assertion|check|test)s?\b' README.md docs/*.md >/dev/null 2>&1; then
  grep -rnE '[0-9]+ (assertion|check|test)s?\b' README.md docs/*.md | while read -r hit; do
    no "hardcoded count in prose: $hit"
  done
else
  ok "no hardcoded test counts in README or docs"
fi

echo
echo "== retired commands leave a working pointer =="
tombfail=0
while read -r t; do
  grep -q 'REMOVED' "$t" || { no "$t exists but is not marked REMOVED"; tombfail=1; continue; }
  repl="$(grep -oE 's3-backup-[a-z-]+' "$t" | head -1)"
  [[ -n "$repl" && -x "bin/$repl" ]] \
    || { no "$t does not name an existing replacement"; tombfail=1; }
done < <(grep -rlE '^# REMOVED' aws/*.sh 2>/dev/null)
(( tombfail )) || ok "every tombstone names a command that exists"

echo
echo "== the canonical step order matches everywhere =="
STEPS='install\.sh|s3-backup-setup-aws|install-canaries|preflight|systemctl start s3-backup|restore-drill'
want="install.sh s3-backup-setup-aws install-canaries preflight systemctl start s3-backup restore-drill"
check_order() { # check_order <label> <text-producing command>
  local label="$1"; shift
  local got; got="$("$@" | grep -ohE "$STEPS" | awk '!seen[$0]++' | paste -sd' ')"
  # Every step the surface mentions must appear in the canonical order, in that
  # relative order. A surface may legitimately omit steps (the installer does
  # not tell you to run install.sh again).
  local remaining="$want" step
  for step in $got; do
    case " $remaining " in
      *" $step "*) remaining="${remaining#*$step}" ;;
      *) no "$label lists '$step' out of canonical order (got '$got')"; return ;;
    esac
  done
  ok "$label is consistent with the canonical order ($got)"
}
check_order "installer output" installer_output
check_order "docs/setup.md"    cat docs/setup.md

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
