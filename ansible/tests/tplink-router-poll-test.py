#!/usr/bin/env python3
"""Test roles/tplink_router_poll: its poller against canned router replies, and
the docs and dashboard that describe it.

Standard library only; run from anywhere:

    python3 -I ansible/tests/tplink-router-poll-test.py

Checks:
  * normalise(): Wi-Fi download is what the router SENT, IPs arrive as 32-bit
    integers, the band comes from the host table with a fallback from the
    association stack, the WAN row is the one ONLINESTATUS points at, memory
    is converted from KiB
  * render(): every series has the router label, label values are escaped,
    each metric has HELP and TYPE, a failed poll writes status only
  * poll_once(): the password is never used when the router MAC does not
    match, a busy router is skipped without logging in, a good poll succeeds
  * the daily JSONL copy: today appended, older days gzipped, expired days
    deleted, unrelated files left alone
  * the ledger: first sight and traffic across a polling gap are booked as
    carried, deltas and a counter reset as measured in their own hour, no
    byte is lost or counted twice, a reconnect closes the old session at the
    last poll before it, a client gone 5 minutes closes at its last sighting,
    and a ledger rebuilt from the JSONL days equals the live one
  * the router log: datagrams from any other address are dropped, the
    router's are parsed, stored, written to the JSONL days and restored by a
    rebuild; the forwarding metric reads 0 when the router points elsewhere
  * the nightly mirror skips the live ledger, never deletes, and catches up
  * the poller only issues `go` and `gl` acts
  * the unit passes the password with LoadCredential and never on ExecStart
  * every default has a row in docs/reference/variables.md and the reverse;
    the playbook is in site.yml and the playbooks docs; every metric the
    poller writes is in monitoring's metrics reference; every flag and
    --report value the docs show exists in the poller; every metric the
    dashboard queries is one the poller writes
"""
import datetime as dt
import gzip
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
REPO = ANSIBLE.parent
ROLE = ANSIBLE / "roles" / "tplink_router_poll"
SCRIPT = ROLE / "files" / "tplink-router-poll.py"
DASHBOARD = REPO / "monitoring" / "grafana" / "dashboards" / "fleet" / "router-tplink-blr.json"
PREFIX = "tplink_router_poll_"
errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


sys.dont_write_bytecode = True  # no __pycache__ next to the role's files
spec = importlib.util.spec_from_file_location("poller", SCRIPT)
poller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poller)

# Canned replies in the router's own shapes. Made-up devices only.
RAW = {
    "DEV2_DEV_INFO": {"modelName": "Archer-C5", "hardwareVersion": "Archer-C5 v6.8", "softwareVersion": "1.0",
                      "upTime": "3600", "X_TP_SerialNumber": "SHOULD-NOT-LEAK"},
    "DEV2_HOST_ENTRY": [
        {"physAddress": "02:00:00:00:00:01", "IPAddress": "192.168.0.2", "hostName": "phone",
         "interfaceType": "2.4GHz", "active": "1"},
        {"physAddress": "02:00:00:00:00:02", "IPAddress": "192.168.0.3", "hostName": 'odd"name\\x',
         "interfaceType": "5GHz", "active": "1"},
        {"physAddress": "02:00:00:00:00:03", "IPAddress": "192.168.0.4", "hostName": "",
         "interfaceType": "Other", "active": "0"},
    ],
    "DEV2_STAT_ENTRY": [{"ipAddress": str((192 << 24) | (168 << 16) | 2), "macAddress": "02:00:00:00:00:01",
                         "totalBytes": "5000", "totalPkts": "50"}],
    "DEV2_WIFI_ASSOC_DEV": [
        {"MACAddress": "02:00:00:00:00:01", "signalStrength": "-60", "lastDataDownlinkRate": "130000",
         "lastDataUplinkRate": "58000", "operatingStandard": "n", "stack": "1,1,0,0,0,0"},
        {"MACAddress": "02:00:00:00:00:02", "signalStrength": "-70", "lastDataDownlinkRate": "866000",
         "lastDataUplinkRate": "400000", "operatingStandard": "ac", "stack": "3,1,0,0,0,0"},
        # Not in the host table: band must come from the stack.
        {"MACAddress": "02:00:00:00:00:09", "signalStrength": "-80", "lastDataDownlinkRate": "6000",
         "lastDataUplinkRate": "6000", "operatingStandard": "ac", "stack": "3,2,0,0,0,0"},
    ],
    "DEV2_WIFI_ASSOC_DEV_STAT": [
        {"stack": "1,1,0,0,0,0", "bytesSent": "1000", "bytesReceived": "10", "packetsSent": "7", "packetsReceived": "3", "retransCount": "2"},
        {"stack": "3,1,0,0,0,0", "bytesSent": "2000", "bytesReceived": "20", "packetsSent": "8", "packetsReceived": "4", "retransCount": "0"},
        {"stack": "3,2,0,0,0,0", "bytesSent": "1", "bytesReceived": "1", "packetsSent": "1", "packetsReceived": "1", "retransCount": "0"},
    ],
    "DEV2_IP_INTF_STATS": [
        {"stack": "1,0,0,0,0,0", "bytesReceived": "111", "bytesSent": "222"},
        {"stack": "6,0,0,0,0,0", "bytesReceived": "9000", "bytesSent": "3000"},
    ],
    "DEV2_ONLINESTATUS": {"interfaceV4": "Device.IP.Interface.6.", "onlineStatusV4": "online", "onlineStatusV6": "offline"},
    "DEV2_PROC_STATUS": {"CPUUsage": "12"},
    "DEV2_MEM_STATUS": {"total": "1000", "free": "250"},
}

# ------------------------------------------------------------ normalise ---
s = poller.normalise(RAW, 1_800_000_000.0)
w = {x["mac"]: x for x in s["wifi"]}
check(w["02:00:00:00:00:01"]["download_bytes"] == 1000 and w["02:00:00:00:00:01"]["upload_bytes"] == 10,
      "Wi-Fi download must be bytesSent (router to client), upload bytesReceived")
check(w["02:00:00:00:00:01"]["band"] == "2.4GHz" and w["02:00:00:00:00:02"]["band"] == "5GHz",
      "band must come from the host table")
check(w["02:00:00:00:00:09"]["band"] == "5GHz", "band must fall back to the association stack")
check(w["02:00:00:00:00:02"]["download_rate_bps"] == 866_000_000, "link rate is reported in kbit/s")
check(s["stats"][0]["ip"] == "192.168.0.2", f"stat IP must be decoded from an integer, got {s['stats'][0]['ip']}")
check(s["wan"] == {"download_bytes": 9000, "upload_bytes": 3000}, f"WAN must be interface 6, got {s['wan']}")
check(s["memory_bytes"] == {"total": 1024000, "free": 256000}, "memory is reported in KiB")
check(s["wan_online"] == {"ipv4": 1, "ipv6": 0}, "online state per family")
check("SHOULD-NOT-LEAK" not in json.dumps(s), "the serial number must not reach the sample")
check({c["name"] for c in s["clients"]} >= {"phone", "Unknown"}, "an empty hostname becomes Unknown")

# --------------------------------------------------------------- render ---
text = poller.render("t", s, {"result": "ok", "ts": 1.0, "duration": 0.5, "error": ""})
series = [l for l in text.splitlines() if l and not l.startswith("#")]
check(all('router="t"' in l for l in series), "every series needs the router label")
check('name="odd\\"name\\\\x"' in text, "label values must escape quotes and backslashes")
for metric in {l.split("{")[0] for l in series}:
    check(f"# HELP {metric} " in text and f"# TYPE {metric} " in text, f"{metric} lacks HELP or TYPE")
check(re.search(r'^tplink_router_wifi_client_bytes_total\{[^}]*direction="download"[^}]*mac="02:00:00:00:00:01"[^}]*\} 1000$',
                text, re.M), "download series for the phone must read 1000")
check(re.search(r'^tplink_router_poll_success\{router="t"\} 1$', text, re.M), "a good poll reports success 1")
failed = poller.render("t", None, {"result": "error", "ts": 1.0, "duration": 0.1, "error": "x"})
check("tplink_router_poll_success{router=\"t\"} 0" in failed, "a failed poll reports success 0")
check("wifi_client" not in failed and "wan_bytes" not in failed, "a failed poll must not write stale counters")

# ------------------------------------------------------------ poll_once ---
class Args:
    router = "http://192.168.0.1"
    username = "admin"
    timeout = 1
    expected_mac = "02:aa:bb:cc:dd:ee"

calls = []
poller.read_router = lambda *a: calls.append(a) or RAW
poller.router_busy = lambda url, timeout: False
poller.gateway_mac = lambda ip: "02:11:22:33:44:55"
sample, status = poller.poll_once(Args, "pw")
check(status["result"] == "wrong_network" and not calls and sample is None,
      "a different router MAC must skip the poll without sending the password")
poller.gateway_mac = lambda ip: "02:AA:BB:CC:DD:EE".lower()
poller.router_busy = lambda url, timeout: True
sample, status = poller.poll_once(Args, "pw")
check(status["result"] == "busy" and not calls, "a busy router must be skipped without logging in")
poller.router_busy = lambda url, timeout: False
sample, status = poller.poll_once(Args, "pw")
check(status["result"] == "ok" and len(calls) == 1 and sample, "a matching, idle router is polled once")

# ---------------------------------------------------------- JSONL files ---
with tempfile.TemporaryDirectory() as d:
    now = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc).timestamp()
    for name in ("2026-10-01.jsonl.gz", "2026-11-19.jsonl", "notes.txt"):
        pathlib.Path(d, name).write_text("{}\n")
    poller.append_sample(d, {"ts": now}, 30, now)
    names = sorted(os.listdir(d))
    check(names == ["2026-11-19.jsonl.gz", "2026-11-20.jsonl", "notes.txt"], f"rotation left {names}")
    check(gzip.open(pathlib.Path(d, "2026-11-19.jsonl.gz")).read() == b"{}\n", "gzip must keep the content")

# ---------------------------------------------------------------- ledger ---
import sqlite3  # noqa: E402

T0 = dt.datetime(2026, 11, 20, 10, 59, tzinfo=dt.timezone.utc).timestamp()


def wsample(ts, clients):
    """A stored sample with Wi-Fi clients given as {mac: (down, up, connected_seconds)}."""
    return {"ts": ts, "poll": "ok",
            "clients": [{"mac": m, "ip": "192.168.0.%d" % i, "name": "dev%d" % i, "band": "5GHz", "active": 1}
                        for i, m in enumerate(sorted(clients), 2)],
            "wifi": [{"mac": m, "band": "5GHz", "download_bytes": d, "upload_bytes": u, "connected_seconds": c}
                     for m, (d, u, c) in clients.items()]}


A, B = "02:00:00:00:00:0a", "02:00:00:00:00:0b"
SAMPLES = [
    wsample(T0, {A: (1000, 100, 600), B: (50, 5, 30)}),     # first sight: carried, start estimated
    wsample(T0 + 60, {A: (1500, 150, 660)}),                 # A measured 500/50 (next hour); B missing
    wsample(T0 + 120, {A: (200, 20, 10)}),                   # A reset: reconnected, measured 200/20
    wsample(T0 + 480, {A: (300, 30, 370)}),                  # B gone > 5 min: its session closes
    wsample(T0 + 480 + 20 * 60, {A: (800, 80, 1570)}),       # 20 min gap: carried 500/50
]
with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "usage.db")
    ledger = poller.Ledger(db)
    lines = []
    for smp in SAMPLES:
        lines += ledger.update(smp)
        poller.append_sample(d, smp, 365, smp["ts"])
    con = sqlite3.connect(db)
    rows = {(h, m, k): (dn, up) for h, m, k, dn, up in con.execute(
        "SELECT hour, mac, kind, download_bytes, upload_bytes FROM usage")}
    check(rows.get(("2026-11-20T10:00Z", A, "carried")) == (1000, 100), f"first sight must be carried: {rows}")
    check(rows.get(("2026-11-20T11:00Z", A, "measured")) == (500 + 200 + 100, 50 + 20 + 10),
          f"deltas and the reset must be measured in their hour: {rows}")
    check(rows.get(("2026-11-20T11:00Z", A, "carried")) == (500, 50), f"traffic across a long gap is carried: {rows}")
    total = sum(v[0] for k, v in rows.items() if k[1] == A)
    check(total == 1000 + 500 + 200 + 100 + 500, f"no byte may be lost or double-counted, got {total}")
    sess = con.execute("SELECT mac, start, start_estimated, end FROM sessions ORDER BY mac, start").fetchall()
    a_sess = [s for s in sess if s[0] == A]
    check(len(a_sess) == 2, f"A reconnected once, so two sessions: {a_sess}")
    check(a_sess and a_sess[0][1] == T0 - 600 and a_sess[0][2] == 1, "a first-seen session starts at its association time")
    check(a_sess and a_sess[0][3] == T0 + 60, "a reconnect closes the old session at the last poll before it")
    b_sess = [s for s in sess if s[0] == B]
    check(b_sess and b_sess[0][3] == T0, f"a client gone 5 minutes closes at the last poll it was seen: {b_sess}")
    check(any(l.startswith("usage ") and "kind=measured" in l for l in lines), "usage lines must be printed")
    check(any(l.startswith("session_end ") for l in lines), "session_end lines must be printed")

    # A phone in power save misses one poll without disconnecting: no
    # session_end for it, and it stays one session.
    C = "02:00:00:00:00:0c"
    flap = poller.Ledger(os.path.join(d, "flap.db"))
    flap_lines = []
    for smp in (wsample(T0, {C: (10, 1, 5)}), wsample(T0 + 60, {}), wsample(T0 + 120, {C: (20, 2, 125)})):
        flap_lines += flap.update(smp)
    check(not any(l.startswith("session_end ") for l in flap_lines),
          f"a client missing for one poll must not be logged as leaving: {flap_lines}")
    check(flap.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1, "a missed poll must not split a session")

    # The ledger rebuilt from the JSONL days must equal the live one.
    rebuilt = os.path.join(d, "rebuilt.db")
    poller.rebuild_ledger(d, rebuilt)
    rcon = sqlite3.connect(rebuilt)
    q = "SELECT hour, mac, kind, download_bytes, upload_bytes, seconds_seen FROM usage ORDER BY 1, 2, 3"
    check(rcon.execute(q).fetchall() == con.execute(q).fetchall(), "rebuild from JSONL must reproduce the usage table")

    # ---------------------------------------------------------- router log ---
    before = poller.LOG_REJECTED[0]
    got = poller.handle_router_log(b"<13> spoofed", "192.168.0.66", "192.168.0.1", db, d, 365, T0 + 2000)
    check(got is None and poller.LOG_REJECTED[0] == before + 1, "a datagram from another address must be dropped")
    got = poller.handle_router_log(b"<30>DHCPD: Send ACK to 192.168.0.9\x00", "192.168.0.1", "192.168.0.1",
                                   db, d, 365, T0 + 2001)
    check(got == {"severity": "info", "facility": "daemon", "message": "DHCPD: Send ACK to 192.168.0.9"},
          f"router log parse: {got}")
    ev = sqlite3.connect(db).execute("SELECT severity, message FROM events").fetchall()
    check(ev == [("info", "DHCPD: Send ACK to 192.168.0.9")], f"only the router's line is stored: {ev}")
    poller.rebuild_ledger(d, rebuilt)
    check(sqlite3.connect(rebuilt).execute("SELECT count(*) FROM events").fetchone()[0] == 1,
          "rebuild must restore router log lines from the JSONL days")

# ------------------------------------------------------- log forwarding ---
fs = poller.normalise(dict(RAW, DEV2_SYSLOG_CFG={"logToRemote": "1", "serverIP": "192.168.0.6", "serverPort": "5140"}), 1.0)
check(fs["log_forward"] == {"enabled": 1, "server": "192.168.0.6", "port": 5140}, f"log forward parse: {fs['log_forward']}")
fs["log_forward"]["here"] = 0
check('tplink_router_log_forwarding{router="t",server="192.168.0.6",port="5140"} 0'
      in poller.render("t", fs, {"result": "ok", "ts": 1.0, "duration": 0.1, "error": ""}),
      "log forwarding pointing elsewhere must read 0")

# ---------------------------------------------------------------- mirror ---
mirror = (ROLE / "templates" / "tplink-router-mirror.service.j2").read_text()
check("--exclude usage.db" in mirror, "the mirror must skip the live ledger and copy the snapshot")
check("--delete" not in mirror, "the mirror must never delete what the poller host pruned")
check("Persistent=true" in (ROLE / "templates" / "tplink-router-mirror.timer.j2").read_text(),
      "a receiving host that was off must catch up")

# ------------------------------------------------------------- read-only ---
check({op for op, _ in poller.ROUTER_ACTS} <= {"go", "gl"}, "the poller may only issue go and gl acts")

# ---------------------------------------------------------------- unit ---
unit = (ROLE / "templates" / "tplink-router-poll.service.j2").read_text()
execstart = unit.split("ExecStart=")[1].split("\n# ")[0]
check("LoadCredential=password:" in unit, "the unit must pass the password with LoadCredential")
check("password" not in execstart.lower(), "the password path must not be on ExecStart")

# ---------------------------------------------------------------- docs ---
defaults = set(re.findall(rf"^({PREFIX}\w+):", (ROLE / "defaults" / "main.yml").read_text(), re.M))
variables = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
documented = set(re.findall(rf"^\| `({PREFIX}\w+)`", variables, re.M))
check(defaults == documented, f"variables.md and defaults differ: {sorted(defaults ^ documented)}")
check("import_playbook: tplink-router.yml" in (ANSIBLE / "playbooks" / "site.yml").read_text(), "playbook missing from site.yml")
check("`tplink-router.yml`" in (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text(), "playbook missing from playbooks.md")
check("`tplink-router.yml`" in (ANSIBLE / "README.md").read_text(), "playbook missing from the ansible README")
metrics_doc = (REPO / "monitoring" / "docs" / "reference" / "metrics.md").read_text()
for metric in poller.HELP:
    check(f"`{metric}" in metrics_doc, f"{metric} is not in monitoring/docs/reference/metrics.md")

# Every flag the docs show with the poller exists in its argument parser.
script_src = SCRIPT.read_text()
parser_flags = set(re.findall(r'add_argument\("(--[a-z-]+)"', script_src))
playbooks_doc = (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text()
role_section = playbooks_doc.split("### `tplink_router_poll`", 1)[1].split("\n### ", 1)[0]
for flag in set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", role_section)):
    check(flag in parser_flags, f"playbooks.md shows {flag}, which the poller does not accept")
report_choices = set(re.search(r'"--report", choices=\[([^\]]+)\]', script_src).group(1).replace('"', "").replace(" ", "").split(","))
documented = set(re.findall(r"--report (\w+)", playbooks_doc))
check(documented and documented <= report_choices, f"documented --report values not accepted: {documented - report_choices}")
for kind in report_choices:
    check(f"--report {kind}" in playbooks_doc, f"--report {kind} is not documented in playbooks.md")

dash = json.loads(DASHBOARD.read_text())
check(dash["title"] == "Router TPLink Blr" and dash["uid"] == "router-tplink-blr", "dashboard title or uid changed")
queried = set()
for p in dash["panels"]:
    for t in p.get("targets", []):
        queried |= set(re.findall(r"\b(tplink_router_\w+)", t["expr"]))
check(queried and queried <= set(poller.HELP), f"dashboard queries unknown metrics: {sorted(queried - set(poller.HELP))}")

if errors:
    print("FAIL")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("ok: tplink_router_poll")
