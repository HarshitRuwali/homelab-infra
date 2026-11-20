#!/usr/bin/env python3
"""Test how grafana_alerting and grafana_dashboards find the Grafana admin login.

Standard library only; run from anywhere:

    python3 -I ansible/tests/grafana-admin-user-test.py

Checks:
  * tasks/admin_user.yml is identical in both roles
  * neither role defaults grafana_admin_user to "admin"; both default to empty
  * every API call with credentials uses the resolved grafana_api_user, and
    admin_user.yml is included before it
  * the regex that reads GF_SECURITY_ADMIN_USER from the env file handles the
    forms lxc-install.sh and hand edits produce, and finds nothing when the
    line is absent
  * the variables reference documents the empty default
"""
import pathlib
import re
import sys

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
ROLES = [ANSIBLE / "roles" / r for r in ("grafana_alerting", "grafana_dashboards")]
errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


files = [(r / "tasks" / "admin_user.yml").read_text() for r in ROLES]
check(files[0] == files[1], "tasks/admin_user.yml differs between the two roles")

for r in ROLES:
    defaults = (r / "defaults" / "main.yml").read_text()
    check(re.search(r'^grafana_admin_user: ""$', defaults, re.M), f"{r.name}: grafana_admin_user must default to empty")
    tasks = (r / "tasks" / "main.yml").read_text()
    calls = [m.start() for m in re.finditer(r"\n\s+user: ", tasks)]
    check(calls, f"{r.name}: no authenticated API call found")
    include = tasks.find("include_tasks: admin_user.yml")
    check(include != -1 and all(include < c for c in calls), f"{r.name}: admin_user.yml must be included before the API calls")
    check("user: \"{{ grafana_admin_user }}\"" not in tasks, f"{r.name}: an API call still uses grafana_admin_user directly")
    check(tasks.count('user: "{{ grafana_api_user }}"') == len(calls), f"{r.name}: every API call must use grafana_api_user")

# The pattern exactly as the task file passes it to regex_search (Python re).
m = re.search(r"regex_search\('(.+?)', '\\1'\)", files[0])
check(m, "admin_user.yml: regex_search call not found")
if m:
    pattern = m.group(1).replace("\\x27", "'")
    samples = {
        'GF_SECURITY_ADMIN_USER="harshitruwali"\n': "harshitruwali",
        "GF_SECURITY_ADMIN_USER=someone\n": "someone",
        "X=1\nGF_SECURITY_ADMIN_USER='quoted'\nY=2\n": "quoted",
        "# GF_SECURITY_ADMIN_USER=commented\nGF_LOG_LEVEL=warn\n": None,
    }
    for text, want in samples.items():
        got = re.search(pattern, text)
        got = got.group(1) if got else None
        check(got == want, f"env {text!r}: read {got!r}, want {want!r}")

variables = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
check('| `grafana_admin_user` | `""` |' in variables, "variables.md must document the empty default")

if errors:
    print("FAIL")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("ok: grafana admin login")
