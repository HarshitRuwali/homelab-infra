#!/usr/local/bin/python3
"""Inter-segment traffic accounting for the OPNsense firewall.

Reads the NetFlow records flowd already collects (/var/log/flowd.log and its
rotations), classifies each side of every flow into a network segment by
subnet, and keeps two things:

  * monotonic counters, written for node_exporter's textfile collector to
    /var/tmp/node_exporter/segflows.prom (Prometheus keeps them 7 days);
  * a permanent SQLite ledger in /var/db/segflows/ledger.sqlite: hourly bytes,
    packets and flows per (src_segment, dst_segment), and per day the top 50
    host pairs of each segment pair.

Run by cron every 5 minutes as root (configd action "segflows run"). Uses only
the python3 that ships with OPNsense and OPNsense's own flow parser
(/usr/local/opnsense/scripts/netflow/lib).

    segflows.py                                   one accounting run
    segflows.py --report pairs [--days N] [--segment X]
    segflows.py --report hosts [--days N] [--segment X]
    segflows.py --report hour  [--days N] [--segment X]
    segflows.py --report day   [--days N] [--segment X]

Reports read the ledger only, print local time (Asia/Kolkata) and change nothing.

What it cannot see: only traffic that crosses a NetFlow-captured firewall
interface is a flow. Traffic inside one segment (two hosts on the same switch
or VLAN) never reaches the firewall, so it is invisible here. The captured
interfaces are listed in the metric opnsense_segflow_capture_interfaces.

Environment overrides (tests; leave unset on the firewall):
  SEGFLOWS_DIR           state and ledger directory   (default /var/db/segflows)
  SEGFLOWS_PROM_DIR      textfile directory           (default /var/tmp/node_exporter)
  SEGFLOWS_NETFLOW_DIR   OPNsense netflow scripts dir (default /usr/local/opnsense/scripts/netflow)
  SEGFLOWS_FLOWD_LOG     flowd log base name          (default /var/log/flowd.log)
  SEGFLOWS_NOW           fake "now" as epoch seconds
  SEGFLOWS_BACKFILL      seconds to look back on the very first run (default 86400)
  SEGFLOWS_MAX_RECORDS   work cap per run, in records (default 500000)
"""
import argparse
import copy
import datetime
import fcntl
import glob
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

# cron and configd give a minimal PATH; ngctl and ifconfig live in /usr/sbin and /sbin
os.environ["PATH"] = os.environ.get("PATH", "") + ":/sbin:/usr/sbin:/bin:/usr/bin:/usr/local/bin:/usr/local/sbin"

STATE_DIR = os.environ.get("SEGFLOWS_DIR", "/var/db/segflows")
PROM_DIR = os.environ.get("SEGFLOWS_PROM_DIR", "/var/tmp/node_exporter")
NETFLOW_DIR = os.environ.get("SEGFLOWS_NETFLOW_DIR", "/usr/local/opnsense/scripts/netflow")
FLOWD_LOG = os.environ.get("SEGFLOWS_FLOWD_LOG", "/var/log/flowd.log")
BACKFILL = int(os.environ.get("SEGFLOWS_BACKFILL", "86400"))
MAX_RECORDS = int(os.environ.get("SEGFLOWS_MAX_RECORDS", "500000"))

STATE_FILE = "state.json"
LEDGER_FILE = "ledger.sqlite"
LOCK_FILE = "segflows.lock"
PROM_FILE = "segflows.prom"

# Records newer than now minus this are left for the next run, so a second that
# flowd is still writing is never half counted.
GUARD_SECONDS = 10
# How often the walk over the flowd logs is repeated when they rotate while it runs.
ROTATION_RETRIES = 3
# A routed flow between two captured interfaces is exported twice, once by the
# netflow node of each interface (ingress on the source side, egress on the
# destination side), with the same 5-tuple and the same in and out interfaces.
# The second record is matched to the first when it arrives within TWIN_WINDOW
# seconds and its flow end is within TWIN_TOLERANCE seconds (the two nodes
# disagree by under a second: the end time is derived from the whole-second
# receive time). The pair counts once; if the second record carries more bytes
# or packets (pf dropped some on the way), the surplus is added, so a pair
# counts the larger of the two.
TWIN_WINDOW = 60
TWIN_TOLERANCE = 2.5
# Host pairs kept per (day, src_segment, dst_segment): TOP_WORK while the day
# is open (so a host that grows late is not evicted early), TOP_KEEP after.
TOP_KEEP = 50
TOP_WORK = 200

# Subnets, first match wins. The six homelab segments are the firewall's own
# interfaces (confirmed in /conf/config.xml, interfaces section).
SEGMENT_NETS = [
    ("LAN", "10.10.50.0/24"),
    ("TENANT", "10.10.60.0/24"),
    ("DMZ", "10.10.70.0/24"),
    ("LAB", "10.10.80.0/24"),
    ("LEASE", "10.10.90.0/24"),
    ("HOME", "10.10.0.0/24"),
    ("HOME", "10.10.1.0/24"),
    ("HOME", "10.10.2.0/24"),
    ("HOME", "192.168.0.0/16"),
    ("TAILNET", "100.64.0.0/10"),
    ("LINKLOCAL", "169.254.0.0/16"),
    ("LINKLOCAL", "224.0.0.0/4"),
    ("LINKLOCAL", "255.255.255.255/32"),
    ("LINKLOCAL", "fe80::/10"),
    ("LINKLOCAL", "ff00::/8"),
    ("PRIVATE", "10.0.0.0/8"),
    ("PRIVATE", "172.16.0.0/12"),
    ("PRIVATE", "fc00::/7"),
]
NETS = [(name, ipaddress.ip_network(net)) for name, net in SEGMENT_NETS]
# Firewall interface name to the OPNsense interface label (the `interface` label of opnsense_interfaces_*).
# vtnet0 is the WAN link to the home LAN; flows seen there are classified by address, not by this map.
INTERFACE_SEGMENT = {
    "vtnet0": "WAN", "vtnet1": "LAN", "vtnet2": "TENANT",
    "vtnet3": "DMZ", "vtnet4": "LAB", "vtnet5": "LEASE",
}

IST_OFFSET = 19800  # Asia/Kolkata, UTC+5:30, no daylight saving
IST = datetime.timezone(datetime.timedelta(seconds=IST_OFFSET), "IST")


# --- classification ---------------------------------------------------------------------------
def firewall_addresses():
    """Every address configured on the firewall itself (ifconfig inet and inet6 lines)."""
    addrs = set()
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return addrs
    for m in re.finditer(r"^\s+inet6? ([0-9a-fA-F:.]+)", out, re.M):
        addrs.add(m.group(1))
    return addrs


class Classifier:
    def __init__(self, own_addrs):
        self.own = set(own_addrs)
        self._cache = {}

    def segment(self, addr):
        seg = self._cache.get(addr)
        if seg is not None:
            return seg
        seg = self._classify(addr)
        if len(self._cache) < 100000:
            self._cache[addr] = seg
        return seg

    def _classify(self, addr):
        if addr in self.own:
            return "FIREWALL"
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            return "INTERNET"
        if ip.compressed in self.own:
            return "FIREWALL"
        for name, net in NETS:
            if ip.version == net.version and ip in net:
                return name
        return "INTERNET"


def service_port(rec):
    """The lower of the two ports (the server side), for TCP and UDP; 0 otherwise."""
    if rec.get("protocol") not in (6, 17):
        return 0
    return min(rec.get("src_port", 0), rec.get("dst_port", 0))


# --- state and ledger -------------------------------------------------------------------------
def load_state(path):
    empty = {"version": 1, "last_recv": 0, "newest_recv": 0, "counters": {}, "pending": {}}
    try:
        with open(path) as fh:
            state = json.load(fh)
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("unexpected state layout")
        for key, val in empty.items():
            state.setdefault(key, val)
        return state
    except FileNotFoundError:
        return empty
    except (ValueError, OSError):
        # keep the bad file for inspection and start over; Prometheus treats the
        # counter drop as a reset
        try:
            os.replace(path, path + ".bad")
        except OSError:
            pass
        return empty


def atomic_write(path, text, mode):
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def open_ledger(path):
    db = sqlite3.connect(path, timeout=30)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS hourly (
            hour INTEGER NOT NULL, src_segment TEXT NOT NULL, dst_segment TEXT NOT NULL,
            bytes INTEGER NOT NULL, packets INTEGER NOT NULL, flows INTEGER NOT NULL,
            PRIMARY KEY (hour, src_segment, dst_segment));
        CREATE TABLE IF NOT EXISTS hosts (
            day TEXT NOT NULL, src_segment TEXT NOT NULL, dst_segment TEXT NOT NULL,
            src_ip TEXT NOT NULL, dst_ip TEXT NOT NULL, port INTEGER NOT NULL, proto INTEGER NOT NULL,
            bytes INTEGER NOT NULL, packets INTEGER NOT NULL, flows INTEGER NOT NULL,
            PRIMARY KEY (day, src_segment, dst_segment, src_ip, dst_ip, port, proto));
        """
    )
    return db


def ledger_last_recv(db):
    row = db.execute("SELECT value FROM meta WHERE key='last_recv'").fetchone()
    return int(row[0]) if row else 0


def hour_start(epoch):
    """Start of the IST hour (the zone is UTC+5:30, so the boundary is at :30 UTC) holding epoch."""
    return (epoch + IST_OFFSET) // 3600 * 3600 - IST_OFFSET


def ist_day(epoch):
    return datetime.datetime.fromtimestamp(epoch, IST).strftime("%Y-%m-%d")


def trim_hosts(db, today, days):
    """Keep the largest TOP_WORK host pairs per segment pair for today and TOP_KEEP for earlier days.

    Only the days in `days` are looked at, so the cost does not grow with the ledger.
    """
    days = sorted(set(days))
    marks = ",".join("?" * len(days))
    db.execute(
        f"""
        DELETE FROM hosts WHERE rowid IN (
            SELECT rowid FROM (
                SELECT rowid, day,
                       ROW_NUMBER() OVER (PARTITION BY day, src_segment, dst_segment
                                          ORDER BY bytes DESC, rowid) AS rn
                FROM hosts WHERE day IN ({marks}))
            WHERE rn > CASE WHEN day = ? THEN ? ELSE ? END)
        """,
        (*days, today, TOP_WORK, TOP_KEEP),
    )


# --- one run ----------------------------------------------------------------------------------
def capture_interfaces():
    """Interfaces with a netgraph netflow node (what OPNsense's Reporting > NetFlow captures)."""
    try:
        out = subprocess.run(["ngctl", "list"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted(set(re.findall(r"Name:\s+netflow_(\S+)\s+Type:\s+netflow", out)))


def log_files(base):
    """flowd.log and its rotations, oldest first (flowd.log.000010 ... flowd.log.000001, flowd.log)."""
    def age_key(name):
        suffix = name[len(base):].lstrip(".")
        return -int(suffix) if suffix.isdigit() else 0
    return sorted((f for f in glob.glob(base + "*") if re.fullmatch(r"\.?\d*", f[len(base):])), key=age_key)


def import_netflow():
    if NETFLOW_DIR not in sys.path:
        sys.path.insert(0, NETFLOW_DIR)
    from lib.flowparser import FlowParser  # noqa: E402
    from lib.parse import Interfaces  # noqa: E402
    return FlowParser, Interfaces


def esc(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prom(state, now, duration, lag, captured):
    lines = []
    for name, idx, help_ in (
        ("opnsense_segflow_bytes_total", 0, "Bytes of NetFlow traffic between two segments, counted once per flow."),
        ("opnsense_segflow_packets_total", 1, "Packets of NetFlow traffic between two segments."),
        ("opnsense_segflow_flows_total", 2, "NetFlow flow records between two segments."),
    ):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} counter")
        for key in sorted(state["counters"]):
            src, dst = key.split("|")
            lines.append(f'{name}{{src_segment="{esc(src)}",dst_segment="{esc(dst)}"}} {state["counters"][key][idx]}')
    lines += [
        "# HELP opnsense_segflow_last_run_timestamp_seconds Unix time the last accounting run finished.",
        "# TYPE opnsense_segflow_last_run_timestamp_seconds gauge",
        f"opnsense_segflow_last_run_timestamp_seconds {int(now)}",
        "# HELP opnsense_segflow_run_duration_seconds How long the last accounting run took.",
        "# TYPE opnsense_segflow_run_duration_seconds gauge",
        f"opnsense_segflow_run_duration_seconds {duration:.3f}",
        "# HELP opnsense_segflow_lag_seconds Now minus the receive time of the newest flow processed.",
        "# TYPE opnsense_segflow_lag_seconds gauge",
        f"opnsense_segflow_lag_seconds {lag}",
        "# HELP opnsense_segflow_capture_interfaces 1 for each interface that has a NetFlow capture node.",
        "# TYPE opnsense_segflow_capture_interfaces gauge",
    ]
    for ifname in captured:
        seg = INTERFACE_SEGMENT.get(ifname, "UNKNOWN")
        lines.append(f'opnsense_segflow_capture_interfaces{{interface="{esc(ifname)}",segment="{seg}"}} 1')
    return "\n".join(lines) + "\n"


def snapshot_logs():
    """(name, inode, mtime) of flowd.log and its rotations, oldest first."""
    out = []
    for name in log_files(FLOWD_LOG):
        try:
            st = os.stat(name)
        except OSError:
            continue
        out.append((name, st.st_ino, st.st_mtime))
    return out


def scan_logs(last_recv, cutoff, FlowParser, interfaces, classifier, cap_set, state):
    """Read every flow newer than last_recv into aggregates, or None if the logs rotated underneath.

    flowd_aggregate rotates flowd.log (rename, shift, new file) while this runs. A file listed
    before the rename can then be read under another file's name, or not at all. The walk is
    therefore validated: the list of (name, inode) taken before reading must equal the list taken
    after, otherwise the aggregates are thrown away and the walk starts again.
    """
    for _attempt in range(ROTATION_RETRIES):
        files = snapshot_logs()
        result = _scan_once(files, last_recv, cutoff, FlowParser, interfaces, classifier, cap_set, state)
        if [(f[0], f[1]) for f in snapshot_logs()] == [(f[0], f[1]) for f in files]:
            return result
    return None


def _scan_once(files, last_recv, cutoff, FlowParser, interfaces, classifier, cap_set, state):
    counters = copy.deepcopy(state["counters"])
    pending = {k: [list(e) for e in v if isinstance(e, list)] for k, v in state["pending"].items()}
    hourly = {}
    hosts = {}
    done_to = cutoff
    newest = state["newest_recv"]
    n = 0
    stop = False
    last_sec = None
    for filename, _ino, mtime in files:
        if stop:
            break
        try:
            if mtime < last_recv:
                continue
            for rec in FlowParser(filename, last_recv):
                sec = rec["recv_sec"]
                if sec <= last_recv:
                    continue
                if sec > cutoff:
                    break
                if n >= MAX_RECORDS and sec != last_sec:
                    done_to = last_sec
                    stop = True
                    break
                last_sec = sec
                if_in = interfaces.if_device(rec["if_ndx_in"])
                if_out = interfaces.if_device(rec["if_ndx_out"])
                src, dst = rec["src_addr"], rec["dst_addr"]
                nbytes, npackets = rec["octets"], rec["packets"]
                # Records are exported per netflow node, and a node cannot be told
                # from the record (the v9 header carries no per-node field; every
                # record arrives from 127.0.0.1). A flow between two captured
                # interfaces therefore arrives as two records with identical
                # in and out interfaces: match them as twins. A flow that crosses
                # one captured interface is a single record.
                flow_inc = 1
                if (if_in in cap_set and if_out in cap_set and if_in != if_out) or not cap_set:
                    key = f"{src}|{dst}|{rec['src_port']}|{rec['dst_port']}|{rec['protocol']}|{if_in}|{if_out}"
                    fend = rec.get("flow_end", sec)
                    entries = pending.get(key, [])
                    twin = next((e for e in entries if sec - e[0] <= TWIN_WINDOW and abs(e[3] - fend) <= TWIN_TOLERANCE), None)
                    if twin is not None:
                        entries.remove(twin)
                        if not entries:
                            del pending[key]
                        flow_inc = 0
                        nbytes, npackets = max(0, nbytes - twin[1]), max(0, npackets - twin[2])
                        if not nbytes and not npackets:
                            n += 1
                            newest = max(newest, sec)
                            continue
                    else:
                        pending.setdefault(key, []).append([sec, nbytes, npackets, fend])
                n += 1
                newest = max(newest, sec)
                s_seg, d_seg = classifier.segment(src), classifier.segment(dst)
                ckey = f"{s_seg}|{d_seg}"
                c = counters.setdefault(ckey, [0, 0, 0])
                c[0] += nbytes
                c[1] += npackets
                c[2] += flow_inc
                hk = (hour_start(sec), s_seg, d_seg)
                h = hourly.setdefault(hk, [0, 0, 0])
                h[0] += nbytes
                h[1] += npackets
                h[2] += flow_inc
                xk = (ist_day(sec), s_seg, d_seg, src, dst, service_port(rec), rec["protocol"])
                x = hosts.setdefault(xk, [0, 0, 0])
                x[0] += nbytes
                x[1] += npackets
                x[2] += flow_inc
        except OSError as exc:
            # a log rotated away between the listing and the open: the next run picks up
            print(f"segflows: skipped {filename}: {exc}", file=sys.stderr)

    return counters, pending, hourly, hosts, n, newest, done_to, stop


def run_once():
    """One accounting run, serialised by a lock on STATE_DIR/segflows.lock.

    The ledger is add-only, so two runs reading the same flows from the same state would both
    add them. The lock is taken without waiting and held until the run, including the ledger
    commit and the state write, is over; a run that finds it held does nothing.
    """
    os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, LOCK_FILE), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        message = "segflows: another run holds the lock; this run does nothing"
        print(message, file=sys.stderr)
        try:
            import syslog
            syslog.openlog("segflows")
            syslog.syslog(syslog.LOG_NOTICE, message)
        except ImportError:
            pass
        return 0
    try:
        return _run_locked()
    finally:
        lock.close()  # releases the lock


def _run_locked():
    started = time.time()
    now = int(os.environ.get("SEGFLOWS_NOW", started))
    os.makedirs(PROM_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, STATE_FILE)
    state = load_state(state_path)
    db = open_ledger(os.path.join(STATE_DIR, LEDGER_FILE))
    try:
        os.chmod(os.path.join(STATE_DIR, LEDGER_FILE), 0o640)
    except OSError:
        pass

    # the ledger commit and the state write are two files; if a crash fell between
    # them, the ledger is ahead and its position wins (the counters miss one run)
    last_recv = max(state["last_recv"], ledger_last_recv(db))
    if last_recv == 0:
        last_recv = now - BACKFILL
    cutoff = now - GUARD_SECONDS

    captured = capture_interfaces()
    cap_set = set(captured or [])
    classifier = Classifier(firewall_addresses())
    FlowParser, Interfaces = import_netflow()
    interfaces = Interfaces()

    scanned = scan_logs(last_recv, cutoff, FlowParser, interfaces, classifier, cap_set, state)
    if scanned is None:
        print("segflows: the flowd logs rotated during every attempt to read them; nothing was counted, "
              "the next run starts from the same position", file=sys.stderr)
        db.close()
        return 1
    counters, pending, hourly, hosts, n, newest, done_to, stop = scanned
    if done_to is None:  # capped before the first record: nothing was processed
        done_to = last_recv

    with db:
        for (hour, s, d), (b, p, f) in hourly.items():
            db.execute(
                "INSERT INTO hourly VALUES (?,?,?,?,?,?) ON CONFLICT(hour, src_segment, dst_segment) DO UPDATE SET "
                "bytes = bytes + excluded.bytes, packets = packets + excluded.packets, flows = flows + excluded.flows",
                (hour, s, d, b, p, f),
            )
        for (day, s, d, sip, dip, port, proto), (b, p, f) in hosts.items():
            db.execute(
                "INSERT INTO hosts VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(day, src_segment, dst_segment, src_ip, "
                "dst_ip, port, proto) DO UPDATE SET bytes = bytes + excluded.bytes, packets = packets + excluded.packets, "
                "flows = flows + excluded.flows",
                (day, s, d, sip, dip, port, proto, b, p, f),
            )
        # Only the days written since the last run's own day need trimming: a day that ended
        # was trimmed to TOP_KEEP on the first run after it, so the scan is one range of the
        # primary key and does not grow with the ledger.
        today = ist_day(now)
        row = db.execute("SELECT value FROM meta WHERE key='hosts_trim_day'").fetchone()
        recent = {r[0] for r in db.execute("SELECT DISTINCT day FROM hosts WHERE day >= ?", (row[0] if row else "",))}
        # a capped run can write a day older than the stored position: trim every day it wrote too
        trim_hosts(db, today, recent | {today} | {k[0] for k in hosts})
        db.execute("INSERT INTO meta VALUES ('hosts_trim_day', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (today,))
        db.execute("INSERT INTO meta VALUES ('last_recv', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (str(done_to),))
    db.close()

    # keep twin candidates only while a twin could still arrive: measured from the position
    # reached, not from the wall clock, because a run is 5 minutes after the last
    pending = {k: [e for e in v if e[0] > done_to - TWIN_WINDOW] for k, v in pending.items()}
    pending = {k: v for k, v in pending.items() if v}
    state.update(last_recv=done_to, newest_recv=newest, counters=counters, pending=pending)
    atomic_write(state_path, json.dumps(state, sort_keys=True), 0o640)

    lag = max(0, now - newest) if newest else 0
    prom = render_prom(state, time.time(), time.time() - started, lag, captured or [])
    atomic_write(os.path.join(PROM_DIR, PROM_FILE), prom, 0o644)
    try:
        import syslog
        syslog.openlog("segflows")
        syslog.syslog(syslog.LOG_NOTICE, f"records={n} flows_counted={sum(v[2] for v in hourly.values())} lag={lag}s "
                      f"capped={'yes' if stop else 'no'} captured={','.join(captured or []) or 'unknown'}")
    except ImportError:
        pass
    return 0


# --- reports ----------------------------------------------------------------------------------
def human(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def table(headers, rows, right=()):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    def fmt(row):
        return "  ".join(str(v).rjust(w) if i in right else str(v).ljust(w) for i, (v, w) in enumerate(zip(row, widths)))
    out = [fmt(headers), fmt(["-" * w for w in widths])]
    out += [fmt(r) for r in rows]
    return "\n".join(out)


FOOTER = ("Seen only where traffic crosses a NetFlow-captured firewall interface; traffic inside one segment "
          "never reaches the firewall and is not counted.")


def report(kind, days, segment):
    path = os.path.join(STATE_DIR, LEDGER_FILE)
    if not os.path.exists(path):
        print(f"segflows: no ledger at {path}; run segflows.py once first", file=sys.stderr)
        return 1
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    now = int(os.environ.get("SEGFLOWS_NOW", time.time()))
    since_hour = now - days * 86400
    since_day = ist_day(now - (days - 1) * 86400)
    seg_sql, seg_args = "", ()
    if segment:
        seg_sql, seg_args = " AND (src_segment = ? OR dst_segment = ?)", (segment, segment)
    scope = f"last {days} day(s)" + (f", segment {segment}" if segment else "") + ", times Asia/Kolkata"
    if kind == "pairs":
        rows = db.execute("SELECT src_segment, dst_segment, SUM(bytes), SUM(packets), SUM(flows) FROM hourly "
                          "WHERE hour >= ?" + seg_sql + " GROUP BY 1, 2 ORDER BY 3 DESC", (since_hour,) + seg_args).fetchall()
        print(f"Bytes between segments, {scope}")
        print(table(["src", "dst", "bytes", "packets", "flows"],
                    [(s, d, human(b), p, f) for s, d, b, p, f in rows], right=(2, 3, 4)))
    elif kind == "hosts":
        rows = db.execute("SELECT day, src_segment, dst_segment, src_ip, dst_ip, port, proto, bytes, flows FROM hosts "
                          "WHERE day >= ?" + seg_sql + " ORDER BY bytes DESC LIMIT ?",
                          (since_day,) + seg_args + (TOP_KEEP,)).fetchall()
        print(f"Top host pairs (the ledger keeps the top {TOP_KEEP} per segment pair per day), {scope}")
        print(table(["day", "src", "dst", "src_ip", "dst_ip", "port", "proto", "bytes", "flows"],
                    [(dy, s, d, si, di, pt, pr, human(b), f) for dy, s, d, si, di, pt, pr, b, f in rows], right=(5, 6, 7, 8)))
    elif kind == "hour":
        rows = db.execute("SELECT hour, src_segment, dst_segment, bytes, packets, flows FROM hourly "
                          "WHERE hour >= ?" + seg_sql + " ORDER BY hour, bytes DESC", (since_hour,) + seg_args).fetchall()
        print(f"Hourly bytes between segments, {scope}")
        print(table(["hour", "src", "dst", "bytes", "packets", "flows"],
                    [(datetime.datetime.fromtimestamp(h, IST).strftime("%Y-%m-%d %H:00"), s, d, human(b), p, f)
                     for h, s, d, b, p, f in rows], right=(3, 4, 5)))
    else:  # day
        rows = db.execute("SELECT hour, src_segment, dst_segment, bytes, packets, flows FROM hourly "
                          "WHERE hour >= ?" + seg_sql, (since_hour,) + seg_args).fetchall()
        agg = {}
        for h, s, d, b, p, f in rows:
            a = agg.setdefault((ist_day(h), s, d), [0, 0, 0])
            a[0] += b
            a[1] += p
            a[2] += f
        print(f"Daily bytes between segments, {scope}")
        print(table(["day", "src", "dst", "bytes", "packets", "flows"],
                    [(dy, s, d, human(v[0]), v[1], v[2]) for (dy, s, d), v in sorted(agg.items(), key=lambda kv: (kv[0][0], -kv[1][0]))],
                    right=(3, 4, 5)))
    print(FOOTER)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Inter-segment traffic accounting from NetFlow (see the module docstring).")
    ap.add_argument("--report", choices=["pairs", "hosts", "hour", "day"], help="print a table from the ledger instead of running")
    ap.add_argument("--days", type=int, default=7, help="report window in days (default 7)")
    ap.add_argument("--segment", help="report only pairs where this segment is the source or the destination")
    args = ap.parse_args(argv)
    if args.days < 1:
        ap.error("--days must be at least 1")
    if args.report:
        return report(args.report, args.days, args.segment)
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
