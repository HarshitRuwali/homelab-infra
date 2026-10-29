#!/usr/bin/env python3
"""Fail when the image_registry docs drift from the role.

Standard library only; run from anywhere:

    python3 -I ansible/tests/image-registry-docs-test.py

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
  * the playbook targets image_registries and runs the role, and the example
    inventory defines the group
  * the surfaces that name the role (READMEs, docs reference, patching page,
    fleet page, example inventory) still do
  * the vault keys the role reads are listed in docs/reference/variables.md
  * the README's prerequisites keep the steps only the owner can do
  * the Compose project name agrees across the defaults, template, README,
    example inventory and playbook header (docker_update_skip_projects matches
    it), the README's serve target port matches image_registry_port, and the
    Compose file publishes on 127.0.0.1 only
"""
import pathlib
import re
import sys

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
ROLE = ANSIBLE / "roles" / "image_registry"
PLAYBOOK = "image-registry.yml"
PREFIX = "image_registry_"
GROUP = "image_registries"
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

# --- preflight: the `always` check ----------------------------------------------------
if not re.search(r"import_tasks: preflight\.yml\n\s+tags: \[always\]", main_tasks):
    fail("tasks/main.yml must import preflight.yml with tags: [always]")
if main_tasks.index("preflight.yml") > main_tasks.index("docker.yml"):
    fail("tasks/main.yml must import preflight.yml before docker.yml")
if "python3-apt" not in readme or "preflight" not in readme:
    fail("README no longer mentions the python3-apt preflight")

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
if "- image_registry" not in playbook:
    fail(f"playbooks/{PLAYBOOK} does not run the image_registry role")
if PLAYBOOK in (ANSIBLE / "playbooks" / "site.yml").read_text():
    fail(f"site.yml must not include {PLAYBOOK}: the role is opt-in")

# --- other surfaces ------------------------------------------------------------------
surfaces = {
    "README.md": [PLAYBOOK, "image_registry", GROUP],
    "docs/index.md": ["image_registry"],
    "docs/reference/playbooks.md": [PLAYBOOK, "image_registry", GROUP, "roles/image_registry/README.md"],
    "docs/reference/variables.md": ["image_registry", "roles/image_registry/README.md"],
    "docs/fleet/patching.md": ["image_registry"],
    "docs/fleet/index.md": [GROUP],
    "inventory/hosts.example.yml": [GROUP, "docker_update_skip_projects"],
}
texts = {rel: (ANSIBLE / rel).read_text() for rel in surfaces}
texts["../README.md"] = (ANSIBLE.parent / "README.md").read_text()
for needle in [PLAYBOOK, "image_registry"]:
    if needle not in texts["../README.md"]:
        fail(f"README.md (repository root) no longer mentions {needle}")
for rel, needles in surfaces.items():
    for needle in needles:
        if needle not in texts[rel]:
            fail(f"ansible/{rel} no longer mentions {needle}")
inventory = texts["inventory/hosts.example.yml"]
if not re.search(rf"^    {GROUP}:$", inventory, re.M):
    fail(f"hosts.example.yml does not define the {GROUP} group")
for rel in re.findall(r"`(roles/image_registry/[A-Za-z_./]+)`", "".join(texts.values())):
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

# --- the owner's steps, and the composition with tailscale_node --------------------------
prereq = readme.split("## Prerequisites")[1].split("\n## ")[0]
for needle in [
    "nesting=1",
    "onboard.yml",
    "hosts.local.yml",
    "vault_image_registry_push_password",
    "tag:registry",
    "docker login",
    "docker_update_skip_projects",
]:
    if needle not in prereq:
        fail(f"README Prerequisites no longer mentions {needle}")
for needle in ["fleet CA", "tailscale serve", "Zot", "garbage collection"]:
    if needle not in readme:
        fail(f"README no longer mentions {needle}")

# The Compose project name is what docker_update_skip_projects must list; the
# README, the example inventory and the playbook header all give it.
project = set_vars.get("image_registry_project_name", "").strip()
if not project:
    fail("defaults/main.yml does not set image_registry_project_name")
for label, text in [
    ("README", readme),
    ("example inventory", inventory),
    ("playbook header", playbook),
]:
    if project and project not in text:
        fail(f"{label} does not name the Compose project {project}")
if project and f"name: {{{{ image_registry_project_name }}}}" not in (ROLE / "templates" / "compose.yml.j2").read_text():
    fail("compose.yml.j2 does not take its project name from image_registry_project_name")

# The serve target in the README example must match the default port.
port = set_vars.get("image_registry_port", "").strip()
targets = re.findall(r"target: http://127\.0\.0\.1:(\d+)", readme)
if not targets or any(t != port for t in targets):
    fail(f"README serve target ports {targets} differ from image_registry_port {port}")

# The registry must stay on loopback.
compose = (ROLE / "templates" / "compose.yml.j2").read_text()
if '"127.0.0.1:{{ image_registry_port }}:5000"' not in compose:
    fail("compose.yml.j2 no longer publishes the registry on 127.0.0.1 only")

if errors:
    print("\n".join(f"FAIL: {e}" for e in errors))
    sys.exit(1)
print("image_registry docs match the role")
