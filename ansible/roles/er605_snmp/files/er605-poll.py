#!/usr/bin/env python3
"""Household internet counters and router health from the ER605 over SNMPv3.

Reads a local snmp_exporter (which holds the SNMPv3 credentials; this script
never sees them) every --interval seconds and:

  * writes Prometheus textfile metrics (atomically) for Alloy to ship;
  * appends the WAN and LAN counters to a raw CSV, one file per UTC day
    (er605-YYYY-MM-DD.csv). Days older than 2 are gzipped, days older than
    --keep-days are deleted;
  * books the byte deltas of every configured WAN into an hourly usage
    ledger, DATA/usage.db (SQLite, never pruned), together with every PPPoE
    reconnect, every WAN down/up and every router reboot;
  * prints one logfmt line per event to stdout (the container log, which
    Alloy ships to Loki): usage, pppoe_reconnect, wan_down, wan_up,
    router_reboot, poll_error.

WANs: --wan NAME=INTERFACE[,pppoe=IFNAME][,isp=LABEL], repeatable; INTERFACE is
the ER605 ifName without the "default/" prefix, pppoe empty means the WAN is
not PPPoE. With none, the single WAN is wan1=eth1,pppoe=pe-wan1_poe. The raw
CSV holds the first WAN and the LAN side; per-WAN history is in the ledger.

The ER605 forwards in hardware and syncs its counters tens of seconds late.
Rates finer than a minute are not meaningful; totals are exact.

Commands (the data directory comes from $DATA or --data-dir):

  er605-poll.py [--wan NAME=INTERFACE[,pppoe=IFNAME][,isp=LABEL]]... run
  er605-poll.py --report hour|day|month|reconnects|wan [--days N] [--wan NAME]
  er605-poll.py report "YYYY-MM-DD HH:MM" "YYYY-MM-DD HH:MM"
      WAN volume, mean, p95 and max 1-minute rate for a window (IST), from
      the raw CSV days. Both commands print local time (Asia/Kolkata).
  er605-poll.py import-csv DIR
      Load er605-*.csv(.gz) files from DIR into the ledger. Safe to repeat:
      an hour already imported is replaced, an hour the live poller booked is
      never touched.
"""
import argparse
import datetime
import glob
import gzip
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.request

DATA = os.environ.get("DATA", "/data")
URL = os.environ.get(
    "EXPORTER",
    "http://127.0.0.1:9116/snmp?target=10.10.0.1&module=if_mib,system,hrDevice,hrStorage,hrSystem&auth=er605")
TEXTFILE = os.environ.get("TEXTFILE", "")  # directory node_exporter reads; empty = off
TEXTFILE_NAME = os.environ.get("TEXTFILE_NAME", "er605.prom")
STEP = int(os.environ.get("INTERVAL", "30"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "400"))
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))  # Asia/Kolkata has no DST
UTC = datetime.timezone.utc
GZIP_AFTER_DAYS = 2
ERROR_EVERY = 600  # poll_error lines: at most one per 10 minutes
BACKLOG_HOURS = 72  # a usage line for an hour closed longer ago than this is not logged

# Interfaces are matched by ifName (ifIndex can change with firmware).
LAN = "default/eth0"
DEFAULT_WANS = [{"name": "wan1", "interface": "eth1", "pppoe": "pe-wan1_poe", "isp": ""}]
DEFAULT_WAN = "wan1"  # what rows from before multi-WAN support (and the CSV history) belong to
LAN_WANT = {"lan_in": ("ifHCInOctets", LAN), "lan_out": ("ifHCOutOctets", LAN), "lan_in_err": ("ifInErrors", LAN)}
# The raw CSV header: the first WAN and the LAN side. Its order must not change.
COLS = ["wan_in", "wan_out", "lan_in", "lan_out", "wan_in_err", "lan_in_err", "wan_in_disc", "wan_out_disc"]
_NAME = re.compile(r"[a-z0-9-]+")
_IFNAME = re.compile(r"[A-Za-z0-9._-]+")


def parse_wan(spec):
    """NAME=INTERFACE[,pppoe=IFNAME][,isp=LABEL] -> {"name", "interface", "pppoe", "isp"}."""
    head, _, rest = spec.partition(",")
    name, eq, iface = head.partition("=")
    w = {"name": name, "interface": iface, "pppoe": "", "isp": ""}
    for part in filter(None, rest.split(",")):
        k, eq2, v = part.partition("=")
        if k not in ("pppoe", "isp") or not eq2:
            raise ValueError("bad --wan option %r in %r" % (part, spec))
        w[k] = v
    if not eq or not _NAME.fullmatch(name) or not _IFNAME.fullmatch(iface) or (w["pppoe"] and not _IFNAME.fullmatch(w["pppoe"])):
        raise ValueError("bad --wan %r: want NAME=INTERFACE[,pppoe=IFNAME][,isp=LABEL], NAME lowercase letters, digits, dashes" % spec)
    return w


def check_wans(wans):
    for key in ("name", "interface"):
        vals = [w[key] for w in wans]
        if len(set(vals)) != len(vals):
            raise ValueError("duplicate WAN %s in %s" % (key, vals))
    return wans


HELP = {
    "er605_if_octets_total": ("counter", "ER605 interface byte counter (IF-MIB ifHC*Octets) via SNMPv3."),
    "er605_if_errors_total": ("counter", "ER605 interface input errors (IF-MIB ifInErrors)."),
    "er605_if_discards_total": ("counter", "ER605 WAN discards (IF-MIB ifInDiscards/ifOutDiscards)."),
    "er605_last_scrape_timestamp_seconds": ("gauge", "When the poller last read the ER605."),
    "er605_cpu_percent": ("gauge", "ER605 CPU load per core (HOST-RESOURCES hrProcessorLoad)."),
    "er605_memory_bytes": ("gauge", "ER605 memory by state: total, used, cached, buffers (HOST-RESOURCES hrStorage)."),
    "er605_uptime_seconds": ("gauge", "ER605 uptime (HOST-RESOURCES hrSystemUptime, else sysUpTime); restarts from zero on a reboot."),
    "er605_if_up": ("gauge", "1 if the interface is operationally up (IF-MIB ifOperStatus)."),
    "er605_wan_info": ("gauge", "One series per configured WAN: its port, PPPoE session interface and ISP label. Always 1."),
    "er605_wan_up": ("gauge", "1 if the WAN port is up and, for PPPoE, its session interface exists and is up; 0 otherwise, also when the interface is missing."),
    "er605_pppoe_session_seconds": ("gauge", "Age of the current PPPoE session of a WAN: sysUpTime minus the last change of its session interface."),
    "er605_pppoe_reconnects_total": ("counter", "PPPoE reconnects seen by the poller per WAN, restored from the ledger at start-up."),
    "er605_pppoe_octets_total": ("counter", "Bytes of the current PPPoE session of a WAN; restarts from zero on a reconnect."),
    "er605_poll_success": ("gauge", "1 if the last poll read the ER605, 0 if it failed."),
    "er605_poll_duration_seconds": ("gauge", "How long the last poll took."),
}

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    hour TEXT NOT NULL,               -- UTC hour start, 2026-11-27T10:00:00Z
    wan TEXT NOT NULL,                -- the WAN's name; rows from before multi-WAN are wan1
    download_bytes INTEGER NOT NULL,  -- WAN port in
    upload_bytes INTEGER NOT NULL,    -- WAN port out
    source TEXT NOT NULL DEFAULT 'live',  -- 'live' (poller) or 'csv' (import-csv)
    PRIMARY KEY (hour, wan)
);
CREATE TABLE IF NOT EXISTS reconnects (
    id INTEGER PRIMARY KEY, at REAL NOT NULL,       -- start of the new session
    previous_session_seconds REAL NOT NULL,
    wan TEXT NOT NULL DEFAULT 'wan1'
);
CREATE TABLE IF NOT EXISTS wan_events (
    id INTEGER PRIMARY KEY, wan TEXT NOT NULL, down_at REAL NOT NULL,
    up_at REAL, duration REAL                       -- NULL while the WAN is still down
);
CREATE TABLE IF NOT EXISTS reboots (id INTEGER PRIMARY KEY, at REAL NOT NULL);  -- boot time
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value);
"""


# ---------------------------------------------------------------- scrape ---

_SAMPLE = re.compile(r'^([A-Za-z_:][\w:]*)(?:\{(.*)\})? (\S+)\s*$')
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_metrics(body):
    """Prometheus text -> [(name, {label: value}, float)]; comments are skipped."""
    out = []
    for line in body.splitlines():
        m = _SAMPLE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        labels = {k: v.replace('\\"', '"').replace("\\\\", "\\") for k, v in _LABEL.findall(m.group(2) or "")}
        out.append((m.group(1), labels, value))
    return out


def build_sample(body, now, wans=None):
    """One poll's reading. The LAN counters are required (an answer without them
    is no answer). A configured WAN whose interface the router did not report is
    kept as down, not an error; everything else optional is simply absent."""
    wans = wans or DEFAULT_WANS
    rows = parse_metrics(body)
    by_if = {}
    for name, lab, v in rows:
        if "ifName" in lab:
            by_if[(name, lab["ifName"])] = v
    s = {"ts": now}
    missing = [col for col, key in LAN_WANT.items() if key not in by_if]
    if missing:
        raise RuntimeError("incomplete scrape: %s" % sorted(missing))
    for col, key in LAN_WANT.items():
        s[col] = int(by_if[key])
    s["wans"] = {}
    if_up = {}
    for w in wans:
        full = "default/" + w["interface"]
        r = {"interface": w["interface"], "pppoe": w["pppoe"], "isp": w["isp"],
             "present": all((m, full) in by_if for m in ("ifHCInOctets", "ifHCOutOctets")), "pppoe_data": None}
        oper = by_if.get(("ifOperStatus", full))
        if r["present"]:
            r.update({"in": int(by_if[("ifHCInOctets", full)]), "out": int(by_if[("ifHCOutOctets", full)]),
                      "in_err": int(by_if.get(("ifInErrors", full), 0)), "in_disc": int(by_if.get(("ifInDiscards", full), 0)),
                      "out_disc": int(by_if.get(("ifOutDiscards", full), 0))})
        if oper is not None:
            if_up[w["interface"]] = int(oper == 1)
        pp_up = False
        if w["pppoe"]:
            pf = "default/" + w["pppoe"]
            if all(k in by_if for k in (("ifLastChange", pf), ("ifHCInOctets", pf), ("ifHCOutOctets", pf))):
                r["pppoe_data"] = {"change_cs": int(by_if[("ifLastChange", pf)]), "in": int(by_if[("ifHCInOctets", pf)]),
                                   "out": int(by_if[("ifHCOutOctets", pf)])}
            if ("ifOperStatus", pf) in by_if:
                if_up[w["pppoe"]] = int(by_if[("ifOperStatus", pf)] == 1)
                pp_up = by_if[("ifOperStatus", pf)] == 1
        r["up"] = int(r["present"] and oper == 1 and (not w["pppoe"] or (r["pppoe_data"] is not None and pp_up)))
        s["wans"][w["name"]] = r
    if ("ifOperStatus", LAN) in by_if:
        if_up["eth0"] = int(by_if[("ifOperStatus", LAN)] == 1)
    s["if_up"] = if_up
    cores = sorted((int(lab.get("hrDeviceIndex", 0)), v) for name, lab, v in rows if name == "hrProcessorLoad")
    s["cpu"] = {str(i): v for i, (_, v) in enumerate(cores)}
    store = {}
    for name, lab, v in rows:
        if name in ("hrStorageSize", "hrStorageUsed", "hrStorageAllocationUnits") and "hrStorageDescr" in lab:
            store.setdefault(lab["hrStorageDescr"], {})[name] = v

    def used(descr, field):
        e = store.get(descr, {})
        return int(e[field] * e["hrStorageAllocationUnits"]) if field in e and "hrStorageAllocationUnits" in e else None
    mem = {"total": used("Physical memory", "hrStorageSize"), "used": used("Physical memory", "hrStorageUsed"),
           "cached": used("Cached memory", "hrStorageUsed"), "buffers": used("Memory buffers", "hrStorageUsed")}
    s["mem"] = {k: v for k, v in mem.items() if v is not None}
    up = [v for name, lab, v in rows if name == "sysUpTime"] or [v for name, lab, v in rows if name == "hrSystemUptime"]
    s["uptime_cs"] = int(up[0]) if up else None  # sysUpTime, timeticks: hundredths of a second. The SNMP agent's clock, the one ifLastChange uses.
    hr = [v for name, lab, v in rows if name == "hrSystemUptime"]
    s["hr_uptime_cs"] = int(hr[0]) if hr else None  # the host's uptime; only a real reboot resets it
    return s


def scrape(now, wans=None):
    body = urllib.request.urlopen(URL, timeout=20).read().decode()
    return build_sample(body, now, wans)


# ---------------------------------------------------------------- metrics ---

def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(sample, status, reconnects=None):
    """The textfile. `status` is {"success", "duration", "last_ok"}; `reconnects`
    maps a WAN name to its reconnect count. A failed poll writes only the poll
    status and the last good timestamp, so Grafana shows a gap in the counters
    instead of a frozen value."""
    series = {}
    reconnects = reconnects or {}

    def add(metric, value, **labels):
        lab = "{%s}" % ",".join('%s="%s"' % (k, _esc(v)) for k, v in labels.items()) if labels else ""
        series.setdefault(metric, []).append("%s%s %s" % (metric, lab, value))
    v = sample
    if v is not None:
        present = [w for w in v["wans"].values() if w["present"]]
        # Every configured WAN port has role="wan", so sum(...{role="wan"}) is the household total.
        for w in present:
            add("er605_if_octets_total", w["in"], interface=w["interface"], role="wan", direction="in")
            add("er605_if_octets_total", w["out"], interface=w["interface"], role="wan", direction="out")
        add("er605_if_octets_total", v["lan_in"], interface="eth0", role="lan", direction="in")
        add("er605_if_octets_total", v["lan_out"], interface="eth0", role="lan", direction="out")
        for w in present:
            add("er605_if_errors_total", w["in_err"], interface=w["interface"], role="wan", direction="in")
        add("er605_if_errors_total", v["lan_in_err"], interface="eth0", role="lan", direction="in")
        for w in present:
            add("er605_if_discards_total", w["in_disc"], interface=w["interface"], role="wan", direction="in")
            add("er605_if_discards_total", w["out_disc"], interface=w["interface"], role="wan", direction="out")
        for core, pct in v["cpu"].items():
            add("er605_cpu_percent", "%g" % pct, core=core)
        for state in ("total", "used", "cached", "buffers"):
            if state in v["mem"]:
                add("er605_memory_bytes", v["mem"][state], state=state)
        host_up = v.get("hr_uptime_cs") if v.get("hr_uptime_cs") is not None else v["uptime_cs"]
        if host_up is not None:
            add("er605_uptime_seconds", "%.10g" % (host_up / 100))
        roles = [(w["interface"], "wan") for w in v["wans"].values()] + [("eth0", "lan")] + \
                [(w["pppoe"], "pppoe") for w in v["wans"].values() if w["pppoe"]]
        for iface, role in roles:
            if iface in v["if_up"]:
                add("er605_if_up", v["if_up"][iface], interface=iface, role=role)
        for name, w in v["wans"].items():
            add("er605_wan_info", 1, wan=name, interface=w["interface"], pppoe=w["pppoe"], isp=w["isp"])
        for name, w in v["wans"].items():
            add("er605_wan_up", w["up"], wan=name)
        for name, w in v["wans"].items():
            p = w["pppoe_data"]
            if p:
                if v["uptime_cs"] is not None:
                    add("er605_pppoe_session_seconds", "%.10g" % max(0, (v["uptime_cs"] - p["change_cs"]) / 100), wan=name)
                add("er605_pppoe_reconnects_total", reconnects.get(name, 0), wan=name)
                add("er605_pppoe_octets_total", p["in"], wan=name, direction="in")
                add("er605_pppoe_octets_total", p["out"], wan=name, direction="out")
    if status.get("last_ok"):
        add("er605_last_scrape_timestamp_seconds", int(status["last_ok"]))
    add("er605_poll_success", 1 if status["success"] else 0)
    add("er605_poll_duration_seconds", "%.3f" % status["duration"])
    out = []
    for metric, (kind, text) in HELP.items():
        if metric in series:
            out += ["# HELP %s %s" % (metric, text), "# TYPE %s %s" % (metric, kind)] + series[metric]
    return "\n".join(out) + "\n"


def write_atomic(path, text):
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write_prom(text):
    """text None removes the file (clean shutdown)."""
    if not TEXTFILE:
        return
    final = os.path.join(TEXTFILE, TEXTFILE_NAME)
    if text is None:
        try:
            os.remove(final)
        except OSError:
            pass
        return
    write_atomic(final, text)


# ------------------------------------------------------------- raw CSV days ---

_CSV = re.compile(r"er605-(\d{4}-\d{2}-\d{2})\.csv(\.gz)?$")


def append_csv(data_dir, sample):
    """The raw CSV holds the first configured WAN and the LAN side. Returns False
    (writes nothing) when that WAN's interface is missing from the poll."""
    first = next(iter(sample["wans"].values()))
    if not first["present"]:
        return False
    row = [first["in"], first["out"], sample["lan_in"], sample["lan_out"], first["in_err"], sample["lan_in_err"],
           first["in_disc"], first["out_disc"]]
    day = datetime.datetime.fromtimestamp(sample["ts"], UTC).strftime("%Y-%m-%d")
    fn = os.path.join(data_dir, "er605-%s.csv" % day)
    new = not os.path.exists(fn)
    with open(fn, "a") as f:
        if new:
            f.write("ts_epoch," + ",".join(COLS) + "\n")
        f.write("%d,%s\n" % (sample["ts"], ",".join(str(c) for c in row)))
    return True


def housekeep(data_dir, keep_days, now):
    """Gzip raw days older than GZIP_AFTER_DAYS, delete days older than keep_days.
    Only er605-YYYY-MM-DD.csv(.gz) in data_dir itself; anything else is left alone."""
    today = datetime.datetime.fromtimestamp(now, UTC).date()
    for name in sorted(os.listdir(data_dir)):
        m = _CSV.fullmatch(name)
        if not m:
            continue
        age = (today - datetime.date.fromisoformat(m.group(1))).days
        path = os.path.join(data_dir, name)
        if age > keep_days:
            os.remove(path)
        elif age > GZIP_AFTER_DAYS and not m.group(2):
            tmp = path + ".gz.tmp"
            with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
                dst.write(src.read())
            os.replace(tmp, path + ".gz")
            os.remove(path)


def csv_rows(directory, t0=None, t1=None):
    """Samples from every er605-*.csv(.gz) in directory: [(ts, {col: int})], oldest first."""
    rows = []
    for fn in sorted(glob.glob(os.path.join(directory, "er605-*.csv*"))):
        if not _CSV.fullmatch(os.path.basename(fn)):
            continue
        opener = gzip.open if fn.endswith(".gz") else open
        with opener(fn, "rt") as f:
            for line in f:
                if line.startswith("ts_epoch") or not line.strip():
                    continue
                c = line.strip().split(",")
                try:
                    ts = int(c[0])
                    vals = dict(zip(COLS, map(int, c[1:])))
                except ValueError:
                    continue  # a torn last line
                if len(vals) == len(COLS) and (t0 is None or t0 <= ts <= t1):
                    rows.append((ts, vals))
    rows.sort(key=lambda r: r[0])
    return rows


# ----------------------------------------------------------------- ledger ---

def hour_of(ts):
    return datetime.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:00:00Z")


def _hour_ts(hour):
    return datetime.datetime.strptime(hour, "%Y-%m-%dT%H:00:00Z").replace(tzinfo=UTC).timestamp()


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def logfmt(event, **kv):
    def q(v):
        v = str(v).replace("\n", " ")
        return '"%s"' % v.replace('"', "'") if (not v or re.search(r'[\s"=]', v)) else v
    return event + " " + " ".join("%s=%s" % (k, q(v)) for k, v in kv.items())


def counter_delta(cur, prev, reset=False):
    """Bytes since the previous reading. A counter that went backwards (router
    reboot, PPPoE reconnect) restarted from zero: the new value is the traffic
    since then, never a huge negative or wrapped number."""
    if prev is None:
        return 0
    return int(cur if (reset or cur < prev) else cur - prev)


def split_hours(t0, t1, total):
    """Spread `total` bytes read at t1 (counter delta since t0) over the UTC
    hours between the two readings, in proportion to the time in each; the
    last hour takes the rounding remainder so not a byte is lost. A normal 30 s
    step inside one hour is one bucket. -> {hour: bytes}"""
    if total <= 0 or t1 <= t0 or hour_of(t0) == hour_of(t1):
        return {hour_of(t1): total} if total else {}
    out = {}
    t = t0
    given = 0
    while t < t1:
        edge = min(t1, _hour_ts(hour_of(t)) + 3600)
        share = total - given if edge >= t1 else int(total * (edge - t) / (t1 - t0))
        out[hour_of(t)] = out.get(hour_of(t), 0) + share
        given += share
        t = edge
    return out


# State keys the single-WAN ledger kept unprefixed; they belong to wan1 now.
_OLD_WAN_STATE = ("ts", "wan_in", "wan_out", "pppoe_change", "pppoe_in", "pppoe_out", "pppoe_start")


def migrate(db):
    """Bring a ledger from before multi-WAN support up to date, losslessly and in
    one transaction: usage gains a wan column (and primary key (hour, wan)),
    reconnects gains wan, and every existing row and state key belongs to wan1.
    Does nothing on a new or already migrated ledger."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(usage)")]
    if not cols or "wan" in cols:
        return
    db.execute("BEGIN")
    try:
        db.execute("CREATE TABLE usage_new (hour TEXT NOT NULL, wan TEXT NOT NULL, download_bytes INTEGER NOT NULL, "
                   "upload_bytes INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'live', PRIMARY KEY (hour, wan))")
        db.execute("INSERT INTO usage_new SELECT hour, ?, download_bytes, upload_bytes, source FROM usage", (DEFAULT_WAN,))
        db.execute("DROP TABLE usage")
        db.execute("ALTER TABLE usage_new RENAME TO usage")
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'reconnects'").fetchone() and \
                "wan" not in [r[1] for r in db.execute("PRAGMA table_info(reconnects)")]:
            db.execute("ALTER TABLE reconnects ADD COLUMN wan TEXT NOT NULL DEFAULT '%s'" % DEFAULT_WAN)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'state'").fetchone():
            for key in _OLD_WAN_STATE:
                db.execute("UPDATE state SET key = ? WHERE key = ?", ("%s:%s" % (DEFAULT_WAN, key), key))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        migrate(self.db)
        self.db.executescript(LEDGER_SCHEMA)
        self.db.commit()

    def state(self):
        return dict(self.db.execute("SELECT key, value FROM state"))

    def _put(self, **kv):
        for k, v in kv.items():
            if v is None:
                self.db.execute("DELETE FROM state WHERE key = ?", (k,))
            else:
                self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (k, v))

    def reconnect_counts(self):
        return dict(self.db.execute("SELECT wan, count(*) FROM reconnects GROUP BY wan"))

    def _book(self, hour, wan, down, up):
        # A live booking marks the hour 'live', so import-csv never replaces it.
        self.db.execute(
            "INSERT INTO usage VALUES (?,?,?,?, 'live') ON CONFLICT (hour, wan) DO UPDATE SET "
            "download_bytes = download_bytes + excluded.download_bytes, "
            "upload_bytes = upload_bytes + excluded.upload_bytes, source = 'live'", (hour, wan, down, up))

    def update(self, sample):
        """Book one successful poll in one transaction; returns the log lines.
        A failure rolls the whole poll back, so a partial write never commits
        with the next poll."""
        try:
            return self._update(sample)
        except BaseException:
            self.db.rollback()
            raise

    def _update(self, sample):
        now = sample["ts"]
        st = self.state()
        lines = []
        up_cs = sample.get("uptime_cs")
        hr_cs = sample.get("hr_uptime_cs")
        # A router reboot is the HOST uptime going down. sysUpTime also drops
        # when only the SNMP agent restarts (and wraps after about 497 days), so
        # it never counts as a reboot. Neither forces a counter reset: bytes are
        # always booked with counter_delta, where a counter that went down is a
        # reset and the new value is the traffic since.
        reboot = hr_cs is not None and "hr_uptime_cs" in st and hr_cs < st["hr_uptime_cs"]
        agent_restart = up_cs is not None and "uptime_cs" in st and up_cs < st["uptime_cs"]
        if reboot:
            at = now - hr_cs / 100
            self.db.execute("INSERT INTO reboots (at) VALUES (?)", (at,))
            lines.append(logfmt("router_reboot", at=iso(at)))
        new = {"uptime_cs": up_cs, "hr_uptime_cs": hr_cs}
        for name, w in sample["wans"].items():
            k = lambda x, n=name: "%s:%s" % (n, x)  # noqa: E731  this WAN's state key
            if w["present"]:
                down = counter_delta(w["in"], st.get(k("wan_in")))
                upl = counter_delta(w["out"], st.get(k("wan_out")))
                if k("ts") in st and (down or upl):
                    # Traffic since the previous poll is spread over the hours between
                    # the two polls in proportion to time, so a poller gap does not pile
                    # into the current hour. A counter that restarted (delta = new value)
                    # happened at an unknown moment and is booked in the current hour.
                    d_in = {hour_of(now): down} if w["in"] < st[k("wan_in")] else split_hours(st[k("ts")], now, down)
                    d_out = {hour_of(now): upl} if w["out"] < st[k("wan_out")] else split_hours(st[k("ts")], now, upl)
                    for hour in sorted(set(d_in) | set(d_out)):
                        self._book(hour, name, d_in.get(hour, 0), d_out.get(hour, 0))
                new.update({k("ts"): now, k("wan_in"): w["in"], k("wan_out"): w["out"]})
            p = w["pppoe_data"]
            if p:
                session = max(0, (up_cs - p["change_cs"]) / 100) if up_cs is not None else 0
                start = now - session
                # ifLastChange is on sysUpTime's clock: after an agent restart or a
                # reboot it is not comparable with the stored one, so only the
                # session counters can show a reconnect then.
                clock_moved = reboot or agent_restart
                if k("pppoe_change") in st and not reboot and (
                        (p["change_cs"] != st[k("pppoe_change")] and not clock_moved)
                        or p["in"] < st[k("pppoe_in")] or p["out"] < st[k("pppoe_out")]):
                    previous = max(0.0, start - st.get(k("pppoe_start"), start))
                    self.db.execute("INSERT INTO reconnects (at, previous_session_seconds, wan) VALUES (?,?,?)",
                                    (start, previous, name))
                    lines.append(logfmt("pppoe_reconnect", at=iso(start), wan=name, previous_session_seconds=int(previous)))
                elif k("pppoe_start") in st and not reboot and (
                        clock_moved or p["change_cs"] == st.get(k("pppoe_change"))):
                    start = st[k("pppoe_start")]  # same session: keep the first estimate of its start
                new.update({k("pppoe_change"): p["change_cs"], k("pppoe_in"): p["in"], k("pppoe_out"): p["out"],
                            k("pppoe_start"): start})
            elif reboot:
                # The session died with the router; the next one is a new baseline.
                new.update({k("pppoe_change"): None, k("pppoe_in"): None, k("pppoe_out"): None, k("pppoe_start"): None})
            # else: the session interface is missing from this poll (the ER605 is
            # redialling). Keep the previous session's state so the reconnect is
            # counted when it returns.
            # WAN down and up: a transition of er605_wan_up. A WAN first seen down
            # (or whose interface is missing) is one wan_down, not one per poll.
            up = w["up"]
            if st.get(k("up")) != up:
                open_ev = self.db.execute("SELECT id, down_at FROM wan_events WHERE wan = ? AND up_at IS NULL", (name,)).fetchone()
                if up == 0 and not open_ev:
                    self.db.execute("INSERT INTO wan_events (wan, down_at) VALUES (?,?)", (name, now))
                    lines.append(logfmt("wan_down", at=iso(now), wan=name))
                elif up == 1 and open_ev:
                    duration = max(0.0, now - open_ev[1])
                    self.db.execute("UPDATE wan_events SET up_at = ?, duration = ? WHERE id = ?", (now, duration, open_ev[0]))
                    lines.append(logfmt("wan_up", at=iso(now), wan=name, duration_seconds=int(duration)))
            new[k("up")] = up
        # One usage line per WAN per hour that has closed. A fresh ledger starts at
        # the previous hour so an import is never replayed into the journal.
        current = hour_of(now)
        prev_hour = _hour_ts(hour_of(now)) - 3600
        row = self.db.execute("SELECT value FROM state WHERE key = 'logged_through'").fetchone()
        logged = row[0] if row else prev_hour
        for hour, wan, d, u in self.db.execute(
                "SELECT hour, wan, download_bytes, upload_bytes FROM usage WHERE hour < ? ORDER BY hour, wan", (current,)).fetchall():
            ts = _hour_ts(hour)
            if ts > logged and ts >= now - BACKLOG_HOURS * 3600:
                lines.append(logfmt("usage", hour=hour, wan=wan, download_bytes=d, upload_bytes=u))
        new["logged_through"] = max(logged, prev_hour)
        self._put(**new)
        self.db.commit()
        return lines


def import_csv(db_path, directory):
    """Hourly WAN usage from raw CSV days (the CSV is the first WAN, so it books
    as wan1). Idempotent: hours it imported
    earlier are replaced with the same values, hours the live poller booked are
    left alone. Returns (hours_written, hours_skipped)."""
    rows = csv_rows(directory)
    hours = {}
    for (ta, a), (tb, b) in zip(rows, rows[1:]):
        d = counter_delta(b["wan_in"], a["wan_in"])
        u = counter_delta(b["wan_out"], a["wan_out"])
        d_in = {hour_of(tb): d} if b["wan_in"] < a["wan_in"] else split_hours(ta, tb, d)
        d_out = {hour_of(tb): u} if b["wan_out"] < a["wan_out"] else split_hours(ta, tb, u)
        for hour in set(d_in) | set(d_out):
            h = hours.setdefault(hour, [0, 0])
            h[0] += d_in.get(hour, 0)
            h[1] += d_out.get(hour, 0)
    led = Ledger(db_path)
    written = skipped = 0
    for hour, (d, u) in sorted(hours.items()):
        cur = led.db.execute("SELECT source FROM usage WHERE hour = ? AND wan = ?", (hour, DEFAULT_WAN)).fetchone()
        if cur and cur[0] == "live":
            skipped += 1
            continue
        led.db.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?,?, 'csv')", (hour, DEFAULT_WAN, d, u))
        written += 1
    led.db.commit()
    led.db.close()
    return written, skipped


# ----------------------------------------------------------------- report ---

def pct(v, p):
    s = sorted(v)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))] if s else 0.0


def table(head, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows)] if rows else [len(h) for h in head]
    for r in [head] + rows:
        print("  ".join(str(x).ljust(w) for x, w in zip(r, widths)))


def hms(seconds):
    s = int(seconds)
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def _col(db, table, column):
    return column in [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]


def ledger_report(data_dir, kind, days, wan=None):
    """hour/day/month/reconnects/wan from the ledger, local time. The ledger
    bins are UTC hours (xx:30 in IST), so a day or month row books each hour to
    the local date its start falls on. Totals first, then one pair of columns per
    WAN; --wan NAME limits everything to that WAN. A ledger from before
    multi-WAN support reads as wan1."""
    db = sqlite3.connect("file:%s?mode=ro" % os.path.join(data_dir, "usage.db"), uri=True)
    since = time.time() - days * 86400
    local = lambda t: datetime.datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    if kind == "reconnects":
        sel = "wan" if _col(db, "reconnects", "wan") else "'%s'" % DEFAULT_WAN
        rows = [[local(at), w, hms(prev)] for at, prev, w in db.execute(
            "SELECT at, previous_session_seconds, %s FROM reconnects WHERE at >= ? ORDER BY at" % sel, (since,))
            if not wan or w == wan]
        table(["new_session_start", "wan", "previous_session"], rows)
        boots = [local(at) for (at,) in db.execute("SELECT at FROM reboots WHERE at >= ? ORDER BY at", (since,))]
        print("router reboots: %s" % (", ".join(boots) or "none"))
        return
    if kind == "wan":
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'wan_events'").fetchone():
            print("no WAN events: the ledger has not been written by a multi-WAN poller yet")
            return
        rows, down = [], {}
        for w, d0, u0, dur in db.execute("SELECT wan, down_at, up_at, duration FROM wan_events WHERE down_at >= ? ORDER BY down_at", (since,)):
            if wan and w != wan:
                continue
            length = dur if u0 is not None else time.time() - d0
            rows.append([w, local(d0), local(u0) if u0 is not None else "down now", hms(length)])
            a = down.setdefault(w, [0, 0.0])
            a[0] += 1
            a[1] += length
        table(["wan", "down_at", "up_at", "duration"], rows)
        for w, (n, total) in sorted(down.items()):
            print("%s: %d down events, %s down in total" % (w, n, hms(total)))
        return
    width = {"hour": 16, "day": 10, "month": 7}[kind]
    sel = "wan" if _col(db, "usage", "wan") else "'%s'" % DEFAULT_WAN
    agg, wans = {}, set()
    for hour, w, d, u in db.execute("SELECT hour, %s, download_bytes, upload_bytes FROM usage ORDER BY hour" % sel):
        if wan and w != wan:
            continue
        t = datetime.datetime.fromtimestamp(_hour_ts(hour), UTC)
        if t.timestamp() < since - 3600:
            continue
        wans.add(w)
        key = t.astimezone(IST).strftime("%Y-%m-%d %H:%M")[:width]
        a = agg.setdefault(key, {}).setdefault(w, [0, 0])
        a[0] += d
        a[1] += u
    head = [kind + " (IST)", "download_gb", "upload_gb", "total_gb"]
    for w in sorted(wans):
        head += ["%s_down_gb" % w, "%s_up_gb" % w]
    out = []
    for key, per in sorted(agg.items()):
        d = sum(x[0] for x in per.values())
        u = sum(x[1] for x in per.values())
        row = [key, "%.3f" % (d / 1e9), "%.3f" % (u / 1e9), "%.3f" % ((d + u) / 1e9)]
        for w in sorted(wans):
            x = per.get(w, [0, 0])
            row += ["%.3f" % (x[0] / 1e9), "%.3f" % (x[1] / 1e9)]
        out.append(row)
    table(head, out)


def window_report(data_dir, a, b):
    def parse(x):
        return datetime.datetime.strptime(x, "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp()
    t0, t1 = parse(a), parse(b)
    rows = csv_rows(data_dir, t0 - 90, t1 + 90)
    inside = [r for r in rows if t0 <= r[0] <= t1]
    print("window %s to %s IST: %d samples (expect about %d)" % (a, b, len(inside), int((t1 - t0) / STEP)))
    if len(inside) < 2:
        print("not enough data: the poller was not running in that window")
        return
    # 1-minute rates from samples about 60 s apart; a counter that went backwards
    # (router reboot) is skipped rather than read as a huge negative rate
    rates = {"wan_in": [], "wan_out": []}
    vol = {"wan_in": 0, "wan_out": 0}
    for (ta, va), (tb, vb) in zip(inside, inside[1:]):
        for k in rates:
            d = vb[k] - va[k]
            if d >= 0:
                vol[k] += d
    i = 0
    while i < len(inside):
        j = i + 1
        while j < len(inside) and inside[j][0] - inside[i][0] < 60:
            j += 1
        if j >= len(inside):
            break
        dt = inside[j][0] - inside[i][0]
        for k in rates:
            d = inside[j][1][k] - inside[i][1][k]
            if d >= 0:
                rates[k].append(d * 8 / dt / 1e6)
        i = j
    for k, label in (("wan_in", "download"), ("wan_out", "upload")):
        r = rates[k]
        print("WAN %-8s %7.2f GB  mean %6.1f Mb/s  p95 %6.1f  max %6.1f  (1-minute rates)"
              % (label, vol[k] / 1e9, sum(r) / len(r) if r else 0, pct(r, .95), max(r or [0])))
    first, last = inside[0][1], inside[-1][1]
    for k in ("wan_in_err", "lan_in_err", "wan_in_disc", "wan_out_disc"):
        print("%-13s +%d" % (k, max(0, last[k] - first[k])))


# -------------------------------------------------------------------- run ---

# BaseException, not Exception: the poll loop catches Exception to survive
# a bad poll, and must not swallow SIGTERM.
class Stop(BaseException):
    pass


def _stop(signum, frame):
    raise Stop()


def run(data_dir=None, once=False, wans=None):
    data_dir = data_dir or DATA
    wans = check_wans(wans or DEFAULT_WANS)
    os.makedirs(data_dir, exist_ok=True)
    signal.signal(signal.SIGTERM, _stop)  # PID 1 in a container ignores SIGTERM without a handler
    ledger = Ledger(os.path.join(data_dir, "usage.db"))
    print(logfmt("startup", interval=STEP, keep_days=KEEP_DAYS, data=data_dir, wans=",".join(w["name"] for w in wans)), flush=True)
    last_ok = None
    last_error = 0.0
    last_housekeeping = 0.0
    nxt = time.time()
    try:
        while True:
            nxt += STEP
            start = time.time()
            sample = None
            error = None
            try:
                sample = scrape(int(start), wans)
            except Exception as e:  # keep polling through a router reboot or a timeout
                error = e
            if sample is not None:
                last_ok = sample["ts"]
                try:
                    append_csv(data_dir, sample)
                    for line in ledger.update(sample):
                        print(line, flush=True)
                except Exception as e:
                    error = e
            if error is not None and start - last_error >= ERROR_EVERY:
                last_error = start
                print(logfmt("poll_error", error=str(error) or type(error).__name__), flush=True)
            try:
                write_prom(render(sample, {"success": sample is not None, "duration": time.time() - start,
                                           "last_ok": last_ok}, ledger.reconnect_counts()))
            except OSError as e:
                if start - last_error >= ERROR_EVERY:
                    last_error = start
                    print(logfmt("poll_error", error=str(e)), flush=True)
            if start - last_housekeeping >= 3600:
                last_housekeeping = start
                try:
                    housekeep(data_dir, KEEP_DAYS, start)
                except OSError as e:
                    print(logfmt("poll_error", error="housekeeping: %s" % e), flush=True)
            if once:
                return
            time.sleep(max(1, nxt - time.time()))
    except (Stop, KeyboardInterrupt):
        write_prom(None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", choices=["hour", "day", "month", "reconnects", "wan"])
    ap.add_argument("--days", type=int, help="how far back --report looks (default: hour 2, day 30, month 400, reconnects 30, wan 30)")
    ap.add_argument("--wan", action="append", default=[],
                    help="NAME=INTERFACE[,pppoe=IFNAME][,isp=LABEL] defines a WAN (repeatable; default wan1=eth1,pppoe=pe-wan1_poe); "
                         "a bare NAME limits --report to that WAN")
    ap.add_argument("--data-dir", default=DATA)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    r = sub.add_parser("report")
    r.add_argument("start")
    r.add_argument("end")
    i = sub.add_parser("import-csv")
    i.add_argument("dir")
    args = ap.parse_args(argv)
    defs = [parse_wan(v) for v in args.wan if "=" in v]
    only = [v for v in args.wan if "=" not in v]
    if args.report:
        ledger_report(args.data_dir, args.report,
                      args.days or {"hour": 2, "day": 30, "month": 400, "reconnects": 30, "wan": 30}[args.report],
                      only[0] if only else None)
    elif args.cmd == "run":
        run(args.data_dir, wans=defs or None)
    elif args.cmd == "report":
        window_report(args.data_dir, args.start, args.end)
    elif args.cmd == "import-csv":
        os.makedirs(args.data_dir, exist_ok=True)
        w, s = import_csv(os.path.join(args.data_dir, "usage.db"), args.dir)
        print("import-csv: %d hours written, %d skipped (booked live)" % (w, s))
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
