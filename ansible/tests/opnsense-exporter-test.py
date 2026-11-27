#!/usr/bin/env python3
"""Test roles/opnsense_exporter, the firewall-side scripts and the central
scrape targets, against the docs that describe them.

Standard library only; run from anywhere:

    python3 -I ansible/tests/opnsense-exporter-test.py

Checks:
  * the compose template runs the exporter as nobody, read-only, with every
    capability dropped, mounts the key and secret read-only and passes them as
    *_FILE paths, never as values
  * every collector in opnsense_exporter_disable is one the pinned exporter
    accepts (its --help, 0.0.19)
  * the readiness check waits for real API data, not opnsense_up (always 0 on
    OPNsense 26.7)
  * each firewall script is a dry run unless given --apply: nothing is saved
    before the --apply gate
  * the Alloy template gives extra scrape targets their own host and role, and
    group_vars/central scrapes the firewall's two exporters
  * every default has a row in docs/reference/variables.md and the reverse;
    the playbook is in site.yml, the README and playbooks.md; the routers page
    names every firewall script
"""
import pathlib
import re
import sys

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
REPO = ANSIBLE.parent
ROLE = ANSIBLE / "roles" / "opnsense_exporter"
PREFIX = "opnsense_exporter_"
errors = []

# `opnsense-exporter --help` of ghcr.io/athennamind/opnsense-exporter:0.0.19.
KNOWN_DISABLE = {"arp-table", "carp", "cron-table", "firewall", "firmware", "ipsec", "kea-dhcpv4",
                 "kea-dhcpv6", "mbuf", "openvpn", "pf-states", "temperature", "unbound", "wireguard"}


def check(cond, msg):
    if not cond:
        errors.append(msg)


defaults_text = (ROLE / "defaults" / "main.yml").read_text()
compose = (ROLE / "templates" / "compose.yml.j2").read_text()
tasks = (ROLE / "tasks" / "main.yml").read_text()

# ------------------------------------------------------------- container ---
check('user: "65534:65534"' in compose, "the exporter must run as nobody")
check("read_only: true" in compose, "the container filesystem must be read-only")
check("cap_drop: [ALL]" in compose, "every capability must be dropped")
check("no-new-privileges:true" in compose, "no-new-privileges must be set")
for name in ("api_key", "api_secret"):
    check(re.search(rf"/{name}:/run/secrets/{name}:ro\b", compose), f"{name} must be mounted read-only")
check("OPS_API_KEY_FILE" in compose and "OPS_API_SECRET_FILE" in compose, "the key must be passed as a file path")
check(not re.search(r"\bOPS_API_(KEY|SECRET)\s*:", compose) and "OPNSENSE_EXPORTER_OPS_API_" not in compose,
      "the key and secret must never be passed as values")
check("--opnsense.api-key" not in compose and "--opnsense.api-secret" not in compose,
      "the key must not be on the command line")

disable = re.search(r"opnsense_exporter_disable:\n((?:\s+- .+\n)+)", defaults_text)
items = re.findall(r"- (\S+)", disable.group(1)) if disable else []
check(items, "opnsense_exporter_disable must be a list")
for c in items:
    check(c in KNOWN_DISABLE, f"opnsense_exporter_disable has {c!r}, which the exporter does not accept")

# ------------------------------------------------------------- readiness ---
wait = tasks.split("Wait for the exporter", 1)[1] if "Wait for the exporter" in tasks else ""
check("opnsense_interfaces_received_bytes_total" in wait, "readiness must wait for real API data")
check("opnsense_up" not in wait.split("ansible.builtin.uri", 1)[-1],
      "readiness must not depend on opnsense_up, which is 0 on OPNsense 26.7")

# --------------------------------------------------------- firewall side ---
scripts = sorted((ROLE / "files" / "opnsense").glob("*.php"))
check(len(scripts) == 4, f"expected the four firewall scripts, found {[s.name for s in scripts]}")
for s in scripts:
    src = s.read_text()
    check("in_array('--apply', $argv, true)" in src, f"{s.name} must take --apply")
    gate = src.find("if (!$apply)")
    save = src.find("->save()")
    check(gate != -1 and save != -1 and gate < save, f"{s.name} must stop before saving unless --apply is given")
    check(src.count("serializeToConfig") == 1 and src.find("serializeToConfig") > gate,
          f"{s.name} must serialize only after the --apply gate")

# ------------------------------------------------------------ scrape side ---
alloy = (ANSIBLE / "roles" / "alloy_collector" / "templates" / "config.alloy.j2").read_text()
block = alloy.split("alloy_extra_scrape_targets", 1)[1] if "alloy_extra_scrape_targets" in alloy else ""
check('"host"        = "{{ t.host }}"' in block and '"role"        = "{{ t.role }}"' in block,
      "extra scrape targets must carry their own host and role labels")
central = (ANSIBLE / "inventory" / "group_vars" / "central" / "main.yml").read_text()
for job in ("opnsense-node", "opnsense-api"):
    check(f"job: {job}" in central, f"group_vars/central must scrape {job}")
check(re.search(r"job: opnsense-api\n\s+address: [0-9.]+:9201", central), "opnsense-api must be scraped on port 9201")

# ---------------------------------------------------------------- docs ---
defaults = set(re.findall(rf"^({PREFIX}\w+):", defaults_text, re.M))
variables = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
documented = set(re.findall(rf"^\| `({PREFIX}\w+)`", variables, re.M))
check(defaults == documented, f"variables.md and defaults differ: {sorted(defaults ^ documented)}")
check("`alloy_extra_scrape_targets`" in variables, "alloy_extra_scrape_targets is not in variables.md")
check("import_playbook: opnsense-exporter.yml" in (ANSIBLE / "playbooks" / "site.yml").read_text(), "playbook missing from site.yml")
check("`opnsense-exporter.yml`" in (ANSIBLE / "README.md").read_text(), "playbook missing from the ansible README")
check("### `opnsense_exporter`" in (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text(), "role missing from playbooks.md")
routers = (REPO / "monitoring" / "docs" / "monitoring" / "routers.md").read_text()
for s in scripts:
    check(f"`{s.name}`" in routers, f"routers.md does not name {s.name}")
check("routers.md" in (REPO / "monitoring" / "mkdocs.yml").read_text(), "routers.md is not in the monitoring nav")

if errors:
    print("FAIL")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("ok: opnsense_exporter")
