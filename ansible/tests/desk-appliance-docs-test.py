#!/usr/bin/env python3
"""Fail when the desk_appliance docs drift from the role.

Standard library only; run from anywhere:

    python3 -I ansible/tests/desk-appliance-docs-test.py

Checks:
  * every variable in defaults/main.yml (set, or commented as optional) has a
    row in the role README, and every variable the README names exists
  * a scalar default shown in the README matches defaults/main.yml
  * every tag in the README table is used in tasks/main.yml, and the reverse
  * the roles, playbooks and groups named under "Run alongside" exist
  * every file a tasks/*.yml file imports exists
  * every distribution in desk_appliance_supported_distributions is named in
    the README's "Supported platforms" section
  * every `--tags a,b` token in a README code block is a tag the role defines
  * every `src:` template named in tasks/*.yml exists under templates/,
    including the ones inside loop items
  * the playbook, the role and the group are named consistently in the
    fleet README, the docs reference and the example inventory
"""
import pathlib
import re
import sys

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
ROLE = ANSIBLE / "roles" / "desk_appliance"
errors = []


def fail(msg):
    errors.append(msg)


def norm(text):
    return re.sub(r"[`\"']", "", text).strip()


defaults = (ROLE / "defaults" / "main.yml").read_text()
readme = (ROLE / "README.md").read_text()
main_tasks = (ROLE / "tasks" / "main.yml").read_text()

# --- variables --------------------------------------------------------------
var_re = r"desk_appliance_[a-z0-9_]+"
set_vars = dict(re.findall(rf"^({var_re}):[ ]*(.*)$", defaults, re.M))
optional_vars = set(re.findall(rf"^#[ ]({var_re}):", defaults, re.M))
role_vars = set(set_vars) | optional_vars
derived = set(
    re.findall(rf"^({var_re}):", (ROLE / "vars" / "main.yml").read_text(), re.M)
)

rows = {}
for line in readme.splitlines():
    m = re.match(rf"^\| `({var_re})` \|(.*)$", line)
    if m:
        rows[m.group(1)] = [c for c in m.group(2).split("|")]

for var in sorted(role_vars):
    if var not in rows:
        fail(f"README has no table row for {var}")
for var in sorted(rows):
    if var not in role_vars:
        fail(f"README documents {var}, which defaults/main.yml does not define")
for var in sorted(set(re.findall(rf"`({var_re})`", readme))):
    if var not in role_vars | derived and var not in rows:
        fail(f"README names {var}, which the role does not define")

for var, raw in set_vars.items():
    value = raw.split("  #")[0].strip()
    if (
        not value
        or value[0] in "[{>|"
        or "{{" in value
        or var not in rows
    ):
        continue
    cell = norm(rows[var][0]) if rows[var] else ""
    if norm(value) not in cell:
        fail(f"README default for {var} is '{cell}', defaults has '{value}'")

for var in optional_vars:
    if var in rows and "unset" not in rows[var][0]:
        fail(f"{var} is optional in defaults; README default should say unset")

# --- tags ---------------------------------------------------------------------
what_it_does = readme.split("## What it does")[1].split("## Variables")[0]
doc_tags = set(re.findall(r"^\| `([a-z]+)` \|", what_it_does, re.M))
code_tags = set(re.findall(r"tags: \[([a-z]+)\]", main_tasks)) - {"always"}
if doc_tags != code_tags:
    fail(f"README tags {sorted(doc_tags)} differ from tasks/main.yml {sorted(code_tags)}")
playbook = (ANSIBLE / "playbooks" / "desk-appliance.yml").read_text()
tag_line = re.search(r"# Tags, one per concern: (.*)\.", playbook)
if not tag_line or set(re.findall(r"[a-z]+", tag_line.group(1))) != code_tags:
    fail("playbooks/desk-appliance.yml tag comment differs from tasks/main.yml")

# Tags shown in README command examples must be ones the role really defines
# (plus `always`, which user.yml carries).
for block in re.findall(r"```[a-z]*\n(.*?)```", readme, re.S):
    for tag_arg in re.findall(r"--tags[ =]([A-Za-z0-9_,-]+)", block):
        for tag in tag_arg.split(","):
            if tag not in code_tags | {"always"}:
                fail(f"README example uses --tags {tag}, which the role does not define")

# --- templates named by tasks ---------------------------------------------------
for task_file in sorted((ROLE / "tasks").glob("*.yml")):
    for src in re.findall(r"^\s+src: ([^\s#]+)", task_file.read_text(), re.M):
        # src: also names a symlink target in ansible.builtin.file (an absolute
        # path); only bare names are templates.
        if "/" in src or "{{" in src:
            continue
        if not (ROLE / "templates" / src).is_file():
            fail(f"{task_file.name} names template {src}, which templates/ does not have")
    # Templates named by loop items (`- src: x.j2` with dest and mode below).
    for src in re.findall(r"^\s+- src: ([A-Za-z0-9_.-]+\.j2)$", task_file.read_text(), re.M):
        if not (ROLE / "templates" / src).is_file():
            fail(f"{task_file.name} names template {src}, which templates/ does not have")

# --- imported task files ---------------------------------------------------------
# main.yml imports one file per concern; updates.yml imports updates_apply.yml.
for task_file in sorted((ROLE / "tasks").glob("*.yml")):
    for name in re.findall(r"import_tasks: ([a-z_.]+)", task_file.read_text()):
        if not (ROLE / "tasks" / name).is_file():
            fail(f"{task_file.name} imports missing file {name}")

# --- supported platforms ------------------------------------------------------------
supported = re.search(
    r"^desk_appliance_supported_distributions:\n((?:[ ]+- .*\n)+)", defaults, re.M
)
platforms_doc = readme.split("## Supported platforms")[1].split("\n## ")[0]
if not supported:
    fail("defaults/main.yml has no desk_appliance_supported_distributions list")
else:
    for name in re.findall(r"- (\S+)", supported.group(1)):
        if name not in platforms_doc:
            fail(f"README 'Supported platforms' does not name {name}")

# --- run alongside -----------------------------------------------------------------
alongside = readme.split("## Run alongside")[1].split("##")[0]
all_playbooks = "\n".join(p.read_text() for p in (ANSIBLE / "playbooks").glob("*.yml"))
for role, play, group in re.findall(
    r"^\| `([a-z_]+)` \| `([a-z-]+\.yml)` \| `([a-z_]+)` \|", alongside, re.M
):
    if not (ANSIBLE / "roles" / role).is_dir():
        fail(f"Run alongside names missing role {role}")
    play_path = ANSIBLE / "playbooks" / play
    if not play_path.is_file():
        fail(f"Run alongside names missing playbook {play}")
    elif not re.search(rf"hosts: {group}\b", play_path.read_text()):
        fail(f"{play} does not target group {group}")
    elif not re.search(rf"^\s+- {role}\b", play_path.read_text(), re.M):
        fail(f"{play} does not run role {role}")
if not re.search(r"hosts: desk_appliances\b", playbook):
    fail("playbooks/desk-appliance.yml does not target desk_appliances")
if "- desk_appliance" not in playbook:
    fail("playbooks/desk-appliance.yml does not run the desk_appliance role")

# --- other surfaces ------------------------------------------------------------------
surfaces = {
    "README.md": ["desk-appliance.yml", "desk_appliance", "desk_appliances"],
    "docs/reference/playbooks.md": ["desk-appliance.yml", "desk_appliance", "desk_appliances"],
    "docs/reference/variables.md": ["desk_appliance"],
    # The fleet policy page carries the desk-appliance exception to
    # "never reboots", and links the role README.
    "docs/fleet/patching.md": [
        "desk_appliances",
        "roles/desk_appliance/README.md",
        "monitored:!desk_appliances",
    ],
    "inventory/hosts.example.yml": ["desk_appliances"],
}
for rel, needles in surfaces.items():
    text = (ANSIBLE / rel).read_text()
    for needle in needles:
        if needle not in text:
            fail(f"ansible/{rel} no longer mentions {needle}")
# The desk appliances reboot themselves, so the forced-update playbook (which a
# human runs by day) must leave them out.
force_updates = (ANSIBLE / "playbooks" / "force-updates.yml").read_text()
if len(re.findall(r"^\s+hosts: monitored:!desk_appliances$", force_updates, re.M)) != 2:
    fail("playbooks/force-updates.yml plays do not all exclude desk_appliances")
for rel in re.findall(r"`(roles/desk_appliance/[A-Za-z_./]+)`", "".join(
    (ANSIBLE / r).read_text() for r in surfaces
)):
    if not (ANSIBLE / rel).exists():
        fail(f"a doc names {rel}, which does not exist")

if errors:
    print("\n".join(f"FAIL: {e}" for e in errors))
    sys.exit(1)
print("desk_appliance docs match the role")
