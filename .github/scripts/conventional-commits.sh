#!/usr/bin/env bash
# Checks that a pull request title, and the commits on its branch, follow
# Conventional Commits. release-please reads these to decide which modules get a
# release, what the next version is, and what goes in its release notes.
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
#   conventional-commits.sh --message-file <file>
#       Exit 0 if the commit message in <file> starts with a matching subject,
#       otherwise explain the format and exit 1. This is what the commit-msg
#       hook in .pre-commit-config.yaml runs, so a commit is checked before it is
#       made, against the same rules as the PR title.
#
# Exit status: 0 ok, 1 subject rejected, 2 bad arguments or an unreadable input.
# The accepted format is documented in CONTRIBUTING.md.

set -u

# type(scope)!: description. The scope is optional, and when given it must be a
# module registered in release-please-config.json. The "!" marks a breaking
# change, and the description must not start with a space.
# GitHub's revert button writes 'Revert "<original subject>"'; that is allowed.
readonly PATTERN='^(feat|fix|perf|refactor|docs|test|build|ci|chore|style|revert)(\(([a-z0-9][a-z0-9._/-]*)\))?!?: [^ ].*'
readonly REVERT_PATTERN='^Revert "'
# Read from the repository this script lives in, not the current directory, so
# a new module is accepted as a scope as soon as its PR registers it.
CONFIG=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/release-please-config.json
readonly CONFIG

usage() {
  cat >&2 <<'EOF'
usage: conventional-commits.sh --title "<subject>"
       conventional-commits.sh --commits <base> <head>
       conventional-commits.sh --message-file <file>
EOF
  exit 2
}

MODULES=""
load_modules() {
  [[ -n $MODULES ]] && return 0
  if ! MODULES=$(python3 -I -c 'import json, sys; print("\n".join(json.load(open(sys.argv[1]))["packages"]))' "$CONFIG" 2>&1) \
     || [[ -z $MODULES ]]; then
    printf 'conventional-commits.sh: cannot read the modules from %s: %s\n' "$CONFIG" "$MODULES" >&2
    exit 2
  fi
}

is_conventional() {
  if [[ $1 =~ $REVERT_PATTERN ]]; then
    return 0
  fi
  [[ $1 =~ $PATTERN ]] || return 1
  local scope=${BASH_REMATCH[3]}
  [[ -z $scope ]] && return 0
  load_modules
  grep -qFx -- "$scope" <<<"$MODULES"
}

# The module names, on one line, for the rejection message.
module_list() {
  load_modules
  printf '%s' "${MODULES//$'\n'/, }"
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
  explain "$1" "PR title"
  return 1
}

explain() {
  printf '%s "%s" is not a conventional commit. Use "type(scope): description", for example "feat(monitoring): add a disk alert". Types: feat, fix, perf, refactor, docs, test, build, ci, chore, style, revert. Scope: optional, one of the modules (%s). Add "!" before the colon for a breaking change.\n' \
    "$2" "$(sanitize "$1")" "$(module_list)" >&2
}

# The subject of a commit message file: its first line that is not blank and not
# a "#" comment, which is how git writes the template. A merge, or a fixup!,
# squash! or amend! commit, is let through: the first is not a change of its own,
# and the others are folded into another commit before it is pushed.
check_message_file() {
  local file=$1 line subject=""
  if [[ ! -r $file ]]; then
    printf 'conventional-commits.sh: cannot read %s\n' "$(sanitize "$file")" >&2
    return 2
  fi
  while IFS= read -r line || [[ -n $line ]]; do
    [[ -z $line || $line == \#* ]] && continue
    subject=$line
    break
  done <"$file"
  if [[ $subject =~ ^(Merge\ |fixup!\ |squash!\ |amend!\ ) ]]; then
    return 0
  fi
  if is_conventional "$subject"; then
    return 0
  fi
  explain "$subject" "Commit subject"
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
      printf '::warning::Commit %s "%s" is not a conventional commit, so it will not appear in the release notes unless the PR is squash-merged with a conventional title.\n' \
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
  --message-file)
    [[ $# -eq 2 ]] || usage
    check_message_file "$2"
    ;;
  *)
    usage
    ;;
esac
