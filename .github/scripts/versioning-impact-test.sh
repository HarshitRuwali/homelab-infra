#!/usr/bin/env bash
# Self-test for versioning-impact.sh against throwaway git repos: modules below
# and above 1.0.0, one never released, per-package overrides, and a table of
# titles and changed paths with the report each must produce. It also covers
# the argument handling, renames and file names that are not UTF-8, malformed
# configs (exit 2), and hostile text in the title and the config, none of which
# may reach the output as a workflow command.
#
# Run from anywhere: .github/scripts/versioning-impact-test.sh

# The backticks in the expected strings are literal Markdown, not substitutions.
# shellcheck disable=SC2016
set -u

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
failures=0

# Keep the user's own git config (signing, hooks) out of the fixtures, and the
# runner's step summary file out of the script under test.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
unset GITHUB_STEP_SUMMARY

fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

has() { [[ $2 == *"$3"* ]] || fail "$1: expected '$3' in: $2"; }
lacks() { [[ $2 != *"$3"* ]] || fail "$1: did not expect '$3' in: $2"; }

# init_repo <dir> <manifest-json>   (the config is read from stdin)
init_repo() {
  local dir=$1
  mkdir -p "$dir/.github/scripts"
  cp "$here/versioning-impact.sh" "$dir/.github/scripts/"
  cat >"$dir/release-please-config.json"
  printf '%s\n' "$2" >"$dir/.release-please-manifest.json"
  echo base >"$dir/README.md"
  git -C "$dir" init -q -b main
  git -C "$dir" config user.email test@example.invalid
  git -C "$dir" config user.name test
  git -C "$dir" add -A
  git -C "$dir" commit -q -m base
}

# run_case <name> <title> <path>...
# Commits the paths on a branch off main of $repo, runs the report on it, and
# leaves status, out (stdout, the annotations) and sum (the step summary) behind.
run_case() {
  local name=$1 title=$2 file
  shift 2
  git -C "$repo" checkout -q -B pr main
  for file in "$@"; do
    mkdir -p "$repo/$(dirname "$file")"
    echo "$name" >>"$repo/$file"
  done
  git -C "$repo" add -A
  git -C "$repo" commit -q -m "case $name"
  report "$name" "$title"
}

# report <name> <title>: run the report on main..pr of $repo.
report() {
  : >"$tmp/summary"
  out=$(GITHUB_STEP_SUMMARY="$tmp/summary" "$repo/.github/scripts/versioning-impact.sh" \
    --title "$2" main pr 2>"$tmp/err")
  status=$?
  sum=$(<"$tmp/summary")
  [[ $status -eq 0 ]] || fail "$1: exit $status, stderr: $(<"$tmp/err")"
}

# ---------------------------------------------------------------- main fixture
repo=$tmp/repo
mkdir -p "$repo/alpha"
seq 1 40 >"$repo/alpha/big.txt" # long enough for git to see a rename
init_repo "$repo" '{ "alpha": "0.3.0", "beta": "1.2.0", "delta": "0.4.0", "epsilon": "0.2.0", "zeta": "1.0.0" }' <<'EOF'
{
  "bump-minor-pre-major": true,
  "bump-patch-for-minor-pre-major": false,
  "changelog-sections": [
    { "type": "feat", "section": "Features" },
    { "type": "fix", "section": "Bug Fixes" },
    { "type": "docs", "section": "Documentation" },
    { "type": "ci", "section": "CI", "hidden": true },
    { "type": "chore", "section": "Chores", "hidden": true }
  ],
  "packages": {
    "alpha": { "component": "alpha", "initial-version": "0.1.0" },
    "beta": { "component": "beta", "initial-version": "0.1.0" },
    "gamma": { "component": "gamma", "initial-version": "0.1.0" },
    "delta": { "component": "delta", "initial-version": "0.1.0", "bump-patch-for-minor-pre-major": true },
    "epsilon": { "component": "epsilon", "initial-version": "0.1.0", "bump-minor-pre-major": false },
    "zeta": {
      "component": "zeta",
      "initial-version": "0.1.0",
      "changelog-sections": [
        { "type": "feat", "section": "Features" },
        { "type": "fix", "section": "Bug Fixes" },
        { "type": "docs", "section": "Documentation", "hidden": true },
        { "type": "ci", "section": "CI", "hidden": true }
      ]
    }
  }
}
EOF

run_case feat-below-1 'feat: add a thing' alpha/a.txt
has feat-below-1 "$sum" '| `alpha` | minor |'
lacks feat-below-1 "$sum" 'beta'
lacks feat-below-1 "$out" '::warning::'

run_case fix-scoped 'fix(beta): repair it' beta/b.txt
has fix-scoped "$sum" '| `beta` | patch |'

run_case first-release 'feat(gamma): start' gamma/c.txt
has first-release "$sum" '| `gamma` | first release at 0.1.0 |'

run_case breaking-below-1 'feat(alpha)!: drop it' alpha/a.txt
has breaking-below-1 "$sum" '| `alpha` | minor (breaking, below 1.0.0) |'

run_case breaking-above-1 'feat(beta)!: drop it' beta/b.txt
has breaking-above-1 "$sum" '| `beta` | major |'

# Per-package overrides of the bump settings.
run_case patch-for-feat 'feat(delta): x' delta/d.txt
has patch-for-feat "$sum" '| `delta` | patch (feat, below 1.0.0) |'

run_case major-pre-1 'feat(epsilon)!: x' epsilon/e.txt
has major-pre-1 "$sum" '| `epsilon` | major |'

# A package's own changelog-sections replace the top-level list: docs ships for
# alpha and is hidden for zeta.
run_case package-sections-visible 'docs(alpha): x' alpha/a.txt
has package-sections-visible "$sum" '| `alpha` | patch |'
run_case package-sections-hidden 'docs(zeta): x' zeta/z.txt
has package-sections-hidden "$sum" '| `zeta` | none (type `docs` is hidden) |'
has package-sections-hidden "$out" "::warning::zeta change under the hidden type 'docs'"

run_case hidden-type 'ci: tweak' alpha/a.txt
has hidden-type "$sum" '| `alpha` | none (type `ci` is hidden) |'
has hidden-type "$out" "::warning::alpha change under the hidden type 'ci'"

run_case hidden-but-breaking 'chore!: drop it' alpha/a.txt
has hidden-but-breaking "$sum" '| `alpha` | minor (breaking, below 1.0.0) |'
lacks hidden-but-breaking "$out" '::warning::'

run_case scope-mismatch 'feat(beta): x' alpha/a.txt
has scope-mismatch "$out" "title scope 'beta' names a module this PR does not touch"

run_case root-only 'docs: wording' README.md
has root-only "$sum" 'touches no module, so it releases nothing'
lacks root-only "$out" '::warning::'

run_case two-modules 'fix: x' alpha/a.txt beta/b.txt
has two-modules "$sum" '| `alpha` | patch |'
has two-modules "$sum" '| `beta` | patch |'

run_case not-conventional 'Add stuff' alpha/a.txt
has not-conventional "$sum" '| `alpha` | none (the title is not a conventional commit) |'
has not-conventional "$out" 'not a conventional commit'

# A lower-case type nobody configured is not called "hidden".
run_case unknown-type 'wip: x' alpha/a.txt
has unknown-type "$sum" '| `alpha` | none (the title is not a conventional commit) |'
has unknown-type "$out" "The type 'wip' is not one of"
lacks unknown-type "$out" 'hidden type'

run_case bare-revert 'Revert "feat: x"' alpha/a.txt
has bare-revert "$out" "Retitle it 'revert: <subject>'"

# release-please reads a renamed file by its new path only.
git -C "$repo" checkout -q -B pr main
mkdir -p "$repo/beta"
git -C "$repo" mv alpha/big.txt beta/big.txt
git -C "$repo" commit -q -m "case rename"
report rename 'fix(beta): move the file'
has rename "$sum" '| `beta` | patch |'
lacks rename "$sum" 'alpha'

# A file name that is not UTF-8 (skipped where the filesystem refuses it).
if touch "$tmp/probe"$'\xff' 2>/dev/null; then
  run_case non-utf8-name 'fix: x' alpha/bad$'\xff'name
  has non-utf8-name "$sum" '| `alpha` | patch |'
fi

# A title is text from whoever opened the PR: a second line must not become a
# workflow command.
run_case title-newline $'feat: x\n::error::boom' alpha/a.txt
lacks title-newline "$out" '::error::'
lacks title-newline "$sum" '::error::'

# Without GITHUB_STEP_SUMMARY the report goes to stdout.
direct=$(env -u GITHUB_STEP_SUMMARY "$repo/.github/scripts/versioning-impact.sh" --title 'feat: x' main pr 2>&1)
has stdout-fallback "$direct" '### Versioning impact'

# Argument handling: each of these must exit 2.
script="$repo/.github/scripts/versioning-impact.sh"
expect_exit_2() {
  local label=$1
  shift
  "$script" "$@" >/dev/null 2>&1
  local code=$?
  [[ $code -eq 2 ]] || fail "$label: exit $code, want 2"
}
expect_exit_2 'no arguments'
expect_exit_2 'missing head' --title 'feat: x' main
expect_exit_2 'wrong flag' --subject 'feat: x' main pr
expect_exit_2 'unknown base' --title 'feat: x' no-such-ref pr
expect_exit_2 'base that looks like a flag' --title 'feat: x' --stat pr

# ------------------------------------------------- malformed config: exit 2
bad_config() { # <label> <config-json>
  local dir=$tmp/bad-$1
  printf '%s\n' "$2" | init_repo "$dir" '{}'
  git -C "$dir" checkout -q -b pr
  echo x >"$dir/x.txt"
  git -C "$dir" add -A
  git -C "$dir" commit -q -m pr
  (cd "$dir" && .github/scripts/versioning-impact.sh --title 'feat: x' main pr >/dev/null 2>"$tmp/err")
  local code=$?
  [[ $code -eq 2 ]] || fail "malformed config ($1): exit $code, want 2"
  [[ -s $tmp/err && $(<"$tmp/err") != *Traceback* ]] || fail "malformed config ($1): no clean error message: $(<"$tmp/err")"
}
bad_config array '[]'
bad_config package-not-object '{ "packages": { "alpha": "x" }, "changelog-sections": [{ "type": "feat" }] }'
bad_config sections-not-list '{ "packages": { "alpha": {} }, "changelog-sections": 5 }'
bad_config section-type-not-string '{ "packages": { "alpha": {} }, "changelog-sections": [{ "type": ["feat"] }] }'
bad_config no-sections '{ "packages": { "alpha": {} } }'

# --------------------------------------------- hostile values in the config
# Package names, versions and types are text from the PR too; a newline in one
# must not start a workflow command in the output or the summary.
repo=$tmp/hostile
mkdir -p "$repo/ev::error::boom"
init_repo "$repo" '{}' <<'EOF'
{
  "changelog-sections": [
    { "type": "feat" },
    { "type": "ci", "hidden": true },
    { "type": "zz\n::error::boom" }
  ],
  "packages": {
    "ev::error::boom": { "component": "x", "initial-version": "0.1.0" },
    "evil": { "component": "evil", "initial-version": "9.9.9\n::error::boom" }
  }
}
EOF

no_commands() { # <label>: only ::warning:: on stdout, no command in the summary
  local label=$1 line
  while IFS= read -r line; do
    [[ -z $line || $line == ::warning::* ]] || fail "$label: stray output line: $line"
  done <<<"$out"
  while IFS= read -r line; do
    [[ $line != ::* ]] || fail "$label: a summary line starts a command: $line"
  done <<<"$sum"
  lacks "$label" "$out" '::error::'
}

run_case hostile-hidden 'ci: x' evil/f.txt 'ev::error::boom/f.txt'
no_commands hostile-hidden
has hostile-hidden "$out" '::warning::'

run_case hostile-first-release 'feat: x' evil/f.txt
no_commands hostile-first-release
has hostile-first-release "$sum" 'first release at 9.9.9'

if [[ $failures -ne 0 ]]; then
  echo "versioning-impact-test: $failures failure(s)" >&2
  exit 1
fi
echo "versioning-impact-test: ok"
