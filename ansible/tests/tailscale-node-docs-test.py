#!/usr/bin/env python3
"""Fail when the tailscale_node docs drift from the role.

Standard library only; run from anywhere:

    python3 -I ansible/tests/tailscale-node-docs-test.py

Checks:
  * every variable in defaults/main.yml (set, or commented as optional) has a
    row in the role README, and every variable the README names exists
  * a scalar default shown in the README equals defaults/main.yml (the whole
    normalised cell, not a substring)
  * every tag in the README table is used in tasks/main.yml, and the reverse,
    and the playbook header lists the same tags
  * the roles, playbooks and groups named under "Run alongside" exist
  * every file tasks/main.yml imports exists, and every `src:` template exists
  * every `--tags a,b` token in a README code block is a tag the role defines
  * the playbook targets tailscale_nodes and runs the role, and the example
    inventory defines the group
  * the surfaces that name the role (READMEs, docs reference, patching page,
    fleet page, example inventory) still do
  * the vault keys the role reads are listed in docs/reference/variables.md
  * the README's prerequisites keep the steps only the owner can do
"""
import pathlib
import re
import sys

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
ROLE = ANSIBLE / "roles" / "tailscale_node"
PLAYBOOK = "tailscale-node.yml"
PREFIX = "tailscale_node_"
GROUP = "tailscale_nodes"
errors = []


def fail(msg):
    errors.append(msg)


def norm(text):
    return re.sub(r"[`\"']", "", text).strip()


defaults = (ROLE / "defaults" / "main.yml").read_text()
readme = (ROLE / "README.md").read_text()
main_tasks = (ROLE / "tasks" / "main.yml").read_text()
playbook = (ANSIBLE / "playbooks" / PLAYBOOK).read_text()

# --- variables --------------------------------------------------------------
var_re = PREFIX + r"[a-z0-9_]+"
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
    # The whole cell, not a substring: a README default of 50000 must not pass
    # for a default of 5000.
    if norm(value) != cell:
        fail(f"README default for {var} is '{cell}', defaults has '{value}'")

for var in optional_vars:
    if var in rows and "unset" not in rows[var][0]:
        fail(f"{var} is optional in defaults; README default should say unset")

# --- tags ---------------------------------------------------------------------
what_it_does = readme.split("## What it does")[1].split("## ")[0]
doc_tags = set(re.findall(r"^\| `([a-z]+)` \|", what_it_does, re.M))
code_tags = set(re.findall(r"tags: \[([a-z]+)\]", main_tasks)) - {"always"}
if doc_tags != code_tags:
    fail(f"README tags {sorted(doc_tags)} differ from tasks/main.yml {sorted(code_tags)}")
tag_line = re.search(r"# Tags, one per concern: (.*)\.", playbook)
if not tag_line or set(re.findall(r"[a-z]+", tag_line.group(1))) != code_tags:
    fail(f"playbooks/{PLAYBOOK} tag comment differs from tasks/main.yml")

for block in re.findall(r"```[a-z]*\n(.*?)```", readme, re.S):
    for tag_arg in re.findall(r"--tags[ =]([A-Za-z0-9_,-]+)", block):
        for tag in tag_arg.split(","):
            if tag not in code_tags | {"always"}:
                fail(f"README example uses --tags {tag}, which the role does not define")

# --- templates and imports ------------------------------------------------------
for task_file in sorted((ROLE / "tasks").glob("*.yml")):
    for src in re.findall(r"^\s+src: ([^\s#]+)", task_file.read_text(), re.M):
        if "/" in src or "{{" in src:
            continue
        if not (ROLE / "templates" / src).is_file():
            fail(f"{task_file.name} names template {src}, which templates/ does not have")
for name in re.findall(r"import_tasks: ([a-z_.]+)", main_tasks):
    if not (ROLE / "tasks" / name).is_file():
        fail(f"tasks/main.yml imports missing file {name}")
for task_file in sorted((ROLE / "tasks").glob("*.yml")):
    for name in re.findall(r"import_tasks: ([a-z_.]+)", task_file.read_text()):
        if not (ROLE / "tasks" / name).is_file():
            fail(f"{task_file.name} imports missing file {name}")

# --- preflight: the `always` guard ----------------------------------------------------
if not re.search(r"import_tasks: preflight\.yml\n\s+tags: \[always\]", main_tasks):
    fail("tasks/main.yml must import preflight.yml with tags: [always]")
if main_tasks.index("preflight.yml") > main_tasks.index("install.yml"):
    fail("tasks/main.yml must import preflight.yml before install.yml")
preflight = (ROLE / "tasks" / "preflight.yml").read_text()
for needle in ["tailscale-router", "tailscale debug prefs", "AdvertiseRoutes", "ExitNodeOption", "import_tasks: state.yml"]:
    if needle not in preflight:
        fail(f"tasks/preflight.yml no longer mentions {needle}")
for needle in ["preflight", "always", "tailscale debug prefs", "tailscale-router"]:
    if needle not in readme:
        fail(f"README no longer mentions {needle}")

# --- run alongside -----------------------------------------------------------------
alongside = readme.split("## Run alongside")[1].split("\n## ")[0]
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
if not re.search(rf"hosts: {GROUP}\b", playbook):
    fail(f"playbooks/{PLAYBOOK} does not target {GROUP}")
if "- tailscale_node" not in playbook:
    fail(f"playbooks/{PLAYBOOK} does not run the tailscale_node role")
if PLAYBOOK in (ANSIBLE / "playbooks" / "site.yml").read_text():
    fail(f"site.yml must not include {PLAYBOOK}: the role is opt-in")

# --- other surfaces ------------------------------------------------------------------
surfaces = {
    "README.md": [PLAYBOOK, "tailscale_node", GROUP],
    "docs/index.md": ["tailscale_node"],
    "docs/reference/playbooks.md": [PLAYBOOK, "tailscale_node", GROUP, "roles/tailscale_node/README.md"],
    "docs/reference/variables.md": ["tailscale_node", "roles/tailscale_node/README.md"],
    "docs/fleet/patching.md": ["tailscale_node", "tailscale-router"],
    "docs/fleet/index.md": [GROUP, "tailscale-router"],
    "inventory/hosts.example.yml": [GROUP, "tailnet router"],
}
texts = {rel: (ANSIBLE / rel).read_text() for rel in surfaces}
texts["../README.md"] = (ANSIBLE.parent / "README.md").read_text()
for needle in [PLAYBOOK, "tailscale_node"]:
    if needle not in texts["../README.md"]:
        fail(f"README.md (repository root) no longer mentions {needle}")
for rel, needles in surfaces.items():
    for needle in needles:
        if needle not in texts[rel]:
            fail(f"ansible/{rel} no longer mentions {needle}")
inventory = texts["inventory/hosts.example.yml"]
if not re.search(rf"^    {GROUP}:$", inventory, re.M):
    fail(f"hosts.example.yml does not define the {GROUP} group")
for rel in re.findall(r"`(roles/tailscale_node/[A-Za-z_./]+)`", "".join(texts.values())):
    if not (ANSIBLE / rel).exists():
        fail(f"a doc names {rel}, which does not exist")

# --- vault keys ------------------------------------------------------------------------
variables_doc = texts["docs/reference/variables.md"]
role_text = "".join(p.read_text() for p in ROLE.rglob("*.yml"))
for key in sorted(set(re.findall(r"\b(vault_[a-z0-9_]+)\b", role_text))):
    if f"`{key}`" not in variables_doc:
        fail(f"docs/reference/variables.md does not list the vault key {key}")
    if key not in readme:
        fail(f"the role README does not name the vault key {key}")

# --- the owner's steps and the router warning stay in the README ----------------------------
prereq = readme.split("## Prerequisites")[1].split("\n## ")[0]
for needle in [
    "HTTPS certificates",
    "tag:matrix",
    "tag:registry",
    "auth key",
    "pct set <id> --dev0 /dev/net/tun",
    "lxc.cgroup2.devices.allow: c 10:200 rwm",
    "lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file",
    "hosts.local.yml",
    "Element",
]:
    if needle not in prereq:
        fail(f"README Prerequisites no longer mentions {needle}")
for needle in ["tailscale-router", "Funnel", "vault_matrix_homeserver_url"]:
    if needle not in readme:
        fail(f"README no longer mentions {needle}")

if errors:
    print("\n".join(f"FAIL: {e}" for e in errors))
    sys.exit(1)
print("tailscale_node docs match the role")
