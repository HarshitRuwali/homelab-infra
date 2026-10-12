#!/usr/bin/env bash
# Checks that a pull request title, and the commits on its branch, follow
# Conventional Commits. release-please reads these to decide which modules get a
# release, what the next version is, and what goes in each CHANGELOG.md.
#
#   conventional-commits.sh --title "<subject>"
#       Exit 0 if the subject matches, otherwise explain the format and exit 1.
#       This is the hard check: a squash merge uses the PR title as the commit
#       subject on master.
#
#   conventional-commits.sh --commits <base> <head>
#       Warn (GitHub ::warning:: lines) about each non-merge commit in
#       base..head whose subject does not match. Always exits 0: with a squash
#       merge the branch commits never reach master.
#
# Exit status: 0 ok, 1 title rejected, 2 bad arguments or an unreadable range.
# The accepted format is documented in CONTRIBUTING.md.

set -u

# type(scope)!: description. The scope is optional and lowercase, the "!" marks
# a breaking change, and the description must not start with a space.
# GitHub's revert button writes 'Revert "<original subject>"'; that is allowed.
readonly PATTERN='^(feat|fix|perf|refactor|docs|test|build|ci|chore|style|revert)(\([a-z0-9][a-z0-9._/-]*\))?!?: [^ ].*'
readonly REVERT_PATTERN='^Revert "'

usage() {
  cat >&2 <<'EOF'
usage: conventional-commits.sh --title "<subject>"
       conventional-commits.sh --commits <base> <head>
EOF
  exit 2
}

is_conventional() {
  [[ $1 =~ $PATTERN || $1 =~ $REVERT_PATTERN ]]
}

# Subjects come from a public contributor. Drop control characters so a subject
# cannot start a second line of output, and escape "%" the way GitHub's
# workflow-command syntax expects.
sanitize() {
  local s
  s=$(printf '%s' "$1" | tr -d '\000-\037\177')
  printf '%s' "${s//%/%25}"
}

check_title() {
  if is_conventional "$1"; then
    return 0
  fi
  printf 'PR title "%s" is not a conventional commit. Use "type(scope): description", for example "feat(monitoring): add a disk alert". Types: feat, fix, perf, refactor, docs, test, build, ci, chore, style, revert. Scope: lowercase module name, optional. Add "!" before the colon for a breaking change.\n' \
    "$(sanitize "$1")" >&2
  return 1
}

check_commits() {
  local base=$1 head=$2 shas sha subject
  if ! shas=$(git rev-list --no-merges --end-of-options "$base..$head" 2>&1); then
    printf 'conventional-commits.sh: cannot list commits in %s..%s: %s\n' \
      "$(sanitize "$base")" "$(sanitize "$head")" "$(sanitize "$shas")" >&2
    return 2
  fi
  while IFS= read -r sha; do
    [[ -n $sha ]] || continue
    subject=$(git log -1 --format=%s "$sha")
    if ! is_conventional "$subject"; then
      printf '::warning::Commit %s "%s" is not a conventional commit, so it will not appear in the changelog unless the PR is squash-merged with a conventional title.\n' \
        "${sha:0:7}" "$(sanitize "$subject")"
    fi
  done <<<"$shas"
  return 0
}

case "${1:-}" in
  --title)
    [[ $# -eq 2 ]] || usage
    check_title "$2"
    ;;
  --commits)
    [[ $# -eq 3 ]] || usage
    check_commits "$2" "$3"
    ;;
  *)
    usage
    ;;
esac
