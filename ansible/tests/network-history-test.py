#!/usr/bin/env python3
"""Test the long-term network history: the 90-day Prometheus tier, the PostgreSQL
aggregator, the Grafana datasources and dashboards, nethist and the role itself.

    python3 -I ansible/tests/network-history-test.py

Needs jinja2 and pyyaml (both come with ansible-core). The database checks need
Docker (a throwaway postgres:16-alpine on 127.0.0.1) and the pure-python pg8000;
without either they are SKIPPED and the output says so. To run them from a
checkout that has pg8000 outside the interpreter's path:

    NET_HISTORY_TEST_PYLIB=/path/to/dir-with-pg8000 python3 -I ansible/tests/network-history-test.py

Checks:
  * role: federation config (honor_labels, 30 s, /federate on the central Prometheus,
    the match[] list), the 90-day unit (retention 90d and 4GB, port 9091, prometheus
    user, hardening, MemoryMax), the aggregator unit and timer, the Grafana datasource
    file (uids, type, no plaintext password in the repo), the SQL the role runs on the
    database host, the nethist launcher, pg8000 pinned, no SQLite anywhere
  * every metric the four network dashboards query from ${ds}, and every metric the
    aggregator reads, is matched by a federation selector, including the node_network
    device and host narrowing
  * dashboards: parse, unique ids, no overlap, the ds variable, every Prometheus
    panel on ${ds}, Loki panels unchanged, 30 d and 90 d quick ranges, the permanent
    history row, the wan variable and repeated row, per-WAN queries with the one-WAN
    fallback, every net.* object a panel queries exists, and every panel SQL runs
  * aggregator: IST day boundaries, parsing of outage lines, upserts are idempotent,
    back-fill, a missing source skips its table, outage events dedupe and paginate,
    monthly views (availability from seconds, wan total row), --import-wan-daily
    never overwrites without --force, the textfile metrics, comments on every
    object and column, the catalog, reader and writer privileges
  * nethist: argument parsing, query building, input validation, and that the
    password is never on the command line
  * docs: every default has a row in variables.md
"""
import contextlib
import datetime as dt
import http.server
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import jinja2
import yaml

sys.dont_write_bytecode = True
ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
REPO = ANSIBLE.parent
ROLE = ANSIBLE / "roles" / "network_history"
DASH = REPO / "monitoring" / "grafana" / "dashboards" / "fleet"
DASHBOARDS = ["internet-usage", "home-isp", "inter-lan", "router-tplink-blr"]
errors = []
skipped = []

pylib = os.environ.get("NET_HISTORY_TEST_PYLIB")
if pylib:
    sys.path.insert(0, pylib)


def check(cond, msg):
    if not cond:
        errors.append(msg)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agg = load_module("net_history", ROLE / "files" / "net-history.py")
nethist = load_module("nethist", ROLE / "files" / "nethist.py")

# ---------------------------------------------------------------- templating ---


def jenv():
    e = jinja2.Environment(keep_trailing_newline=True, undefined=jinja2.StrictUndefined,
                           loader=jinja2.FileSystemLoader(str(ROLE / "templates")))
    e.filters["to_json"] = json.dumps
    e.filters["quote"] = lambda v: shlex.quote(str(v))
    e.filters["bool"] = lambda v: str(v).lower() in ("1", "true", "yes")
    e.filters["dirname"] = os.path.dirname
    return e


DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())


def resolved(**extra):
    v = dict(DEFAULTS)
    v.update({"alloy_textfile_dir": "/var/lib/node_exporter/textfile_collector", "ansible_user": "agent",
              "network_history_reader_password": "READERPW", "network_history_writer_password": "WRITERPW",
              "hostvars": {"memory": {"ansible_host": "10.10.50.126"}}})
    v.update(extra)
    e = jenv()
    for _ in range(5):
        for k, val in list(v.items()):
            if isinstance(val, str) and "{{" in val:
                v[k] = e.from_string(val).render(**v)
    return v


def render(name, **extra):
    return jenv().get_template(name).render(**resolved(**extra))


V = resolved()

# --------------------------------------------------------------- 1. the role ---
for t in sorted((ROLE / "templates").iterdir()):
    lines = t.read_text().splitlines()
    head = lines[1] if lines[0].startswith("#!") else lines[0]
    check(head.startswith("# Managed by ansible/roles/network_history. Do not edit by hand.") or head.startswith("-- Managed by ansible/roles/network_history."),
          "%s must start with the Managed by header" % t.name)

cfg = yaml.safe_load(render("prometheus-network.yml.j2"))
job = cfg["scrape_configs"][0]
check(len(cfg["scrape_configs"]) == 1, "the 90-day Prometheus has exactly one scrape job")
check(job["honor_labels"] is True, "federation must use honor_labels: true")
check(job.get("track_timestamps_staleness") is True, "federation must set track_timestamps_staleness so replaced series end at their last sample")
check(job["metrics_path"] == "/federate", "federation scrapes /federate")
check(cfg["global"]["scrape_interval"] == "30s", "federation scrape interval is 30s")
check(job["static_configs"][0]["targets"] == ["127.0.0.1:9090"], "federation targets the central Prometheus on 127.0.0.1:9090")
SELECTORS = job["params"]["match[]"]
check(SELECTORS == DEFAULTS["network_history_federation_selectors"], "rendered match[] equals the role default")
check(len(SELECTORS) == len(set(SELECTORS)), "match[] has no duplicates")

unit = render("prometheus-network.service.j2")
for needle in ("--storage.tsdb.retention.time=90d", "--storage.tsdb.retention.size=4GB", "--web.listen-address=127.0.0.1:9091",
               "--storage.tsdb.path=/var/lib/prometheus-network", "--config.file=/etc/prometheus-network/prometheus.yml",
               "ExecStart=/usr/bin/prometheus", "User=prometheus", "MemoryMax=256M", "NoNewPrivileges=true", "ProtectSystem=strict",
               "ProtectHome=true", "PrivateTmp=true", "CapabilityBoundingSet=", "ReadWritePaths=/var/lib/prometheus-network",
               "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", "ProtectKernelTunables=true", "PrivateDevices=true"):
    check(needle in unit, "prometheus-network.service lacks " + needle)

svc = render("net-history.service.j2")
check("--password-file /etc/net-history/writer.password" in svc, "net-history.service reads the writer password from a file")
check("WRITERPW" not in svc and not re.search(r"--password(\s|=)", svc), "net-history.service must not carry a password")
for needle in ("Type=oneshot", "CapabilityBoundingSet=", "ProtectSystem=strict", "NoNewPrivileges=true", "MemoryMax=256M",
               "--prometheus http://127.0.0.1:9091", "--central-prometheus http://127.0.0.1:9090", "--textfile /var/lib/node_exporter/textfile_collector/net-history.prom",
               "/opt/net-history/venv/bin/python", "--db-user net_history_writer"):
    check(needle in svc, "net-history.service lacks " + needle)
timer = render("net-history.timer.j2")
check("OnCalendar=*:07" in timer and "OnBootSec=2min" in timer and "Persistent=true" in timer, "net-history.timer is hourly and runs after boot")

ds = yaml.safe_load(render("network-history-datasources.yml.j2"))["datasources"]
byuid = {d["uid"]: d for d in ds}
check(set(byuid) == {"prometheus-90d", "net-history"}, "the datasource file defines exactly prometheus-90d and net-history")
check(byuid["prometheus-90d"]["name"] == "Prometheus (network, 90 d)" and byuid["prometheus-90d"]["url"] == "http://127.0.0.1:9091"
      and byuid["prometheus-90d"]["type"] == "prometheus", "prometheus-90d points at 127.0.0.1:9091")
pg = byuid["net-history"]
check(pg["type"] == "grafana-postgresql-datasource" and pg["name"] == "Network history", "net-history is the built-in PostgreSQL datasource")
check(pg["url"] == "10.10.50.126:5432" and pg["user"] == "net_history_reader" and pg["jsonData"]["database"] == "net_history"
      and pg["jsonData"]["sslmode"] == "disable", "net-history connects to the reader role on the memory LXC")
check(pg["secureJsonData"]["password"] == "READERPW", "the reader password goes in secureJsonData")
check(not any(d.get("isDefault") for d in ds), "the history datasources must not take over as default")
ROLE_TEXT = {p: p.read_text() for p in ROLE.rglob("*") if p.is_file() and p.suffix in (".yml", ".py", ".md", ".j2", ".sql")}
check(not any("READERPW" in t or "WRITERPW" in t for p, t in ROLE_TEXT.items() if p.name != "network-history-datasources.yml.j2"), "no password literal in the role files")

prov = render("db-provision.sql.j2")
check("DROP " not in prov.upper() and "TRUNCATE" not in prov.upper(), "the provisioning SQL never drops or truncates")
check("\\connect net_history" in prov and prov.index("\\connect net_history") > prov.index("CREATE DATABASE"),
      "schema work happens after connecting to net_history")
check("jarvis_memory" not in prov, "the provisioning SQL never names the memory store's database")
check("REVOKE ALL ON DATABASE net_history FROM PUBLIC" in prov, "CONNECT is revoked from PUBLIC on net_history")
check("ALTER DEFAULT PRIVILEGES FOR ROLE net_history_writer IN SCHEMA net GRANT SELECT ON TABLES TO net_history_reader" in prov, "reader gets default privileges")
check("default_transaction_read_only = on" in prov, "the reader role is read-only by default")
check("GRANT INSERT" not in prov and "GRANT ALL" not in prov, "the reader is never granted writes")

launcher = render("nethist.j2")
check(launcher.startswith("#!/bin/sh\n# Managed by ansible/roles/network_history.") and "nethist.py" in launcher and "$@" in launcher, "the nethist launcher passes its arguments")
nconf = render("nethist.conf.j2")
check("WRITERPW" not in nconf and "READERPW" not in nconf and "password_file = /home/agent/.config/net-history/reader.password" in nconf, "nethist.conf holds a password path, not a password")

main_tasks = (ROLE / "tasks" / "main.yml").read_text()
check(V["network_history_db_address"] == "10.10.50.126", "the database address derives from the db host's ansible_host: %s" % V["network_history_db_address"])
check(resolved(hostvars={})["network_history_db_address"] == "memory", "without an inventory entry the address falls back to the host name")
check(resolved(hostvars={"memory": {}})["network_history_db_address"] == "memory", "an inventory entry without ansible_host falls back to the host name")
check(V["network_history_pg8000"] == "pg8000==1.31.6" and "network_history_pg8000" in main_tasks, "pg8000 is pinned and installed into a venv")
check("virtualenv:" in main_tasks, "the driver goes in a virtualenv, not system packages")
check(not any("sqlite" in t.lower() for t in ROLE_TEXT.values()), "no SQLite plugin or database in the role")
db_tasks = (ROLE / "tasks" / "database.yml").read_text()
check("no_log: true" in db_tasks and "delegate_to:" in db_tasks and "stdin:" in db_tasks, "the database task is delegated, uses stdin and no_log")
check("ansible_check_mode" in db_tasks, "the database task does nothing in check mode")
client_tasks = (ROLE / "tasks" / "client.yml").read_text()
check(".claude/skills" in "".join(V["network_history_skill_dirs"]) and ".agents/skills" in "".join(V["network_history_skill_dirs"])
      and V["network_history_hermes_skill_dir"] == ".hermes/skills", "the skill goes to the three agent skill directories")
check("no_log: true" in client_tasks and "slurp" in client_tasks, "the client fetches the reader password with slurp and no_log")
skill = (ROLE / "files" / "SKILL.md").read_text()
fm = yaml.safe_load(skill.split("---")[1])
check(fm["name"] == "network-history" and "outage" in fm["description"], "SKILL.md frontmatter has the name and when-to-use description")
for needle in ("nethist catalog", "nethist month", "nethist outages", "nethist sql", "Prometheus", "prometheus-network", "PostgreSQL", "wan <> 'total'"):
    check(needle in skill, "SKILL.md lacks " + needle)
check(len(re.findall(r"^\d\. \"", skill, re.M)) == 5, "SKILL.md has five example questions")
for cmd in re.findall(r"`(nethist [^`]+)`", skill):
    word = shlex.split(cmd)
    sub = [w for w in word[1:] if not w.startswith("--")][0]
    check(sub in ("catalog", "coverage", "month", "day", "outages", "sql"), "SKILL.md shows an unknown nethist command: " + cmd)

# playbook shape
pb = yaml.safe_load((ANSIBLE / "playbooks" / "network-history.yml").read_text())
check(pb[0]["hosts"] == "central" and pb[0]["gather_facts"] is False and pb[1]["gather_facts"] is False, "the playbook targets central and gathers no facts")
check("network-history.yml" in (ANSIBLE / "playbooks" / "site.yml").read_text(), "site.yml imports network-history.yml")

# ------------------------------------------- 2. federation covers every metric ---
SEL = re.compile(r'(\w+)(=~|!=|=)"((?:[^"\\]|\\.)*)"')


def parse_selector(sel):
    return [(n, op, val) for n, op, val in SEL.findall(sel)]


def matches(sel, labels):
    for n, op, val in parse_selector(sel):
        have = labels.get(n, "")
        if op == "=" and have != val:
            return False
        if op == "!=" and have == val:
            return False
        if op == "=~" and not re.fullmatch(val, have):
            return False
    return True


def covered(name, labels=None, selectors=None):
    lab = dict(labels or {}, __name__=name)
    return any(matches(s, lab) for s in (selectors or SELECTORS))


METRIC = re.compile(r"\b((?:internet_probe|er605|opnsense|tplink_router|node_network|fleet_pve)_[a-z0-9_]+)")
NOT_METRICS = {"er605_selector"}  # the aggregator's own option name


def dash(name):
    return json.loads((DASH / ("%s.json" % name)).read_text())


def panels_of(d):
    out = []
    def rec(ps):
        for p in ps:
            out.append(p)
            rec(p.get("panels", []))
    rec(d["panels"])
    return out


def prom_exprs(d):
    """(where, expr) for every query that runs on the ${ds} Prometheus datasource."""
    out = []
    for p in panels_of(d):
        for t in p.get("targets", []):
            if (t.get("datasource") or {}).get("uid") == "${ds}" and t.get("expr"):
                out.append((p["title"], t["expr"]))
    for v in d["templating"]["list"]:
        if v.get("type") == "query" and (v.get("datasource") or {}).get("uid") == "${ds}":
            out.append(("variable " + v["name"], v["definition"]))
    return out


needed = {}
for name in DASHBOARDS:
    for where, expr in prom_exprs(dash(name)):
        for m in METRIC.findall(expr):
            needed.setdefault(m, set()).add("%s: %s" % (name, where))
src = (ROLE / "files" / "net-history.py").read_text()
for m in METRIC.findall(src):
    if not m.endswith("_") and m not in NOT_METRICS:
        needed.setdefault(m, set()).add("net-history.py")
check(len(needed) > 40, "expected to find the dashboard metrics, found %d" % len(needed))
for m, where in sorted(needed.items()):
    if m in ("fleet_pve_guest_nic_info",) or True:
        check(covered(m, {"device": "tailscale0", "host": "t7920", "interface": "WAN"}) or covered(m, {"device": "tap100i0", "host": "t7920"}),
              "no federation selector matches %s (used by %s)" % (m, sorted(where)[0]))
# narrowing: the node_network selectors keep only what the dashboards use
for dev, host, keep in [("tailscale0", "ubuntu-dev", True), ("tap100i0", "t7920", True), ("veth101i0", "t7920", True),
                        ("nic1", "t7920", True), ("nic1", "rpi5", False), ("eno1", "t7920", False), ("lo", "rpi5", False),
                        ("docker0", "ubuntu-dev", False), ("vmbr0", "t7920", False)]:
    for m in ("node_network_receive_bytes_total", "node_network_transmit_bytes_total"):
        check(covered(m, {"device": dev, "host": host}) == keep, "%s device=%s host=%s should be %s by the federation" % (m, dev, host, "kept" if keep else "dropped"))
check(not covered("node_network_receive_drop_total", {"device": "tailscale0"}), "node_network_receive_drop_total is not federated")
check(not covered("opnsense_interfaces_received_bytes_total", {"interface": "igc9"}), "opnsense interfaces are limited to the segment labels")
check(not covered("node_cpu_seconds_total"), "unrelated metrics are not federated")
for m in ("er605_wan_info", "er605_wan_up", "er605_pppoe_session_seconds", "er605_pppoe_reconnects_total", "er605_pppoe_octets_total",
          "internet_probe_path_up", "internet_probe_path_rtt_seconds", "internet_probe_path_failures_total", "internet_probe_path_outage_active",
          "internet_probe_path_down_seconds_total", "internet_probe_path_outages_total"):
    check(covered(m), "multi-WAN metric %s must be federated" % m)
# federation covers only metrics something reads: every name in an alternation is used
used = set(needed) | {"internet_probe_path_outage_active"}  # in the contract; kept for ad-hoc 90-day queries
for sel in SELECTORS:
    for n, op, val in parse_selector(sel):
        if n == "__name__" and op == "=~":
            m = re.fullmatch(r"(\w+?_)\(([^)]*)\)", val)
            if m:
                for alt in m.group(2).split("|"):
                    check(m.group(1) + alt in used, "selector lists %s, which no dashboard or the aggregator reads" % (m.group(1) + alt))

# ------------------------------------------------------------- 3. dashboards ---
SCHEMA_OBJECTS = {t for t, *_ in agg.DAILY} | {v for v, *_ in agg.MONTHLY} | {
    "outage_events", "meta", "schema_version", "coverage", "catalog", "catalog_columns"}
for name in DASHBOARDS:
    d = dash(name)
    ps = panels_of(d)
    ids = [p["id"] for p in d["panels"]]
    check(len(ids) == len(set(ids)) and all(isinstance(i, int) for i in ids), "%s: panel ids unique" % name)
    for i, a in enumerate(d["panels"]):
        for b in d["panels"][i + 1:]:
            ga, gb = a["gridPos"], b["gridPos"]
            if ga["x"] < gb["x"] + gb["w"] and gb["x"] < ga["x"] + ga["w"] and ga["y"] < gb["y"] + gb["h"] and gb["y"] < ga["y"] + ga["h"]:
                check(False, "%s: panels overlap: %s / %s" % (name, a["title"], b["title"]))
    dsv = [v for v in d["templating"]["list"] if v["name"] == "ds"]
    check(len(dsv) == 1 and dsv[0]["type"] == "datasource" and dsv[0]["query"] == "prometheus", "%s: one ds datasource variable" % name)
    if dsv:
        check(dsv[0]["current"]["value"] == "prometheus-90d", "%s: ds defaults to prometheus-90d" % name)
        rx = dsv[0]["regex"].strip("/")
        check(re.search(rx, "Prometheus") and re.search(rx, "Prometheus (network, 90 d)") and not re.search(rx, "Loki") and not re.search(rx, "Network history"),
              "%s: ds regex admits exactly the two Prometheus datasources" % name)
    for p in ps:
        for holder in [p] + p.get("targets", []):
            u = (holder.get("datasource") or {}).get("uid") if isinstance(holder.get("datasource"), dict) else None
            check(u in (None, "${ds}", "loki", "net-history"), "%s: %s uses datasource %s" % (name, p["title"], u))
    for v in d["templating"]["list"]:
        if v["name"] != "ds" and v.get("datasource"):
            check(v["datasource"]["uid"] == "${ds}", "%s: variable %s must use ${ds}" % (name, v["name"]))
    qr = [q["display"] for q in d["timepicker"].get("quick_ranges", [])]
    check("Last 30 days" in qr and "Last 90 days" in qr, "%s: quick ranges include 30 and 90 days" % name)
    check(any(p["type"] == "row" and p["title"] == "Monthly history (permanent)" and p["collapsed"] is False for p in d["panels"]), "%s: has the permanent history row" % name)
    last_row = max(i for i, p in enumerate(d["panels"]) if p["type"] == "row")
    check(d["panels"][last_row]["title"] == "Monthly history (permanent)", "%s: the history row is at the bottom" % name)
    pgp = [p for p in ps if (p.get("datasource") or {}).get("uid") == "net-history"]
    check(len(pgp) >= 2, "%s: has SQL panels" % name)
    for p in pgp:
        check(p["datasource"]["type"] == "grafana-postgresql-datasource", "%s: %s datasource type" % (name, p["title"]))
        for t in p["targets"]:
            check(t["format"] in ("time_series", "table") and t["rawQuery"] and t["rawSql"].lstrip().upper().startswith("SELECT"), "%s: %s target shape" % (name, p["title"]))
            for obj in re.findall(r"\bnet\.(\w+)", t["rawSql"]):
                check(obj in SCHEMA_OBJECTS, "%s: %s queries net.%s, which is not in the schema" % (name, p["title"], obj))
            if t["format"] == "time_series":
                check(re.match(r"SELECT [^,]+ AS time, ", t["rawSql"]) and " AS metric" in t["rawSql"] and " AS value" in t["rawSql"], "%s: %s time series query needs time, metric, value" % (name, p["title"]))
    # Loki panels keep their queries: the originals are in git history; check the selectors are intact
    for p in ps:
        for t in p.get("targets", []):
            if (t.get("datasource") or {}).get("uid") == "loki":
                check(t["expr"].lstrip().startswith(("{", "sum(", "sum by", "count")), "%s: Loki query changed shape in %s" % (name, p["title"]))

for name in DASHBOARDS:
    for where, expr in prom_exprs(dash(name)):
        check(not re.search(r"group_left \([^)]*\)\s+(fleet_pve_guest_nic_info|er605_wan_info)\b", expr),
              "%s: %s joins an info metric without the newest-series dedupe (federated data keeps overlaps)" % (name, where))
        check("sum(tplink_router_client_info" not in expr, "%s: %s counts clients without deduplicating by mac" % (name, where))
        if 'wan=""' in expr:
            check("label_replace(" in expr and "and on (wan)" in expr, "%s: %s: the unlabelled PPPoE fallback must become wan1 and be filtered by $wan" % (name, where))
for t_ in [t for p in panels_of(dash("internet-usage")) if p["title"].startswith("Top 10 guests") for t in p["targets"]]:
    check("topk by (host, device) (1, timestamp(fleet_pve_guest_nic_info))" in t_["expr"], "the Top 10 guests panels join the newest guest NIC info series")
for p in panels_of(dash("home-isp")):
    if p["title"] in ("Downloaded, last 24 h", "Uploaded, last 24 h"):
        check(all(t["expr"].startswith("sum(") for t in p["targets"]), "home-isp: %s sums across WANs" % p["title"])
    if p["title"] == "Download and upload per month, per WAN":
        check(p["targets"][0]["rawSql"].count("wan <> 'total'") == 2, "home-isp: the per-WAN monthly chart excludes the total rows")
check(any("|| ' ' || mac AS metric" in t["rawSql"] for p in panels_of(dash("router-tplink-blr")) if p["title"] == "Usage per client per month" for t in p["targets"]),
      "router-tplink-blr: clients sharing a name stay separate in the monthly chart")

lan = dash("inter-lan")
p22 = next(p for p in panels_of(lan) if p["title"] == "Segments NetFlow does not capture")
check("WAN" not in p22["targets"][0]["expr"] and "LAN|TENANT|DMZ|LAB|LEASE" in p22["targets"][0]["expr"], "inter-lan: the not-captured query no longer lists WAN")
check(any("WAN" in t["expr"] for p in panels_of(lan) if p["title"].startswith("Received per segment") for t in p["targets"]), "inter-lan: the segment rate panels still show WAN")

home = dash("home-isp")
wanv = next(v for v in home["templating"]["list"] if v["name"] == "wan")
check(wanv["multi"] and wanv["includeAll"] and "er605_wan_info" in wanv["definition"] and "wan1" in wanv["definition"], "home-isp: wan variable with the one-WAN fallback")
check(re.search(wanv["regex"].strip("/"), '{wan="wan2"} 1 @1').group(1) == "wan2", "home-isp: the wan variable regex extracts the name")
wrow = next(p for p in home["panels"] if p.get("repeat") == "wan")
check(wrow["type"] == "row" and "$wan" in wrow["title"], "home-isp: the per-WAN row repeats by wan")
nxt = [p for p in home["panels"] if p["gridPos"]["y"] > wrow["gridPos"]["y"]]
inside = []
for p in sorted(home["panels"], key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"])):
    if p["gridPos"]["y"] > wrow["gridPos"]["y"]:
        if p["type"] == "row":
            break
        inside.append(p)
titles = [p["title"] for p in inside]
for want in ("WAN state", "WAN up or down", "Throughput", "Daily volume", "PPPoE session age", "PPPoE reconnects", "Path up or down", "Path availability in range"):
    check(want in titles, "home-isp: per-WAN row lacks %s" % want)
for p in inside:
    for t in p["targets"]:
        check("$wan" in t["expr"], "home-isp: %s must filter on $wan" % p["title"])
        if "er605_if_octets_total" in t["expr"]:
            check("on (interface) group_left (wan) ((topk by (interface) (1, timestamp(er605_wan_info" in t["expr"] and "unless on() er605_wan_info" in t["expr"],
                  "home-isp: %s joins on interface with the one-WAN fallback" % p["title"])
        if "pppoe" in t["expr"]:
            check('wan=""' in t["expr"], "home-isp: %s keeps unlabelled PPPoE metrics visible" % p["title"])
for p in inside:
    if "path" in p["title"].lower():
        check(p["fieldConfig"]["defaults"].get("noValue"), "home-isp: %s says what an empty panel means" % p["title"])
iu = dash("internet-usage")
split = [p for p in iu["panels"] if p["title"] in ("Daily usage per WAN", "Household usage per WAN, in range")]
check(len(split) == 2 and all("er605_wan_info" in t["expr"] and "unless on() er605_wan_info" in t["expr"] for p in split for t in p["targets"]),
      "internet-usage: household usage is split per WAN with the one-WAN fallback")
check(any("Outage history" in p["title"] and "'path'" in p["targets"][0]["rawSql"] for p in iu["panels"]), "internet-usage: permanent outage list")
check(any("outage history" in p["title"].lower() and "kind IN ('lan')" in p["targets"][0]["rawSql"] for p in lan["panels"]), "inter-lan: permanent LAN outage list")

# ---------------------------------------------------- 4. aggregator, unit level ---
ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
check(agg.ist_day(dt.datetime(2026, 11, 26, 18, 29, 59, tzinfo=dt.timezone.utc).timestamp()) == dt.date(2026, 11, 26), "18:29:59Z is still 26 Nov in IST")
check(agg.ist_day(dt.datetime(2026, 11, 26, 18, 30, 0, tzinfo=dt.timezone.utc).timestamp()) == dt.date(2026, 11, 27), "18:30:00Z starts 27 Nov in IST")
check(agg.day_start(dt.date(2026, 11, 27)).astimezone(dt.timezone.utc) == dt.datetime(2026, 11, 26, 18, 30, tzinfo=dt.timezone.utc), "IST midnight is 18:30Z the day before")
check(agg.day_end(dt.date(2026, 11, 27)) - agg.day_start(dt.date(2026, 11, 27)) == dt.timedelta(days=1), "an IST day is 24 hours")
now_u = dt.datetime(2026, 11, 26, 20, 0, tzinfo=dt.timezone.utc)  # 28 Nov? no: 27 Nov 01:30 IST
check(agg.days_to_compute(now_u, 2) == [dt.date(2026, 11, 25), dt.date(2026, 11, 26), dt.date(2026, 11, 27)], "days_to_compute counts IST days")
check(agg.parse_when("2026-11-27T05:01:02Z") == dt.datetime(2026, 11, 27, 5, 1, 2, tzinfo=dt.timezone.utc), "parse_when reads Z")
e1 = agg.parse_outage_line('outage_end site=home start=2026-11-27T01:00:00Z end=2026-11-27T01:10:00Z duration_seconds=600 cause=gateway closed_by=probe')
check(e1 and e1["kind"] == "internet" and e1["name"] == "internet" and e1["duration_seconds"] == 600 and e1["cause"] == "gateway", "outage_end line parses")
e2 = agg.parse_outage_line('lan_up site=blr name=er605 target=10.0.0.1:80 start=2026-11-27T01:00:00Z end=2026-11-27T01:01:00Z duration_seconds=60 closed_by=restart')
check(e2 and e2["kind"] == "lan" and e2["name"] == "er605" and e2["target"] == "10.0.0.1:80" and e2["closed_by"] == "restart", "lan_up line parses")
e3 = agg.parse_outage_line('path_up site=home name=wan2 start=2026-11-27T01:00:00Z end=2026-11-27T01:05:00Z duration_seconds=300 closed_by=probe')
check(e3 and e3["kind"] == "path" and e3["name"] == "wan2", "path_up line parses")
e4 = agg.parse_outage_line('wan_up at=2026-11-27T02:00:00Z wan=wan2 duration_seconds=120')
check(e4 and e4["kind"] == "wan" and e4["started_at"] == dt.datetime(2026, 11, 27, 1, 58, tzinfo=dt.timezone.utc) and e4["site"] == "home", "wan_up line parses (start = at - duration)")
check(agg.parse_outage_line("outage_start site=home start=2026-11-27T01:00:00Z") is None and agg.parse_outage_line("lan_up site=x") is None, "other and malformed lines are ignored")
check(set(agg.build_parser().parse_args([]).__dict__) >= {"password_file"} and not any(o in ("--password", "--db-password") for a in agg.build_parser()._actions for o in a.option_strings),
      "the aggregator takes a password file, never a password option")
check("PGPASSWORD" not in (ROLE / "files" / "net-history.py").read_text(), "the aggregator never uses PGPASSWORD")
table = agg.format_table(["a", "bb"], [(1, "x"), (22, None)])
check(table.splitlines()[0].split() == ["a", "bb"] and len(table.splitlines()) == 4, "format_table aligns")


class FakeFetcher:
    window = {dt.date(2026, 11, 27): 86400.0}
    def __init__(self, answers):
        self.answers = answers
    def fetch(self, template):
        for key, val in self.answers.items():
            if key in template:
                return {dt.date(2026, 11, 27): val}
        return {}


d27 = dt.date(2026, 11, 27)
rows = agg.build_wan(FakeFetcher({"group_left (wan)": [({"wan": "wan1"}, 100.0), ({"wan": "wan2"}, 40.0)], "increase(er605_pppoe": [({}, 3.0)]}))
check(sorted((r["wan"], r["download_bytes"]) for r in rows) == [("wan1", 100), ("wan2", 40)], "build_wan splits per WAN from the join")
check({r["wan"]: r["pppoe_reconnects"] for r in rows} == {"wan1": 3, "wan2": None}, "an unlabelled PPPoE counter belongs to wan1")
rows = agg.build_wan(FakeFetcher({"sum(increase(er605_if_octets_total{role=\"wan\",direction=\"in\"}": [({}, 500.0)]}))
check([(r["wan"], r["download_bytes"]) for r in rows] == [("wan1", 500)], "without er605_wan_info the household total is wan1")


# ---- review fixes that need no database ----
class CaptureFetcher(FakeFetcher):
    def __init__(self, answers):
        super().__init__(answers)
        self.templates = []
    def fetch(self, template):
        self.templates.append(template)
        return super().fetch(template)


cf = CaptureFetcher({"group_left (wan)": [({"wan": "wan1"}, 100.0)]})
agg.build_wan(cf)
join = [t for t in cf.templates if "group_left (wan)" in t]
check(len(join) == 2 and all("max by (interface, wan) (last_over_time(er605_wan_info[__W__]))" in t for t in join),
      "the WAN join reads er605_wan_info over the whole window, not at the evaluation instant: %s" % (join,))
check(not any("er605_wan_info)" in t.replace("last_over_time(er605_wan_info[__W__]))", "") for t in join), "no bare er605_wan_info instant join is left")
rows = agg.build_wan(FakeFetcher({"group_left (wan)": [({"wan": "wan1"}, 100.0)],
                                  "increase(er605_pppoe": [({}, 2.0), ({"wan": "wan1"}, 3.0), ({"wan": "wan2"}, 1.0)]}))
check({r["wan"]: r["pppoe_reconnects"] for r in rows} == {"wan1": 5}, "the unlabelled and the wan1 reconnect series are summed, not overwritten: %s" % ({r["wan"]: r["pppoe_reconnects"] for r in rows},))


class FakeCur:
    def __init__(self, conn):
        self.conn = conn
    def execute(self, sql, params=()):
        self.conn.statements.append(sql)
        self.last = sql
    def fetchall(self):
        return [(dt.date(2026, 11, 26),)] if "coverage IS NULL" in self.last and "SELECT DISTINCT" in self.last and "internet_daily" in self.last else []
    rowcount = 1


class FakeConn:
    def __init__(self):
        self.statements, self.commits = [], 0
    def cursor(self):
        return FakeCur(self)
    def commit(self):
        self.commits += 1


class DownProm:
    def range(self, *a):
        raise agg.RunError("90-day store down")


class EmptyProm:
    def range(self, *a):
        return []


now_c = dt.datetime(2026, 11, 27, 10, 0, tzinfo=dt.timezone.utc)


fc = FakeConn()
try:
    agg.fill_unknown_coverage(fc, [("90d", DownProm())], now_c)
    check(False, "fill_unknown_coverage must stop when the 90-day store fails")
except agg.RunError:
    pass
check(not any(s.startswith("UPDATE") for s in fc.statements) and fc.commits == 0, "a failing 90-day store leaves NULL coverage alone and commits nothing")
fc = FakeConn()
try:
    with contextlib.redirect_stdout(io.StringIO()):
        agg.fill_unknown_coverage(fc, [("90d", EmptyProm()), ("central", DownProm())], now_c)
except agg.RunError:
    check(False, "a failing central store must not stop the fill")
check(any(s.startswith("UPDATE net.internet_daily") for s in fc.statements), "a failing central store is tolerated; the update runs")

# ---- coverage arithmetic and source selection (no database) ----
now_c = dt.datetime(2026, 11, 27, 10, 0, tzinfo=dt.timezone.utc)
check(agg.coverage_of(None, dt.date(2026, 11, 26), now_c) == 0.0, "no first sample: no coverage")
check(agg.coverage_of(agg.EVERYTHING, dt.date(2026, 11, 26), now_c) == 1.0, "data before the window: full coverage")
check(agg.coverage_of(dt.datetime(2026, 11, 26, 6, 0, tzinfo=dt.timezone.utc).timestamp(), dt.date(2026, 11, 26), now_c) == 0.521, "12.5 h of 24 is 0.521")
check(agg.coverage_of(dt.datetime(2026, 11, 27, 5, 0, tzinfo=dt.timezone.utc).timestamp(), dt.date(2026, 11, 27), now_c) == 0.323, "today: 5 h of the 15.5 h elapsed is 0.323")
check(agg.coverage_of(dt.datetime(2026, 11, 28, 5, 0, tzinfo=dt.timezone.utc).timestamp(), dt.date(2026, 11, 26), now_c) == 0.0, "a store that starts after the day covers none of it")


class RecProm:
    def __init__(self):
        self.calls = []
    def range(self, query, start, end, step):
        self.calls.append((start, end, step))
        return []
    def instant(self, query, when):
        return []


rp = RecProm()
agg.Fetcher(rp, [dt.date(2026, 11, 20), dt.date(2026, 11, 25), dt.date(2026, 11, 26)], now_c).fetch("x")
check(len(rp.calls) == 2, "scattered days are fetched as contiguous runs, not as one wide range: %s" % (rp.calls,))
fw = agg.Fetcher(rp, [dt.date(2026, 11, 26)], now_c, {dt.date(2026, 11, 26): 0.5})
check(fw.window[dt.date(2026, 11, 26)] == 43200 and fw.span[dt.date(2026, 11, 26)] == 86400, "window is the covered part, span the queried one")

# ------------------------------------------------------- 5. nethist, no DB needed ---
P = nethist.parser()
check(P.parse_args(["month", "wan", "--months", "3"]).months == 3, "month --months parses")
check(P.parse_args(["--json", "day", "path"]).table == "path" and P.parse_args(["--json", "day", "path"]).json, "global --json before the command")
check(P.parse_args(["outages", "--lan", "--site", "home"]).lan, "outages --lan")
for bad in (["month", "nope"], ["day"], ["outages", "--lan", "--all"], []):
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            P.parse_args(bad)
            check(False, "nethist should reject %r" % (bad,))
        except SystemExit:
            pass
for tname in ("internet", "lan", "path", "wan", "segment", "segflow", "client", "guest", "tailnet"):
    q = nethist.build_query(P.parse_args(["month", tname]))
    obj = re.search(r"net\.(\w+)", q).group(1)
    check(obj in SCHEMA_OBJECTS and obj.startswith("monthly_"), "nethist month %s reads a real view (%s)" % (tname, obj))
    q = nethist.build_query(P.parse_args(["day", tname]))
    check(re.search(r"net\.(\w+)", q).group(1) in SCHEMA_OBJECTS, "nethist day %s reads a real table" % tname)
check("'lan'" in nethist.build_query(P.parse_args(["outages", "--lan"])) and "'internet'" not in nethist.build_query(P.parse_args(["outages", "--lan"])), "--lan shows LAN only")
check("site = 'home'" in nethist.build_query(P.parse_args(["outages", "--site", "home"])), "--site filters")
try:
    nethist.build_query(P.parse_args(["outages", "--site", "x'; DROP TABLE a;--"]))
    check(False, "a quote in --site must be rejected")
except SystemExit:
    pass
check(nethist.build_query(P.parse_args(["sql", "SELECT 1;"])) == "SELECT 1", "sql strips the trailing semicolon")
help_text = P.format_help()
for sub in ("catalog", "coverage", "month", "day", "outages", "sql"):
    check(sub in help_text, "nethist --help lists " + sub)
check("--json" in help_text and "--csv" in help_text and "examples:" in help_text, "nethist --help shows the formats and examples")


def run_nethist(argv, extra_env=None):
    """Run nethist against a fake docker that records argv and the pgpass it was given."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nethist-test-"))
    try:
        (tmp / "bin").mkdir()
        fake = tmp / "bin" / "docker"
        fake.write_text("""#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_ARGV"
for a in "$@"; do case "$a" in *:/pgpass:ro) f="${a%%:/pgpass:ro}"; cp "$f" "$FAKE_PGPASS"; stat -c %a "$f" > "$FAKE_MODE";; esac; done
echo "fake output"
""")
        fake.chmod(0o755)
        pw = tmp / "reader.password"
        pw.write_text("S3cr3t-Pa55:word\n")
        pw.chmod(0o600)
        env = dict(os.environ, PATH=str(tmp / "bin") + os.pathsep + os.environ["PATH"], FAKE_ARGV=str(tmp / "argv"),
                   FAKE_PGPASS=str(tmp / "pgpass"), FAKE_MODE=str(tmp / "mode"), HOME=str(tmp))
        env.pop("PGPASSWORD", None)
        script = str(ROLE / "files" / "nethist.py")
        r = subprocess.run([sys.executable, script, "--password-file", str(pw)] + argv, env=env, capture_output=True, text=True)
        argv_seen = (tmp / "argv").read_text().splitlines() if (tmp / "argv").exists() else []
        return r, argv_seen, (tmp / "pgpass").read_text() if (tmp / "pgpass").exists() else "", \
            (tmp / "mode").read_text().strip() if (tmp / "mode").exists() else "", tmp
    finally:
        pass


r, argv_seen, pgpass, mode, tmpd = run_nethist(["month", "wan"])
check(r.returncode == 0 and "fake output" in r.stdout, "nethist runs docker and prints its output: %s" % r.stderr[-200:])
check(argv_seen[:4] == ["run", "--rm", "--network", "host"] and "postgres:16-alpine" in argv_seen and "psql" in argv_seen, "nethist runs psql in the pinned image")
check(not any("S3cr3t" in a for a in argv_seen), "the password is never on the docker command line")
check("S3cr3t-Pa55\\:word" in pgpass and pgpass.startswith("10.10.50.126:5432:net_history:net_history_reader:"), "the pgpass handed to psql holds the escaped password")
check(mode == "600", "the temporary pgpass is mode 0600 (was %s)" % mode)
check(not list(pathlib.Path(tempfile.gettempdir()).glob("nethist-pgpass-*")), "the temporary pgpass is deleted")
check(not any(a.startswith("-e") and "S3cr3t" in a for a in argv_seen) and not any("PASSWORD" in a for a in argv_seen), "no password environment variable is passed")
check("default_transaction_read_only=on" in " ".join(argv_seen), "psql is started read-only")
r2, argv2, _, _, tmp2 = run_nethist(["--json", "sql", "SELECT 1"])
check("json_agg" in " ".join(argv2) and "-A" in argv2, "--json wraps the query in json_agg")
r3, argv3, _, _, tmp3 = run_nethist(["--csv", "day", "wan"])
check("--csv" in argv3, "--csv passes --csv to psql")
shutil.rmtree(tmpd, ignore_errors=True); shutil.rmtree(tmp2, ignore_errors=True); shutil.rmtree(tmp3, ignore_errors=True)

# ------------------------------------------------ 6. fake Prometheus and Loki ---

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path.startswith("/api/v1/query"):
            body = self.server.prom(url.path, qs)
        elif url.path == "/loki/api/v1/query_range":
            body = self.server.loki(qs)
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.missing = set()
        self.first = None   # epoch seconds of the first sample this store holds (None: all history)
        self.scale = 1.0    # multiplies every data value, so a test can tell which store a row came from
        self.no_wan_info = False
        self.queries = []
        self.loki_lines = {"internet-probe": [], "er605-poll": []}
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]

    def answer(self, q):
        self.queries.append(q)
        if q.startswith("count("):  # a coverage probe: does the source metric exist at all?
            return [({}, 1.0)] if self.answer(q[6:-1]) else []
        pairs = [
            ("internet_probe_lan_down_seconds_total", "lan", [({"site": "home", "name": "er605"}, 120.0)]),
            ("internet_probe_lan_outages_total", "lan", [({"site": "home", "name": "er605"}, 1.0)]),
            ("internet_probe_path_down_seconds_total", "path", [({"site": "home", "name": "wan1"}, 60.0), ({"site": "home", "name": "wan2"}, 0.0)]),
            ("internet_probe_path_outages_total", "path", [({"site": "home", "name": "wan1"}, 1.0)]),
            ("internet_probe_down_seconds_total", "internet", [({"site": "home"}, 600.0), ({"site": "blr"}, 0.0)]),
            ("internet_probe_outages_total", "internet", [({"site": "home"}, 2.0), ({"site": "blr"}, 0.0)]),
            ("er605_pppoe_reconnects_total", "reconnects", [({"wan": "wan1"}, 1.0)]),
            ("opnsense_interfaces_received_bytes_total", "segment", [({"interface": "LAN"}, 5000.0)]),
            ("opnsense_interfaces_transmitted_bytes_total", "segment", [({"interface": "LAN"}, 4000.0)]),
            ("opnsense_segflow_bytes_total", "segflow", [({"src_segment": "TENANT", "dst_segment": "INTERNET"}, 7000.0)]),
            ("opnsense_segflow_packets_total", "segflow", [({"src_segment": "TENANT", "dst_segment": "INTERNET"}, 70.0)]),
            ("opnsense_segflow_flows_total", "segflow", [({"src_segment": "TENANT", "dst_segment": "INTERNET"}, 7.0)]),
            ('direction="download"', "client", [({"router": "blr", "mac": "aa:bb", "name": "phone"}, 900.0)]),
            ('direction="upload"', "client", [({"router": "blr", "mac": "aa:bb", "name": "phone"}, 100.0)]),
            ("tplink_router_wifi_client_bytes_total", "client", [({"router": "blr", "mac": "aa:bb", "name": "phone"}, 1000.0)]),
            ("node_network_receive_bytes_total{device=~", "guest", [({"guest": "vm1"}, 300.0)]),
            ("node_network_transmit_bytes_total{device=~", "guest", [({"guest": "vm1"}, 200.0)]),
            ('node_network_receive_bytes_total{device="tailscale0"', "tailnet", [({"host": "ubuntu-dev"}, 10.0)]),
            ('node_network_transmit_bytes_total{device="tailscale0"', "tailnet", [({"host": "ubuntu-dev"}, 20.0)]),
        ]
        for needle, key, res in pairs:
            if needle in q:
                return [] if key in self.missing else res
        if "er605_if_octets_total" in q:
            din = 'direction="in"' in q
            if "group_left (wan)" in q:
                if self.no_wan_info or "octets" in self.missing:
                    return []
                return [({"wan": "wan1"}, 1000.0 if din else 200.0), ({"wan": "wan2"}, 400.0 if din else 50.0)]
            return [] if "octets" in self.missing else [({}, 1400.0 if din else 250.0)]
        return []

    def prom(self, path, qs):
        if path.endswith("/query_range"):
            start, end, step = float(qs["start"]), float(qs["end"]), float(qs["step"])
            res = self.answer(qs["query"])
            is_count = qs["query"].startswith("count(")
            vals = []
            t = start
            while t <= end + 1e-6:
                if not (is_count and self.first is not None and t < self.first):
                    vals.append([t, "0"])
                t += step
            series = []
            for labels, v in res:
                if vals:
                    series.append({"metric": labels, "values": [[ts, repr(v if is_count else v * self.scale)] for ts, _ in vals]})
            return {"status": "success", "data": {"resultType": "matrix", "result": series}}
        res = self.answer(qs["query"])
        return {"status": "success", "data": {"resultType": "vector", "result": [{"metric": l, "value": [float(qs["time"]), repr(v * self.scale)]} for l, v in res]}}

    def loki(self, qs):
        key = "er605-poll" if "er605-poll" in qs["query"] else "internet-probe"
        start, end, limit = int(qs["start"]), int(qs["end"]), int(qs["limit"])
        lines = sorted((ts, ln) for ts, ln in self.loki_lines[key] if start <= ts < end)[:limit]
        return {"status": "success", "data": {"resultType": "streams", "result": [{"stream": {"unit": key}, "values": [[str(ts), ln] for ts, ln in lines]}] if lines else []}}


# ------------------------------------------------- 7. real PostgreSQL, if possible ---
def have_docker():
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


try:
    import pg8000.dbapi  # noqa: F401
    HAVE_PG8000 = True
except ImportError:
    HAVE_PG8000 = False

DB_OK = have_docker() and HAVE_PG8000
if not DB_OK:
    skipped.append("database checks (needs docker%s and pg8000%s)" % ("" if have_docker() else " (missing)", "" if HAVE_PG8000 else " (missing; set NET_HISTORY_TEST_PYLIB)"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Pg:
    def __init__(self):
        self.name = "nh-test-%d" % os.getpid()
        self.port = free_port()
        subprocess.run(["docker", "run", "--rm", "-d", "--name", self.name, "-p", "127.0.0.1:%d:5432" % self.port,
                        "-e", "POSTGRES_USER=jarvis", "-e", "POSTGRES_PASSWORD=test", "postgres:16-alpine"], check=True, capture_output=True)
        for _ in range(60):
            r = subprocess.run(["docker", "exec", self.name, "psql", "-U", "jarvis", "-h", "127.0.0.1", "-c", "select 1"], capture_output=True)
            if r.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("postgres did not start")

    def psql(self, sql, db="postgres", user="jarvis", stdin=True):
        r = subprocess.run(["docker", "exec", "-i", self.name, "psql", "-U", user, "-d", db, "-v", "ON_ERROR_STOP=1", "-X", "-A", "-t", "-f", "-"],
                           input=sql, capture_output=True, text=True)
        return r

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def connect(pgobj, user, password, db="net_history"):
    import pg8000.dbapi
    return pg8000.dbapi.connect(user=user, password=password, host="127.0.0.1", port=pgobj.port, database=db, timeout=20)


def agg_args(pgobj, prom, loki, pwfile, *extra):
    return ["--prometheus", prom.url, "--central-prometheus", "", "--loki", loki.url, "--db-host", "127.0.0.1", "--db-port", str(pgobj.port),
            "--password-file", str(pwfile)] + list(extra)


def run_agg(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = agg.main(argv)
    return rc, out.getvalue()


def q1(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = [tuple(r) for r in cur.fetchall()]
    conn.rollback()
    return rows


if DB_OK:
    pgdb = Pg()
    prom, loki = FakeServer(), FakeServer()
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="nethist-db-"))
    try:
        # the decoy "memory store": the role must never touch it
        pgdb.psql("CREATE DATABASE jarvis_memory;")
        sql = render("db-provision.sql.j2", network_history_writer_password="wpw-test", network_history_reader_password="rpw-test")
        first = pgdb.psql(sql)
        check(first.returncode == 0, "provisioning SQL runs: %s" % first.stderr[-300:])
        check("net_history: created database net_history" in first.stdout and "created role net_history_writer" in first.stdout, "first run reports what it created")
        second = pgdb.psql(sql)
        check(second.returncode == 0 and "net_history: created" not in second.stdout, "provisioning SQL is idempotent (second run creates nothing): %s" % second.stderr[-200:])
        roles = pgdb.psql("SELECT rolname, rolsuper, rolcreatedb FROM pg_roles WHERE rolname LIKE 'net_history_%' ORDER BY 1").stdout.split()
        check(roles == ["net_history_reader|f|f", "net_history_writer|f|f"], "two non-super roles exist: %s" % (roles,))
        check(pgdb.psql("SELECT datacl::text FROM pg_database WHERE datname='jarvis_memory'").stdout.strip() == "", "the memory store's ACL was not modified")
        (tmpdir / "w.pw").write_text("wpw-test")
        (tmpdir / "r.pw").write_text("rpw-test")
        # an unrelated role cannot connect to net_history
        pgdb.psql("CREATE ROLE stranger LOGIN PASSWORD 'x';")
        try:
            connect(pgdb, "stranger", "x")
            check(False, "a role without a grant must not connect to net_history")
        except Exception:
            pass

        NOW = "2026-11-27T10:00:00Z"
        for lst in (loki.loki_lines["internet-probe"], loki.loki_lines["er605-poll"]):
            lst.clear()
        base = int(dt.datetime(2026, 11, 27, 9, 0, tzinfo=dt.timezone.utc).timestamp() * 1e9)
        loki.loki_lines["internet-probe"] += [
            (base + 1, "outage_end site=home start=2026-11-27T08:00:00Z end=2026-11-27T08:10:00Z duration_seconds=600 cause=gateway closed_by=probe"),
            (base + 2, "outage_end site=home start=2026-11-27T08:00:00Z end=2026-11-27T08:10:00Z duration_seconds=600 cause=gateway closed_by=probe"),
            (base + 3, "lan_up site=home name=er605 target=10.0.0.1:80 start=2026-11-27T07:00:00Z end=2026-11-27T07:01:00Z duration_seconds=60 closed_by=probe"),
            (base + 4, "path_up site=home name=wan2 start=2026-11-27T06:00:00Z end=2026-11-27T06:05:00Z duration_seconds=300 closed_by=probe"),
            (base + 5, "outage_start site=home start=2026-11-27T09:50:00Z"),
        ]
        loki.loki_lines["er605-poll"] += [(base + 6, "wan_up at=2026-11-27T05:00:00Z wan=wan2 duration_seconds=120")]
        agg.LOKI_LIMIT = 2  # force pagination
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--now", NOW, "--textfile", str(tmpdir / "nh.prom")))
        check(rc == 0, "first run succeeds: %s" % out)
        first_line = [l for l in out.splitlines() if l.startswith("run ")]
        check(len(first_line) == 1 and "result=ok" in first_line[0] and "backfill=90" in first_line[0], "one run line, first run back-fills 90 days: %s" % (first_line,))
        w = connect(pgdb, "net_history_writer", "wpw-test")
        count = lambda t: q1(w, "SELECT count(*) FROM net.%s" % t)[0][0]
        check(count("internet_daily") == 2 * 91, "back-fill wrote 91 days x 2 sites (got %d)" % count("internet_daily"))
        check(q1(w, "SELECT value FROM net.meta WHERE key='backfilled_at'") != [], "meta records the back-fill")
        check(q1(w, "SELECT max(version) FROM net.schema_version")[0][0] == agg.schema_version(), "schema_version is recorded")
        check(count("outage_events") == 4, "outage events deduped and paginated: %s" % (q1(w, "SELECT kind, name FROM net.outage_events ORDER BY 1,2"),))
        check(sorted(r[0] for r in q1(w, "SELECT kind FROM net.outage_events")) == ["internet", "lan", "path", "wan"], "all four outage kinds stored")
        wan_ev = q1(w, "SELECT started_at, ended_at FROM net.outage_events WHERE kind='wan'")[0]
        check((wan_ev[1] - wan_ev[0]).total_seconds() == 120, "a wan outage starts duration_seconds before wan_up")
        today = q1(w, "SELECT window_seconds, down_seconds, availability FROM net.internet_daily WHERE day = '2026-11-27' AND site = 'home'")[0]
        check(abs(today[0] - 15.5 * 3600) < 1, "today is a partial IST day (10:00Z = 15:30 IST -> 15.5 h, got %s)" % (today[0],))
        check(abs(today[2] - (1 - today[1] / today[0])) < 1e-5, "availability = 1 - down/window")
        full = q1(w, "SELECT window_seconds FROM net.internet_daily WHERE day = '2026-11-26' AND site = 'home'")[0][0]
        check(full == 86400, "a complete day covers 86400 s")
        check(sorted(r[0] for r in q1(w, "SELECT DISTINCT wan FROM net.wan_daily")) == ["wan1", "wan2"], "wan_daily has one row per WAN")
        check(count("path_daily") == 2 * 91 and count("segflow_daily") == 91 and count("client_daily") == 91 and count("guest_daily") == 91 and count("tailnet_daily") == 91 and count("segment_daily") == 91 and count("lan_daily") == 91,
              "every daily table filled for 91 days")
        # idempotent: run again, same rows, still one run line, no second back-fill
        before = {t: count(t) for t in ("internet_daily", "wan_daily", "client_daily", "outage_events")}
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--now", NOW))
        after = {t: count(t) for t in before}
        check(rc == 0 and before == after, "a second run changes no row counts (%s vs %s)" % (before, after))
        check("backfill=0" in out, "the second run does not back-fill")
        # outage update: the same event, different duration and cause, updates in place
        loki.loki_lines["internet-probe"][0] = (base + 1, "outage_end site=home start=2026-11-27T08:00:00Z end=2026-11-27T08:20:00Z duration_seconds=1200 cause=upstream closed_by=restart")
        loki.loki_lines["internet-probe"][1] = loki.loki_lines["internet-probe"][0]
        run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--now", NOW))
        row = q1(w, "SELECT duration_seconds, cause, closed_by FROM net.outage_events WHERE kind='internet'")
        check(count("outage_events") == 4 and row == [(1200.0, "upstream", "restart")], "an outage event upserts: %s" % (row,))
        # monthly math
        mi = q1(w, "SELECT days, window_seconds, down_seconds, outages, availability FROM net.monthly_internet WHERE site='home' AND month='2026-11-01'")[0]
        exp = 1 - mi[2] / mi[1]
        check(abs(mi[4] - exp) < 1e-9, "monthly availability is 1 - sum(down)/sum(window): %s vs %s" % (mi[4], exp))
        check(mi[0] == 27 and mi[3] == 2 * 27, "November rows: 27 days, outages summed")
        daily_avg = q1(w, "SELECT avg(availability) FROM net.internet_daily WHERE site='home' AND day >= '2026-11-01'")[0][0]
        check(abs(daily_avg - mi[4]) > 1e-7, "the monthly value is not the mean of the daily ratios (today is partial)")
        mw = {r[0]: r[1:] for r in q1(w, "SELECT wan, download_bytes, upload_bytes, total_bytes, days FROM net.monthly_wan WHERE month='2026-11-01'")}
        check(set(mw) == {"wan1", "wan2", "total"}, "monthly_wan has wan1, wan2 and total")
        check(mw["total"][0] == mw["wan1"][0] + mw["wan2"][0] and mw["total"][2] == mw["total"][0] + mw["total"][1] and mw["total"][3] == mw["wan1"][3],
              "the total row sums the WANs and counts days once: %s" % (mw,))
        check(mw["wan1"][0] == 1000 * 27 and mw["wan2"][0] == 400 * 27, "per-WAN bytes sum the days")
        mp = q1(w, "SELECT availability FROM net.monthly_path WHERE name='wan1' AND month='2026-11-01'")
        check(mp and 0 < mp[0][0] < 1, "monthly_path has availability")
        for view in ("monthly_lan", "monthly_segment", "monthly_segflow", "monthly_client", "monthly_guest", "monthly_tailnet"):
            check(q1(w, "SELECT count(*) FROM net.%s" % view)[0][0] >= 1, "%s has rows" % view)
        check(q1(w, "SELECT name FROM net.monthly_client")[0][0] == "phone", "monthly_client keeps the latest name")
        # missing source: segflow absent, octets join gone (one WAN)
        prom.missing = {"segflow"}
        prom.no_wan_info = True
        w.cursor().execute("DELETE FROM net.segflow_daily"); w.commit()
        w.cursor().execute("DELETE FROM net.wan_daily WHERE day = '2026-11-27'"); w.commit()
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--now", NOW))
        check(rc == 0 and "skip table=segflow_daily reason=no_data" in out and "skipped=segflow_daily" in out, "a missing metric skips its table and the run succeeds: %s" % out)
        check(count("segflow_daily") == 0, "no rows for the skipped table")
        check(q1(w, "SELECT wan, download_bytes FROM net.wan_daily WHERE day='2026-11-27'") == [("wan1", 1400)], "without er605_wan_info the household total is stored as wan1")
        prom.missing, prom.no_wan_info = set(), False
        # zero path metrics
        prom.missing = {"path"}
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--now", NOW))
        check(rc == 0 and "skip table=path_daily" in out, "zero path metrics skip path_daily only")
        prom.missing = set()
        # failing Prometheus fails the run and the textfile says so
        agg.http_json_saved = agg.http_json
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--now", NOW, "--textfile", str(tmpdir / "bad.prom")) + ["--prometheus", "http://127.0.0.1:1"])
        check(rc == 1 and "result=error" in out and "net_history_run_success 0" in (tmpdir / "bad.prom").read_text(), "an unreachable Prometheus fails the run")
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--now", NOW, "--textfile", str(tmpdir / "nh.prom")))
        prom_txt = (tmpdir / "nh.prom").read_text()
        check("net_history_run_success 1" in prom_txt and "net_history_last_run_timestamp_seconds 17" in prom_txt and 'net_history_rows{table="internet_daily"} 182' in prom_txt
              and 'net_history_rows{table="outage_events"} 4' in prom_txt, "textfile metrics: success, timestamp, rows per table")
        check(oct(os.stat(tmpdir / "nh.prom").st_mode & 0o777) == "0o644", "the textfile is world-readable for Alloy")
        # import: no overwrite without --force
        (tmpdir / "wan.csv").write_text("day,download_bytes,upload_bytes\n2026-01-01,111,22\n2026-11-26,5,5\n")
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--import-wan-daily", str(tmpdir / "wan.csv")))
        check(rc == 0 and "inserted=1 replaced_lower_coverage=0 skipped_existing=1" in out, "import inserts the new day and skips the existing one: %s" % out)
        check(q1(w, "SELECT download_bytes, source, coverage FROM net.wan_daily WHERE day='2026-01-01' AND wan='wan1'") == [(111, "er605-ledger", 1)], "imported row stored as wan1, source er605-ledger, coverage 1")
        check(q1(w, "SELECT download_bytes FROM net.wan_daily WHERE day='2026-11-26' AND wan='wan1'")[0][0] == 1000, "an existing row is not overwritten without --force")
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--import-wan-daily", str(tmpdir / "wan.csv"), "--force"))
        check(q1(w, "SELECT download_bytes, source FROM net.wan_daily WHERE day='2026-11-26' AND wan='wan1'") == [(5, "er605-ledger")], "--force overwrites")
        (tmpdir / "bad.csv").write_text("a,b\n1,2\n")
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--import-wan-daily", str(tmpdir / "bad.csv")))
        check(rc == 1, "a CSV without the right columns is rejected")
        # report
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--report", "month", "--table", "wan"))
        check(rc == 0 and out.startswith("== net.monthly_wan") and "total" in out and "2026-11-01" in out, "--report month --table wan prints the view")
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--report", "month"))
        check(all(("net." + v) in out for _, v in agg.TABLE_NAMES.values()), "--report month prints every monthly view")
        # comments and the catalog
        missing = q1(w, """
            SELECT c.relname, a.attname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'net'
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE c.relkind IN ('r','v') AND col_description(c.oid, a.attnum) IS NULL
            UNION ALL SELECT c.relname, '(object)' FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'net'
            WHERE c.relkind IN ('r','v') AND obj_description(c.oid, 'pg_class') IS NULL""")
        check(missing == [], "every table, view and column has a comment: %s" % (missing[:5],))
        cat = q1(w, "SELECT object_name, kind, description, example_query FROM net.catalog")
        names = {r[0] for r in cat}
        check(names == SCHEMA_OBJECTS, "the catalog lists every object: %s" % (sorted(names ^ SCHEMA_OBJECTS),))
        check(all(r[2] and r[3] for r in cat), "every catalog row has a description and an example query")
        check({r[1] for r in cat} == {"table", "view"}, "catalog kinds")
        check(q1(w, "SELECT count(*) FROM net.catalog_columns WHERE description IS NULL")[0][0] == 0, "catalog_columns has no undocumented column")
        w.close()
        # reader
        rd = connect(pgdb, "net_history_reader", "rpw-test")
        for objname, ex in [(r[0], r[3]) for r in cat]:
            try:
                q1(rd, ex)
            except Exception as exc:
                check(False, "the example query for %s fails for the reader: %s" % (objname, exc))
                rd.rollback()
        try:
            c = rd.cursor(); c.execute("INSERT INTO net.meta VALUES ('x','y')"); rd.commit()
            check(False, "the reader must not write")
        except Exception:
            rd.rollback()
        try:
            c = rd.cursor(); c.execute("CREATE TABLE net.evil (a int)"); rd.commit()
            check(False, "the reader must not create tables")
        except Exception:
            rd.rollback()
        check(q1(rd, "SHOW default_transaction_read_only")[0][0] == "on", "the reader session is read-only by default")
        # every panel SQL runs for the reader
        ran = 0
        for name in DASHBOARDS:
            for p in panels_of(dash(name)):
                if (p.get("datasource") or {}).get("uid") == "net-history":
                    for t in p["targets"]:
                        try:
                            q1(rd, t["rawSql"])
                            ran += 1
                        except Exception as exc:
                            check(False, "%s / %s SQL fails: %s" % (name, p["title"], exc))
                            rd.rollback()
        check(ran >= 20, "ran %d dashboard SQL queries" % ran)
        # a time series query returns the columns Grafana needs
        c = rd.cursor()
        c.execute(next(t["rawSql"] for p in panels_of(dash("internet-usage")) if p["title"] == "Availability per month" for t in p["targets"]))
        check([d[0] for d in c.description] == ["time", "metric", "value"], "time series panels return time, metric, value")
        rd.close()
        # the writer cannot create objects outside schema net's ownership model but owns it
        w2 = connect(pgdb, "net_history_writer", "wpw-test")
        check(q1(w2, "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname='net'")[0][0] == "net_history_writer", "the writer owns schema net")
        w2.close()
        # new objects the writer creates later are readable by the reader (default privileges)
        pgdb.psql("CREATE TABLE net.later (a int); INSERT INTO net.later VALUES (1);", db="net_history", user="net_history_writer")
        # (the writer connects through the socket trust of the image)
        rd = connect(pgdb, "net_history_reader", "rpw-test")
        try:
            check(q1(rd, "SELECT a FROM net.later") == [(1,)], "default privileges give the reader SELECT on later tables")
        except Exception as exc:
            check(False, "default privileges: %s" % exc)
        rd.close()
        # ---- coverage: a fresh 90-day store must never degrade what the central store already gave ----
        central = FakeServer()
        central.scale, prom.scale = 1.0, 7.0   # a row's value says which store it came from: 1000 central, 7000 the 90-day store
        epoch = lambda iso: agg.parse_when(iso).timestamp()
        now_ts = epoch(NOW)

        def reset_schema():
            pgdb.psql("DROP SCHEMA IF EXISTS net CASCADE;", db="net_history")
            r = pgdb.psql(sql)
            check(r.returncode == 0, "re-provisioning after a schema drop works: %s" % r.stderr[-200:])

        def cov_run(*extra, now=NOW):
            return run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--now", now, "--central-prometheus", central.url, *extra))

        def wan(w, day):
            return q1(w, "SELECT wan, download_bytes, coverage, source FROM net.wan_daily WHERE day = %s ORDER BY wan", (day,))

        # A: the 90-day store began 5 h ago, the central one 7 days ago
        reset_schema()
        prom.first, central.first = now_ts - 5 * 3600, now_ts - 7 * 86400
        rc, out = cov_run("--backfill", "7")
        check(rc == 0 and "migrated=1,2" in out, "A: the run succeeds and applies both migrations: %s" % out)
        w = connect(pgdb, "net_history_writer", "wpw-test")
        check(wan(w, "2026-11-23") == [("wan1", 1000, 1, "prometheus"), ("wan2", 400, 1, "prometheus")], "A: a recent complete day comes from the central store with coverage 1: %s" % (wan(w, "2026-11-23"),))
        check(wan(w, "2026-11-27")[0][1] == 1000 and wan(w, "2026-11-27")[0][2] == 1, "A: today comes from the central store too (it covers all of today)")
        d20 = wan(w, "2026-11-20")[0]
        check(abs(float(d20[2]) - 0.354) <= 0.002 and d20[1] == 1000, "A: the first central day is partial (8.5 h of 24): %s" % (d20,))
        check(q1(w, "SELECT count(*) FROM net.wan_daily WHERE day < '2026-11-20'")[0][0] == 0, "A: no rows for days no store covers")
        check(q1(w, "SELECT count(*) FROM net.internet_daily WHERE coverage IS NULL")[0][0] == 0, "A: every row has a coverage")
        iw = q1(w, "SELECT window_seconds, coverage FROM net.internet_daily WHERE day = '2026-11-20' AND site = 'home'")[0]
        check(abs(iw[0] - 86400 * 0.354) < 200 and abs(float(iw[1]) - 0.354) <= 0.002, "A: window_seconds is the covered part of a partial day: %s" % (iw,))
        mw = q1(w, "SELECT partial_days, days FROM net.monthly_wan WHERE wan = 'total' AND month = '2026-11-01'")[0]
        check(mw == (1, 8), "A: the monthly view counts the partial day: %s" % (mw,))
        check(q1(w, "SELECT count(*) FROM net.catalog_columns WHERE column_name = 'coverage' AND description IS NOT NULL")[0][0] == 9, "A: coverage is documented on every daily table")

        # B: the central store rolls on; its older days are now partial, but stored rows must not be lowered
        central.first = now_ts - 86400
        rc, out = cov_run("--backfill", "7")
        check(rc == 0, "B: the run succeeds")
        check(wan(w, "2026-11-26")[0][1:3] == (1000, 1), "B: a complete day is not replaced by a lower-coverage one: %s" % (wan(w, "2026-11-26"),))
        check(wan(w, "2026-11-23")[0][1:3] == (1000, 1), "B: a day the store no longer holds is kept")
        # today is always recomputed, even to a lower coverage
        central.first = now_ts - 2 * 3600
        rc, out = cov_run("--backfill", "2")
        t = wan(w, "2026-11-27")[0]
        check(t[1] == 7000 and abs(float(t[2]) - 0.323) <= 0.002, "B: today is recomputed from the best store even at lower coverage: %s" % (t,))

        # C: the ER605 ledger: wins over partial days, never lowers, ties keep the ledger row
        central.first = now_ts - 7 * 86400
        cov_run("--backfill", "7")
        (tmpdir / "ledger.csv").write_text("day,wan,download_bytes,upload_bytes,coverage\n2026-11-20,wan1,111,22,1\n2026-11-23,wan1,5,5,1\n"
                                           "2026-11-19,wan2,7,8,0.5\n2026-11-18,,9,9,\n")
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--import-wan-daily", str(tmpdir / "ledger.csv")))
        check(rc == 0 and "inserted=2 replaced_lower_coverage=1 skipped_existing=1" in out, "C: import: 2 new, 1 partial day replaced, 1 full day kept: %s" % out)
        check(wan(w, "2026-11-20") == [("wan1", 111, 1, "er605-ledger"), ("wan2", 400, wan(w, "2026-11-20")[1][2], "prometheus")], "C: the ledger replaces the partial day: %s" % (wan(w, "2026-11-20"),))
        check(wan(w, "2026-11-23")[0][1] == 1000, "C: a full-coverage Prometheus row is not replaced without --force")
        check(wan(w, "2026-11-18") == [("wan1", 9, 1, "er605-ledger")], "C: a blank wan and coverage default to wan1 and 1")
        cov_run("--backfill", "7")
        check(wan(w, "2026-11-20")[0][1:] == (111, 1, "er605-ledger"), "C: a later run does not overwrite the ledger row with a partial Prometheus day")
        (tmpdir / "ledger2.csv").write_text("day,wan,download_bytes,upload_bytes,coverage\n2026-11-22,wan1,77,7,1\n2026-11-19,wan2,1,1,0.3\n")
        run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--import-wan-daily", str(tmpdir / "ledger2.csv"), "--force"))
        check(wan(w, "2026-11-22")[0][1:] == (77, 1, "er605-ledger"), "C: --force replaces a full day")
        cov_run("--backfill", "7")
        check(wan(w, "2026-11-22")[0][1:] == (77, 1, "er605-ledger"), "C: an equal-coverage Prometheus row does not replace a ledger row")
        (tmpdir / "ledger3.csv").write_text("day,wan,download_bytes,upload_bytes,coverage\n2026-11-19,wan2,2,2,0.2\n")
        rc, out = run_agg(agg_args(pgdb, prom, loki, tmpdir / "w.pw", "--import-wan-daily", str(tmpdir / "ledger3.csv")))
        check("skipped_existing=1" in out and wan(w, "2026-11-19")[0][1] == 1, "C: an imported row of lower coverage never replaces a stored one (without --force): %s" % out)
        w.close()

        # D: today is partial in both stores: the better one wins and the row says how partial
        reset_schema()
        prom.first, central.first = now_ts - 5 * 3600, now_ts - 3 * 3600
        rc, out = cov_run("--backfill", "2")
        w = connect(pgdb, "net_history_writer", "wpw-test")
        rows = wan(w, "2026-11-27")
        check(rc == 0 and len(rows) == 2 and rows[0][1] == 7000 and abs(float(rows[0][2]) - 0.323) <= 0.002, "D: a partial today comes from the 90-day store, coverage 0.323: %s" % (rows,))
        check(q1(w, "SELECT count(*) FROM net.wan_daily WHERE day < '2026-11-27'")[0][0] == 0, "D: no rows for yesterday, which neither store covers")
        w.close()

        # E: an existing schema-1 database with rows is migrated and its coverage is recomputed
        reset_schema()
        prom.first, central.first = now_ts - 5 * 3600, now_ts - 7 * 86400
        w = connect(pgdb, "net_history_writer", "wpw-test")
        check(agg.migrate(w, upto=1) == [1], "E: schema 1 applied alone")
        cur = w.cursor()
        cur.execute("INSERT INTO net.wan_daily (day, wan, download_bytes, upload_bytes, source) VALUES ('2026-11-26', 'wan1', 4242, 1, 'import'), ('2026-11-25', 'wan1', 1, 1, 'prometheus'), ('2026-11-27', 'wan1', 3, 3, 'prometheus')")
        cur.execute("INSERT INTO net.internet_daily (day, site, window_seconds, down_seconds, outages, availability) VALUES ('2026-11-26', 'home', 86400, 0, 0, 1)")
        w.commit()
        check([r[0] for r in q1(w, "SELECT column_name FROM information_schema.columns WHERE table_schema='net' AND table_name='wan_daily' AND column_name='coverage'")] == [], "E: schema 1 has no coverage column")
        rc, out = cov_run("--backfill", "7")
        check(rc == 0 and "migrated=2" in out, "E: the next run applies migration 2: %s" % out)
        check(q1(w, "SELECT max(version) FROM net.schema_version")[0][0] == 2, "E: schema_version is 2")
        check(wan(w, "2026-11-26") == [("wan1", 4242, 1, "er605-ledger"), ("wan2", 400, 1, "prometheus")], "E: an old import row becomes er605-ledger with coverage 1 and is kept: %s" % (wan(w, "2026-11-26"),))
        check(wan(w, "2026-11-25")[0][2] == 1, "E: an old Prometheus row gets recomputed coverage")
        check(q1(w, "SELECT count(*) FROM net.wan_daily WHERE coverage IS NULL")[0][0] == 0 and q1(w, "SELECT count(*) FROM net.internet_daily WHERE coverage IS NULL")[0][0] == 0, "E: no row is left without coverage")
        check(q1(w, "SELECT count(*) FROM information_schema.columns WHERE table_schema='net' AND table_name LIKE 'monthly_%' AND column_name='partial_days'")[0][0] == len(agg.MONTHLY), "E: every monthly view has partial_days")
        rd = connect(pgdb, "net_history_reader", "rpw-test")
        check(q1(rd, "SELECT count(*) FROM net.monthly_wan")[0][0] >= 1 and q1(rd, "SELECT count(*) FROM net.catalog")[0][0] == len(SCHEMA_OBJECTS), "E: the reader sees the recreated views and catalog")
        rd.close(); w.close()
        central.shutdown()
    finally:
        pgdb.stop()
        prom.shutdown(); loki.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

# ------------------------------------------------------------------ 8. docs ---
variables_md = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
for name in DEFAULTS:
    check("`" + name + "`" in variables_md, name + " has no row in docs/reference/variables.md")
playbooks_md = (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text()
check("network-history.yml" in playbooks_md and "network_history" in playbooks_md, "playbooks.md documents the playbook and the role")
check("network-history.yml" in (ANSIBLE / "README.md").read_text() and "network_history" in (ANSIBLE / "README.md").read_text(), "ansible/README.md lists the playbook and the role")
metrics_md = (REPO / "monitoring" / "docs" / "reference" / "metrics.md").read_text()
for m in ("net_history_last_run_timestamp_seconds", "net_history_run_success", "net_history_rows"):
    check("`" + m + "`" in metrics_md, m + " is not documented in monitoring/docs/reference/metrics.md")
src = (ROLE / "files" / "net-history.py").read_text()
for m in re.findall(r"HELP (net_history_\w+)", src):
    check(m in metrics_md, "metric %s written by the aggregator is not in metrics.md" % m)

if skipped:
    print("SKIPPED: " + "; ".join(skipped))
if errors:
    print("FAILED: %d problem(s)" % len(errors))
    for e in errors:
        print("  - " + e)
    sys.exit(1)
print("ok")
