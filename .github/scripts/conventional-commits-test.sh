#!/usr/bin/env bash
# Self-test for conventional-commits.sh: a table of accepted and rejected
# titles, the argument handling, and --commits against a throwaway git repo
# (one good commit, one bad commit, one merge commit).
#
# Run from anywhere: .github/scripts/conventional-commits-test.sh

set -u

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
script="$here/conventional-commits.sh"
failures=0

fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

good_titles=(
  'feat: x'
  'fix(monitoring): y'
  'feat(ansible)!: z'
  'chore: release master'
  'chore(master): release 0.1.0'
  'Revert "feat: x"'
)

bad_titles=(
  'Feat: x'
  'feat:x'
  'feat(Ansible): x'
  'feat x'
  'Add Grafana alerting rules for hardware monitoring'
  'desk_appliance role: x'
  ''
)

for title in "${good_titles[@]}"; do
  if ! "$script" --title "$title" >/dev/null 2>&1; then
    fail "title should be accepted: '$title'"
  fi
done

for title in "${bad_titles[@]}"; do
  "$script" --title "$title" >/dev/null 2>&1
  status=$?
  if [[ $status -ne 1 ]]; then
    fail "title should be rejected with exit 1 (got $status): '$title'"
  fi
done

# A rejected title is explained in one line on stderr.
message=$("$script" --title 'Add things' 2>&1 >/dev/null)
if [[ $(printf '%s\n' "$message" | wc -l) -ne 1 || $message != *'type(scope): description'* ]]; then
  fail "a rejected title should print a one-line explanation of the format, got: '$message'"
fi

# A hostile title cannot start a GitHub workflow command on a line of its own.
message=$("$script" --title $'bad\n::error::injected' 2>&1 >/dev/null)
if [[ $(printf '%s\n' "$message" | wc -l) -ne 1 || $message == ::* ]]; then
  fail "a title with a newline must not produce a second output line, got: '$message'"
fi

# Bad arguments print usage and exit 2.
bad_args=(
  ''
  '--title'
  '--title a b'
  '--commits'
  '--commits onlyone'
  '--commits a b c'
  '--nope'
)
for args in "${bad_args[@]}"; do
  # shellcheck disable=SC2086  # word splitting of the table entry is intended
  "$script" $args >/dev/null 2>&1
  status=$?
  if [[ $status -ne 2 ]]; then
    fail "arguments '$args' should exit 2 (got $status)"
  fi
done

# --commits, against a real (temporary) repository.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
g() { git -C "$tmp" -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@"; }

g init -q -b main
g commit -q --allow-empty -m 'chore: base'
base=$(g rev-parse HEAD)
g commit -q --allow-empty -m 'fix(memory): a good commit'
g commit -q --allow-empty -m 'Add a bad commit'
g checkout -q -b side "$base"
g commit -q --allow-empty -m 'docs: a good commit on a side branch'
g checkout -q main
g merge -q --no-ff -m "Merge branch 'side'" side
head=$(g rev-parse HEAD)

output=$(cd "$tmp" && "$script" --commits "$base" "$head")
status=$?
if [[ $status -ne 0 ]]; then
  fail "--commits should always exit 0 for bad subjects (got $status)"
fi
warnings=$(printf '%s\n' "$output" | grep -c '^::warning::')
if [[ $warnings -ne 1 ]]; then
  fail "--commits should warn about exactly one commit (got $warnings): $output"
fi
if [[ $output != *'"Add a bad commit"'* ]]; then
  fail "--commits should name the bad commit, got: $output"
fi
if [[ $output == *"Merge branch"* || $output == *"a good commit"* ]]; then
  fail "--commits warned about a merge or a good commit: $output"
fi

# A range git cannot resolve is an error, not a silent pass.
(cd "$tmp" && "$script" --commits does-not-exist "$head") >/dev/null 2>&1
status=$?
if [[ $status -ne 2 ]]; then
  fail "--commits with an unknown base should exit 2 (got $status)"
fi

if [[ $failures -ne 0 ]]; then
  echo "conventional-commits-test: $failures check(s) failed" >&2
  exit 1
fi
echo "conventional-commits-test: ok"
