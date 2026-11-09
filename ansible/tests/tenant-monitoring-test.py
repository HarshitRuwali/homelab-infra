#!/usr/bin/env python3
"""Fail when the tenant monitoring rules, dashboard, deploy wiring or docs drift.

Needs PyYAML (the Ansible controller has it); everything else is the standard
library. No network, no host contacted. Run from anywhere:

    python3 -I ansible/tests/tenant-monitoring-test.py

Checks:
  * rules-tenant.yaml and tenant-activity.json parse
  * rule uids and titles are unique across EVERY alerting rules file, the
    templated rules-availability.yaml.j2 included, and uids fit Grafana's limit
  * every rule in every file has a severity label; every tenant rule also has
    a runbook and a summary, no `$` in a label (Grafana expands it as an
    environment variable), no backtick in an annotation (the Matrix message
    wraps the runbook in one), a Loki or Prometheus query and the shared A, B,
    C data shape; only the dead-man rule alerts on no data
  * the LogQL expressions that were tested against the live Loki are present
    verbatim, the rules decided later (lease packet ceiling, lease Suricata
    split, exporter staleness, firewall not filtering, outbound-only probing,
    bridge-restricted egress) have the exact severity, `for`, threshold, no
    data state and expression, the dashboard keeps the matching windows and
    matchers, and no expression uses `| logfmt` (it breaks aggregation)
  * every dashboard panel and target has a datasource, panel ids are unique and
    panels neither overlap nor leave the 24 column grid, every $variable used
    in a query is defined, and every defined variable is used
  * every rule's __dashboardUid__ and __panelId__ name a real panel, and that
    panel reads the same metric or stream as the rule
  * the Suricata rule and dashboard select `{job="suricata"}`, not the whole
    firewall's `{host="opnsense"}`
  * deploy: rules-tenant.yaml is in grafana_alerting_static_files (and every
    rules-*.yaml in the directory is), the role probes a tenant uid, the
    dashboard sits in a directory the dashboard role, the LXC installer and the
    Compose file all pick up
  * docs: alerting.md lists every tenant rule with its severity and `for`,
    dashboards.md names every panel, retention.md documents the Loki override,
    metrics.md has a row, with its labels, for each fleet_pve_ metric the rules
    and dashboard read (and the role writes it), the security module documents
    the new streams and what they do not cover, no ntopng claim is left in
    either module, the root README or the docs landing page, and none of the
    overstatements about full logging or all DNS is back
  * cross-site links into the other module's docs resolve to a real heading
  * relative links and #anchors in the docs of both modules resolve
  * no em dash (U+2014) anywhere in monitoring/ or security/
"""
import json
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: run this from the Ansible controller environment")

ROOT = pathlib.Path(__file__).resolve().parents[2]
MON = ROOT / "monitoring"
SEC = ROOT / "security"
ALERTING = MON / "grafana" / "provisioning" / "alerting"
DASHBOARDS = MON / "grafana" / "dashboards"
RULES_FILE = ALERTING / "rules-tenant.yaml"
DASH_FILE = DASHBOARDS / "fleet" / "tenant-activity.json"
GA_ROLE = ROOT / "ansible" / "roles" / "grafana_alerting"
GD_ROLE = ROOT / "ansible" / "roles" / "grafana_dashboards"

DASH_UID = "tenant-activity"
SEVERITIES = {"critical", "warning", "info"}
DATASOURCES = {"loki", "prometheus"}
BUILTIN_VARS = {"__range", "__auto", "__interval", "__interval_ms", "__rate_interval",
                "__range_s", "__range_ms", "__from", "__to", "__dashboard", "__name",
                "__org", "__user", "__all"}
UID_LIMIT = 40

# Expressions that were run against the live Loki and returned one series.
# They must reach the YAML byte for byte: the escaping is the fragile part.
VERBATIM = [
    r'max(max_over_time({job="pfstats"} | regexp "\\bstates=(?P<v>[0-9]+)" | unwrap v [5m])) / max(max_over_time({job="pfstats"} | regexp "\\bstates_max=(?P<v>[0-9]+)" | unwrap v [5m]))',
    r'sum(rate_counter({job="pfstats"} | regexp "\\bsrc_limit_hits=(?P<v>[0-9]+)" | unwrap v [10m])) * 600',
    r'sum(rate_counter({job="pfstats"} | regexp "\\bstate_limit_hits=(?P<v>[0-9]+)" | unwrap v [10m])) * 600',
    r'max(max_over_time({job="pfstats"} | regexp "\\bbanned=(?P<v>[0-9]+)" | unwrap v [5m]))',
    r'count_over_time({job="pfstats"} [10m])',
    r'sum(rate_counter({job="pfstats"} | regexp "vtnet5_ipkts=(?P<v>[0-9]+)" | unwrap v [5m]))',
    r'sum(rate_counter({job="pfstats"} | regexp "vtnet2_ipkts=(?P<v>[0-9]+)" | unwrap v [5m]))',
    r'sum(count_over_time({job="suricata"} |= "\"event_type\":\"alert\"" |~ "\"in_iface\":\"vtnet5\"" [10m]))',
    r'sum(count_over_time({job="suricata"} |= "\"event_type\":\"alert\"" |~ "\"in_iface\":\"vtnet2\"" [10m]))',
    r'sum(count_over_time({job="pve-firewall", chain=~"GROUP-(tenant|lease)-OUT"} |= "DPT=25 " [10m]))',
    r'sum(count_over_time({job="unbound"} |= "NXDOMAIN" |~ " 10\\.10\\.(60|90)\\.[0-9]+ " [5m]))',
    r'sum(count_over_time({job="filterlog", iface=~"vtnet(2|5)", action="pass"} [1m]))',
]

# Rules whose numbers were decided after review. uid -> (severity, for, evaluator, threshold,
# noDataState, fragment that must be in query A).
SPEC = {
    "tenant-lease-packet-rate-high": ("warning", "5m", "gt", 15000, "OK", 'vtnet5_ipkts'),
    "tenant-lease-packet-limit-reached": ("warning", "5m", "gt", 100, "OK",
                                          "sum by (host, vmid, guest) (rate(fleet_pve_tenant_policed_packets_total[15m]))"),
    "tenant-firewall-not-filtering": ("critical", "10m", "lt", 1, "OK",
                                      "min by (host, vmid, guest, segment) (fleet_pve_tenant_firewall_active)"),
    "tenant-private-address-probing": ("warning", "0m", "gt", 20, "OK",
                                       'chain=~"GROUP-(tenant|lease)-OUT"} != "DPT=41641 " [10m]'),
    "tenant-guest-egress-high": ("warning", "0m", "gt", 10000000, "OK",
                                 'fleet_pve_guest_nic_info{bridge=~"vmbr(2|5)"}'),
    "tenant-smtp-dropped-counter": ("critical", "0m", "gt", 0, "OK",
                                    'sum by (host) (increase(fleet_pve_firewall_dropped_packets_total{rule="smtp"}[10m]))'),
    "tenant-lease-packet-limit-missing": ("critical", "10m", "lt", 1, "OK",
                                          "min by (host, vmid, guest) (fleet_pve_tenant_packet_limit_active)"),
    "tenant-guest-exporter-stale": ("warning", "5m", "gt", 1200, "OK",
                                    "time() - max by (host) (fleet_pve_guests_collection_timestamp_seconds)"),
    "tenant-lease-suricata-alert": ("critical", "0m", "gt", 0, "OK",
                                    r'|~ "\"in_iface\":\"vtnet5\"" | json | alert_severity="1" [10m]'),
    "tenant-lease-suricata-alerts": ("warning", "0m", "gte", 3, "OK",
                                     r'|~ "\"in_iface\":\"vtnet5\"" [10m]'),
    "tenant-work-suricata-alerts": ("warning", "0m", "gte", 5, "OK",
                                    r'|~ "\"in_iface\":\"vtnet2\"" [10m]'),
}

errors = []


def fail(msg):
    errors.append(msg)


def read(path):
    try:
        return pathlib.Path(path).read_text()
    except OSError as e:
        fail(f"cannot read {path}: {e}")
        return ""


def load_yaml(path):
    try:
        return yaml.safe_load(read(path))
    except yaml.YAMLError as e:
        fail(f"{path.relative_to(ROOT)} is not valid YAML: {e}")
        return None


def load_json(path):
    try:
        return json.loads(read(path))
    except ValueError as e:
        fail(f"{path.relative_to(ROOT)} is not valid JSON: {e}")
        return None


def rules_of(doc):
    out = []
    for group in (doc or {}).get("groups") or []:
        out.extend(group.get("rules") or [])
    return out


def expr_of(rule, ref="A"):
    for q in rule.get("data") or []:
        if q.get("refId") == ref:
            return (q.get("model") or {}).get("expr", "")
    return ""


# --- the tenant rules ------------------------------------------------------------------
doc = load_yaml(RULES_FILE)
tenant = rules_of(doc)
if doc is not None:
    if doc.get("apiVersion") != 1:
        fail("rules-tenant.yaml: apiVersion must be 1")
    groups = doc.get("groups") or []
    if not groups:
        fail("rules-tenant.yaml defines no groups")
    for g in groups:
        for key in ("orgId", "name", "folder", "interval"):
            if key not in g:
                fail(f"rules-tenant.yaml group {g.get('name')} has no {key}")
if not tenant:
    fail("rules-tenant.yaml defines no rules")

no_data_alerting = []
for r in tenant:
    uid = r.get("uid", "?")
    where = f"rules-tenant.yaml rule {uid}"
    labels = r.get("labels") or {}
    annotations = r.get("annotations") or {}
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(uid)) or len(str(uid)) > UID_LIMIT:
        fail(f"{where}: uid must be 1-{UID_LIMIT} characters of [A-Za-z0-9_-]")
    if not r.get("title"):
        fail(f"{where}: no title")
    if labels.get("severity") not in SEVERITIES:
        fail(f"{where}: severity label must be one of {sorted(SEVERITIES)}, got {labels.get('severity')!r}")
    for key in ("runbook", "summary"):
        if not str(annotations.get(key) or "").strip():
            fail(f"{where}: no {key} annotation")
    for k, v in labels.items():
        if "$" in str(v):
            fail(f"{where}: label {k} contains $, which Grafana expands as an environment variable")
    for k, v in annotations.items():
        if "`" in str(v):
            fail(f"{where}: annotation {k} contains a backtick; the Matrix message wraps runbooks in one")
    if r.get("condition") != "C":
        fail(f"{where}: condition must be C")
    if "for" not in r:
        fail(f"{where}: no for")
    if r.get("execErrState") != "Error":
        fail(f"{where}: execErrState must be Error")
    nd = r.get("noDataState")
    if nd not in ("OK", "Alerting"):
        fail(f"{where}: noDataState must be OK or Alerting, got {nd!r}")
    if nd == "Alerting":
        no_data_alerting.append(uid)
    refs = [q.get("refId") for q in r.get("data") or []]
    if refs != ["A", "B", "C"]:
        fail(f"{where}: data must be refIds A, B, C, got {refs}")
    else:
        a, b, c = r["data"]
        if a.get("datasourceUid") not in DATASOURCES:
            fail(f"{where}: query A datasourceUid must be one of {sorted(DATASOURCES)}")
        if not str(expr_of(r)).strip():
            fail(f"{where}: query A has no expr")
        if (b.get("model") or {}).get("type") != "reduce" or (b.get("model") or {}).get("expression") != "A":
            fail(f"{where}: B must reduce A")
        cm = c.get("model") or {}
        if cm.get("type") != "threshold" or cm.get("expression") != "B" or not cm.get("conditions"):
            fail(f"{where}: C must be a threshold on B")
    if "logfmt" in expr_of(r):
        fail(f"{where}: expr uses logfmt, which turns every field into a label and breaks aggregation")
    if annotations.get("__dashboardUid__") != DASH_UID:
        fail(f"{where}: __dashboardUid__ must be {DASH_UID}")

if no_data_alerting != ["tenant-pf-stats-stopped"]:
    fail(f"only tenant-pf-stats-stopped may use noDataState: Alerting, found {no_data_alerting}")

exprs = {expr_of(r).strip() for r in tenant}
for want in VERBATIM:
    if want not in exprs:
        fail("a tested LogQL expression is missing or altered in rules-tenant.yaml: " + want[:90] + "...")

by_uid = {r.get("uid"): r for r in tenant}
for uid, (sev, dur, op, value, nd, fragment) in SPEC.items():
    r = by_uid.get(uid)
    if r is None:
        fail(f"rules-tenant.yaml has no rule {uid}")
        continue
    ev = (((r.get("data") or [{}, {}, {}])[2].get("model") or {}).get("conditions") or [{}])[0].get("evaluator") or {}
    got = (r.get("labels", {}).get("severity"), str(r.get("for")), ev.get("type"), (ev.get("params") or [None])[0],
           r.get("noDataState"))
    if got != (sev, dur, op, value, nd):
        fail(f"rule {uid}: severity, for, evaluator, threshold, noData are {got}, expected {(sev, dur, op, value, nd)}")
    if fragment not in expr_of(r):
        fail(f"rule {uid}: query A no longer contains {fragment}")
# the probing rule counts outbound chains only: an IN chain drop names the guest that received the packet
chain = re.search(r'chain=~"([^"]+)"', expr_of(by_uid.get("tenant-private-address-probing", {})))
if not chain:
    fail("tenant-private-address-probing no longer selects a chain")
else:
    for name in ("GROUP-tenant-OUT", "GROUP-lease-OUT"):
        if not re.fullmatch(chain.group(1), name):
            fail(f"tenant-private-address-probing no longer counts {name}")
    for name in ("GROUP-tenant-IN", "GROUP-lease-IN"):
        if re.fullmatch(chain.group(1), name):
            fail(f"tenant-private-address-probing counts {name}, which names the guest that received the packet")
# the lease Suricata rule that pages must filter on severity; the lower severity rule must not
if "alert_severity" in expr_of(by_uid.get("tenant-lease-suricata-alerts", {})):
    fail("tenant-lease-suricata-alerts must count alerts of any severity")

# --- the Suricata rules and dashboard select the Suricata stream, not the whole firewall ----
suricata_queries = {}
for r in rules_of(load_yaml(ALERTING / "rules-security.yaml")):
    suricata_queries[f"rules-security.yaml rule {r.get('uid')}"] = [expr_of(r)]
suri_dash = load_json(DASHBOARDS / "fleet" / "suricata-alerts.json") or {}
for p in suri_dash.get("panels") or []:
    suricata_queries[f"suricata-alerts.json panel {p.get('id')} ({p.get('title')})"] = [
        str(t.get("expr") or "") for t in p.get("targets") or []]
if not suricata_queries:
    fail("found no Suricata queries to check")
for where, qs in suricata_queries.items():
    for q in qs:
        if 'host="opnsense"' in q:
            fail(f'{where} selects {{host="opnsense"}}, which also matches the filter log, resolver log and '
                 f'pfstats; select job="suricata"')
        if q and 'job="suricata"' not in q:
            fail(f'{where} does not select job="suricata"')

# the state table comment must say which threshold each rule fires at
flat = re.sub(r"\n\s*#\s?", " ", read(RULES_FILE))
for needle in ("fires above 50%", "fires above 80%"):
    if needle not in flat:
        fail(f"rules-tenant.yaml: the state table comment no longer says it {needle}")
if "Both fire above 80%" in flat:
    fail("rules-tenant.yaml: the state table comment claims both rules fire only above 80%")

# --- uniqueness across every rules file --------------------------------------------------
seen_uid, seen_title = {}, {}
for path in sorted(ALERTING.glob("rules-*.yaml")):
    for r in rules_of(load_yaml(path)):
        rel = path.name
        for table, key in ((seen_uid, r.get("uid")), (seen_title, r.get("title"))):
            if key in table:
                fail(f"{'uid' if table is seen_uid else 'title'} {key!r} appears in both {table[key]} and {rel}")
            table[key] = rel
        if not (r.get("labels") or {}).get("severity"):
            fail(f"{rel}: rule {r.get('uid')} has no severity label")
templated = (GA_ROLE / "templates" / "rules-availability.yaml.j2").read_text()
for uid, title in re.findall(r"^\s+- uid: (\S+)\n\s+title: (.+)$", templated, re.M):
    for table, key, label in ((seen_uid, uid, "uid"), (seen_title, title.strip(), "title")):
        if key in table:
            fail(f"{label} {key!r} appears in both {table[key]} and rules-availability.yaml.j2")
        table[key] = "rules-availability.yaml.j2"

# --- the dashboard -----------------------------------------------------------------------
dash = load_json(DASH_FILE)
panels = {}
if dash is not None:
    if dash.get("uid") != DASH_UID:
        fail(f"tenant-activity.json: uid must be {DASH_UID}")
    if not dash.get("title"):
        fail("tenant-activity.json has no title")
    defined = {v.get("name") for v in (dash.get("templating") or {}).get("list") or []}
    if "segment" not in defined:
        fail("tenant-activity.json has no segment variable")
    used = set()
    boxes = []
    for p in dash.get("panels") or []:
        pid, title = p.get("id"), p.get("title")
        where = f"tenant-activity.json panel {pid} ({title})"
        if pid in panels:
            fail(f"{where}: duplicate panel id")
        panels[pid] = p
        ds = (p.get("datasource") or {}).get("uid")
        if ds not in DATASOURCES:
            fail(f"{where}: panel datasource must be one of {sorted(DATASOURCES)}, got {ds!r}")
        if not p.get("targets"):
            fail(f"{where}: no targets")
        for t in p.get("targets") or []:
            tds = (t.get("datasource") or {}).get("uid")
            if tds not in DATASOURCES:
                fail(f"{where}: target {t.get('refId')} has no datasource")
            elif tds != ds:
                fail(f"{where}: target {t.get('refId')} reads {tds} but the panel says {ds}")
            expr = str(t.get("expr") or "")
            if not expr.strip():
                fail(f"{where}: target {t.get('refId')} has no expr")
            if "logfmt" in expr:
                fail(f"{where}: target {t.get('refId')} uses logfmt")
            for m in re.finditer(r"\$\{(\w+)[^}]*\}|\$(\w+)|\[\[(\w+)", expr):
                name = next(g for g in m.groups() if g)
                used.add(name)
                if name not in defined | BUILTIN_VARS:
                    fail(f"{where}: target {t.get('refId')} uses undefined variable ${name}")
        gp = p.get("gridPos") or {}
        x, y, w, h = (gp.get(k) for k in ("x", "y", "w", "h"))
        if not all(isinstance(v, int) for v in (x, y, w, h)) or x < 0 or w < 1 or x + w > 24:
            fail(f"{where}: gridPos {gp} is not inside the 24 column grid")
        else:
            for ox, oy, ow, oh, other in boxes:
                if x < ox + ow and ox < x + w and y < oy + oh and oy < y + h:
                    fail(f"{where}: overlaps panel {other}")
            boxes.append((x, y, w, h, pid))
    for var in sorted(defined - used):
        fail(f"tenant-activity.json defines variable {var} that no query uses")
    for var in (dash.get("templating") or {}).get("list") or []:
        ds = var.get("datasource")
        if var.get("type") == "query" and (ds or {}).get("uid") not in DATASOURCES:
            fail(f"tenant-activity.json variable {var.get('name')} has no datasource")

# windows and matchers the dashboard must share with the rules
DASH_FRAGMENTS = {
    9: 'fleet_pve_guest_nic_info{bridge=~"vmbr(2|5)",guest=~"$guest"}',
    19: "increase(fleet_pve_firewall_dropped_packets_total[15m])",
    21: "rate(fleet_pve_tenant_policed_packets_total[15m])",
    24: "min by (host, vmid, guest, segment) (fleet_pve_tenant_firewall_active)",
}
for pid, fragment in DASH_FRAGMENTS.items():
    panel = panels.get(pid)
    if panel is None or not any(fragment in str(t.get("expr", "")) for t in panel.get("targets") or []):
        fail(f"tenant-activity.json panel {pid} no longer contains {fragment}")
if dash is not None:
    guest_var = next((v for v in (dash.get("templating") or {}).get("list") or [] if v.get("name") == "guest"), {})
    if 'bridge=~"vmbr(2|5)"' not in str(guest_var.get("definition")):
        fail("the guest picker is not limited to the tenant and lease bridges, unlike the Egress per Guest panel")
    dns_panel = next((p for p in panels.values() if p.get("title") == "Top DNS Names"), {})
    for needle in ("port 53", "853", "Tailscale", "DNS over HTTPS"):
        if needle not in str(dns_panel.get("description")):
            fail(f"the Top DNS Names panel description does not say {needle!r}: it must not imply all DNS is visible")

# every dashboard uid in the tree is unique
dash_uids = {}
for path in sorted(DASHBOARDS.rglob("*.json")):
    d = load_json(path)
    if d is None:
        continue
    uid = d.get("uid")
    if uid in dash_uids:
        fail(f"dashboard uid {uid!r} is in both {dash_uids[uid]} and {path.name}")
    dash_uids[uid] = path.name

# --- rules point at real panels ----------------------------------------------------------
for r in tenant:
    pid = (r.get("annotations") or {}).get("__panelId__")
    try:
        key = int(pid)
    except (TypeError, ValueError):
        fail(f"rule {r.get('uid')}: __panelId__ {pid!r} is not a number")
        continue
    if key not in panels:
        fail(f"rule {r.get('uid')}: __panelId__ {pid} is not a panel in tenant-activity.json")

def sources(expr):
    """Metric names and stream selectors a query reads."""
    return set(re.findall(r'\b(?:fleet|node)_[a-z0-9_]+|job="[a-z-]+"|vtnet[25]_[io]pkts', expr))


for r in tenant:
    try:
        panel = panels.get(int((r.get("annotations") or {}).get("__panelId__")))
    except (TypeError, ValueError):
        continue
    if not panel:
        continue
    wanted = sources(expr_of(r))
    have = set()
    for t in panel.get("targets") or []:
        have |= sources(str(t.get("expr") or ""))
    if wanted and not wanted & have:
        fail(f"rule {r.get('uid')} points at panel {panel.get('id')} ({panel.get('title')}), which reads "
             f"{sorted(have)} and none of the rule's {sorted(wanted)}")

# --- deploy: is it picked up everywhere --------------------------------------------------
ga_defaults = load_yaml(GA_ROLE / "defaults" / "main.yml") or {}
static = ga_defaults.get("grafana_alerting_static_files") or []
for path in sorted(ALERTING.glob("rules-*.yaml")):
    if path.name not in static:
        fail(f"{path.name} is not in grafana_alerting_static_files, so Ansible never installs it")
for name in static:
    if not (ALERTING / name).exists():
        fail(f"grafana_alerting_static_files lists {name}, which does not exist")
ga_tasks = read(GA_ROLE / "tasks" / "main.yml")
probe_uids = set(re.findall(r"'([a-z0-9-]+)' in \(ga_rules\.json", ga_tasks))
tenant_uids = {r.get("uid") for r in tenant}
if not probe_uids & tenant_uids:
    fail("roles/grafana_alerting/tasks/main.yml does not probe any tenant rule uid, so a "
         "rules-tenant.yaml that fails to load would not turn the play red")
for uid in probe_uids:
    if uid.startswith("tenant-") and uid not in tenant_uids:
        fail(f"roles/grafana_alerting probes {uid}, which rules-tenant.yaml does not define")

gd_defaults = load_yaml(GD_ROLE / "defaults" / "main.yml") or {}
if DASH_FILE.parent.name not in (gd_defaults.get("grafana_dashboard_dirs") or []):
    fail(f"{DASH_FILE.parent.name}/ is not in grafana_dashboard_dirs, so the dashboard role never installs it")
if '"*.json"' not in read(GD_ROLE / "tasks" / "main.yml") and "'*.json'" not in read(GD_ROLE / "tasks" / "main.yml"):
    fail("roles/grafana_dashboards no longer finds dashboards by *.json")
installer = read(MON / "scripts" / "lxc-install.sh")
if "grafana/provisioning/alerting/*.yaml" not in installer:
    fail("scripts/lxc-install.sh no longer installs every alerting/*.yaml")
if "for dashboard_dir in fleet" not in installer or "$dashboard_dir\"/*.json" not in installer:
    fail("scripts/lxc-install.sh no longer installs fleet/*.json")
compose = read(MON / "docker-compose.yml")
for mount in ("./grafana/provisioning:/etc/grafana/provisioning", "./grafana/dashboards:/var/lib/grafana/dashboards"):
    if mount not in compose:
        fail(f"docker-compose.yml no longer mounts {mount.split(':')[0]}")

# --- docs ---------------------------------------------------------------------------------
alerting = read(MON / "docs" / "monitoring" / "alerting.md")
tab = alerting.split('=== "Tenant and lease"')[-1].split("\n=== ")[0].split("\n## ")[0] if '=== "Tenant and lease"' in alerting else ""
if not tab:
    fail('alerting.md has no "Tenant and lease" tab')
doc_rows = {m.group(1): (m.group(2), m.group(3))
            for m in re.finditer(r"^\s+\| `(tenant-[a-z0-9-]+)` \| (\w+) \| (\w+) \|", tab, re.M)}
for r in tenant:
    uid = r.get("uid")
    if uid not in doc_rows:
        fail(f"alerting.md does not list {uid}")
    else:
        sev, dur = doc_rows[uid]
        if sev != (r.get("labels") or {}).get("severity"):
            fail(f"alerting.md says {uid} is {sev}, the rule says {(r.get('labels') or {}).get('severity')}")
        if dur != str(r.get("for")):
            fail(f"alerting.md says {uid} waits {dur}, the rule says {r.get('for')}")
lease_rate = str(SPEC["tenant-lease-packet-rate-high"][3])
if not any("`tenant-lease-packet-rate-high`" in line and lease_rate in line for line in tab.splitlines()):
    fail(f"alerting.md does not state the {lease_rate} packets/s threshold of tenant-lease-packet-rate-high")
rate_panel = next((p for p in panels.values() if p.get("title") == "Interface Packet Rates"), {})
if lease_rate not in str(rate_panel.get("description")):
    fail(f"the Interface Packet Rates panel description does not state the {lease_rate} packets/s alert threshold")
for uid in doc_rows:
    if uid not in tenant_uids:
        fail(f"alerting.md lists {uid}, which rules-tenant.yaml does not define")
if "#tenant-and-lease-alerts" not in alerting or "## Tenant and lease alerts" not in alerting:
    fail("alerting.md lost its Tenant and lease alerts section")

dashboards_md = read(MON / "docs" / "monitoring" / "dashboards.md")
section = dashboards_md.split("## Tenant Activity")[-1].split("\n## ")[0] if "## Tenant Activity" in dashboards_md else ""
if not section:
    fail("dashboards.md has no Tenant Activity section")
elif f"uid: {DASH_UID}" not in section:
    fail(f"dashboards.md does not give uid: {DASH_UID}")
for p in panels.values():
    if f"| {p.get('title')} |" not in section:
        fail(f"dashboards.md does not list the panel {p.get('title')!r}")

retention = read(MON / "docs" / "operations" / "retention.md")
for needle in ("Default in `scripts/lxc-install.sh` | `168h`",
               "Live install | `720h` (30 days), set by `LOKI_RETENTION_PERIOD`",
               "about 185 MB", "## Loki retention for the tenant logs"):
    if needle not in retention:
        fail(f"retention.md no longer mentions {needle}")
install_sh = read(MON / "scripts" / "lxc-install.sh")
if "${LOKI_RETENTION_PERIOD:-168h}" not in install_sh:
    fail("scripts/lxc-install.sh no longer defaults LOKI_RETENTION_PERIOD to 168h, which retention.md states")

metrics_md = read(MON / "docs" / "reference" / "metrics.md")
METRIC_LABELS = {
    "fleet_pve_tenant_conntrack_entries": ("vmid", "guest", "ip", "segment"),
    "fleet_pve_firewall_dropped_packets_total": ("chain", "rule", "family", "ipv4", "ipv6"),
    "fleet_pve_tenant_policed_packets_total": ("vmid", "guest", "segment"),
    "fleet_pve_tenant_packet_limit_active": ("vmid", "guest", "segment"),
    "fleet_pve_tenant_firewall_active": ("vmid", "guest", "segment"),
}
metric_rows = {m.group(1): m.group(0) for m in re.finditer(r"^\| `(fleet_pve_[a-z_]+)` \|.*$", metrics_md, re.M)}
for metric, labels in METRIC_LABELS.items():
    row = metric_rows.get(metric)
    if row is None:
        fail(f"metrics.md has no row for {metric}")
        continue
    for label in labels:
        if f"`{label}`" not in row:
            fail(f"metrics.md row for {metric} does not name the label {label}")
used_metrics = set()
for r in tenant:
    for q in r.get("data") or []:
        used_metrics |= set(re.findall(r"\bfleet_pve_[a-z0-9_]+", str((q.get("model") or {}).get("expr", ""))))
for p in panels.values():
    for t in p.get("targets") or []:
        used_metrics |= set(re.findall(r"\bfleet_pve_[a-z0-9_]+", str(t.get("expr", ""))))
role_template = read(ROOT / "ansible" / "roles" / "pve_guest_metrics" / "templates" / "fleet-pve-guests.j2")
for metric in sorted(used_metrics):
    if metric not in metric_rows:
        fail(f"a rule or panel reads {metric}, which metrics.md does not document")
    if not re.search(r'"' + metric + r'[" {]', role_template):
        fail(f"a rule or panel reads {metric}, which roles/pve_guest_metrics does not write")

wiring = read(SEC / "docs" / "wiring" / "index.md")
for needle in ("vtnet1", "vtnet5", "in_iface", "eve.json", "carries **alerts only**", 'job="suricata"', "virusprot", "41641",
               "logfmt", "NetFlow", "Insight", "## 3. Firewall streams and flow visibility"):
    if needle not in wiring:
        fail(f"security/docs/wiring/index.md no longer mentions {needle}")
for stream in ("suricata", "filterlog", "unbound", "pfstats", "pve-firewall"):
    if not re.search(rf"^\| `{stream}` \|", wiring, re.M):
        fail(f"wiring/index.md has no table row for the {stream} stream")
for iface, segment in (("vtnet1", "lan"), ("vtnet2", "tenants"), ("vtnet3", "DMZ"), ("vtnet4", "LAB"), ("vtnet5", "lease")):
    if not re.search(rf"\| `{iface}` \| {segment}", wiring):
        fail(f"wiring/index.md does not map {iface} to {segment}")

alloy_defaults = read(ROOT / "ansible" / "roles" / "alloy_collector" / "defaults" / "main.yml")
alloy_template = read(ROOT / "ansible" / "roles" / "alloy_collector" / "templates" / "config.alloy.j2")
for var in sorted(set(re.findall(r"\balloy_(?:syslog|pve)_[a-z0-9_]+", wiring))):
    if not re.search(rf"^#?\s*{var}:", alloy_defaults, re.M):
        fail(f"wiring/index.md names {var}, which roles/alloy_collector/defaults/main.yml does not define")
for needle in ('job = "firewall-other"', "suricata|filterlog|unbound|pfstats"):
    if needle not in wiring or needle not in alloy_template:
        fail(f"wiring/index.md and the Alloy template no longer agree on {needle}")

for needle in ("`tenant-firewall-not-filtering`", "`family`", "GROUP-tenant-OUT", "`vmbr2`", "15 minute"):
    if needle not in alerting:
        fail(f"alerting.md no longer mentions {needle}")

not_covered = wiring.split("### What is not covered")[-1].split("\n### ")[0] if "### What is not covered" in wiring else ""
if not not_covered:
    fail("security/docs/wiring/index.md has no 'What is not covered' subsection")
for needle in ("TCP 853", "UDP 853", "DNS over HTTPS", "Tailscale", "rate-limited", "one line a second", "--allow",
               "Suricata"):
    if needle not in not_covered:
        fail(f"the wiring doc's 'What is not covered' no longer says {needle!r}")

OVERSTATEMENTS = [r"logged in full", r"every new flow(?! it lets)", r"only record", r"\ball dns\b", r"\bevery dns\b",
                  r"so (its|their) lookups are logged", r"so they are counted too", r"\bevery flow\b"]
scanned = {
    "rules-tenant.yaml": re.sub(r"\n\s*#\s?", " ", read(RULES_FILE)),
    "tenant-activity.json": read(DASH_FILE),
    "alerting.md": alerting,
    "dashboards.md": dashboards_md,
    "retention.md": retention,
    "wiring/index.md": wiring,
}
for rel in ("components", "architecture", "understanding"):
    scanned[f"security/docs/{rel}/index.md"] = re.sub(r"\s+", " ", read(SEC / "docs" / rel / "index.md"))
for name, text in scanned.items():
    flat = re.sub(r"\s+", " ", text)
    for pattern in OVERSTATEMENTS:
        m = re.search(pattern, flat, re.I)
        if m:
            fail(f"{name} says {m.group(0)!r}, which overstates what is logged or visible")

ntop = []
for path in ([SEC / "README.md", SEC / "mkdocs.yml", ROOT / "README.md", ROOT / "docs-landing" / "index.html"]
             + sorted((SEC / "docs").rglob("*.md"))):
    for n, line in enumerate(read(path).splitlines(), 1):
        if re.search(r"ntop", line, re.I):
            ntop.append(f"{path.relative_to(ROOT)}:{n}")
if ntop:
    fail("ntopng is not installed but is still claimed at " + ", ".join(ntop))

# --- relative links and anchors in both modules' docs ------------------------------------
def slug(text):
    """python-markdown's toc slugify, which MkDocs uses."""
    text = re.sub(r"`", "", text)
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def anchors_of(path):
    out = set()
    text = read(path)
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*(?:\{#[^}]+\})?$", text, re.M):
        out.add(slug(m.group(1)))
    # tab titles get slug ids (the site's pymdownx.tabbed slugify)
    for m in re.finditer(r'^\s*=== "([^"]+)"', text, re.M):
        out.add(slug(m.group(1)))
    # admonition titles do not create anchors; explicit ids do
    out.update(re.findall(r'\{#([\w-]+)\}', text))
    return out


doc_files = sorted(list((MON / "docs").rglob("*.md")) + list((SEC / "docs").rglob("*.md"))
                   + [MON / "README.md", SEC / "README.md"])
anchor_cache = {}
for path in doc_files:
    text = re.sub(r"```.*?```", "", read(path), flags=re.S)
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-z]+:", target) or target.startswith("mailto:"):
            continue
        file_part, _, frag = target.partition("#")
        dest = path if not file_part else (path.parent / file_part).resolve()
        if not dest.exists():
            fail(f"{path.relative_to(ROOT)} links to {target}, which does not exist")
            continue
        if frag and dest.suffix == ".md":
            if dest not in anchor_cache:
                anchor_cache[dest] = anchors_of(dest)
            if frag not in anchor_cache[dest]:
                fail(f"{path.relative_to(ROOT)} links to {target}, but {dest.name} has no heading #{frag}")

# --- links from one module's docs into the other's site resolve to a real heading ---------------
SITE = "https://harshitruwali.github.io/homelab-infra/"
for path in doc_files:
    text = re.sub(r"```.*?```", "", read(path), flags=re.S)
    for url in re.findall(r"\]\((" + re.escape(SITE) + r"[^)\s]+)\)", text):
        rest, _, frag = url[len(SITE):].partition("#")
        module, _, doc = rest.strip("/").partition("/")
        if module not in ("monitoring", "security") or not doc:
            continue
        docs_dir = ROOT / module / "docs"
        target = next((c for c in (docs_dir / f"{doc}.md", docs_dir / doc / "index.md") if c.exists()), None)
        if target is None:
            fail(f"{path.relative_to(ROOT)} links to {url}, which matches no page of the {module} docs")
        elif frag:
            if target not in anchor_cache:
                anchor_cache[target] = anchors_of(target)
            if frag not in anchor_cache[target]:
                fail(f"{path.relative_to(ROOT)} links to {url}, but {target.name} has no heading #{frag}")

# --- style: no em dash in either module ---------------------------------------------------
EM_DASH = chr(0x2014)
for base in (MON, SEC):
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.suffix in {".md", ".yaml", ".yml", ".json", ".sh", ".j2", ".py", ".html", ".css"}:
            if ".venv" in path.parts or "site" in path.relative_to(base).parts[:1]:
                continue
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            if EM_DASH in text:
                fail(f"{path.relative_to(ROOT)} contains an em dash")
for path in (pathlib.Path(__file__),):
    if EM_DASH in path.read_text():
        fail("this test contains an em dash")

if errors:
    print("\n".join(f"FAIL: {e}" for e in errors))
    sys.exit(1)
print("tenant monitoring rules, dashboard, deploy wiring and docs agree")
