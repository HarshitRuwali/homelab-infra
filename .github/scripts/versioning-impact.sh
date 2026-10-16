#!/usr/bin/env bash
# Reports what a pull request does to module versions, from its title and the
# files it changes. release-please decides the real version when the release PR
# is built; this shows the part the PR controls, before it merges.
#
#   versioning-impact.sh --title "<subject>" <base> <head>
#       Lists each module the PR touches (a top-level directory configured as a
#       package in release-please-config.json) with the bump its title would
#       cause, as a Markdown table written to $GITHUB_STEP_SUMMARY (stdout when
#       that is unset). GitHub ::warning:: lines go to stdout for:
#         - a module changed under a type that is hidden in the release notes,
#           so nothing is released
#         - a title that is not a conventional commit, or a bare Revert title
#         - a title scope that names a module the PR does not touch
#       A renamed file counts for its new path only, as it does for release-please.
#
# It only reports. Whether a new module directory is registered for versioning
# is the hard check, and lives in release-config-test.sh.
#
# Exit status: 0 for a readable range and a well-formed config, whatever the
# title and files say. 2 for bad arguments, an unreadable range, or an unreadable
# or malformed config or manifest.
# The rules it reports are documented in CONTRIBUTING.md.

set -u

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

exec python3 -I - "$root" "$@" <<'PY'
import json
import os
import re
import subprocess
import sys
from pathlib import Path

USAGE = 'usage: versioning-impact.sh --title "<subject>" <base> <head>'
CONFIG = "release-please-config.json"
MANIFEST = ".release-please-manifest.json"
# type(scope)!: description, the same shape conventional-commits.sh accepts.
TITLE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?(?P<bang>!)?: [^ ]"
)

root = Path(sys.argv[1])
args = sys.argv[2:]


def plain(text):
    # Error text: readable, but no control characters and no "::" that could
    # open a workflow command.
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(text)).replace("::", ": :")


def fail(message):
    print(f"versioning-impact.sh: {plain(message)}", file=sys.stderr)
    sys.exit(2)


def safe(text):
    # Report text. Titles, paths and config values come from whoever opened the
    # PR, so keep only characters that cannot start a workflow command, break a
    # table or carry markup.
    return re.sub(r"[^A-Za-z0-9._/-]", "?", str(text))


if len(args) != 4 or args[0] != "--title":
    print(USAGE, file=sys.stderr)
    sys.exit(2)
title, base, head = args[1:]


def load(name):
    try:
        return json.loads((root / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read {name}: {exc}")


config = load(CONFIG)
manifest = load(MANIFEST)
if not isinstance(config, dict) or not isinstance(manifest, dict):
    fail(f"{CONFIG} and {MANIFEST} must be JSON objects")
packages = config.get("packages")
if (
    not isinstance(packages, dict)
    or not packages
    or not all(isinstance(pkg, dict) for pkg in packages.values())
):
    fail(f"{CONFIG} needs a non-empty packages object")


def setting(name, key):
    return packages[name].get(key, config.get(key))


def sections(name):
    # release-please takes a package's own list over the top-level one.
    raw = setting(name, "changelog-sections")
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(s, dict) and isinstance(s.get("type"), str) for s in raw)
    ):
        fail(f"{CONFIG}: {name} needs a changelog-sections list of objects with a type")
    return raw


visible = {name: {s["type"] for s in sections(name) if not s.get("hidden")} for name in packages}
known = {s["type"] for name in packages for s in sections(name)}


def resolve(rev):
    if rev.startswith("-"):
        fail(f"not a revision: {rev}")
    done = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
         "--end-of-options", f"{rev}^{{commit}}"],
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        fail(f"cannot resolve {rev}")
    return done.stdout.strip()


base_sha, head_sha = resolve(base), resolve(head)
# -M lists a renamed file by its new path only, which is all release-please reads.
done = subprocess.run(
    ["git", "-C", str(root), "diff", "--name-only", "-M", "-z", f"{base_sha}...{head_sha}"],
    capture_output=True,
)
if done.returncode != 0:
    fail(f"cannot diff {base_sha[:7]}...{head_sha[:7]}: {done.stderr.decode('utf-8', 'replace').strip()}")
paths = [p for p in done.stdout.decode("utf-8", "replace").split("\0") if p]
touched = sorted({p.split("/", 1)[0] for p in paths if "/" in p} & set(packages))

parsed = TITLE.match(title)
ctype = parsed["type"] if parsed else None
scope = parsed["scope"] if parsed else None
breaking = bool(parsed and parsed["bang"])
unknown = None
if ctype is not None and ctype not in known:
    unknown, ctype = ctype, None


def releasable(name):
    # A breaking change is releasable even under a hidden type (chore!:).
    return ctype is not None and (ctype in visible[name] or breaking)


def bump(name):
    current = manifest.get(name)
    if current is None:
        version = packages[name].get("initial-version")
        return f"first release at {safe(version)}" if isinstance(version, str) else "first release"
    pre_major = str(current).split(".")[0] == "0"
    if breaking:
        return "minor (breaking, below 1.0.0)" if pre_major and setting(name, "bump-minor-pre-major") else "major"
    if ctype == "feat":
        return "patch (feat, below 1.0.0)" if pre_major and setting(name, "bump-patch-for-minor-pre-major") else "minor"
    return "patch"


rows = []
for name in touched:
    if ctype is None:
        effect = "none (the title is not a conventional commit)"
    elif not releasable(name):
        effect = f"none (type `{ctype}` is hidden)"
    else:
        effect = bump(name)
    rows.append(f"| `{safe(name)}` | {effect} |")

summary = ["### Versioning impact", ""]
if ctype is not None:
    shown = f"type `{ctype}`" + (f", scope `{scope}`" if scope else "") + (", breaking" if breaking else "")
    summary += [f"PR title: {shown}.", ""]
if rows:
    summary += ["| Module | Release from this PR |", "|---|---|", *rows, ""]
else:
    summary += ["This PR touches no module, so it releases nothing.", ""]
summary.append(
    "Modules not listed are not touched. release-please sets the final version when "
    "the release PR is built: other pending changes, and a `BREAKING CHANGE:` or "
    "`Release-As:` footer in the squash commit body, can change it."
)

notes = []
if unknown is not None:
    notes.append(
        f"The type '{unknown}' is not one of {', '.join(sorted(safe(t) for t in known))}, "
        f"so release-please cannot read the title."
    )
elif ctype is None:
    if title.startswith('Revert "'):
        notes.append(
            "A bare Revert title is not recognised by release-please, so this change "
            "will be missing from the release notes. Retitle it 'revert: <subject>' if it should be listed."
        )
    else:
        notes.append("The title is not a conventional commit, so release-please cannot read it.")
else:
    blocked = [name for name in touched if not releasable(name)]
    if blocked:
        notes.append(
            f"{', '.join(safe(n) for n in blocked)} change under the hidden type '{ctype}', so nothing is "
            f"released. Use a visible type ({', '.join(sorted(safe(t) for t in visible[blocked[0]]))}) "
            f"if the change should ship."
        )
if scope and scope in packages and scope not in touched:
    notes.append(f"The title scope '{scope}' names a module this PR does not touch.")

text = "\n".join(summary) + "\n"
target = os.environ.get("GITHUB_STEP_SUMMARY")
if target:
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(text)
else:
    print(text, end="")
for note in notes:
    print(f"::warning::{note}")
PY
