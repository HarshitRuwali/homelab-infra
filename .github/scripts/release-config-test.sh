#!/usr/bin/env bash
# Guards the release-please setup. The release PR that release-please opens is
# created with GITHUB_TOKEN, so no workflow runs on it and it shows no checks.
# This test is what stands in for them: it runs on every pull request and on
# master (checks.yml), so a hand edit or a forgotten module is caught before the
# release PR is built from it.
#
# What it asserts:
#   a. release-please-config.json and .release-please-manifest.json parse, and
#      every manifest key is a configured package
#   b. the configured packages are exactly the top-level directories that hold
#      a README.md (minus docs-landing) and exactly the modules linked in the
#      README "Modules" table
#   c. every package has component == its directory name and initial-version
#      0.1.0 (1.0.0 is the public release, cut on purpose with Release-As), and
#      gets release-type simple, include-component-in-tag and include-v-in-tag
#      true, separate-pull-requests false (the tag shape and the single grouped
#      release PR), skip-changelog false (the release PR edits each module's
#      CHANGELOG.md) and bump-minor-pre-major true (a breaking change
#      below 1.0.0 bumps the minor, so nothing reaches 1.0.0 by accident)
#   d. every extra-file exists (resolved from the package directory)
#   e. every version-bearing extra-file holds the package's current version:
#      the manifest version, or initial-version before the first release
#   f. every generic extra-file carries the x-release-please-version marker
#   g. the docs and workflows name only things that exist: release.yml points at
#      the config and manifest tested here and can be started by hand,
#      checks.yml runs every script in .github/scripts/, and
#      README.md and CONTRIBUTING.md name only existing .github/ paths, resolving
#      links and anchors, and flags that conventional-commits.sh and
#      versioning-impact.sh accept
#
# Needs python3 >= 3.11 (tomllib) and nothing else.
# Run from anywhere: .github/scripts/release-config-test.sh

set -u

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

exec python3 -I - "$root" <<'PY'
import json
import re
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit("release-config-test: python >= 3.11 is required (tomllib)")

import tomllib

root = Path(sys.argv[1])
CONFIG = "release-please-config.json"
MANIFEST = ".release-please-manifest.json"
MARKER = "x-release-please-version"
# The same pattern release-please's generic updater uses to find the version.
SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[\w.]+)?(?:\+[-\w.]+)?")
# Directories that have a README.md but are not modules. tests/ holds the
# cross-module regression suite for the ansible and security modules.
NOT_MODULES = {"docs-landing", "tests"}
# A bare string with one of these suffixes goes through a structured updater
# before the marker-based one. Rejected so that every entry says what it edits.
BARE_STRING_REASONS = {
    (".yaml", ".yml"): "a bare YAML string is re-dumped whole, which drops the marker comment and reformats the file",
    (".json",): "a bare JSON string is re-dumped whole, and JSON cannot carry the marker",
    (".toml", ".xml"): "a bare TOML or XML string is edited in place but also rewrites a top-level 'version' key",
}
# Settings every module must get, once the package value overrides the top
# level one: they fix the shape of the tags and the grouping of the release PR.
REQUIRED_SETTINGS = {
    "release-type": "simple",
    "include-component-in-tag": True,
    "include-v-in-tag": True,
    "separate-pull-requests": False,
    "bump-minor-pre-major": True,
    "skip-changelog": False,
}

errors = []


def check(ok, message):
    if not ok:
        errors.append(message)


def same(got, want):
    # True == 1 in Python; keep a JSON 1 from passing for true.
    return type(got) is type(want) and got == want


def load_json(name):
    try:
        return json.loads((root / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"FAIL: cannot read {name}: {exc}", file=sys.stderr)
        sys.exit(1)


config = load_json(CONFIG)
manifest = load_json(MANIFEST)

# a. ---------------------------------------------------------------------
packages = config.get("packages")
if not isinstance(packages, dict) or not packages:
    print(f"FAIL: {CONFIG} has no packages", file=sys.stderr)
    sys.exit(1)
check(
    "." not in packages,
    f"{CONFIG} configures a root package '.', which would receive every commit in the repository",
)
if not isinstance(manifest, dict):
    print(f"FAIL: {MANIFEST} must be a JSON object", file=sys.stderr)
    sys.exit(1)
for key, value in manifest.items():
    check(
        key in packages,
        f"{MANIFEST} has '{key}', which {CONFIG} does not configure as a package",
    )
    check(
        isinstance(value, str) and re.fullmatch(SEMVER.pattern, value) is not None,
        f"{MANIFEST}: version of '{key}' is not a SemVer string: {value!r}",
    )

for key, want in REQUIRED_SETTINGS.items():
    # separate-pull-requests is read from the top level by release-please; the
    # others can be overridden per package, which is checked below.
    check(
        same(config.get(key), want),
        f"{CONFIG}: top-level {key} must be {want!r}, got {config.get(key)!r}",
    )

# b. ---------------------------------------------------------------------
configured = set(packages)
on_disk = {
    d.name
    for d in root.iterdir()
    if d.is_dir() and not d.name.startswith(".") and (d / "README.md").is_file()
} - NOT_MODULES
for name in sorted(on_disk - configured):
    errors.append(
        f"directory '{name}/' has a README.md but is not a package in {CONFIG} "
        f"(add it, or add it to NOT_MODULES in this test if it is not a module)"
    )
for name in sorted(configured - on_disk):
    errors.append(
        f"{CONFIG} configures '{name}' but there is no top-level '{name}/' directory with a README.md"
    )

readme = (root / "README.md").read_text(encoding="utf-8") if (root / "README.md").is_file() else ""
section = re.search(r"^## Modules\n(.*?)(?=^## )", readme, re.S | re.M)
if section is None:
    errors.append("README.md has no '## Modules' section")
else:
    linked = set(re.findall(r"\]\(([A-Za-z0-9._-]+)/README\.md\)", section.group(1)))
    for name in sorted(linked - configured):
        errors.append(f"README.md Modules table links '{name}/' but {CONFIG} does not configure it")
    for name in sorted(configured - linked):
        errors.append(f"{CONFIG} configures '{name}' but the README.md Modules table does not link it")

# JSONPath subset: $.a.b and $.a[?(@.k.value=='v')].b . release-please 17.x
# parses TOML scalars into {start,end,value} objects, so its filters compare
# '@.k.value'; tomllib returns plain values, so the '.value' is dropped here.
STEP = re.compile(
    r"\.(?P<key>[A-Za-z0-9_-]+)"
    r"(?:\[\?\(@\.(?P<fkey>[A-Za-z0-9_-]+)\.value=='(?P<fval>[^']*)'\)\])?"
)


def toml_values(data, jsonpath):
    if not jsonpath.startswith("$"):
        raise ValueError("jsonpath must start with '$'")
    rest = jsonpath[1:]
    nodes = [data]
    pos = 0
    while pos < len(rest):
        step = STEP.match(rest, pos)
        if step is None:
            raise ValueError(
                f"unsupported jsonpath near '{rest[pos:]}' (filters must read "
                f"@.<key>.value; extend this test to support anything else)"
            )
        pos = step.end()
        found = []
        for node in nodes:
            if not isinstance(node, dict) or step["key"] not in node:
                continue
            value = node[step["key"]]
            if step["fkey"] is None:
                found.append(value)
            elif isinstance(value, list):
                found.extend(
                    item
                    for item in value
                    if isinstance(item, dict) and item.get(step["fkey"]) == step["fval"]
                )
        nodes = found
    return nodes


checked_files = 0
for name, pkg in sorted(packages.items()):
    pkg_dir = root / name

    # c. -----------------------------------------------------------------
    check(
        pkg.get("component") == name,
        f"{name}: component must equal the directory name, got {pkg.get('component')!r}",
    )
    initial = pkg.get("initial-version")
    check(
        initial == "0.1.0",
        f"{name}: initial-version must be \"0.1.0\" (1.0.0 is the public release, "
        f"cut on purpose with Release-As), got {initial!r}",
    )
    for key, want in REQUIRED_SETTINGS.items():
        got = pkg.get(key, config.get(key))
        check(
            same(got, want),
            f"{name}: effective {key} must be {want!r}, got {got!r}",
        )
    expected = manifest.get(name, initial)

    for entry in pkg.get("extra-files", []):
        # Normalise to (kind, path, jsonpath).
        if isinstance(entry, str):
            kind, rel, jsonpath = "generic", entry, None
            reason = next((why for sfx, why in BARE_STRING_REASONS.items() if entry.endswith(sfx)), None)
            if reason:
                errors.append(
                    f"{name}: extra-file '{entry}' is a bare string; {reason}. Use "
                    f"{{\"type\": \"generic\", \"path\": \"{entry}\"}} for a marker line, "
                    f"or a toml entry with a jsonpath to edit a key"
                )
                continue
        elif isinstance(entry, dict) and entry.get("type") in ("generic", "toml"):
            kind, rel, jsonpath = entry["type"], entry.get("path", ""), entry.get("jsonpath")
            if entry.get("glob"):
                errors.append(f"{name}: extra-file '{rel}' uses glob, which this test cannot resolve")
                continue
        else:
            errors.append(f"{name}: unsupported extra-file {entry!r} (extend this test to cover it)")
            continue

        # d. -------------------------------------------------------------
        path = pkg_dir / rel
        if not path.is_file():
            errors.append(f"{name}: extra-file '{rel}' does not exist at {name}/{rel}")
            continue
        checked_files += 1
        text = path.read_text(encoding="utf-8")
        where = f"{name}/{rel}"

        if kind == "generic":
            # f. ---------------------------------------------------------
            marker_lines = [line for line in text.splitlines() if MARKER in line]
            if not marker_lines:
                errors.append(f"{where}: no line carries the {MARKER} marker")
                continue
            # e. ---------------------------------------------------------
            for line in marker_lines:
                found = SEMVER.search(line)
                if found is None:
                    errors.append(f"{where}: marker line has no version: {line.strip()!r}")
                elif found.group(0) != expected:
                    errors.append(
                        f"{where}: holds {found.group(0)} but {name} is at {expected} "
                        f"(line: {line.strip()!r})"
                    )
        else:
            # e. -----------------------------------------------------------
            try:
                values = toml_values(tomllib.loads(text), jsonpath or "")
            except (ValueError, tomllib.TOMLDecodeError) as exc:
                errors.append(f"{where}: {exc}")
                continue
            if not values:
                errors.append(
                    f"{where}: jsonpath {jsonpath} matches nothing, so release-please "
                    f"would silently skip this file"
                )
            for value in values:
                if value != expected:
                    errors.append(
                        f"{where}: {jsonpath} holds {value!r} but {name} is at {expected}"
                    )

# g. ---------------------------------------------------------------------
def slug(heading):
    # GitHub's anchor for a heading: lowercase, drop punctuation, spaces to '-'.
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def read(rel):
    try:
        return (root / rel).read_text(encoding="utf-8")
    except OSError:
        errors.append(f"{rel} is missing or unreadable")
        return ""


release_yml = read(".github/workflows/release.yml")
for key, expected_file in (("config-file", CONFIG), ("manifest-file", MANIFEST)):
    found = re.findall(rf"^\s*{key}:\s*(\S+)\s*$", release_yml, re.M)
    check(
        found == [expected_file],
        f".github/workflows/release.yml: {key} must be {expected_file}, found {found}",
    )

check(
    re.search(r"^\s+workflow_dispatch:", release_yml, re.M) is not None,
    ".github/workflows/release.yml has no workflow_dispatch trigger, which CONTRIBUTING.md tells you to use",
)

checks_yml = read(".github/workflows/checks.yml")
cc_script = read(".github/scripts/conventional-commits.sh")
vi_script = read(".github/scripts/versioning-impact.sh")
# The command of each single-line `run:` step, so a comment that only names a
# script does not count as running it.
run_commands = set(re.findall(r"^\s*run:\s*(\S+)", checks_yml, re.M))
# Globbed, so a script added later has to be wired into checks.yml too.
for script in sorted((root / ".github/scripts").glob("*.sh")):
    rel = script.relative_to(root).as_posix()
    check(rel in run_commands, f".github/workflows/checks.yml has no step that runs {rel}")

for doc in ("README.md", "CONTRIBUTING.md"):
    text = read(doc)
    for ref in sorted(set(re.findall(r"\.github/[A-Za-z0-9_./-]*", text))):
        ref = ref.rstrip(".")
        check((root / ref).exists(), f"{doc}: names {ref}, which does not exist")
    for flag in sorted(set(re.findall(r"conventional-commits\.sh (--[a-z-]+)", text))):
        check(f"{flag})" in cc_script, f"{doc}: shows conventional-commits.sh {flag}, which the script does not accept")
    for flag in sorted(set(re.findall(r"versioning-impact\.sh (--[a-z-]+)", text))):
        check(f'"{flag}"' in vi_script, f"{doc}: shows versioning-impact.sh {flag}, which the script does not accept")
    for target in sorted(set(re.findall(r"\]\(([^)\s]+)\)", text))):
        if re.match(r"[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (root / doc).parent / file_part if file_part else root / doc
        if not dest.exists():
            errors.append(f"{doc}: link {target} points at a missing file")
        elif anchor and dest.suffix == ".md":
            headings = re.findall(r"^#+\s+(.*)$", dest.read_text(encoding="utf-8"), re.M)
            check(anchor in {slug(h) for h in headings}, f"{doc}: link {target} points at a missing heading")

if errors:
    for message in errors:
        print(f"FAIL: {message}", file=sys.stderr)
    print(f"release-config-test: {len(errors)} problem(s)", file=sys.stderr)
    sys.exit(1)
print(f"release-config-test: ok ({len(packages)} packages, {checked_files} extra-files)")
PY
