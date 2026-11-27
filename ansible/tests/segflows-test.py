#!/usr/bin/env python3
"""Offline test of security/opnsense/segflows, the inter-segment traffic accounting on the firewall.

Standard library only; no network, no firewall, nothing installed. flowd's parser
is replaced by a fake `lib` package that reads JSON lines and yields records in
the shape OPNsense's FlowParser yields (recv_sec, if_ndx_in, if_ndx_out,
src_addr, dst_addr, src_port, dst_port, protocol, octets, packets, flow_start);
ngctl and ifconfig are shims that print fixture output. Run from anywhere:

    python3 -I ansible/tests/segflows-test.py

Checks:
  * syntax: py_compile on segflows.py, `sh -n` on install.sh
  * classification of addresses into LAN, TENANT, DMZ, LAB, LEASE, HOME, TAILNET,
    FIREWALL, LINKLOCAL, PRIVATE and INTERNET
  * one run on fixture flows: exact bytes, packets and flows per segment pair; a
    flow exported twice because it crosses two captured interfaces counts once,
    while identical records that are not twins still count; records newer than
    the guard wait for the next run
  * counters are monotonic across runs and process restarts, survive a deleted
    .prom and a lost state file (ledger position wins), and a corrupt state
    file is set aside
  * the work cap stops at a second boundary and later runs lose and double nothing
  * ledger: hourly rows book into the receive hour, host pairs are bounded at 200
    while the day is open and 50 once it is over, and keep the largest
  * the textfile: every metric, valid exposition lines, mode 0644, atomic temp
    file left behind never, capture interfaces from ngctl
  * --report pairs|hosts|hour|day with --days and --segment, in Asia/Kolkata
  * every internal interface captured: a LAN to DMZ flow exported by both nodes counts once per
    direction, at the larger of the two records, also when the twin lands in the next run; flows
    that cross one captured interface, and records with the same in and out, are not twins
  * netflow-capture.php (static checks; php is not installed here): flags, original and default
    lists, add-only, dry run before save, the GUI's reload order
  * install.sh: dry run on a fake root changes nothing; refusals; --apply installs
    with the documented modes, creates the ledger directory, runs the cron script,
    runs the script once and fails when a metric is missing; --uninstall
  * the configd action, cron command and docs agree; every metric written is in
    monitoring/docs/reference/metrics.md; every segflow metric the dashboard
    queries is written by the script
"""
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "security" / "opnsense" / "segflows"
SCRIPT = SRC / "segflows.py"
INSTALL_SH = SRC / "install.sh"
ACTIONS = SRC / "actions_segflows.conf"
CRON_PHP = SRC / "add-segflows-cron.php"
CAPTURE_PHP = SRC / "netflow-capture.php"
METRICS_MD = ROOT / "monitoring" / "docs" / "reference" / "metrics.md"
ROUTERS_MD = ROOT / "monitoring" / "docs" / "monitoring" / "routers.md"
DASHBOARDS_MD = ROOT / "monitoring" / "docs" / "monitoring" / "dashboards.md"
DASH = ROOT / "monitoring" / "grafana" / "dashboards" / "fleet" / "inter-lan.json"
SEC_README = ROOT / "security" / "README.md"
MON_README = ROOT / "monitoring" / "README.md"

errors = []


def fail(msg):
    errors.append(msg)


def check(cond, msg):
    if not cond:
        fail(msg)


# --- fake OPNsense netflow lib ----------------------------------------------------------------
FAKE_FLOWPARSER = '''
import json
import os


class FlowParser:
    def __init__(self, filename, recv_stamp=None):
        self._filename = filename
        self._recv_stamp = recv_stamp

    def __iter__(self):
        # test hook: rotate flowd.log the way flowd_aggregate does, while this file is being opened
        marker = os.environ.get("ROTATE_MARKER")
        if marker and os.path.exists(marker) and os.path.basename(self._filename) == os.environ.get("ROTATE_AT"):
            os.remove(marker)
            base = os.path.join(os.path.dirname(self._filename), "flowd.log")
            os.rename(base + ".000001", base + ".000002")
            os.rename(base, base + ".000001")
            os.replace(os.environ["ROTATE_NEW"], base)
        with open(self._filename) as fh:
            for line in fh:
                rec = json.loads(line)
                if self._recv_stamp is not None and rec["recv_sec"] < self._recv_stamp:
                    continue
                rec.setdefault("flow_start", rec["recv_sec"] - 1.0)
                rec.setdefault("flow_end", float(rec["recv_sec"]))
                yield rec
'''
FAKE_PARSE = '''
class Interfaces(object):
    def __init__(self):
        self._if_index = {str(i + 1): "vtnet%d" % i for i in range(6)}
        self._if_index["7"] = "lo0"

    def if_device(self, if_index):
        return self._if_index.get("%s" % if_index, "%s" % if_index)
'''
SHIMS = {
    "ngctl": """#!/bin/sh
case "$*" in
    list) cat "$FIXDIR/ngctl.txt" ;;
    *) echo "ngctl shim: unexpected arguments: $*" >&2; exit 1 ;;
esac
""",
    "ifconfig": """#!/bin/sh
cat "$FIXDIR/ifconfig.txt"
""",
    "service": """#!/bin/sh
printf 'service %s\\n' "$*" >> "$FIXDIR/calls.log"
""",
    "configctl": """#!/bin/sh
printf 'configctl %s\\n' "$*" >> "$FIXDIR/calls.log"
""",
    "php": """#!/bin/sh
printf 'php %s (cwd %s)\\n' "$*" "$(pwd)" >> "$FIXDIR/calls.log"
""",
}
NGCTL = """There are 6 total nodes:
  Name: vtnet2          Type: ether           ID: 00000004   Num hooks: 2
  Name: netflow_vtnet2  Type: netflow         ID: 0000000a   Num hooks: 3
  Name: ksocket_netflow_vtnet2 Type: ksocket         ID: 00000010   Num hooks: 1
  Name: netflow_vtnet5  Type: netflow         ID: 00000015   Num hooks: 3
  Name: ngctl12591      Type: socket          ID: 0000001e   Num hooks: 0
"""
IFCONFIG = """vtnet0: flags=1008843<UP> metric 0 mtu 1500
\tinet 10.10.0.114 netmask 0xffffff00 broadcast 10.10.0.255
vtnet1: flags=1008843<UP> metric 0 mtu 1500
\tinet 10.10.50.1 netmask 0xffffff00 broadcast 10.10.50.255
vtnet2: flags=1008843<UP> metric 0 mtu 1500
\tinet 10.10.60.1 netmask 0xffffff00 broadcast 10.10.60.255
\tinet6 fe80::be24:11ff:fe00:2%vtnet2 prefixlen 64 scopeid 0x3
lo0: flags=8049<UP,LOOPBACK> metric 0 mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
"""

# 2026-11-27 ~ 00:00 UTC area; the exact value only needs to be an hour start plus 10 minutes
T0 = 1795800000 - (1795800000 % 3600) + 600
HOUR = (T0 + 19800) // 3600 * 3600 - 19800  # Asia/Kolkata hour boundaries sit at :30 UTC


def make_shims(base):
    shims = base / "shims"
    shims.mkdir()
    for name, body in SHIMS.items():
        path = shims / name
        path.write_text(body)
        path.chmod(0o755)
    return shims


class Sandbox:
    """A scratch tree with the fake lib, shims, a flowd log and state directories."""

    def __init__(self, base, name):
        self.dir = base / name
        self.dir.mkdir()
        self.fix = self.dir / "fix"
        self.fix.mkdir()
        (self.fix / "ngctl.txt").write_text(NGCTL)
        (self.fix / "ifconfig.txt").write_text(IFCONFIG)
        self.netflow = self.dir / "netflow"
        (self.netflow / "lib").mkdir(parents=True)
        (self.netflow / "lib" / "__init__.py").write_text("")
        (self.netflow / "lib" / "flowparser.py").write_text(FAKE_FLOWPARSER)
        (self.netflow / "lib" / "parse.py").write_text(FAKE_PARSE)
        self.logs = self.dir / "logs"
        self.logs.mkdir()
        self.state = self.dir / "state"
        self.prom_dir = self.dir / "prom"
        self.shims = make_shims(self.dir)
        self.log = self.logs / "flowd.log"
        self.log.write_text("")

    def env(self, now, **extra):
        env = dict(os.environ)
        env["PATH"] = f"{self.shims}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        env.update(FIXDIR=str(self.fix), SEGFLOWS_DIR=str(self.state), SEGFLOWS_PROM_DIR=str(self.prom_dir),
                   SEGFLOWS_NETFLOW_DIR=str(self.netflow), SEGFLOWS_FLOWD_LOG=str(self.log),
                   SEGFLOWS_NOW=str(now))
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def append(self, records, path=None):
        path = path or self.log
        with open(path, "a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        newest = max([r["recv_sec"] for r in records] + [0])
        os.utime(path, (newest, newest))

    def run(self, now, *args, **extra):
        return subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args), capture_output=True, text=True,
                              env=self.env(now, **extra))

    def prom(self):
        return parse_prom((self.prom_dir / "segflows.prom").read_text())


def rec(sec, src, dst, if_in, if_out, nbytes, npackets, sport=40000, dport=443, proto=6, fend=None):
    extra = {} if fend is None else {"flow_end": fend}
    return {**extra, "recv_sec": sec, "src_addr": src, "dst_addr": dst, "if_ndx_in": if_in, "if_ndx_out": if_out,
            "octets": nbytes, "packets": npackets, "src_port": sport, "dst_port": dport, "protocol": proto}


def parse_prom(text):
    """name -> {labels tuple: value}, and the set of TYPE lines; fails on a malformed sample line."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r'([a-z_]+)(\{[^}]*\})? (-?[0-9.]+)', line)
        if not m:
            fail(f"malformed exposition line: {line!r}")
            continue
        labels = tuple(sorted(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', m.group(2) or "")))
        out.setdefault(m.group(1), {})[labels] = float(m.group(3))
    return out


def pair(prom, name, src, dst):
    return prom.get(name, {}).get((("dst_segment", dst), ("src_segment", src)))


def triple(prom, src, dst):
    return tuple(pair(prom, f"opnsense_segflow_{m}_total", src, dst) for m in ("bytes", "packets", "flows"))


def ist_day(epoch):
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return datetime.datetime.fromtimestamp(epoch, ist).strftime("%Y-%m-%d")


def snapshot(tree):
    out = {}
    for path in sorted(tree.rglob("*")):
        rel = str(path.relative_to(tree))
        if path.is_dir():
            out[rel] = ("dir", stat.S_IMODE(path.stat().st_mode))
        else:
            st = path.stat()
            out[rel] = ("file", stat.S_IMODE(st.st_mode), st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


# --- syntax -------------------------------------------------------------------------------------
try:
    compile(SCRIPT.read_text(), str(SCRIPT), "exec")
except SyntaxError as exc:
    fail(f"segflows.py does not compile: {exc}")
p = subprocess.run(["sh", "-n", str(INSTALL_SH)], capture_output=True, text=True)
check(p.returncode == 0, f"sh -n install.sh failed: {p.stderr.strip()}")
check(os.access(SCRIPT, os.X_OK) and os.access(INSTALL_SH, os.X_OK), "segflows.py and install.sh must be executable")
check(SCRIPT.read_text().startswith("#!/usr/local/bin/python3\n"), "segflows.py must start with the firewall's python3 shebang")

# --- classification -----------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("segflows_under_test", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
cls = mod.Classifier({"10.10.0.114", "10.10.50.1", "10.10.60.1", "fe80::be24:11ff:fe00:2"})
EXPECT_SEG = {
    "10.10.50.7": "LAN", "10.10.60.20": "TENANT", "10.10.70.106": "DMZ", "10.10.80.4": "LAB", "10.10.90.5": "LEASE",
    "10.10.0.103": "HOME", "10.10.1.9": "HOME", "10.10.2.9": "HOME", "192.168.1.5": "HOME",
    "100.88.214.113": "TAILNET", "100.64.0.1": "TAILNET", "10.10.0.114": "FIREWALL", "10.10.60.1": "FIREWALL",
    "fe80::be24:11ff:fe00:2": "FIREWALL", "fe80::1": "LINKLOCAL", "ff02::fb": "LINKLOCAL", "224.0.0.251": "LINKLOCAL",
    "255.255.255.255": "LINKLOCAL", "169.254.1.1": "LINKLOCAL", "172.20.0.5": "PRIVATE", "10.99.0.1": "PRIVATE",
    "1.1.1.1": "INTERNET", "172.236.162.42": "INTERNET", "2606:4700::1111": "INTERNET", "100.128.0.1": "INTERNET",
    "10.10.60.256": "INTERNET",
}
for addr, want in EXPECT_SEG.items():
    got = cls.segment(addr)
    check(got == want, f"classify {addr}: want {want}, got {got}")
check(mod.service_port({"protocol": 6, "src_port": 443, "dst_port": 59572}) == 443, "service_port must be the lower port")
check(mod.service_port({"protocol": 1, "src_port": 0, "dst_port": 0}) == 0, "service_port of ICMP must be 0")

with tempfile.TemporaryDirectory() as tmp:
    base = pathlib.Path(tmp)

    # --- one run on fixture flows -------------------------------------------------------------
    sb = Sandbox(base, "main")
    NOW1 = T0 + 3600 + 100
    R1 = [
        rec(T0, "10.10.60.20", "1.1.1.1", 3, 1, 1000, 10),
        rec(T0, "1.1.1.1", "10.10.60.20", 1, 3, 5000, 20, sport=443, dport=40000),
        rec(T0 + 1, "10.10.70.106", "10.10.60.20", 4, 3, 700, 7, sport=41641, dport=41641, proto=17),
        # TENANT to LEASE crosses two captured interfaces: exported by both nodes
        rec(T0 + 2, "10.10.60.20", "10.10.90.5", 3, 6, 300, 3),
        rec(T0 + 3, "10.10.60.20", "10.10.90.5", 3, 6, 300, 3),
        # two identical pings, each exported twice: two flows, not one
        rec(T0 + 4, "10.10.60.20", "10.10.90.5", 3, 6, 84, 1, sport=0, dport=0, proto=1),
        rec(T0 + 4, "10.10.60.20", "10.10.90.5", 3, 6, 84, 1, sport=0, dport=0, proto=1),
        rec(T0 + 5, "10.10.60.20", "10.10.90.5", 3, 6, 84, 1, sport=0, dport=0, proto=1),
        rec(T0 + 5, "10.10.60.20", "10.10.90.5", 3, 6, 84, 1, sport=0, dport=0, proto=1),
        rec(T0 + 6, "10.10.60.21", "10.10.60.1", 3, 7, 82, 1, sport=56028, dport=5351, proto=17),
        rec(T0 + 7, "10.10.60.20", "10.10.0.103", 3, 1, 400, 4, dport=22),
        rec(T0 + 8, "10.10.60.20", "100.88.214.113", 3, 1, 250, 2, dport=22),
        rec(T0 + 9, "fe80::1", "ff02::2", 3, 1, 56, 1, sport=0, dport=0, proto=58),
        rec(T0 + 10, "10.10.60.20", "172.20.0.5", 3, 1, 90, 1),
        rec(T0 + 3600, "10.10.60.20", "1.1.1.1", 3, 1, 2000, 4),
        # inside the 10 s guard: left for the next run
        rec(NOW1 - 5, "10.10.60.20", "1.1.1.1", 3, 1, 111, 1),
    ]
    sb.append(R1)
    p = sb.run(NOW1)
    check(p.returncode == 0, f"run 1 exited {p.returncode}: {p.stderr.strip()}")
    prom = sb.prom()
    WANT1 = {
        ("TENANT", "INTERNET"): (3000, 14, 2), ("INTERNET", "TENANT"): (5000, 20, 1), ("DMZ", "TENANT"): (700, 7, 1),
        ("TENANT", "LEASE"): (468, 5, 3), ("TENANT", "FIREWALL"): (82, 1, 1), ("TENANT", "HOME"): (400, 4, 1),
        ("TENANT", "TAILNET"): (250, 2, 1), ("LINKLOCAL", "LINKLOCAL"): (56, 1, 1), ("TENANT", "PRIVATE"): (90, 1, 1),
    }
    for (s, d), want in WANT1.items():
        got = triple(prom, s, d)
        check(got == want, f"run 1 {s} to {d}: bytes, packets, flows want {want}, got {got}")
    series = {tuple(dict(k).values()) for k in prom.get("opnsense_segflow_bytes_total", {})}
    check(len(series) == len(WANT1), f"run 1 has {len(series)} pairs, expected {len(WANT1)}: {sorted(series)}")
    check(prom["opnsense_segflow_lag_seconds"][()] == NOW1 - (T0 + 3600) , f"lag wrong: {prom['opnsense_segflow_lag_seconds']}")
    check(prom["opnsense_segflow_last_run_timestamp_seconds"][()] > 1e9, "last_run_timestamp_seconds missing")
    check(prom["opnsense_segflow_run_duration_seconds"][()] >= 0, "run_duration_seconds missing")
    caps = {dict(k)["interface"]: dict(k)["segment"] for k in prom.get("opnsense_segflow_capture_interfaces", {})}
    check(caps == {"vtnet2": "TENANT", "vtnet5": "LEASE"}, f"capture interfaces from ngctl: {caps}")
    check(stat.S_IMODE((sb.prom_dir / "segflows.prom").stat().st_mode) == 0o644, "segflows.prom must be mode 0644")
    check(sorted(x.name for x in sb.prom_dir.iterdir()) == ["segflows.prom"], f"temp files left in the textfile dir: {list(sb.prom_dir.iterdir())}")
    check(stat.S_IMODE(sb.state.stat().st_mode) == 0o750, "the state directory must be mode 0750")

    # ledger: hourly rows book into the receive hour
    db = sqlite3.connect(sb.state / "ledger.sqlite")
    hourly = {(h, s, d): (b, pk, f) for h, s, d, b, pk, f in db.execute("SELECT * FROM hourly")}
    check(hourly.get((HOUR, "TENANT", "INTERNET")) == (1000, 10, 1), f"hour 1 TENANT to INTERNET: {hourly.get((HOUR, 'TENANT', 'INTERNET'))}")
    check(hourly.get((HOUR + 3600, "TENANT", "INTERNET")) == (2000, 4, 1), "the T0+3600 flow must book into the next hour")
    check(hourly.get((HOUR, "TENANT", "LEASE")) == (468, 5, 3), "the deduped TENANT to LEASE hour row is wrong")
    check(sum(v[0] for v in hourly.values()) == sum(v[0] for v in WANT1.values()), "ledger hourly bytes differ from the counters")
    hosts = db.execute("SELECT src_ip, dst_ip, port, proto, bytes FROM hosts WHERE src_segment='TENANT' AND dst_segment='INTERNET'").fetchall()
    check(hosts == [("10.10.60.20", "1.1.1.1", 443, 6, 3000)], f"host pair row for TENANT to INTERNET: {hosts}")
    reply = db.execute("SELECT port FROM hosts WHERE src_segment='INTERNET' AND dst_segment='TENANT'").fetchall()
    check(reply == [(443,)], f"the reply direction must be filed under the server port 443: {reply}")
    db.close()
    check(stat.S_IMODE((sb.state / "ledger.sqlite").stat().st_mode) == 0o640, "ledger.sqlite must be mode 0640")

    # a second run with no new flows changes nothing and reads nothing twice
    NOW2 = NOW1 + 300
    sb.run(NOW1 + 1)
    check(triple(sb.prom(), "TENANT", "INTERNET") == (3000, 14, 2), "a run with nothing new changed the counters")

    # a third run: new flows plus the one that was inside the guard; counters only grow, by exactly that
    before = sb.prom()
    sb.append([rec(NOW1 + 120, "10.10.60.20", "1.1.1.1", 3, 1, 100, 1)])
    p = sb.run(NOW2)
    after = sb.prom()
    check(triple(after, "TENANT", "INTERNET") == (3000 + 111 + 100, 14 + 2, 4), f"run 3 TENANT to INTERNET: {triple(after, 'TENANT', 'INTERNET')}")
    for name in ("bytes", "packets", "flows"):
        for labels, value in before.get(f"opnsense_segflow_{name}_total", {}).items():
            check(after[f"opnsense_segflow_{name}_total"].get(labels, -1) >= value, f"{name} counter went down for {labels}")
    # the ping twins are not carried over as pending forever
    state = json.loads((sb.state / "state.json").read_text())
    check(state["last_recv"] == NOW2 - 10, f"state last_recv {state['last_recv']} != {NOW2 - 10}")
    check(len(state["pending"]) <= 2, f"pending twin keys are not expiring: {len(state['pending'])}")
    pend_before = state["pending"]

    # a deleted .prom is rebuilt from the state file, counters intact
    (sb.prom_dir / "segflows.prom").unlink()
    sb.run(NOW2 + 300)
    check(triple(sb.prom(), "TENANT", "INTERNET") == triple(after, "TENANT", "INTERNET"), "counters did not survive a deleted .prom")
    check(pair(sb.prom(), "opnsense_segflow_bytes_total", "TENANT", "LEASE") == 468, "TENANT to LEASE lost after a restart")

    # the state file lost: the ledger's position wins, so nothing is read twice
    (sb.state / "state.json").unlink()
    sb.run(NOW2 + 600)
    db = sqlite3.connect(sb.state / "ledger.sqlite")
    total_ledger = db.execute("SELECT SUM(bytes) FROM hourly").fetchone()[0]
    db.close()
    check(total_ledger == sum(v[0] for v in WANT1.values()) + 211, f"ledger total after a lost state file: {total_ledger}")

    # corrupt state: set aside, run continues
    (sb.state / "state.json").write_text("{not json")
    p = sb.run(NOW2 + 900)
    check(p.returncode == 0 and (sb.state / "state.json.bad").exists(), f"a corrupt state file must be set aside, exit {p.returncode}")

    # --- twin matching expires ------------------------------------------------------------------
    sb2 = Sandbox(base, "twins")
    one = rec(T0, "10.10.60.20", "10.10.90.5", 3, 6, 50, 1)
    sb2.append([one, rec(T0 + 100, "10.10.60.20", "10.10.90.5", 3, 6, 50, 1)])
    sb2.run(T0 + 200)
    check(triple(sb2.prom(), "TENANT", "LEASE") == (100, 2, 2), "two identical records 100 s apart are two flows, not twins")

    # --- every internal interface captured: routed flows arrive as ingress and egress twins -------
    sb6 = Sandbox(base, "alltwins")
    (sb6.fix / "ngctl.txt").write_text("There are 5 total nodes:\n" + "".join(
        f"  Name: netflow_vtnet{i}  Type: netflow         ID: 0000000{i}   Num hooks: 3\n" for i in (1, 2, 3, 4, 5)))
    TA = float(T0)
    LD = ("10.10.50.7", "10.10.70.9")
    R6 = [
        # LAN to DMZ, ingress record from the lan node and egress record from the dmz node
        rec(T0, *LD, 2, 4, 1000, 10, sport=40000, fend=TA - 0.2), rec(T0 + 1, *LD, 2, 4, 1000, 10, sport=40000, fend=TA + 0.5),
        # and the reply, DMZ to LAN
        rec(T0, LD[1], LD[0], 4, 2, 5000, 20, sport=443, dport=40000, fend=TA - 0.1),
        rec(T0 + 1, LD[1], LD[0], 4, 2, 5000, 20, sport=443, dport=40000, fend=TA + 0.4),
        # pf dropped some packets: ingress saw more than egress; counts the larger, once
        rec(T0, *LD, 2, 4, 2000, 20, sport=40001, fend=TA), rec(T0 + 1, *LD, 2, 4, 1800, 18, sport=40001, fend=TA + 0.5),
        # the smaller record arrives first: the surplus is added when the larger one arrives
        rec(T0, *LD, 2, 4, 1800, 18, sport=40002, fend=TA), rec(T0 + 1, *LD, 2, 4, 2000, 20, sport=40002, fend=TA + 0.5),
        # same 5-tuple twice, 10 s apart: two flows, not twins
        rec(T0 + 3, *LD, 2, 4, 1000, 10, sport=40003, fend=TA + 2.0), rec(T0 + 13, *LD, 2, 4, 1000, 10, sport=40003, fend=TA + 12.5),
        # the twin lands in the next run
        rec(T0 + 2, *LD, 2, 4, 700, 7, sport=40004, fend=TA + 0.8), rec(T0 + 22, *LD, 2, 4, 700, 7, sport=40004, fend=TA + 1.0),
        # one captured interface only (the WAN has no node): one record each
        rec(T0, "10.10.60.20", "1.1.1.1", 3, 1, 1000, 10, fend=TA), rec(T0, "10.10.50.7", "10.10.0.103", 2, 1, 400, 4, fend=TA),
        rec(T0 + 1, "1.1.1.1", "10.10.70.9", 1, 4, 300, 3, fend=TA),
        # in and out the same interface is one node's record: never twins
        rec(T0, "10.10.60.20", "10.10.60.21", 3, 3, 50, 1, sport=1111, fend=TA), rec(T0 + 1, "10.10.60.20", "10.10.60.21", 3, 3, 50, 1, sport=1111, fend=TA + 0.5),
    ]
    sb6.append(sorted(R6, key=lambda r: r["recv_sec"]))  # flowd writes in receive order
    sb6.run(T0 + 30)
    got = triple(sb6.prom(), "LAN", "DMZ")
    check(got == (7700, 77, 6), f"LAN to DMZ with both ends captured: want (7700, 77, 6), got {got}")
    sb6.run(T0 + 300)
    check(triple(sb6.prom(), "LAN", "DMZ") == (7700, 77, 6), "the twin that lands in the next run was counted again")
    for (s_, d_), want in {("DMZ", "LAN"): (5000, 20, 1), ("TENANT", "INTERNET"): (1000, 10, 1), ("LAN", "HOME"): (400, 4, 1),
                           ("INTERNET", "DMZ"): (300, 3, 1), ("TENANT", "TENANT"): (100, 2, 2)}.items():
        got = triple(sb6.prom(), s_, d_)
        check(got == want, f"all interfaces captured, {s_} to {d_}: want {want}, got {got}")
    caps6 = {dict(k)["interface"] for k in sb6.prom().get("opnsense_segflow_capture_interfaces", {})}
    check(caps6 == {"vtnet1", "vtnet2", "vtnet3", "vtnet4", "vtnet5"}, f"capture interfaces with five nodes: {caps6}")
    db = sqlite3.connect(sb6.state / "ledger.sqlite")
    row = db.execute("SELECT SUM(bytes), SUM(packets), SUM(flows) FROM hourly WHERE src_segment='LAN' AND dst_segment='DMZ'").fetchone()
    check(tuple(row) == (7700, 77, 6), f"the ledger disagrees with the counters for LAN to DMZ: {row}")
    db.close()

    # --- one run at a time ----------------------------------------------------------------------
    sb7 = Sandbox(base, "lock")
    sb7.append([rec(T0, "10.10.60.20", "1.1.1.1", 3, 1, 1000, 10)])
    sb7.run(T0 + 100)
    sb7.append([rec(T0 + 95, "10.10.60.20", "1.1.1.1", 3, 1, 500, 5)])

    def fingerprint(box):
        files = [box.state / "ledger.sqlite", box.state / "state.json", box.prom_dir / "segflows.prom"]
        return [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]

    held = open(sb7.state / "segflows.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before_lock = fingerprint(sb7)
    p = sb7.run(T0 + 300)
    check(p.returncode == 0 and "holds the lock" in p.stderr, f"a run while the lock is held must exit 0 and say so: {p.returncode} {p.stderr!r}")
    check(fingerprint(sb7) == before_lock, "a run while the lock is held changed the ledger, the state or the textfile")
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    sb7.run(T0 + 300)
    check(triple(sb7.prom(), "TENANT", "INTERNET") == (1500, 15, 2), "the run after the lock was released did not count the new flow once")
    check(stat.S_IMODE((sb7.state / "segflows.lock").stat().st_mode) in (0o644, 0o600, 0o640, 0o664), "lock file mode unexpected")

    # --- Asia/Kolkata buckets: a flow at 00:10 IST ------------------------------------------------
    sb8 = Sandbox(base, "ist")
    MID = ((T0 + 19800) // 86400) * 86400 - 19800 + 86400  # an IST midnight
    sb8.append([rec(MID - 600, "10.10.60.20", "1.1.1.1", 3, 1, 111, 1), rec(MID + 600, "10.10.60.20", "1.1.1.1", 3, 1, 222, 2)])
    sb8.run(MID + 700)
    db = sqlite3.connect(sb8.state / "ledger.sqlite")
    hours = {h: b for h, b in db.execute("SELECT hour, SUM(bytes) FROM hourly GROUP BY hour")}
    check(hours == {MID - 3600: 111, MID: 222}, f"hour buckets must start on IST hours (00:10 IST is in the 00:00 bucket): {hours}")
    check(all(h % 3600 == 1800 for h in hours), "IST hour buckets start at :30 UTC")
    days = dict(db.execute("SELECT day, SUM(bytes) FROM hosts GROUP BY day").fetchall())
    check(days == {ist_day(MID - 600): 111, ist_day(MID): 222} and ist_day(MID - 600) != ist_day(MID), f"host pairs must book on IST dates: {days}")
    db.close()
    out = sb8.run(MID + 700, "--report", "hour", "--days", "3").stdout
    d0 = ist_day(MID)
    check(f"{d0} 00:00" in out and f"{ist_day(MID - 600)} 23:00" in out, f"--report hour must show 00:00 and 23:00 IST hours:\n{out}")
    out = sb8.run(MID + 700, "--report", "day", "--days", "3").stdout
    check(re.search(rf"{d0}\s+TENANT\s+INTERNET\s+222 B", out) is not None and re.search(rf"{ist_day(MID - 600)}\s+TENANT\s+INTERNET\s+111 B", out) is not None,
          f"--report day must put each flow on its IST date:\n{out}")

    # --- flowd rotates while the logs are being read -----------------------------------------------
    sb9 = Sandbox(base, "rotate")
    old = sb9.logs / "flowd.log.000001"
    sb9.append([rec(T0 + i, "10.10.60.20", "1.1.1.1", 3, 1, 100 * (i + 1), 1, sport=40000 + i) for i in range(4)], old)
    sb9.append([rec(T0 + 10 + i, "10.10.60.20", "1.1.1.1", 3, 1, 1000 * (i + 1), 1, sport=41000 + i) for i in range(4)])
    newf = sb9.dir / "next-flowd.log"
    sb9.append([rec(T0 + 20 + i, "10.10.60.20", "1.1.1.1", 3, 1, 10000 * (i + 1), 1, sport=42000 + i) for i in range(3)], newf)
    (sb9.dir / "rotate.marker").write_text("")
    p = sb9.run(T0 + 100, ROTATE_MARKER=sb9.dir / "rotate.marker", ROTATE_AT="flowd.log.000001", ROTATE_NEW=newf)
    check(p.returncode == 0 and not (sb9.dir / "rotate.marker").exists(), f"the rotation hook did not fire or the run failed: {p.returncode} {p.stderr!r}")
    want = sum(100 * (i + 1) for i in range(4)) + sum(1000 * (i + 1) for i in range(4)) + sum(10000 * (i + 1) for i in range(3))
    got = triple(sb9.prom(), "TENANT", "INTERNET")
    check(got == (want, 11, 11), f"a rotation during the read must lose and double nothing: want {(want, 11, 11)}, got {got}")

    # --- host-pair cleanup only looks at recent days --------------------------------------------------
    sb10 = Sandbox(base, "recent")
    sb10.append([rec(T0, "10.10.60.20", "1.1.1.1", 3, 1, 10, 1)])
    sb10.run(T0 + 100)
    db = sqlite3.connect(sb10.state / "ledger.sqlite")
    db.executemany("INSERT INTO hosts VALUES ('2001-01-01','TENANT','INTERNET',?,?,443,6,?,1,1)",
                   [("10.10.60.20", f"3.3.3.{i}", 1000 + i) for i in range(120)])
    db.commit()
    db.close()
    sb10.append([rec(T0 + 200, "10.10.60.20", "1.1.1.2", 3, 1, 20, 1)])
    sb10.run(T0 + 300)
    db = sqlite3.connect(sb10.state / "ledger.sqlite")
    check(db.execute("SELECT COUNT(*) FROM hosts WHERE day='2001-01-01'").fetchone()[0] == 120, "the host-pair cleanup touched a day it was not asked to look at")
    db.close()

    # --- a run that writes a day older than the stored trim day still trims it -----------------------
    sb11 = Sandbox(base, "olderday")
    sb11.append([rec(T0, "10.10.60.20", "1.1.1.1", 3, 1, 10, 1)])
    sb11.run(T0 + 100)
    OLD = T0 - 3 * 86400
    sb11.append([rec(OLD + i // 50, "10.10.60.20", f"7.7.{i // 250}.{i % 250 + 1}", 3, 1, 1000 + i, 1) for i in range(120)])
    # the ledger position is ahead of these records, as after a catch-up; let the scan see them
    db = sqlite3.connect(sb11.state / "ledger.sqlite")
    db.execute("UPDATE meta SET value='0' WHERE key='last_recv'")
    db.commit()
    db.close()
    st = json.loads((sb11.state / "state.json").read_text())
    st["last_recv"] = OLD - 1
    (sb11.state / "state.json").write_text(json.dumps(st))
    sb11.run(T0 + 200, SEGFLOWS_MAX_RECORDS=1000)
    db = sqlite3.connect(sb11.state / "ledger.sqlite")
    n_old = db.execute("SELECT COUNT(*) FROM hosts WHERE day=?", (ist_day(OLD),)).fetchone()[0]
    check(n_old == mod.TOP_KEEP, f"a day older than the stored trim day must still be trimmed to {mod.TOP_KEEP}, has {n_old}")
    db.close()

    # --- the work cap --------------------------------------------------------------------------
    sb3 = Sandbox(base, "cap")
    recs = [rec(T0 + i // 2, "10.10.60.20", "1.1.1.1", 3, 1, 10 + i, 1, sport=40000 + i) for i in range(20)]
    sb3.append(recs)
    last = 0
    runs = 0
    for runs in range(1, 15):
        p = sb3.run(T0 + 500, SEGFLOWS_MAX_RECORDS=3)
        got = (triple(sb3.prom(), "TENANT", "INTERNET") if pair(sb3.prom(), "opnsense_segflow_bytes_total", "TENANT", "INTERNET") else (0, 0, 0))
        check(got[2] >= last, "a capped run made the flow counter go down")
        if got[2] == last and runs > 1:
            break
        last = got[2]
    check(last == 20, f"capped runs counted {last} of 20 flows")
    check(triple(sb3.prom(), "TENANT", "INTERNET")[0] == sum(10 + i for i in range(20)), "capped runs lost or doubled bytes")
    check(runs > 2, "the cap of 3 records did not split the work across runs")

    # --- host pairs are bounded ----------------------------------------------------------------
    sb4 = Sandbox(base, "top")
    many = [rec(T0 + i // 100, "10.10.60.20", f"8.8.{i // 250}.{i % 250 + 1}", 3, 1, 1000 + i, 1) for i in range(300)]
    sb4.append(many)
    sb4.run(T0 + 600)
    db = sqlite3.connect(sb4.state / "ledger.sqlite")
    n_open = db.execute("SELECT COUNT(*) FROM hosts WHERE src_segment='TENANT' AND dst_segment='INTERNET'").fetchone()[0]
    check(n_open == mod.TOP_WORK, f"an open day keeps {mod.TOP_WORK} host pairs per segment pair, has {n_open}")
    db.close()
    DAY2 = T0 + 86400 + 7200
    sb4.append([rec(DAY2 - 100, "10.10.60.20", "9.9.9.9", 3, 1, 5, 1)])
    sb4.run(DAY2)
    db = sqlite3.connect(sb4.state / "ledger.sqlite")
    day1 = ist_day(T0)
    rows = db.execute("SELECT bytes FROM hosts WHERE day=? AND src_segment='TENANT' AND dst_segment='INTERNET' ORDER BY bytes DESC", (day1,)).fetchall()
    check(len(rows) == mod.TOP_KEEP == 50, f"a finished day keeps {mod.TOP_KEEP} host pairs per segment pair, has {len(rows)}")
    check([r[0] for r in rows] == list(range(1299, 1249, -1)), "the kept host pairs are not the 50 largest")
    check(db.execute("SELECT COUNT(*) FROM hosts WHERE day=?", (ist_day(DAY2 - 100),)).fetchone()[0] == 1, "the new day lost its host pair")
    db.close()

    # --- reports ---------------------------------------------------------------------------------
    NOWR = NOW1 + 400
    out = sb.run(NOWR, "--report", "pairs", "--days", "30")
    check(out.returncode == 0, f"--report pairs exited {out.returncode}: {out.stderr.strip()}")
    check(re.search(r"TENANT\s+INTERNET\s+3\.\d+ KiB\s+16\s+4", out.stdout) is not None, f"--report pairs row for TENANT to INTERNET missing:\n{out.stdout}")
    check("Asia/Kolkata" in out.stdout and "inside one segment" in out.stdout, "--report pairs must say the time zone and the blind spot")
    out = sb.run(NOWR, "--report", "pairs", "--days", "30", "--segment", "DMZ")
    check("DMZ" in out.stdout and "HOME" not in out.stdout and "INTERNET" not in out.stdout, f"--segment DMZ did not filter:\n{out.stdout}")
    out = sb.run(NOWR, "--report", "hosts", "--days", "30", "--segment", "DMZ")
    check("10.10.70.106" in out.stdout and "41641" in out.stdout, f"--report hosts lacks the DMZ pair:\n{out.stdout}")
    out = sb.run(NOWR, "--report", "hour", "--days", "30", "--segment", "LEASE")
    hour_ist = datetime.datetime.fromtimestamp(HOUR, datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:00")
    check(hour_ist in out.stdout, f"--report hour does not show the Asia/Kolkata hour {hour_ist}:\n{out.stdout}")
    out = sb.run(NOWR, "--report", "day", "--days", "30")
    check(ist_day(T0) in out.stdout, f"--report day lacks {ist_day(T0)}:\n{out.stdout}")
    out = sb.run(NOWR + 86400 * 40, "--report", "pairs", "--days", "7")
    check("TENANT" not in out.stdout, "--days 7 must not show rows from 40 days earlier")
    ledger_before = hashlib.sha256((sb.state / "ledger.sqlite").read_bytes()).hexdigest()
    sb.run(NOWR, "--report", "pairs")
    check(hashlib.sha256((sb.state / "ledger.sqlite").read_bytes()).hexdigest() == ledger_before, "a report changed the ledger")
    empty = Sandbox(base, "noledger")
    p = empty.run(NOWR, "--report", "pairs")
    check(p.returncode == 1 and "no ledger" in p.stderr, "a report with no ledger must exit 1 and say so")
    check(subprocess.run([sys.executable, "-I", str(SCRIPT), "--report", "bogus"], capture_output=True).returncode == 2, "an unknown report must exit 2")

    # --- install.sh --------------------------------------------------------------------------------
    def fake_root(name, with_opnsense=True):
        root = base / name
        for d in ("usr/local/sbin", "usr/local/opnsense/service/conf/actions.d", "usr/local/opnsense/mvc", "var/tmp/node_exporter"):
            if with_opnsense or "opnsense" not in d:
                (root / d).mkdir(parents=True)
        return root

    ins = Sandbox(base, "install")
    ins.append([rec(T0, "10.10.60.20", "1.1.1.1", 3, 1, 1000, 10)])
    calls = ins.fix / "calls.log"

    def inst_env(root):
        return ins.env(T0 + 100, SEGFLOWS_ROOT=root, SEGFLOWS_BACKFILL=3600)

    root = fake_root("root-dry")
    before = snapshot(root)
    p = subprocess.run(["sh", str(INSTALL_SH)], capture_output=True, text=True, env=inst_env(root))
    check(p.returncode == 0, f"install.sh dry run exited {p.returncode}: {p.stderr.strip()}")
    check(snapshot(root) == before, "install.sh changed the fake root in a dry run")
    check(not (calls.exists() and calls.read_text().strip()), "install.sh ran commands in a dry run")
    for needle in ("would run:", "--apply", "nothing was changed"):
        check(needle in p.stdout, f"the install.sh dry run output does not say {needle!r}")
    p = subprocess.run(["sh", str(INSTALL_SH), "--uninstall"], capture_output=True, text=True, env=inst_env(root))
    check(p.returncode == 0 and snapshot(root) == before and "--uninstall --apply" in p.stdout, "install.sh --uninstall must be a dry run by default")
    check(not (calls.exists() and calls.read_text().strip()), "install.sh --uninstall ran commands in a dry run")

    none = fake_root("root-none", with_opnsense=False)
    nb = snapshot(none)
    for args in ([], ["--apply"]):
        p = subprocess.run(["sh", str(INSTALL_SH)] + args, capture_output=True, text=True, env=inst_env(none))
        check(p.returncode != 0 and "not an OPNsense firewall" in p.stderr, f"install.sh {' '.join(args)} must refuse a root with no /usr/local/opnsense")
    check(snapshot(none) == nb, "install.sh changed a root that is not an OPNsense firewall")
    p = subprocess.run(["sh", str(INSTALL_SH), "--bogus"], capture_output=True, text=True, env=inst_env(root))
    check(p.returncode == 2, f"install.sh --bogus must exit 2, got {p.returncode}")

    # apply on a COPY whose script has a shebang that exists here
    def copy_src(name):
        d = base / name
        shutil.copytree(SRC, d)
        text = (d / "segflows.py").read_text().split("\n", 1)[1]
        (d / "segflows.py").write_text(f"#!{sys.executable}\n" + text)
        (d / "segflows.py").chmod(0o755)
        return d

    src = copy_src("src")
    root = fake_root("root-apply")
    calls.unlink(missing_ok=True)
    p = subprocess.run(["sh", str(src / "install.sh"), "--apply"], capture_output=True, text=True, env=inst_env(root))
    check(p.returncode == 0, f"install.sh --apply exited {p.returncode}: {p.stdout.strip()} {p.stderr.strip()}")
    DEST = {"segflows.py": ("usr/local/sbin/segflows.py", 0o755),
            "actions_segflows.conf": ("usr/local/opnsense/service/conf/actions.d/actions_segflows.conf", 0o644)}
    for name, (dest, mode) in DEST.items():
        path = root / dest
        if not path.is_file():
            fail(f"install.sh --apply did not install {dest}")
            continue
        check(stat.S_IMODE(path.stat().st_mode) == mode, f"{dest} has mode {oct(stat.S_IMODE(path.stat().st_mode))}, expected {oct(mode)}")
        check(path.read_bytes() == (src / name).read_bytes(), f"{dest} differs from {name}")
    check((root / "var/db/segflows").is_dir() and stat.S_IMODE((root / "var/db/segflows").stat().st_mode) == 0o750,
          "install.sh --apply must create /var/db/segflows with mode 0750")
    check((root / "var/db/segflows/ledger.sqlite").exists(), "the verification run did not write the ledger")
    check((root / "var/tmp/node_exporter/segflows.prom").exists(), "the verification run did not write segflows.prom")
    expected_calls = ["service configd restart",
                      f"php {src}/add-segflows-cron.php apply (cwd {root}/usr/local/opnsense/mvc)",
                      "configctl template reload OPNsense/Cron", "configctl cron restart"]
    got_calls = calls.read_text().strip().splitlines() if calls.exists() else []
    check(got_calls == expected_calls, f"install.sh --apply ran {got_calls}, expected {expected_calls}")
    check("ok: " in p.stdout and "capture metrics" in p.stdout, "install.sh --apply did not report the metric check passing")
    check(p.stdout.index("segflows.py\n") < p.stdout.index("add-segflows-cron.php apply"),
          "install.sh --apply must run the script once BEFORE it adds the cron job")

    # the installer refuses to call it good when a metric is missing
    for metric in ("opnsense_segflow_last_run_timestamp_seconds", "opnsense_segflow_lag_seconds",
                   "opnsense_segflow_run_duration_seconds", "opnsense_segflow_capture_interfaces"):
        bad = copy_src(f"src-no-{metric}")
        text = (bad / "segflows.py").read_text()
        text2 = text.replace(f'"{metric}', f'"x_{metric}').replace(f"{metric} ", f"x_{metric} ").replace(f"{metric}{{", f"x_{metric}{{")
        if text2 == text:
            fail(f"could not remove {metric} from the script to test the installer's check")
            continue
        (bad / "segflows.py").write_text(text2)
        p = subprocess.run(["sh", str(bad / "install.sh"), "--apply"], capture_output=True, text=True,
                           env=inst_env(fake_root(f"root-no-{metric}")))
        check(p.returncode != 0 and metric in p.stderr, f"install.sh --apply accepted a prom file with no {metric} (exit {p.returncode}): {p.stderr.strip()!r}")

    # uninstall --apply on the applied root: files, cron and prom go, the ledger stays
    calls.unlink(missing_ok=True)
    p = subprocess.run(["sh", str(src / "install.sh"), "--uninstall", "--apply"], capture_output=True, text=True, env=inst_env(root))
    check(p.returncode == 0, f"install.sh --uninstall --apply exited {p.returncode}: {p.stderr.strip()}")
    for name, (dest, mode) in DEST.items():
        check(not (root / dest).exists(), f"--uninstall --apply left {dest}")
    check(not (root / "var/tmp/node_exporter/segflows.prom").exists(), "--uninstall --apply left segflows.prom")
    check((root / "var/db/segflows/ledger.sqlite").exists(), "--uninstall --apply must keep the ledger")
    got_calls = calls.read_text().strip().splitlines() if calls.exists() else []
    check(got_calls[:1] == [f"php {src}/add-segflows-cron.php remove (cwd {root}/usr/local/opnsense/mvc)"],
          f"--uninstall --apply must remove the cron job first: {got_calls}")

# --- the pieces agree ----------------------------------------------------------------------------
actions = ACTIONS.read_text()
section = re.search(r"^\[(\w+)\]", actions, re.M)
check(bool(section) and ACTIONS.name == "actions_segflows.conf", "actions_segflows.conf must be named actions_<name>.conf with a [section]")
if section:
    check(f"'command' => 'segflows {section.group(1)}'" in CRON_PHP.read_text(), "the cron job does not run the configd action 'segflows run'")
check("command:/usr/local/sbin/segflows.py" in actions and "type:script" in actions, "the configd action must be a script running /usr/local/sbin/segflows.py")
check("'minutes' => '*/5'" in CRON_PHP.read_text(), "the cron job must run every 5 minutes")
for mode in ("dry", "apply", "remove"):
    check(mode in CRON_PHP.read_text(), f"add-segflows-cron.php lacks the {mode} mode")

# --- metrics: written, documented, queried ----------------------------------------------------
script_text = SCRIPT.read_text()
written = set(re.findall(r"opnsense_segflow_[a-z_]+", script_text))
written -= {"opnsense_segflow_"}
metrics_md = METRICS_MD.read_text()
for name in sorted(written):
    check(re.search(rf"\| `{name}` \|", metrics_md) is not None, f"{name} is written by segflows.py but has no row in metrics.md")
doc_names = set(re.findall(r"\| `(opnsense_segflow_[a-z_]+)` \|", metrics_md))
check(doc_names == written, f"metrics.md lists {sorted(doc_names ^ written)} that the script does not write (or vice versa)")
dash = json.loads(DASH.read_text())
queried = set()
for pnl in dash["panels"]:
    for t in pnl.get("targets") or []:
        queried |= set(re.findall(r"opnsense_segflow_[a-z_]+", t.get("expr", "")))
for v in dash["templating"]["list"]:
    queried |= set(re.findall(r"opnsense_segflow_[a-z_]+", v.get("definition", "")))
check(bool(queried), "the Inter-LAN dashboard queries no opnsense_segflow metric")
check(queried <= written, f"the dashboard queries {sorted(queried - written)}, which segflows.py does not write")
check({"opnsense_segflow_bytes_total", "opnsense_segflow_lag_seconds", "opnsense_segflow_capture_interfaces",
       "opnsense_segflow_last_run_timestamp_seconds"} <= queried, "the dashboard does not read the pair, lag, capture and last-run metrics")
check(dash["uid"] == "inter-lan" and dash["title"] == "Inter-LAN" and {"network", "lan", "opnsense"} <= set(dash["tags"]),
      "the dashboard uid, title or tags are wrong")
for seg in ("LAN", "TENANT", "DMZ", "LAB", "LEASE", "HOME", "TAILNET", "FIREWALL", "INTERNET"):
    check(f"`{seg}`" in metrics_md, f"metrics.md does not name the segment {seg}")

# --- docs -------------------------------------------------------------------------------------
routers = ROUTERS_MD.read_text()
sect = routers.split("### Inter-segment traffic")[-1].split("\n## ")[0] if "### Inter-segment traffic" in routers else ""
sect_flat = " ".join(sect.split())
check(bool(sect), "routers.md has no '### Inter-segment traffic' section")
for needle in ("security/opnsense/segflows/", "sh install.sh", "sh install.sh --apply", "sh install.sh --uninstall --apply", "csh",
               "/var/db/segflows/ledger.sqlite", "/var/tmp/node_exporter/segflows.prom", "--report pairs", "--report hosts",
               "--report hour", "--report day", "--segment", "--days", "Asia/Kolkata", "ansible/tests/segflows-test.py",
               "inside one segment", "netflow-capture.php", "--revert", "--interfaces", "opt1,opt4", "lan,opt2,opt3",
               "127.0.0.1:2056", "netflow aggregate restart", "Coverage row"):
    check(needle in sect_flat, f"the routers.md segflows section does not mention {needle}")
check("## Inter-LAN" in DASHBOARDS_MD.read_text() and "uid: inter-lan" in DASHBOARDS_MD.read_text(), "dashboards.md has no Inter-LAN section")
check("opnsense/segflows/" in SEC_README.read_text(), "security/README.md does not list opnsense/segflows/")
check("Inter-LAN" in MON_README.read_text(), "monitoring/README.md does not list the Inter-LAN dashboard")
check("netflow-capture.php --apply" in sect_flat and sect_flat.index("netflow-capture.php") < sect_flat.index("sh install.sh"),
      "the capture step must come before the segflows install step in routers.md")
check("LAN and DMZ" not in " ".join(DASHBOARDS_MD.read_text().split()) and "TENANT and LEASE today" not in " ".join(DASHBOARDS_MD.read_text().split()),
      "dashboards.md still claims specific segments are or are not captured")
check("Coverage row" in DASHBOARDS_MD.read_text(), "dashboards.md does not point at the Coverage row for what is captured")
wiring = " ".join((ROOT / "security" / "docs" / "wiring" / "index.md").read_text().split())
check("netflow-capture.php" in wiring and "--revert" in wiring and "opt1,opt4" in wiring and wiring.index("netflow-capture.php") < wiring.index("then install it"),
      "the wiring doc's segflows section must name netflow-capture.php, --revert and opt1,opt4 before the install step")
for text in (DASH.read_text(),):
    check("LAN and DMZ" not in text and "are invisible" not in text.replace("uncaptured segments is invisible", ""),
          "the dashboard still claims LAN and DMZ are invisible")

# --- netflow-capture.php (php is not available here, so these are static checks of the source) ---
php = CAPTURE_PHP.read_text()
check("--apply" in php and "--revert" in php and "--interfaces" in php, "netflow-capture.php must accept --apply, --revert and --interfaces")
check("const ORIGINAL = ['opt1', 'opt4'];" in php, "netflow-capture.php --revert must restore opt1,opt4")
check("const DEFAULT_ADD = ['lan', 'opt2', 'opt3'];" in php, "netflow-capture.php must add lan,opt2,opt3 by default")
check("array_merge($current, $add)" in php, "netflow-capture.php must keep every interface already in the list and only add")
check(not re.search(r"capture->(targets|version|egress_only)\s*=[^=]", php), "netflow-capture.php must not change the target, version or egress_only")
check("'dry run OK, nothing saved" in php.replace('"', "'") and php.index("dry run OK") < php.index("serializeToConfig"),
      "netflow-capture.php must exit as a dry run before it saves")
order = [php.index(x) for x in ("$configctl netflow stop", "$configctl template reload OPNsense/Netflow", "$configctl netflow start",
                                          "$configctl netflow aggregate restart")]
check(order == sorted(order), "netflow-capture.php must run netflow stop, template reload, netflow start, aggregate restart in the GUI's order")
check(php.count("performValidation") == 1 and php.index("performValidation") < php.index("serializeToConfig"), "netflow-capture.php must validate before it saves")
check("\\OPNsense\\Diagnostics\\Netflow" in php, "netflow-capture.php must use the Diagnostics Netflow model")

# every flag the docs show is in the argument parser
for flag in re.findall(r"segflows\.py (?:--report \w+ )?((?:--[a-z]+ ?\w*)*)", sect):
    for f in re.findall(r"--[a-z]+", flag):
        check(f'"{f}"' in script_text, f"the docs show {f} but segflows.py does not accept it")

EM_DASH = chr(0x2014)
for path in sorted(SRC.iterdir()) + [pathlib.Path(__file__), DASH]:
    if path.is_file():
        check(EM_DASH not in path.read_text(), f"{path.name} contains an em dash")

if errors:
    print("\n".join(f"FAIL: {e}" for e in errors))
    sys.exit(1)
print("ok: segflows classifies, dedupes, counts monotonically, books the ledger, reports, installs and matches its docs and dashboard")
