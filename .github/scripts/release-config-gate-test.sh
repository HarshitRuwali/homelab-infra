#!/usr/bin/env bash
# Self-test for the registration gate in release-config-test.sh. A copy of the
# repository is changed the way a pull request that adds a module would change
# it, and the config test must reject every incomplete version of that change
# (directory without a package, package without a README row, row without a
# package, package without a directory) and accept the complete one.
#
# Without this, the gate has only ever been seen green on the real repository.
#
# Needs git, tar and python3. Run from anywhere:
#   .github/scripts/release-config-gate-test.sh

set -u

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
root=$(cd "$here/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
failures=0

fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

# A copy of every tracked and not-ignored file, so a local run tests the
# working tree and CI tests the checkout.
mkdir "$tmp/base"
(cd "$root" && git ls-files -z --cached --others --exclude-standard | tar --null -T - -cf -) |
  tar -xf - -C "$tmp/base" 2>/dev/null
if [[ ! -f "$tmp/base/.github/scripts/release-config-test.sh" ]]; then
  echo "release-config-gate-test: cannot copy the repository (is $root a git checkout?)" >&2
  exit 1
fi

# edit package|row <dir> <module>
edit() {
  python3 -I - "$@" <<'PY'
import json
import re
import sys
from pathlib import Path

cmd, case, name = sys.argv[1:4]
root = Path(case)
if cmd == "package":
    path = root / "release-please-config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["packages"][name] = {"component": name, "initial-version": "0.1.0"}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
elif cmd == "row":
    path = root / "README.md"
    text = path.read_text(encoding="utf-8")
    section = re.search(r"^## Modules\n(.*?)(?=^## )", text, re.S | re.M)
    lines = section.group(1).split("\n")
    last = max(i for i, line in enumerate(lines) if line.startswith("|"))
    lines.insert(last + 1, f"| | [**`{name}/`**]({name}/README.md) | A new module. | [README]({name}/README.md) |")
    path.write_text(text[: section.start(1)] + "\n".join(lines) + text[section.end(1):], encoding="utf-8")
else:
    sys.exit(f"unknown edit {cmd}")
PY
}

# case_dir <name>: a fresh copy of the repository.
case_dir() {
  cp -a "$tmp/base" "$tmp/$1"
  echo "$tmp/$1"
}

# run_gate <dir>: leaves status and err behind.
run_gate() {
  err=$(bash "$1/.github/scripts/release-config-test.sh" 2>&1 >/dev/null)
  status=$?
}

expect_rejected() { # <label> <needle>
  [[ $status -eq 1 ]] || fail "$1: exit $status, want 1"
  [[ $err == *"$2"* ]] || fail "$1: expected '$2' in: $err"
}

dir=$(case_dir baseline)
run_gate "$dir"
[[ $status -eq 0 ]] || fail "baseline: the repository itself is rejected: $err"

dir=$(case_dir dir-only)
mkdir "$dir/newmod" && echo "# newmod" >"$dir/newmod/README.md"
run_gate "$dir"
expect_rejected dir-only "directory 'newmod/' has a README.md but is not a package"

dir=$(case_dir dir-and-package)
mkdir "$dir/newmod" && echo "# newmod" >"$dir/newmod/README.md"
edit package "$dir" newmod
run_gate "$dir"
expect_rejected dir-and-package "configures 'newmod' but the README.md Modules table does not link it"
[[ $err != *"is not a package"* ]] || fail "dir-and-package: still reports the module as unregistered"

dir=$(case_dir dir-and-row)
mkdir "$dir/newmod" && echo "# newmod" >"$dir/newmod/README.md"
edit row "$dir" newmod
run_gate "$dir"
expect_rejected dir-and-row "README.md Modules table links 'newmod/' but release-please-config.json does not configure it"

dir=$(case_dir package-only)
edit package "$dir" ghost
run_gate "$dir"
expect_rejected package-only "configures 'ghost' but there is no top-level 'ghost/' directory"

dir=$(case_dir complete)
mkdir "$dir/newmod" && echo "# newmod" >"$dir/newmod/README.md"
edit package "$dir" newmod
edit row "$dir" newmod
run_gate "$dir"
[[ $status -eq 0 ]] || fail "complete: a fully registered module is rejected: $err"

# A directory without a README.md is not a module and needs no package.
dir=$(case_dir not-a-module)
mkdir "$dir/scratch" && echo x >"$dir/scratch/file.txt"
run_gate "$dir"
[[ $status -eq 0 ]] || fail "not-a-module: a directory without a README.md is rejected: $err"

if [[ $failures -ne 0 ]]; then
  echo "release-config-gate-test: $failures failure(s)" >&2
  exit 1
fi
echo "release-config-gate-test: ok"
