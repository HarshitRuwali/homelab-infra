#!/usr/bin/env python3
"""Track internet outages from one vantage point, for Grafana and a ledger.

Every --interval seconds it runs one ROUND: all probes in parallel (threads),
each bounded by --timeout. A round REACHES THE INTERNET when any `tcp` or
`dns` target succeeds. `http` targets and the gateway probe are informational
and never decide.

Targets, repeatable `--target KIND:HOST:PORT`; with none given:
  tcp:1.1.1.1:443 tcp:8.8.8.8:443 tcp:9.9.9.9:443
  dns:1.1.1.1:53 dns:8.8.8.8:53 http:connectivitycheck.gstatic.com:80

  tcp   a TCP connect succeeds; RTT is the connect time
  dns   a raw UDP A query for --dns-name; any reply with the same ID and QR=1
        is a success (any RCODE); RTT is the reply time
  http  GET /generate_204 through the system resolver; 204 is a success

The gateway (--gateway HOST:PORT, default: the default route's gateway from
/proc/net/route, port 80) is probed with a TCP connect; a refusal counts as up,
because something answered. No default route means the gateway is down.

State machine: a failed round starts a pending outage at that round's start.
--confirm-rounds consecutive failed rounds confirm it: a ledger row with end
NULL is inserted and `outage_start` is logged. Fewer failed rounds followed by
a good round is a BLIP: logged, counted, never stored. The first good round
after an outage ends it (end = that round's start). Durations come from the
monotonic clock, so a clock step never makes one negative. The cause is `local` if
the gateway probe failed in the confirming round, else `upstream`. A row still
open at start-up (the process died) is closed at the start-up time with
closed_by=restart. If the ledger cannot be opened or written, a rate-limited
ledger_error line is logged and the probe keeps running without it.

LAN targets (--lan-target NAME=HOST:PORT, repeatable) are probed every round
with the same timeout, up when a TCP connect succeeds or is refused. They never
decide whether the internet is up. Each has its own state machine with the same
--confirm-rounds, the ledger table lan_outages, and the journal lines lan_down,
lan_up and lan_blip. At most one probe per target is in flight.

Named paths (--path NAME=KIND:HOST:PORT[,KIND:HOST:PORT...], repeatable, kinds
tcp and dns) measure one route to the internet, for example one WAN of a
multi-WAN router when the router sends those targets out that WAN. A path is up
in a round when any of its targets answers. Paths never decide whether the
internet is up. Each has its own state machine with the same --confirm-rounds,
the ledger table path_outages and the journal lines path_down, path_up and
path_blip. At most one probe per target is in flight.

Outputs:
  * Prometheus textfile metrics to --textfile (tmp file and rename), every
    --textfile-every seconds and at once on an outage start or end;
    internet_probe_up is 0 if any round failed within the last
    --textfile-every seconds, however often files are written;
  * logfmt journal lines, the first word being the event: startup,
    outage_start, outage_end, blip, lan_down, lan_up, lan_blip, path_down, path_up, path_blip, and ledger_error or textfile_error (at
    most one per 10 minutes). No per-round lines;
  * the ledger --data-dir/outages.db (SQLite), written only on transitions and
    never pruned.

  internet-probe.py --site home --textfile /var/lib/alloy/textfile/internet-probe.prom
  internet-probe.py --data-dir DIR --report outages [--days N]
  internet-probe.py --data-dir DIR --report daily [--days N]
  internet-probe.py --data-dir DIR --report lan [--days N]
  internet-probe.py --data-dir DIR --report paths [--days N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import re
import socket
import sqlite3
import struct
import sys
import threading
import time
from http.client import HTTPConnection

DEFAULT_TARGETS = [
    "tcp:1.1.1.1:443", "tcp:8.8.8.8:443", "tcp:9.9.9.9:443",
    "dns:1.1.1.1:53", "dns:8.8.8.8:53",
    "http:connectivitycheck.gstatic.com:80",
]
DECIDING = ("tcp", "dns")
KINDS = ("tcp", "dns", "http")


# ----------------------------------------------------------------- probes ---

def tcp_probe(host: str, port: int, timeout: float, refused_is_up: bool = False):
    """(ok, rtt seconds or None). With refused_is_up a refusal counts as up."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, time.monotonic() - t0
    except ConnectionRefusedError:
        return (True, time.monotonic() - t0) if refused_is_up else (False, None)
    except OSError:
        return False, None


def dns_query(name: str, qid: int) -> bytes:
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.strip(".").split("."):
        q += bytes([len(label)]) + label.encode("ascii")
    return q + b"\x00" + struct.pack(">HH", 1, 1)


def dns_probe(host: str, port: int, timeout: float, name: str):
    t0 = time.monotonic()
    deadline = t0 + timeout
    try:
        family, _, _, _, addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
        qid = random.randrange(0x10000)
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.send(dns_query(name, qid))
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False, None
                s.settimeout(left)
                data = s.recv(2048)
                if len(data) >= 12:
                    rid, flags = struct.unpack(">HH", data[:4])
                    if rid == qid and flags & 0x8000:
                        return True, time.monotonic() - t0
    except (OSError, UnicodeError, ValueError):
        return False, None


def _resolve(host: str, port: int, timeout: float):
    """getaddrinfo in a daemon thread joined with the timeout, so a stalled
    resolver cannot hold a probe past --timeout. Returns (family, addr) or None."""
    box: list = []

    def work():
        try:
            box.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0])
        except (OSError, UnicodeError):
            pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(max(0.0, timeout))
    if not box:
        return None
    return box[0][0], box[0][4]


def http_probe(host: str, port: int, timeout: float):
    """GET /generate_204. The name is resolved with the system resolver within
    the timeout, then the connection goes to the resolved address with the
    Host header set."""
    t0 = time.monotonic()
    try:
        r = _resolve(host, port, timeout)
        if r is None:
            return False, None
        addr = r[1][0]
        left = timeout - (time.monotonic() - t0)
        if left <= 0:
            return False, None
        c = HTTPConnection(addr, port, timeout=left)
        try:
            c.request("GET", "/generate_204", headers={"Host": host if port == 80 else f"{host}:{port}"})
            ok = c.getresponse().status == 204
        finally:
            c.close()
        return (True, time.monotonic() - t0) if ok else (False, None)
    except Exception:  # http.client.HTTPException and friends: a failed probe, never a crash
        return False, None


def default_gateway(route_text: str | None = None) -> str:
    """The default route's gateway address from /proc/net/route, or ''."""
    if route_text is None:
        try:
            with open("/proc/net/route") as f:
                route_text = f.read()
        except OSError:
            return ""
    for line in route_text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 4 and f[1] == "00000000" and int(f[3], 16) & 2:
            return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
    return ""


def split_hostport(spec: str, default_port: int | None = None):
    host, sep, port = spec.rpartition(":")
    if not sep:
        if default_port is None:
            raise ValueError(f"expected HOST:PORT, got {spec!r}")
        return spec, default_port
    return host, int(port)


def parse_target(spec: str):
    kind, sep, rest = spec.partition(":")
    if not sep or kind not in KINDS:
        raise ValueError(f"target {spec!r}: expected KIND:HOST:PORT with KIND one of {', '.join(KINDS)}")
    host, port = split_hostport(rest)
    return kind, host, port


def parse_lan_target(spec: str):
    """NAME=HOST:PORT -> (name, host, port)."""
    name, sep, rest = spec.partition("=")
    if not sep or not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError(f"--lan-target {spec!r}: expected NAME=HOST:PORT with NAME of lowercase letters, digits and dashes")
    host, port = split_hostport(rest)
    return name, host, port


def parse_path(spec: str):
    """NAME=KIND:HOST:PORT[,KIND:HOST:PORT...] -> (name, [(kind, host, port), ...]); kinds tcp and dns."""
    name, sep, rest = spec.partition("=")
    if not sep or not re.fullmatch(r"[a-z0-9-]+", name) or not rest:
        raise ValueError(f"--path {spec!r}: expected NAME=KIND:HOST:PORT[,KIND:HOST:PORT...] with NAME of lowercase letters, digits and dashes")
    targets = [parse_target(t) for t in rest.split(",")]
    if any(k not in DECIDING for k, _, _ in targets):
        raise ValueError(f"--path {spec!r}: kinds are tcp and dns")
    return name, targets


def run_round(targets, gateway: str, timeout: float, dns_name: str, http_slots: dict | None = None,
              lan_targets=(), path_targets=()):
    """One round: every probe in its own thread. Returns (results, gateway)
    where results maps (kind, "HOST:PORT") to (ok, rtt) and gateway is None
    (no default route) or (label, ok, rtt).

    The verdict waits for the tcp and dns probes and the gateway, each bounded
    by the timeout. An http probe is collected only if it finishes within the
    same budget, else it counts as failed for the round, and no new one starts
    while the previous is still running (http_slots carries that between rounds).
    LAN targets (name, host, port) are bounded the same way, one in flight per
    target; their results are keyed ("lan", name). A connect that succeeds or
    is refused is up. Path targets (name, [(kind, host, port), ...]) are probed
    the same way, keyed ("path", name, kind, "HOST:PORT")."""
    if http_slots is None:
        http_slots = {}
    results: dict = {}
    lock = threading.Lock()
    t_start = time.monotonic()

    def work(key, fn):
        try:
            r = fn()
        except Exception:
            r = (False, None)
        with lock:
            results[key] = r

    jobs = []
    http_jobs = []
    for kind, host, port in targets:
        key = (kind, f"{host}:{port}")
        if kind == "tcp":
            jobs.append((key, lambda h=host, p=port: tcp_probe(h, p, timeout)))
        elif kind == "dns":
            jobs.append((key, lambda h=host, p=port: dns_probe(h, p, timeout, dns_name)))
        else:
            http_jobs.append((key, lambda h=host, p=port: http_probe(h, p, timeout)))
    for name, host, port in lan_targets:
        http_jobs.append((("lan", name), lambda h=host, p=port: tcp_probe(h, p, timeout, refused_is_up=True)))
    for name, tgts in path_targets:
        for kind, host, port in tgts:
            if kind == "tcp":
                fn = lambda h=host, p=port: tcp_probe(h, p, timeout)
            else:
                fn = lambda h=host, p=port: dns_probe(h, p, timeout, dns_name)
            http_jobs.append((("path", name, kind, f"{host}:{port}"), fn))
    gw_label = gateway or default_gateway()
    gw_key = None
    if gw_label:
        gh, gp = split_hostport(gw_label, 80) if ":" in gw_label else (gw_label, 80)
        gw_label = f"{gh}:{gp}"
        gw_key = ("gateway", gw_label)
        jobs.append((gw_key, lambda: tcp_probe(gh, gp, timeout, refused_is_up=True)))
    threads = [threading.Thread(target=work, args=j, daemon=True) for j in jobs]
    for t in threads:
        t.start()
    for key, fn in http_jobs:
        slot = http_slots.get(key)
        if slot is not None and slot["thread"].is_alive():
            continue  # the previous probe is still running: no thread growth
        slot = {"result": None}

        def hwork(slot=slot, fn=fn):
            try:
                slot["result"] = fn()
            except Exception:
                slot["result"] = (False, None)

        slot["thread"] = threading.Thread(target=hwork, daemon=True)
        http_slots[key] = slot
        slot["thread"].start()
    deadline = t_start + timeout + 0.5
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    for key, _ in http_jobs:
        slot = http_slots[key]
        slot["thread"].join(max(0.0, deadline - time.monotonic()))
        res = slot["result"] if not slot["thread"].is_alive() else None
        results[key] = res or (False, None)
    with lock:
        done = dict(results)
    gw = None
    if gw_key:
        ok, rtt = done.pop(gw_key, (False, None))
        gw = (gw_label, ok, rtt)
    for key, _ in jobs + http_jobs:
        if key != gw_key:
            done.setdefault(key, (False, None))
    return done, gw


# ----------------------------------------------------------------- ledger ---

class Ledger:
    """SQLite ledger. Written only on outage transitions; `writes` counts them."""

    def __init__(self, data_dir: str, timeout: float = 1.0):
        os.makedirs(data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "outages.db"), timeout=timeout)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS outages(id INTEGER PRIMARY KEY, site TEXT, start REAL, "
            "end REAL NULL, duration REAL NULL, cause TEXT, closed_by TEXT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS lan_outages(id INTEGER PRIMARY KEY, site TEXT, name TEXT, target TEXT, "
            "start REAL, end REAL NULL, duration REAL NULL, closed_by TEXT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS path_outages(id INTEGER PRIMARY KEY, site TEXT, name TEXT, "
            "start REAL, end REAL NULL, duration REAL NULL, closed_by TEXT NULL)")
        self.db.commit()
        self.writes = 0

    def health(self):
        """A read that touches the file (the schema page) and writes nothing."""
        self.db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()

    def path_open_rows(self, site):
        return self.db.execute("SELECT id, name, start FROM path_outages WHERE site=? AND end IS NULL", (site,)).fetchall()

    def path_totals(self, site):
        return {n: (c, float(d)) for n, c, d in self.db.execute(
            "SELECT name, COUNT(*), COALESCE(SUM(duration), 0) FROM path_outages WHERE site=? GROUP BY name", (site,))}

    def path_insert(self, site, name, start) -> int:
        cur = self.db.execute("INSERT INTO path_outages(site, name, start, end, duration, closed_by) "
                              "VALUES(?,?,?,NULL,NULL,NULL)", (site, name, start))
        self.db.commit()
        self.writes += 1
        return cur.lastrowid

    def path_record(self, site, name, start, end, duration, closed_by):
        self.db.execute("INSERT INTO path_outages(site, name, start, end, duration, closed_by) VALUES(?,?,?,?,?,?)",
                        (site, name, start, end, duration, closed_by))
        self.db.commit()
        self.writes += 1

    def path_close(self, row_id, end, duration, closed_by):
        self.db.execute("UPDATE path_outages SET end=?, duration=?, closed_by=? WHERE id=?",
                        (end, duration, closed_by, row_id))
        self.db.commit()
        self.writes += 1

    def lan_open_rows(self, site):
        return self.db.execute("SELECT id, name, target, start FROM lan_outages WHERE site=? AND end IS NULL",
                               (site,)).fetchall()

    def lan_totals(self, site):
        return {n: (c, float(d)) for n, c, d in self.db.execute(
            "SELECT name, COUNT(*), COALESCE(SUM(duration), 0) FROM lan_outages WHERE site=? GROUP BY name", (site,))}

    def lan_insert(self, site, name, target, start) -> int:
        cur = self.db.execute("INSERT INTO lan_outages(site, name, target, start, end, duration, closed_by) "
                              "VALUES(?,?,?,?,NULL,NULL,NULL)", (site, name, target, start))
        self.db.commit()
        self.writes += 1
        return cur.lastrowid

    def lan_record(self, site, name, target, start, end, duration, closed_by):
        self.db.execute("INSERT INTO lan_outages(site, name, target, start, end, duration, closed_by) "
                        "VALUES(?,?,?,?,?,?,?)", (site, name, target, start, end, duration, closed_by))
        self.db.commit()
        self.writes += 1

    def lan_close(self, row_id, end, duration, closed_by):
        self.db.execute("UPDATE lan_outages SET end=?, duration=?, closed_by=? WHERE id=?",
                        (end, duration, closed_by, row_id))
        self.db.commit()
        self.writes += 1

    def open_rows(self, site):
        return self.db.execute("SELECT id, start, cause FROM outages WHERE site=? AND end IS NULL", (site,)).fetchall()

    def totals(self, site):
        n, down = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration), 0) FROM outages WHERE site=?", (site,)).fetchone()
        return n, float(down)

    def last_closed(self, site):
        return self.db.execute(
            "SELECT start, end, duration, cause FROM outages WHERE site=? AND end IS NOT NULL "
            "ORDER BY end DESC LIMIT 1", (site,)).fetchone()

    def insert(self, site, start, cause) -> int:
        cur = self.db.execute("INSERT INTO outages(site, start, end, duration, cause, closed_by) "
                              "VALUES(?,?,NULL,NULL,?,NULL)", (site, start, cause))
        self.db.commit()
        self.writes += 1
        return cur.lastrowid

    def record(self, site, start, end, duration, cause, closed_by):
        """A whole row at once, for an outage whose opening insert failed."""
        self.db.execute("INSERT INTO outages(site, start, end, duration, cause, closed_by) VALUES(?,?,?,?,?,?)",
                        (site, start, end, duration, cause, closed_by))
        self.db.commit()
        self.writes += 1

    def close(self, row_id, end, duration, closed_by):
        self.db.execute("UPDATE outages SET end=?, duration=?, closed_by=? WHERE id=?",
                        (end, duration, closed_by, row_id))
        self.db.commit()
        self.writes += 1


# ---------------------------------------------------------------- logging ---

def _iso(ts) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _logfmt(event: str, **kv) -> str:
    def q(v):
        v = str(v).replace('"', "'")
        return f'"{v}"' if (not v or any(c in v for c in ' "=')) else v
    return event + " " + " ".join(f"{k}={q(v)}" for k, v in kv.items())


def _print_line(line: str) -> None:
    print(line, flush=True)


ERROR_EVERY = 600.0  # seconds between repeats of the same error line


def open_ledger(data_dir: str, site: str, log=_print_line, timeout: float = 1.0):
    """The ledger, or None (logged) if it cannot be opened: the probe then runs
    without one rather than crash-looping."""
    try:
        return Ledger(data_dir, timeout)
    except (sqlite3.Error, OSError) as e:
        log(_logfmt("ledger_error", site=site, op="open", error=e))
        return None


# ---------------------------------------------------------- state machine ---

class Tracker:
    """The outage state machine for ONE thing (the internet, or one LAN
    target). Wall-clock round starts are only labels; every duration comes from
    the monotonic clock `mono` passed in, so a clock step never makes one
    negative. It stores nothing: feed() returns the event and Monitor books it."""

    def __init__(self, confirm: int):
        self.confirm = max(1, int(confirm))
        self.fail_streak = 0
        self.pending_start = self.pending_mono = None
        self.open_active = False
        self.open_id = None
        self.open_start = self.open_mono = None
        self.cause = ""
        self.last_fail_mono = None
        self.outages = 0
        self.closed_seconds = 0.0
        self.exported_down = 0.0
        self.last = None

    def feed(self, ok: bool, start: float, mono: float):
        """One round. Returns None, ("start",), ("end", start, end, duration,
        row_id) or ("blip", start, duration)."""
        if not ok:
            self.last_fail_mono = mono
            if self.fail_streak == 0:
                self.pending_start, self.pending_mono = start, mono
            self.fail_streak += 1
            if not self.open_active and self.fail_streak >= self.confirm:
                self.open_active = True
                self.open_start, self.open_mono = self.pending_start, self.pending_mono
                self.outages += 1
                return ("start",)
            return None
        ev = None
        if self.open_active:
            dur = max(0.0, mono - self.open_mono)
            end = self.open_start + dur
            self.closed_seconds += dur
            self.last = (self.open_start, end, dur, self.cause)
            ev = ("end", self.open_start, end, dur, self.open_id)
            self.open_active = False
            self.open_id = self.open_start = self.open_mono = None
        elif self.fail_streak > 0:
            ev = ("blip", self.pending_start, max(0.0, mono - self.pending_mono))
        self.fail_streak = 0
        self.pending_start = self.pending_mono = None
        return ev

    def view(self, mono: float, window: float):
        """(elapsed in the open outage, down total, up flag). The total never
        goes below what was exported before; up is 0 for `window` seconds
        after any failed round."""
        elapsed = max(0.0, mono - self.open_mono) if self.open_active else 0.0
        down = max(self.closed_seconds + elapsed, self.exported_down)
        self.exported_down = down
        recent = self.last_fail_mono is not None and mono - self.last_fail_mono < window
        return elapsed, down, 0 if recent else 1


class Monitor:
    """Probe results and both clocks are passed in, so it is testable. Owns one
    Tracker for the internet and one per LAN target, and books their events in
    the ledger and the journal."""

    HEALTH_BAD = 60.0    # re-check a failed ledger this often
    HEALTH_OK = 600.0    # and a healthy one this often

    def __init__(self, site, targets, ledger, confirm_rounds=2, interval=5.0, log=_print_line,
                 mono=time.monotonic, window=15.0, lan_targets=(), paths=()):
        self.site = site
        self.keys = [(k, f"{h}:{p}") for k, h, p in targets]
        self.ledger = ledger
        self.confirm = max(1, int(confirm_rounds))
        self.interval = interval
        self.log = log
        self.mono = mono
        self.window = window  # internet_probe_up is 0 for this long after a failed round
        self.rounds = self.failed_rounds = self.blips = 0
        self.last_gw_ok = False
        self.net = Tracker(self.confirm)
        self.target_state = {k: {"up": 0, "rtt": None, "failures": 0} for k in self.keys}
        self.gateway = None  # (label, ok, rtt)
        self.lan = {name: {"target": f"{h}:{p}", "tr": Tracker(self.confirm), "rtt": None, "failures": 0}
                    for name, h, p in lan_targets}
        self.paths = {name: {"tr": Tracker(self.confirm),
                             "targets": {(k, f"{h}:{p}"): {"rtt": None, "failures": 0} for k, h, p in tgts}}
                      for name, tgts in paths}
        self.ledger_ok = 1 if ledger is not None else 0
        self.error_logged: dict = {}
        self.last_health = self.mono()

    # read-only views of the internet tracker
    outages = property(lambda self: self.net.outages)
    closed_seconds = property(lambda self: self.net.closed_seconds)
    last = property(lambda self: self.net.last)
    open_active = property(lambda self: self.net.open_active)

    def _ledger(self, op, fn, default=None):
        """Run a ledger operation; a sqlite error is logged (once per op per
        ERROR_EVERY), flips ledger_ok to 0 and never stops the probe."""
        if self.ledger is None:
            return default
        try:
            r = fn(self.ledger)
            self.ledger_ok = 1
            return r
        except sqlite3.Error as e:
            self.ledger_ok = 0
            try:
                self.ledger.db.rollback()
            except sqlite3.Error:
                pass
            now = self.mono()
            last = self.error_logged.get(op)
            if last is None or now - last >= ERROR_EVERY:
                self.error_logged[op] = now
                self.log(_logfmt("ledger_error", site=self.site, op=op, error=e))
            return default

    def check_health(self) -> None:
        """Let ledger_ok recover without waiting for a transition: a read that
        writes nothing, at most every HEALTH_BAD seconds after a failure and
        every HEALTH_OK seconds otherwise."""
        if self.ledger is None:
            return
        now = self.mono()
        if now - self.last_health >= (self.HEALTH_OK if self.ledger_ok else self.HEALTH_BAD):
            self.last_health = now
            self._ledger("health", lambda l: l.health())

    def startup(self, now: float) -> None:
        """Restore totals from the ledger; close rows left open by a dead process."""
        for row_id, start, cause in self._ledger("restore", lambda l: l.open_rows(self.site), []):
            dur = max(0.0, now - start)
            end = start + dur
            self._ledger("close", lambda l: l.close(row_id, end, dur, "restart"))
            self.log(_logfmt("outage_end", site=self.site, start=_iso(start), end=_iso(end),
                             duration_seconds=int(dur), cause=cause, closed_by="restart"))
        for row_id, name, target, start in self._ledger("restore", lambda l: l.lan_open_rows(self.site), []):
            dur = max(0.0, now - start)
            end = start + dur
            self._ledger("lan_close", lambda l: l.lan_close(row_id, end, dur, "restart"))
            self.log(_logfmt("lan_up", site=self.site, name=name, target=target, start=_iso(start),
                             end=_iso(end), duration_seconds=int(dur), closed_by="restart"))
        for row_id, name, start in self._ledger("restore", lambda l: l.path_open_rows(self.site), []):
            dur = max(0.0, now - start)
            end = start + dur
            self._ledger("path_close", lambda l: l.path_close(row_id, end, dur, "restart"))
            self.log(_logfmt("path_up", site=self.site, name=name, start=_iso(start), end=_iso(end),
                             duration_seconds=int(dur), closed_by="restart"))
        self.net.outages, self.net.closed_seconds = self._ledger("restore", lambda l: l.totals(self.site), (0, 0.0))
        self.net.last = self._ledger("restore", lambda l: l.last_closed(self.site), None)
        totals = self._ledger("restore", lambda l: l.lan_totals(self.site), {})
        for name, st in self.lan.items():
            st["tr"].outages, st["tr"].closed_seconds = totals.get(name, (0, 0.0))
        ptotals = self._ledger("restore", lambda l: l.path_totals(self.site), {})
        for name, st in self.paths.items():
            st["tr"].outages, st["tr"].closed_seconds = ptotals.get(name, (0, 0.0))
        self.last_health = self.mono()

    def on_round(self, start: float, results: dict, gateway) -> bool:
        """Feed one round (`start` is its wall-clock start). Returns True on an
        outage start or end, internet or LAN (write the metrics now)."""
        mono = self.mono()
        self.rounds += 1
        self.gateway = gateway
        for key in self.keys:
            ok, rtt = results.get(key, (False, None))
            st = self.target_state[key]
            st["up"] = 1 if ok else 0
            st["rtt"] = rtt if ok else None
            if not ok:
                st["failures"] += 1
        reached = any(results.get(k, (False, None))[0] for k in self.keys if k[0] in DECIDING)
        gw_ok = bool(gateway and gateway[1])
        if not reached:
            self.failed_rounds += 1
            self.last_gw_ok = gw_ok
        changed = False
        ev = self.net.feed(reached, start, mono)
        if ev and ev[0] == "start":
            cause = "upstream" if gw_ok else "local"
            self.net.cause = cause
            self.net.open_id = self._ledger("insert", lambda l: l.insert(self.site, self.net.open_start, cause))
            self.log(_logfmt("outage_start", site=self.site, start=_iso(self.net.open_start),
                             cause=cause, gateway_up=int(gw_ok)))
            changed = True
        elif ev and ev[0] == "end":
            _, ostart, end, dur, row_id = ev
            cause = self.net.cause
            if row_id is not None:
                self._ledger("close", lambda l: l.close(row_id, end, dur, "probe"))
            else:  # the opening insert failed: store the whole row now
                self._ledger("insert", lambda l: l.record(self.site, ostart, end, dur, cause, "probe"))
            self.log(_logfmt("outage_end", site=self.site, start=_iso(ostart), end=_iso(end),
                             duration_seconds=int(dur), cause=cause, closed_by="probe"))
            changed = True
        elif ev and ev[0] == "blip":
            self.blips += 1
            self.log(_logfmt("blip", site=self.site, start=_iso(ev[1]), duration_seconds=int(ev[2]),
                             gateway_up=int(self.last_gw_ok)))
        for name, st in self.lan.items():
            ok, rtt = results.get(("lan", name), (False, None))
            st["rtt"] = rtt if ok else None
            if not ok:
                st["failures"] += 1
            changed |= self._aux_event("lan", name, st["tr"], st["target"], st["tr"].feed(bool(ok), start, mono))
        for name, st in self.paths.items():
            any_ok = False
            for key, ts in st["targets"].items():
                ok, rtt = results.get(("path", name) + key, (False, None))
                any_ok |= bool(ok)
                ts["rtt"] = rtt if ok else None
                if not ok:
                    ts["failures"] += 1
            changed |= self._aux_event("path", name, st["tr"], None, st["tr"].feed(any_ok, start, mono))
        self.check_health()
        return changed

    def _aux_event(self, prefix, name, tr, target, ev) -> bool:
        """Book a LAN-target or path event: ledger table <prefix>_outages, lines
        <prefix>_down, <prefix>_up and <prefix>_blip. `target` is None for paths."""
        if ev is None:
            return False
        extra = () if target is None else (target,)
        who = dict(name=name) if target is None else dict(name=name, target=target)
        if ev[0] == "start":
            tr.open_id = self._ledger(f"{prefix}_insert",
                                      lambda l: getattr(l, f"{prefix}_insert")(self.site, name, *extra, tr.open_start))
            self.log(_logfmt(f"{prefix}_down", site=self.site, **who, start=_iso(tr.open_start)))
            return True
        if ev[0] == "end":
            _, ostart, end, dur, row_id = ev
            if row_id is not None:
                self._ledger(f"{prefix}_close", lambda l: getattr(l, f"{prefix}_close")(row_id, end, dur, "probe"))
            else:
                self._ledger(f"{prefix}_insert", lambda l: getattr(l, f"{prefix}_record")(
                    self.site, name, *extra, ostart, end, dur, "probe"))
            self.log(_logfmt(f"{prefix}_up", site=self.site, **who, start=_iso(ostart), end=_iso(end),
                             duration_seconds=int(dur), closed_by="probe"))
            return True
        self.log(_logfmt(f"{prefix}_blip", site=self.site, **who, start=_iso(ev[1]), duration_seconds=int(ev[2])))
        return False

    def snapshot(self, now: float) -> dict:
        """What a textfile write reports. `now` is the wall time for the
        timestamp only."""
        mono = self.mono()
        elapsed, down, up = self.net.view(mono, self.window)
        lan = []
        for name, st in self.lan.items():
            _e, l_down, l_up = st["tr"].view(mono, self.window)
            lan.append({"name": name, "target": st["target"], "up": l_up, "rtt": st["rtt"],
                        "failures": st["failures"], "active": 1 if st["tr"].open_active else 0,
                        "down_seconds": l_down, "outages": st["tr"].outages})
        paths = []
        for name, st in self.paths.items():
            _e, p_down, p_up = st["tr"].view(mono, self.window)
            paths.append({"name": name, "up": p_up, "active": 1 if st["tr"].open_active else 0,
                          "down_seconds": p_down, "outages": st["tr"].outages,
                          "targets": [{"target": k[1], "rtt": v["rtt"], "failures": v["failures"]}
                                      for k, v in st["targets"].items()]})
        return {
            "up": up,
            "outage_active": 1 if self.net.open_active else 0,
            "outage_seconds": elapsed,
            "down_seconds": down,
            "outages": self.net.outages,
            "blips": self.blips,
            "rounds": self.rounds,
            "failed_rounds": self.failed_rounds,
            "targets": [dict(kind=k[0], target=k[1], **self.target_state[k]) for k in self.keys],
            "gateway": self.gateway,
            "last": self.net.last,
            "lan": lan,
            "paths": paths,
            "interval": self.interval,
            "ledger_ok": self.ledger_ok,
            "ts": now,
        }


class Poller:
    """One step of the main loop after the probes ran: feed the state machine
    and decide whether to write the textfile. Clocks and the writer are
    injected for the tests."""

    def __init__(self, mon, site, textfile, every, wall=time.time, write=None, log=_print_line):
        self.mon, self.site, self.textfile, self.every = mon, site, textfile, every
        self.wall, self.log = wall, log
        self.write = write or write_atomic
        self.last_attempt = None
        self.error_logged = None

    def step(self, start: float, results: dict, gateway) -> bool:
        changed = self.mon.on_round(start, results, gateway)
        now = self.mon.mono()
        if self.textfile and (changed or self.last_attempt is None or now - self.last_attempt >= self.every):
            self.last_attempt = now  # also on failure, so a broken path is retried at the usual pace
            try:
                self.write(self.textfile, render(self.site, self.mon.snapshot(self.wall())))
            except OSError as e:
                if self.error_logged is None or now - self.error_logged >= ERROR_EVERY:
                    self.error_logged = now
                    self.log(_logfmt("textfile_error", site=self.site, error=e))
        return changed


# -------------------------------------------------------------- rendering ---

def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**kv) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kv.items()) + "}"


HELP = {
    "internet_probe_up": ("gauge", "0 if any round failed to reach the internet within the last textfile interval, else 1."),
    "internet_probe_outage_active": ("gauge", "1 while a confirmed outage is open."),
    "internet_probe_outage_seconds": ("gauge", "Seconds into the open outage, 0 when none."),
    "internet_probe_down_seconds_total": ("counter", "Seconds of confirmed outage, restored from the ledger at start-up."),
    "internet_probe_outages_total": ("counter", "Confirmed outages in the ledger."),
    "internet_probe_blips_total": ("counter", "Failed rounds too short to confirm, since the process started."),
    "internet_probe_rounds_total": ("counter", "Probe rounds since the process started."),
    "internet_probe_failed_rounds_total": ("counter", "Rounds that did not reach the internet, since the process started."),
    "internet_probe_target_up": ("gauge", "1 if the target answered in the last round (kind and target labels)."),
    "internet_probe_target_rtt_seconds": ("gauge", "Round trip of the last round; absent when the target failed."),
    "internet_probe_target_failures_total": ("counter", "Failed probes of the target since the process started."),
    "internet_probe_gateway_up": ("gauge", "1 if the gateway answered (or refused) in the last round."),
    "internet_probe_gateway_rtt_seconds": ("gauge", "Gateway round trip of the last round; absent when down."),
    "internet_probe_last_outage_start_timestamp_seconds": ("gauge", "Unix start of the latest closed outage."),
    "internet_probe_last_outage_end_timestamp_seconds": ("gauge", "Unix end of the latest closed outage."),
    "internet_probe_last_outage_duration_seconds": ("gauge", "Duration of the latest closed outage, by cause."),
    "internet_probe_lan_up": ("gauge", "0 if any round failed to reach the LAN target within the last textfile interval, else 1."),
    "internet_probe_lan_rtt_seconds": ("gauge", "Connect time to the LAN target in the last round; absent when it failed."),
    "internet_probe_lan_failures_total": ("counter", "Failed rounds for the LAN target since the process started."),
    "internet_probe_lan_outage_active": ("gauge", "1 while a confirmed LAN outage is open."),
    "internet_probe_lan_down_seconds_total": ("counter", "Seconds of confirmed LAN outage, restored from the ledger, including the open one."),
    "internet_probe_lan_outages_total": ("counter", "Confirmed LAN outages in the ledger."),
    "internet_probe_path_up": ("gauge", "0 if any round failed to reach the internet over the path within the last textfile interval, else 1."),
    "internet_probe_path_rtt_seconds": ("gauge", "Round trip to a path target in the last round; absent when it failed."),
    "internet_probe_path_failures_total": ("counter", "Failed probes of a path target since the process started."),
    "internet_probe_path_outage_active": ("gauge", "1 while a confirmed path outage is open."),
    "internet_probe_path_down_seconds_total": ("counter", "Seconds of confirmed path outage, restored from the ledger, including the open one."),
    "internet_probe_path_outages_total": ("counter", "Confirmed path outages in the ledger."),
    "internet_probe_ledger_ok": ("gauge", "1 if the last ledger operation succeeded, 0 if it failed or there is no ledger."),
    "internet_probe_interval_seconds": ("gauge", "Configured seconds between rounds."),
    "internet_probe_timestamp_seconds": ("gauge", "Unix time this file was written."),
}


def render(site: str, snap: dict) -> str:
    out: dict[str, list[str]] = {k: [] for k in HELP}

    def add(metric, value, **labels):
        out[metric].append(f"{metric}{_labels(site=site, **labels)} {value}")

    def num(v):
        return f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)

    add("internet_probe_up", snap["up"])
    add("internet_probe_outage_active", snap["outage_active"])
    add("internet_probe_outage_seconds", num(round(snap["outage_seconds"], 3)))
    add("internet_probe_down_seconds_total", num(round(snap["down_seconds"], 3)))
    add("internet_probe_outages_total", snap["outages"])
    add("internet_probe_blips_total", snap["blips"])
    add("internet_probe_rounds_total", snap["rounds"])
    add("internet_probe_failed_rounds_total", snap["failed_rounds"])
    for t in snap["targets"]:
        lab = dict(kind=t["kind"], target=t["target"])
        add("internet_probe_target_up", t["up"], **lab)
        if t["rtt"] is not None and t["up"]:
            add("internet_probe_target_rtt_seconds", f'{t["rtt"]:.6f}', **lab)
        add("internet_probe_target_failures_total", t["failures"], **lab)
    gw = snap["gateway"]
    label = gw[0] if gw else "none"
    add("internet_probe_gateway_up", 1 if gw and gw[1] else 0, gateway=label)
    if gw and gw[1] and gw[2] is not None:
        add("internet_probe_gateway_rtt_seconds", f"{gw[2]:.6f}", gateway=label)
    if snap["last"]:
        start, end, dur, cause = snap["last"]
        add("internet_probe_last_outage_start_timestamp_seconds", f"{start:.3f}")
        add("internet_probe_last_outage_end_timestamp_seconds", f"{end:.3f}")
        add("internet_probe_last_outage_duration_seconds", f"{dur:.3f}", cause=cause)
    for l in snap.get("lan", []):
        lab = dict(name=l["name"], target=l["target"])
        add("internet_probe_lan_up", l["up"], **lab)
        if l["rtt"] is not None:
            add("internet_probe_lan_rtt_seconds", f'{l["rtt"]:.6f}', **lab)
        add("internet_probe_lan_failures_total", l["failures"], **lab)
        add("internet_probe_lan_outage_active", l["active"], name=l["name"])
        add("internet_probe_lan_down_seconds_total", num(round(l["down_seconds"], 3)), name=l["name"])
        add("internet_probe_lan_outages_total", l["outages"], name=l["name"])
    for pth in snap.get("paths", []):
        add("internet_probe_path_up", pth["up"], name=pth["name"])
        for t in pth["targets"]:
            if t["rtt"] is not None:
                add("internet_probe_path_rtt_seconds", f'{t["rtt"]:.6f}', name=pth["name"], target=t["target"])
            add("internet_probe_path_failures_total", t["failures"], name=pth["name"], target=t["target"])
        add("internet_probe_path_outage_active", pth["active"], name=pth["name"])
        add("internet_probe_path_down_seconds_total", num(round(pth["down_seconds"], 3)), name=pth["name"])
        add("internet_probe_path_outages_total", pth["outages"], name=pth["name"])
    add("internet_probe_ledger_ok", snap["ledger_ok"])
    add("internet_probe_interval_seconds", num(float(snap["interval"])))
    add("internet_probe_timestamp_seconds", f'{snap["ts"]:.3f}')

    lines = []
    for metric, rows in out.items():
        if rows:
            typ, text = HELP[metric]
            lines += [f"# HELP {metric} {text}", f"# TYPE {metric} {typ}"] + rows
    return "\n".join(lines) + "\n"


def write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ---------------------------------------------------------------- reports ---

def _hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _local(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _rows(db_path: str):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"no ledger at {db_path}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return db.execute("SELECT start, end, duration, cause, closed_by FROM outages ORDER BY start").fetchall()
    finally:
        db.close()


def _table(header, rows) -> str:
    cells = [header] + rows
    widths = [max(len(str(r[i])) for r in cells) for i in range(len(header))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells) + "\n"


def report_outages(db_path: str, days: int, now: float) -> str:
    since = now - days * 86400
    rows = []
    for start, end, dur, cause, closed_by in _rows(db_path):
        if end is not None and end < since:
            continue
        if end is None:
            rows.append((_local(start), "ongoing", _hms(now - start), cause, "-"))
        else:
            rows.append((_local(start), _local(end), _hms(dur), cause, closed_by or "-"))
    if not rows:
        return f"no outages in the last {days} days\n"
    return _table(("start", "end", "duration", "cause", "closed_by"), rows)


def report_daily(db_path: str, days: int, now: float) -> str:
    rows = _rows(db_path)
    today = dt.datetime.fromtimestamp(now).date()
    out = []
    for i in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=i)
        d0 = dt.datetime.combine(day, dt.time.min).timestamp()
        day_end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min).timestamp()
        d1 = min(day_end, now)
        span = d1 - d0
        count = down = 0.0
        ongoing = False
        for start, end, _dur, _cause, _by in rows:
            e = now if end is None else end
            if d0 <= start < day_end:
                count += 1
                ongoing = ongoing or end is None
            down += max(0.0, min(e, d1) - max(start, d0))
        avail = 100.0 * (1 - down / span) if span > 0 else 100.0
        out.append((day.isoformat(), int(count), _hms(down) + (" (ongoing)" if ongoing else ""), f"{avail:.3f}"))
    return _table(("day", "outages", "downtime", "availability_%"), out)


def _report_aux(db_path: str, table: str, label: str, days: int, now: float) -> str:
    """Per name (and target, for LAN): outages, downtime and availability over
    the last `days`, then every outage in that window. Names with no outage are
    not in the ledger and so not listed."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"no ledger at {db_path}")
    has_target = table == "lan_outages"
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchall():
            return f"no {label} outages recorded\n"
        cols = "name, target, start, end, duration, closed_by" if has_target else "name, '', start, end, duration, closed_by"
        rows = db.execute(f"SELECT {cols} FROM {table} ORDER BY start").fetchall()
    finally:
        db.close()
    since = now - days * 86400
    per: dict = {}
    listing = []
    for name, target, start, end, dur, by in rows:
        e = now if end is None else end
        if e < since:
            continue
        agg = per.setdefault((name, target), [0, 0.0])
        agg[0] += 1
        agg[1] += max(0.0, min(e, now) - max(start, since))
        listing.append((name, target, _local(start), "ongoing" if end is None else _local(end),
                        _hms(e - start if end is None else dur), by or "-"))
    if not per:
        return f"no {label} outages in the last {days} days\n"
    span = days * 86400.0
    summary = [(n, t, c, _hms(d), f"{100.0 * (1 - min(d, span) / span):.3f}") for (n, t), (c, d) in sorted(per.items())]
    h1, h2 = ("name", "target"), ("name", "target")
    if not has_target:  # drop the empty target column
        summary = [(r[0],) + r[2:] for r in summary]
        listing = [(r[0],) + r[2:] for r in listing]
        h1, h2 = ("name",), ("name",)
    return (_table(h1 + ("outages", "downtime", "availability_%"), summary) + "\n"
            + _table(h2 + ("start", "end", "duration", "closed_by"), listing))


def report_lan(db_path: str, days: int, now: float) -> str:
    return _report_aux(db_path, "lan_outages", "LAN", days, now)


def report_paths(db_path: str, days: int, now: float) -> str:
    return _report_aux(db_path, "path_outages", "path", days, now)


# ------------------------------------------------------------------- main ---

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--site", default="", help="value of the site label, e.g. home; required when polling")
    p.add_argument("--target", action="append", default=[], metavar="KIND:HOST:PORT",
                   help="probe target, repeatable; KIND is tcp, dns or http; default set when none given")
    p.add_argument("--gateway", default="", help="HOST:PORT; empty means the default route's gateway, port 80")
    p.add_argument("--lan-target", action="append", default=[], metavar="NAME=HOST:PORT",
                   help="LAN target, repeatable; up when a TCP connect succeeds or is refused; never decides internet up/down")
    p.add_argument("--path", action="append", default=[], metavar="NAME=KIND:HOST:PORT[,...]",
                   help="named internet path, repeatable; up when any of its tcp or dns targets answers; never decides internet up/down")
    p.add_argument("--interval", type=float, default=5.0, help="seconds between rounds")
    p.add_argument("--timeout", type=float, default=2.0, help="seconds each probe may take")
    p.add_argument("--confirm-rounds", type=int, default=2, help="consecutive failed rounds that confirm an outage")
    p.add_argument("--dns-name", default="one.one.one.one", help="name the dns probes ask for")
    p.add_argument("--textfile", default="", help="metrics file for the textfile collector")
    p.add_argument("--textfile-every", type=float, default=15.0, help="seconds between metrics writes")
    p.add_argument("--data-dir", default="/var/lib/internet-probe", help="holds the ledger outages.db")
    p.add_argument("--report", choices=["outages", "daily", "lan", "paths"], help="print a report from the ledger and exit")
    p.add_argument("--days", type=int, default=30, help="report: how many days back")
    args = p.parse_args()

    if args.report:
        db_path = os.path.join(args.data_dir, "outages.db")
        try:
            fn = {"outages": report_outages, "daily": report_daily, "lan": report_lan, "paths": report_paths}[args.report]
            sys.stdout.write(fn(db_path, args.days, time.time()))
        except (FileNotFoundError, sqlite3.Error) as e:
            print(f"cannot read the ledger: {e}", file=sys.stderr)
            return 1
        return 0

    if not args.site:
        p.error("--site is required when polling")
    try:
        targets = [parse_target(t) for t in (args.target or DEFAULT_TARGETS)]
        if args.gateway:
            split_hostport(args.gateway, 80)
        lan_targets = [parse_lan_target(t) for t in args.lan_target]
        if len({n for n, _, _ in lan_targets}) != len(lan_targets):
            raise ValueError("--lan-target names must be unique")
        paths = [parse_path(t) for t in args.path]
        if len({n for n, _ in paths}) != len(paths):
            raise ValueError("--path names must be unique")
    except ValueError as e:
        p.error(str(e))

    ledger = open_ledger(args.data_dir, args.site)
    mon = Monitor(args.site, targets, ledger, args.confirm_rounds, args.interval, window=args.textfile_every,
                  lan_targets=lan_targets, paths=paths)
    mon.startup(time.time())
    poller = Poller(mon, args.site, args.textfile, args.textfile_every)
    gw_text = args.gateway or default_gateway()
    _print_line(_logfmt("startup", site=args.site, interval=f"{args.interval:g}", confirm_rounds=args.confirm_rounds,
                        targets=len(targets), lan_targets=len(lan_targets), paths=len(paths), gateway=(gw_text + ("" if ":" in gw_text else ":80")) if gw_text else "none"))

    http_slots: dict = {}
    next_t = time.monotonic()
    while True:
        start = time.time()
        results, gw = run_round(targets, args.gateway, args.timeout, args.dns_name, http_slots, lan_targets, paths)
        poller.step(start, results, gw)
        mono = time.monotonic()
        next_t += args.interval
        if next_t < mono:
            next_t = mono
        time.sleep(max(0.0, next_t - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
