#!/usr/bin/env python3
"""Fail when the variable reference drifts from the alloy_collector firewall-log
variables.

Standard library only; run from anywhere:

    python3 -I ansible/tests/alloy-collector-docs-test.py

Checks, for every default in roles/alloy_collector/defaults/main.yml whose name
starts with alloy_syslog or alloy_pve:
  * it has a row in the "Collector" table of docs/reference/variables.md
  * the default shown in that row is the default in the file, verbatim
    (`""`, `[]`, `false`), not a paraphrase
  * every such variable the Collector section names exists in the defaults
and for the firewall-log examples the reference carries:
  * every filterlog rule label in group_vars/central (the drop list and the
    rate-limit list) is in the reference, and the reference shows no label
    group_vars/central does not set, except the one label that must never be
    dropped (6125cb20...), which group_vars/central must not list and the
    reference must explain
  * the YAML examples of the two filterlog lists equal what group_vars/central sets
  * host_vars/t7920.yml sets alloy_pve_firewall_log, as the reference says
"""
import pathlib
import re
import sys

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
DEFAULTS = ANSIBLE / "roles" / "alloy_collector" / "defaults" / "main.yml"
REFERENCE = ANSIBLE / "docs" / "reference" / "variables.md"
VAR = r"alloy_(?:syslog|pve)\w*"
errors = []


def fail(msg):
    errors.append(msg)


def strip_code(cell):
    return cell.strip().replace("`", "")


defaults_text = DEFAULTS.read_text()
reference = REFERENCE.read_text()

# Top-level scalar or list defaults, with their value text up to any comment.
role_vars = {}
for name, raw in re.findall(rf"^({VAR}):[ ]*(.*)$", defaults_text, re.M):
    role_vars[name] = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
if not role_vars:
    fail("found no alloy_syslog* or alloy_pve* default in " + str(DEFAULTS))

# The Collector section, up to the next second-level heading.
parts = re.split(r"^## ", reference, flags=re.M)
collector = next((p for p in parts if p.startswith("Collector\n")), None)
if collector is None:
    fail("variables.md has no 'Collector' section")
    collector = ""

rows = {}
for line in collector.splitlines():
    m = re.match(rf"^\| `({VAR})` \| (.*?) \| (.*) \|$", line)
    if m:
        rows[m.group(1)] = (strip_code(m.group(2)), m.group(3))

for name, value in sorted(role_vars.items()):
    if name not in rows:
        fail("variables.md Collector table has no row for " + name)
        continue
    shown = rows[name][0]
    if shown != value:
        fail("variables.md shows " + name + " defaulting to '" + shown + "', defaults/main.yml has '" + value + "'")
    if not rows[name][1].strip():
        fail("variables.md row for " + name + " has empty notes")

for name in sorted(set(re.findall(rf"`({VAR})`", collector))):
    if name not in role_vars:
        fail("variables.md names " + name + ", which defaults/main.yml does not define")

# --- the examples the reference carries -------------------------------------------
central = (ANSIBLE / "inventory" / "group_vars" / "central" / "main.yml").read_text()
labels = set()
for var in ("alloy_syslog_drop_filterlog_rules", "alloy_syslog_ratelimit_filterlog_rules"):
    block = re.search(rf"^{var}:\n((?:[ ]+- .*\n?)+)", central, re.M)
    if not block:
        fail("group_vars/central sets no " + var + " list")
        continue
    found = set(re.findall(r"^[ ]+- ([0-9a-f]{32})\b", block.group(1), re.M))
    if not found:
        fail("group_vars/central sets " + var + " but no 32-character rule label in it")
    labels |= found
for label in sorted(labels):
    if label not in reference:
        fail("variables.md does not show filterlog rule label " + label + " from group_vars/central")
# A label that must never be dropped: its rule logs the outbound leg of every
# tenant and lease flow (pf translates NAT before it filters), which is what
# ties an abuse report to a VM. The reference explains why it is absent.
NEVER_DROP = {"6125cb207f65033775d1069fdf6d0ccf"}
for label in sorted(NEVER_DROP & labels):
    fail("group_vars/central lists filterlog rule label " + label
         + ", which logs the outbound leg of every tenant and lease flow and must reach Loki")
for label in sorted(NEVER_DROP):
    if label not in collector or "abuse report" not in collector:
        fail("variables.md does not explain why filterlog rule label " + label + " is not dropped")
for label in sorted(set(re.findall(r"\b[0-9a-f]{32}\b", collector)) - labels - NEVER_DROP):
    fail("variables.md shows filterlog rule label " + label + ", which group_vars/central does not set")

# The YAML examples in the reference are the real lists, not a paraphrase.
for var, source in (("alloy_syslog_drop_filterlog_rules", "drop"), ("alloy_syslog_ratelimit_filterlog_rules", "ratelimit")):
    shown = re.search(rf"^{var}:\n((?:  - .*\n)+)", collector, re.M)
    block_labels = set(re.findall(r"^  - ([0-9a-f]{32})\b", shown.group(1), re.M)) if shown else None
    block = re.search(rf"^{var}:\n((?:[ ]+- .*\n?)+)", central, re.M)
    real = set(re.findall(r"^[ ]+- ([0-9a-f]{32})\b", block.group(1), re.M)) if block else set()
    if not block_labels:
        fail("variables.md has no YAML example of " + var + " with a rule label in it")
    elif not real:
        fail("group_vars/central sets no label for " + var)
    elif block_labels != real:
        fail("the YAML example of " + var + " in variables.md shows " + str(sorted(block_labels))
             + ", group_vars/central sets " + str(sorted(real)))

t7920 = (ANSIBLE / "inventory" / "host_vars" / "t7920.yml").read_text()
if not re.search(r"^alloy_pve_firewall_log: true$", t7920, re.M):
    fail("host_vars/t7920.yml does not set alloy_pve_firewall_log: true, which variables.md says it does")

if errors:
    print("\n".join("FAIL: " + e for e in errors))
    sys.exit(1)
print("alloy_collector syslog and PVE variables match the variable reference")
