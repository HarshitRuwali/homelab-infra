#!/usr/bin/env python3
"""Test roles/internet_probe: its poller against fake clocks, fake probe
results and real local sockets, and the docs and dashboard that describe it.

Standard library only; run from anywhere:

    python3 -I ansible/tests/internet-probe-test.py

Checks:
  * the state machine: a blip is counted and never stored, an outage needs
    confirm_rounds failed rounds, starts at the FIRST failed round and ends at
    the FIRST good round, the cause is local or upstream by the gateway in the
    confirming round, a restart closes an open row with closed_by=restart, and
    the totals are restored from the ledger including the open outage
  * internet_probe_up is 0 if any round since the last write failed
  * http and the gateway never decide; a refused gateway connect counts as up
  * tcp against a real listening socket, dns against a local UDP responder
    (same ID with QR set succeeds, a wrong ID fails)
  * render(): HELP and TYPE for every metric, the site label on every series,
    escaping, the RTT omitted when a probe failed
  * the ledger is written only on transitions
  * --report outages and daily on a fixture ledger, with an ongoing outage
  * the write logic of main()'s loop (Poller.step): up is 0 for the whole
    textfile_every window after a failed round, event writes do not shorten
    it, the first file is written after the first round, a broken textfile
    path is logged once per 10 minutes
  * clock steps never lower down_seconds_total or store a negative duration
  * a corrupt or locked ledger is logged (rate-limited), probing goes on, and
    internet_probe_ledger_ok reports it
  * the http probe resolves within --timeout and never stacks threads
  * LAN targets: per-target blip vs outage, ledger rows and lan_down, lan_up
    and lan_blip lines, restart closing, counters restored per name, a LAN
    outage never touching the internet metrics, refused counts as up, one
    probe in flight per target, --report lan, and the flag parser
  * ledger_ok recovers on its own through a cheap health read
  * named paths (any-of targets, one Tracker each): blip, outage, ledger
    rows and path_down, path_up, path_blip lines, restart closing, counters
    restored per name, never touching the internet metrics, --report paths,
    the flag parser and the role validation
  * the unit has an empty CapabilityBoundingSet and no secrets
  * every default has a row in docs/reference/variables.md and the reverse;
    the playbook is in site.yml, the playbooks docs and the README; every
    metric the poller writes is in monitoring's metrics reference; every flag
    and --report value the docs show exists in the poller; every metric the
    dashboard queries (if it exists) is one the poller writes
"""
import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
REPO = ANSIBLE.parent
ROLE = ANSIBLE / "roles" / "internet_probe"
SCRIPT = ROLE / "files" / "internet-probe.py"
DASHBOARD = REPO / "monitoring" / "grafana" / "dashboards" / "fleet" / "internet-usage.json"
PREFIX = "internet_probe_"
errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


os.environ["TZ"] = "UTC"
time.tzset()
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

TMP = tempfile.mkdtemp()
T0 = 1_800_000_000.0
TARGETS = [("tcp", "1.1.1.1", 443), ("tcp", "8.8.8.8", 443), ("dns", "1.1.1.1", 53), ("http", "x.example", 80)]
KEYS = [(k, f"{h}:{p}") for k, h, p in TARGETS]
GW = ("10.0.0.1:80", True, 0.001)
GW_DOWN = ("10.0.0.1:80", False, None)


def good(http=True):
    return {KEYS[0]: (True, 0.01), KEYS[1]: (True, 0.02), KEYS[2]: (True, 0.03), KEYS[3]: (http, 0.1 if http else None)}


def bad(http=False, tcp=False, dns=False):
    return {KEYS[0]: (tcp, 0.01 if tcp else None), KEYS[1]: (False, None),
            KEYS[2]: (dns, 0.03 if dns else None), KEYS[3]: (http, 0.1 if http else None)}


def fake(mon):
    """Give a Monitor its own fake monotonic clock, advanced by feed() and take()."""
    mon._t = 0.0
    mon.mono = lambda: mon._t
    return mon


def feed(mon, t, results, gateway):
    """One round at wall time t; the fake monotonic clock follows t."""
    mon._t = t - T0
    return mon.on_round(t, results, gateway)


def take(mon, t):
    mon._t = t - T0
    return mon.snapshot(t)


def make(name, confirm=2):
    d = os.path.join(TMP, name)
    logs = []
    led = probe.Ledger(d)
    mon = fake(probe.Monitor("home", TARGETS, led, confirm, 5, log=logs.append))
    mon.startup(T0)
    return mon, led, logs, d


def rows(d):
    return sqlite3.connect(os.path.join(d, "outages.db")).execute(
        "SELECT site, start, end, duration, cause, closed_by FROM outages ORDER BY id").fetchall()


def events(logs):
    return [line.split(" ", 1)[0] for line in logs]


# --------------------------------------------------------- state machine ---
# Blip: one failed round, then good. Counted, logged, no ledger row.
mon, led, logs, d = make("blip")
check(led.writes == 0 and rows(d) == [], "a fresh ledger must be empty and unwritten")
t = T0
for res in [good(), bad(), good(), good()]:
    feed(mon, t, res, GW)
    t += 5
check(mon.blips == 1 and events(logs) == ["blip"], f"one failed round must be a blip, got {logs}")
check("start=" + probe._iso(T0 + 5) in logs[0] and "duration_seconds=5" in logs[0], f"blip line: {logs}")
check(rows(d) == [] and led.writes == 0, "a blip must not touch the ledger")

# Outage with confirm_rounds=2, upstream cause.
mon, led, logs, d = make("outage")
sched = [good(), bad(), bad(), bad(), bad(), good(), good()]
t = T0
changes = []
for res in sched:
    changes.append(feed(mon, t, res, GW))
    t += 5
check(changes == [False, False, True, False, False, True, False], f"outage start and end must be flagged: {changes}")
check(events(logs) == ["outage_start", "outage_end"], f"events: {events(logs)}")
r = rows(d)
check(len(r) == 1 and r[0][1] == T0 + 5, f"start must be the FIRST failed round (T0+5), got {r}")
check(r[0][2] == T0 + 25 and r[0][3] == 20 and r[0][4] == "upstream" and r[0][5] == "probe",
      f"end must be the FIRST good round (T0+25): {r}")
check("cause=upstream" in logs[0] and "gateway_up=1" in logs[0], f"outage_start line: {logs[0]}")
check("duration_seconds=20" in logs[1] and "closed_by=probe" in logs[1] and "end=" + probe._iso(T0 + 25) in logs[1],
      f"outage_end line: {logs[1]}")
check(led.writes == 2, f"ledger writes must be one insert and one update, got {led.writes}")

# Cause local: the gateway failed in the confirming round.
mon, led, logs, d = make("local")
feed(mon, T0, bad(), GW)
feed(mon, T0 + 5, bad(), GW_DOWN)
check(rows(d)[0][4] == "local" and "gateway_up=0" in logs[0], f"gateway down at confirmation is local: {rows(d)}")
mon, led, logs, d = make("nogw")
feed(mon, T0, bad(), None)
feed(mon, T0 + 5, bad(), None)
check(rows(d)[0][4] == "local", "no default route must count as a local failure")

# confirm_rounds=3: two failed rounds are only a blip.
mon, led, logs, d = make("confirm3", confirm=3)
for i, res in enumerate([bad(), bad(), good()]):
    feed(mon, T0 + 5 * i, res, GW)
check(rows(d) == [] and mon.blips == 1, "two failed rounds with confirm_rounds=3 are a blip")

# http and the gateway never decide.
mon, led, logs, d = make("http")
for i in range(5):
    feed(mon, T0 + 5 * i, bad(http=True), GW)
check(mon.failed_rounds == 5 and len(rows(d)) == 1, "only http answering must still be an outage")
mon, led, logs, d = make("dnsdecides")
for i in range(5):
    feed(mon, T0 + 5 * i, bad(dns=True), GW_DOWN)
check(mon.failed_rounds == 0 and rows(d) == [], "a single answering dns target must keep the round good")
mon, led, logs, d = make("tcpdecides")
feed(mon, T0, bad(tcp=True), GW_DOWN)
check(mon.failed_rounds == 0, "a single answering tcp target must keep the round good, even with the gateway down")

# Restart closes an open row; totals restored from the ledger.
mon, led, logs, d = make("restart")
for i, res in enumerate([bad(), bad(), bad()]):
    feed(mon, T0 + 5 * i, res, GW)
led2 = probe.Ledger(d)
logs2 = []
mon2 = fake(probe.Monitor("home", TARGETS, led2, 2, 5, log=logs2.append))
mon2.startup(T0 + 100)
r = rows(d)
check(r[0][2] == T0 + 100 and r[0][3] == 100 and r[0][5] == "restart", f"restart must close the row: {r}")
check(len(logs2) == 1 and logs2[0].startswith("outage_end ") and "closed_by=restart" in logs2[0], f"restart line: {logs2}")
snap = take(mon2, T0 + 100)
check(snap["outages"] == 1 and snap["down_seconds"] == 100 and snap["outage_active"] == 0, f"restored totals: {snap}")
check(snap["last"][3] == "upstream", "last outage restored from the ledger")
# A second outage, still open: the total includes its elapsed time.
feed(mon2, T0 + 200, bad(), GW)
feed(mon2, T0 + 205, bad(), GW)
snap = take(mon2, T0 + 230)
check(snap["outage_active"] == 1 and snap["outage_seconds"] == 30 and snap["down_seconds"] == 130 and snap["outages"] == 2,
      f"open outage must count in the totals: {snap}")

# internet_probe_up: 0 if any round since the last write failed.
mon, led, logs, d = make("up")
feed(mon, T0, good(), GW)
check(take(mon, T0 + 1)["up"] == 1, "all good rounds: up=1")
feed(mon, T0 + 5, bad(), GW)
feed(mon, T0 + 10, good(), GW)
check(take(mon, T0 + 11)["up"] == 0, "a blip within the window must show as up=0")
feed(mon, T0 + 15, good(), GW)
check(take(mon, T0 + 16)["up"] == 0, "up stays 0 for the whole window after a failed round, not just one write")
check(take(mon, T0 + 21)["up"] == 1, "up returns to 1 once the window has passed")

# The ledger is written only on transitions.
mon, led, logs, d = make("writes")
for i in range(200):
    feed(mon, T0 + 5 * i, good(), GW)
check(led.writes == 0, "good rounds must not write the ledger")
feed(mon, T0 + 1000, bad(), GW)
feed(mon, T0 + 1005, good(), GW)
check(led.writes == 0, "a blip must not write the ledger")
for i in range(10):
    feed(mon, T0 + 2000 + 5 * i, bad(), GW)
check(led.writes == 1, f"an outage start is one write, however long it lasts, got {led.writes}")
feed(mon, T0 + 2100, good(), GW)
check(led.writes == 2, f"an outage end is the second write, got {led.writes}")

# ---------------------------------------------------------- real sockets ---
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(5)
ok, rtt = probe.tcp_probe("127.0.0.1", srv.getsockname()[1], 1.0)
check(ok and rtt is not None and rtt >= 0, "tcp probe must succeed against a listening socket")
closed = socket.socket()
closed.bind(("127.0.0.1", 0))
closed_port = closed.getsockname()[1]
closed.close()
check(probe.tcp_probe("127.0.0.1", closed_port, 1.0)[0] is False, "a refused connect is a failed tcp probe")
ok, rtt = probe.tcp_probe("127.0.0.1", closed_port, 1.0, refused_is_up=True)
check(ok and rtt is not None, "ECONNREFUSED on the gateway probe must count as up")
srv.close()

# dns: tiny UDP responder; mode "good" echoes the ID with QR set, "wrong" flips the ID.
mode = {"v": "good"}
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("127.0.0.1", 0))
udp.settimeout(0.2)
stop = threading.Event()
seen = []


def responder():
    while not stop.is_set():
        try:
            data, addr = udp.recvfrom(512)
        except OSError:
            continue
        qid = struct.unpack(">H", data[:2])[0]
        seen.append(data)
        if mode["v"] == "silent":
            continue
        rid = qid if mode["v"] == "good" else (qid ^ 0xFFFF)
        flags = 0x8183 if mode["v"] == "good" else 0x8180  # QR set; NXDOMAIN is still a success
        udp.sendto(struct.pack(">HHHHHH", rid, flags, 1, 0, 0, 0) + data[12:], addr)


th = threading.Thread(target=responder, daemon=True)
th.start()
port = udp.getsockname()[1]
ok, rtt = probe.dns_probe("127.0.0.1", port, 1.0, "one.one.one.one")
check(ok and rtt is not None, "dns probe must succeed on a reply with the same ID and QR set (any RCODE)")
check(seen and seen[-1][2:4] == b"\x01\x00" and b"\x03one\x03one\x03one\x03one\x00" in seen[-1],
      "dns query must be RD=1 with the asked name")
mode["v"] = "wrong"
check(probe.dns_probe("127.0.0.1", port, 0.5, "one.one.one.one")[0] is False, "a reply with the wrong ID must fail")
mode["v"] = "silent"
check(probe.dns_probe("127.0.0.1", port, 0.3, "one.one.one.one")[0] is False, "no reply must fail")
stop.set()
th.join(1)
udp.close()

# run_round with real sockets: closed ports fail, gateway refusal is up.
res, gw = probe.run_round([("tcp", "127.0.0.1", closed_port)], f"127.0.0.1:{closed_port}", 0.5, "one.one.one.one")
check(res[("tcp", f"127.0.0.1:{closed_port}")][0] is False and gw[1] is True, f"run_round: {res} {gw}")
check(probe.default_gateway("Iface\tDestination\tGateway\tFlags\n"
                            "eth0\t00000000\t0100000A\t0003\neth0\t0000000A\t00000000\t0001\n") == "10.0.0.1",
      "default gateway parse")
check(probe.default_gateway("Iface\tDestination\tGateway\tFlags\neth0\t0000000A\t00000000\t0001\n") == "",
      "no default route gives an empty gateway")

# ---------------------------------------------------------------- render ---
mon, led, logs, d = make("render")
feed(mon, T0, good(), GW)
feed(mon, T0 + 5, bad(), GW)
feed(mon, T0 + 10, bad(), GW)
feed(mon, T0 + 15, good(), GW)
feed(mon, T0 + 20, {**good(), KEYS[1]: (False, None)}, ("10.0.0.1:80", False, None))
text = probe.render("home", take(mon, T0 + 21))
full = take(mon, T0 + 21)
full["gateway"] = GW
full["targets"][0]["rtt"] = 0.01
full["targets"][0]["up"] = 1
full["paths"] = [{"name": "p", "up": 1, "active": 0, "down_seconds": 0.0, "outages": 0,
                  "targets": [{"target": "h:1", "rtt": 0.001, "failures": 0}]}]
full["lan"] = [{"name": "n", "target": "h:1", "up": 1, "rtt": 0.001, "failures": 0, "active": 0,
                "down_seconds": 0.0, "outages": 0}]
full_text = probe.render("home", full)
for metric in probe.HELP:
    check(f"# HELP {metric} " in full_text and f"# TYPE {metric} " in full_text, f"{metric} lacks HELP or TYPE")
series = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
check(series and all('site="home"' in ln for ln in series), "every series must carry the site label")
check("internet_probe_target_rtt_seconds" in text and 'target="8.8.8.8:443"} ' in text, "target series present")
check(not any(ln.startswith("internet_probe_target_rtt_seconds") and "8.8.8.8:443" in ln for ln in series),
      "RTT must be omitted when the target failed")
check(not any(ln.startswith("internet_probe_gateway_rtt_seconds") for ln in series), "gateway RTT omitted when down")
check(any(ln.startswith("internet_probe_last_outage_duration_seconds{") and 'cause="upstream"' in ln for ln in series),
      "last outage series present with its cause")
fresh, _, _, _ = make("render-fresh")
ftext = probe.render("home", take(fresh, T0))
check("last_outage" not in ftext, "last-outage series omitted when there is no closed outage")
check(probe._esc('a"b\\c\nd') == 'a\\"b\\\\c\\nd', "label values must be escaped")
odd = probe.render('we"ird', take(fresh, T0))
check('site="we\\"ird"' in odd, "site label must be escaped")
for ln in text.splitlines():
    if ln and not ln.startswith("#"):
        check(re.fullmatch(r'[a-z_]+\{[^}]*\} -?[0-9.]+', ln) is not None, f"malformed series line: {ln}")
path = os.path.join(TMP, "atomic.prom")
probe.write_atomic(path, text)
check(open(path).read() == text and not [f for f in os.listdir(TMP) if ".tmp." in f], "atomic write leaves only the final file")

# --------------------------------------------------------------- reports ---
d = os.path.join(TMP, "report")
led = probe.Ledger(d)
DB = os.path.join(d, "outages.db")
NOW = dt.datetime(2026, 11, 27, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def at(day, h, m=0, s=0):
    return dt.datetime(2026, 11, day, h, m, s, tzinfo=dt.timezone.utc).timestamp()


db = sqlite3.connect(os.path.join(d, "outages.db"))
db.executemany("INSERT INTO outages(site,start,end,duration,cause,closed_by) VALUES('home',?,?,?,?,?)", [
    (at(25, 10), at(25, 10, 3, 5), 185, "upstream", "probe"),
    (at(26, 23, 59), at(27, 0, 2), 180, "local", "probe"),        # crosses midnight
    (at(27, 9), at(27, 9, 10), 600, "upstream", "restart"),
    (at(27, 11, 30), None, None, "local", None),                  # ongoing
])
db.commit()
db.close()
out = probe.report_outages(DB, 30, NOW)
check("ongoing" in out and "0:30:00" in out, f"ongoing outage with elapsed time: {out}")
check("2026-11-25 10:00:00" in out and "0:03:05" in out and "upstream" in out and "probe" in out, f"closed row: {out}")
check("restart" in out and "closed_by" in out.splitlines()[0], f"restart row: {out}")
check("2026-11-26" in probe.report_outages(DB, 2, NOW) and "2026-11-25" not in probe.report_outages(DB, 1, NOW),
      "--days limits the report")
daily = probe.report_daily(DB, 3, NOW)
lines = {ln.split()[0]: ln for ln in daily.splitlines()[1:]}
check(set(lines) == {"2026-11-25", "2026-11-26", "2026-11-27"}, f"daily days: {daily}")
check("0:03:05" in lines["2026-11-25"] and lines["2026-11-25"].split()[1] == "1", f"daily 25th: {lines['2026-11-25']}")
check("0:01:00" in lines["2026-11-26"], f"midnight crossing must split the downtime: {lines['2026-11-26']}")
check("0:42:00" in lines["2026-11-27"],
      f"daily 27th downtime: {lines['2026-11-27']}")
check("ongoing" in lines["2026-11-27"], "daily report flags the ongoing outage")
check(lines["2026-11-27"].split()[1] == "2", f"the 27th has two outages starting that day: {lines['2026-11-27']}")
try:
    probe.report_outages(os.path.join(TMP, "nodb", "outages.db"), 30, NOW)
    check(False, "a missing ledger must raise")
except FileNotFoundError:
    pass

# ------------------------------------------------- the real write logic ---
# Poller.step is the body of main()'s loop. Drive it with fake clocks.
def make_poller(name, confirm=2, every=15, write=None):
    d = os.path.join(TMP, name)
    logs, writes = [], []
    led = probe.Ledger(d)
    mon = fake(probe.Monitor("home", TARGETS, led, confirm, 5, log=logs.append, window=every))
    mon.startup(T0)
    pol = probe.Poller(mon, "home", "/nonexistent/x.prom", every, wall=lambda: T0 + mon._t,
                       write=write or (lambda path, text: writes.append((mon._t, text))), log=logs.append)
    return mon, pol, writes, logs


def val(text, metric):
    return int(re.search(rf"^{metric}\{{[^}}]*\}} (\d+)", text, re.M).group(1))


mon, pol, writes, logs = make_poller("poller")
plan = [good(), bad(), bad(), good()] + [good()] * 12   # rounds at 0, 5, 10, 15, ... 75 s
for i, res in enumerate(plan):
    mon._t = 5.0 * i
    pol.step(T0 + 5 * i, res, GW)
check(writes and writes[0][0] == 0, "the first textfile must be written right after the first round")
after_fail = [(m, t) for m, t in writes if 10 < m < 10 + 15]
check(after_fail, "a file must be written within textfile_every after the last failed round")
check(all(val(t, "internet_probe_up") == 0 for m, t in after_fail),
      f"up must be 0 in every file written within 15 s of the last failed round: {[(m, val(t, 'internet_probe_up')) for m, t in writes]}")
starts = [(m, t) for m, t in writes if val(t, "internet_probe_outage_active") == 1]
check(starts and starts[0][0] == 10 and val(starts[0][1], "internet_probe_up") == 0,
      "the outage-start write must show outage_active=1 and up=0")
check(any(m == 15 and val(t, "internet_probe_outage_active") == 0 and val(t, "internet_probe_up") == 0 for m, t in writes),
      "the outage-end write must be written at the good round, still with up=0")
check(all(val(t, "internet_probe_up") == 1 for m, t in writes if m >= 25), "up returns to 1 after the window")
check(take(mon, T0 + 24)["up"] == 0 and take(mon, T0 + 26)["up"] == 1, "the window is 15 s from the last failed round")
check(len(writes) < len(plan), "metrics are not written every round")

# A broken textfile path: one error line per 10 minutes, not one per round.
mon, pol, writes, logs = make_poller("poller-err", write=lambda path, text: (_ for _ in ()).throw(FileNotFoundError("no dir")))
attempts = []
orig_write = pol.write
pol.write = lambda path, text: (attempts.append(mon._t), orig_write(path, text))
for i in range(100):
    mon._t = 5.0 * i
    pol.step(T0 + 5 * i, good(), GW)
errs = [ln for ln in logs if ln.startswith("textfile_error ")]
check(len(errs) == 1, f"textfile_error must be logged once per 10 minutes, got {len(errs)}")
check(len(attempts) < 100 and attempts[0] == 0, f"a failing write is retried at textfile_every, not every round: {len(attempts)} attempts")
mon._t = 5.0 * 100 + 700
pol.step(T0 + mon._t, good(), GW)
check(len([ln for ln in logs if ln.startswith("textfile_error ")]) == 2, "the error is logged again after 10 minutes")

# ------------------------------------------------------------ clock steps ---
mon, led, logs, d = make("clockstep")
seen = []
mon._t = 0.0
mon.on_round(T0, bad(), GW)
mon._t = 5.0
mon.on_round(T0 + 5, bad(), GW)
seen.append(mon.snapshot(T0 + 5)["down_seconds"])
mon._t = 10.0
seen.append(mon.snapshot(T0 + 10)["down_seconds"])
# The wall clock now steps back 30 s while the monotonic clock goes on.
mon._t = 15.0
mon.on_round(T0 - 15, bad(), GW)
s2 = mon.snapshot(T0 - 15)
seen.append(s2["down_seconds"])
check(s2["outage_seconds"] == 15 and s2["down_seconds"] >= seen[1], f"the elapsed time follows the monotonic clock: {s2['outage_seconds']}")
mon._t = 20.0
mon.on_round(T0 - 10, good(), GW)
seen.append(mon.snapshot(T0 - 10)["down_seconds"])
r = rows(d)
check(r[0][3] == 20 and r[0][2] == r[0][1] + 20 and r[0][3] >= 0, f"stored duration must come from the monotonic clock: {r}")
check(seen == sorted(seen), f"down_seconds_total must never go down: {seen}")
# A counter that would drop (state rebuilt lower) is clamped to the last value exported.
mon.net.closed_seconds = 0.0
check(mon.snapshot(T0)["down_seconds"] >= seen[-1], "down_seconds_total never lower than the last value written")
# Restart closing with a wall clock behind the row's start: duration 0, never negative.
d2 = os.path.join(TMP, "clockstep-restart")
l2 = probe.Ledger(d2)
l2.insert("home", T0 + 100, "upstream")
m2 = fake(probe.Monitor("home", TARGETS, l2, 2, 5, log=lambda x: None))
m2.startup(T0 + 70)
r = rows(d2)
check(r[0][3] == 0 and r[0][2] == r[0][1] and r[0][5] == "restart", f"restart close is clamped: {r}")

# ----------------------------------------------------------- ledger errors ---
# A corrupt file: no crash, a ledger_error line, probing and metrics go on.
d = os.path.join(TMP, "corrupt")
os.makedirs(d)
with open(os.path.join(d, "outages.db"), "wb") as f:
    f.write(b"this is not a database" * 200)
logs = []
led = probe.open_ledger(d, "home", logs.append)
check(led is None and len(logs) == 1 and logs[0].startswith("ledger_error site=home op=open error="), f"corrupt db: {logs}")
mon = fake(probe.Monitor("home", TARGETS, led, 2, 5, log=logs.append))
mon.startup(T0)
for i, res in enumerate([bad(), bad(), bad(), good()]):
    feed(mon, T0 + 5 * i, res, GW)
check(mon.outages == 1 and mon.closed_seconds == 15 and mon.last[2] == 15, "outage state is kept in memory without a ledger")
check(val(probe.render("home", take(mon, T0 + 20)), "internet_probe_ledger_ok") == 0, "ledger_ok is 0 without a ledger")
check(val(probe.render("home", take(mon, T0 + 20)), "internet_probe_outages_total") == 1, "counters still exported")
import contextlib
import io
err = io.StringIO()
old_argv = sys.argv
sys.argv = ["internet-probe.py", "--data-dir", d, "--report", "outages"]
with contextlib.redirect_stderr(err):
    rc = probe.main()
sys.argv = old_argv
check(rc == 1 and "ledger" in err.getvalue(), f"--report on a corrupt ledger must fail cleanly: rc={rc} {err.getvalue()!r}")

# A locked file: errors are caught, rate-limited, state kept, and it recovers.
d = os.path.join(TMP, "locked")
logs = []
led = probe.Ledger(d, timeout=0.1)
mon = fake(probe.Monitor("home", TARGETS, led, 2, 5, log=logs.append))
mon.startup(T0)
check(mon.ledger_ok == 1, "ledger_ok is 1 on a healthy ledger")
other = sqlite3.connect(os.path.join(d, "outages.db"), isolation_level=None)
other.execute("BEGIN EXCLUSIVE")
for i, res in enumerate([bad(), bad(), bad(), good()]):
    feed(mon, T0 + 5 * i, res, GW)
errs = [ln for ln in logs if ln.startswith("ledger_error ")]
check(len(errs) == 1 and "op=insert" in errs[0] and "locked" in errs[0], f"one rate-limited ledger_error per op: {errs}")
check(mon.ledger_ok == 0 and mon.outages == 1 and mon.closed_seconds == 15, "locked ledger: state kept in memory, ledger_ok 0")
check(events(logs).count("outage_start") == 1 and events(logs).count("outage_end") == 1, "outage lines still logged")
other.execute("ROLLBACK")
other.close()
feed(mon, T0 + 1000, bad(), GW)
feed(mon, T0 + 1005, bad(), GW)
check(mon.ledger_ok == 1 and len(rows(d)) == 1 and rows(d)[0][2] is None, "the ledger works again once unlocked")
other = sqlite3.connect(os.path.join(d, "outages.db"), isolation_level=None)
other.execute("BEGIN EXCLUSIVE")
feed(mon, T0 + 1010, good(), GW)  # the close fails
feed(mon, T0 + 1015 + 700, bad(), GW)
feed(mon, T0 + 1020 + 700, bad(), GW)  # insert fails again, 10+ minutes later
other.execute("ROLLBACK")
other.close()
errs = [ln for ln in logs if ln.startswith("ledger_error ") and "op=health" not in ln]
check(len(errs) == 3 and any("op=close" in e for e in errs), f"the same op is logged again after 10 minutes: {errs}")

# ------------------------------------------------------------ http timeout ---
import http.server

heard = []


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        heard.append((self.path, self.headers["Host"]))
        self.send_response(204 if self.path == "/generate_204" else 200)
        self.end_headers()

    def log_message(self, *a):
        pass


httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
hp = httpd.server_address[1]
ok, rtt = probe.http_probe("127.0.0.1", hp, 1.0)
check(ok and rtt is not None and heard == [("/generate_204", f"127.0.0.1:{hp}")], f"http probe: 204 with a Host header: {ok} {heard}")
httpd.shutdown()
real_getaddrinfo = socket.getaddrinfo


def slow_getaddrinfo(host, *a, **k):
    if host == "slow.example":
        time.sleep(1.5)
    return real_getaddrinfo(host, *a, **k)


socket.getaddrinfo = slow_getaddrinfo
try:
    t0 = time.monotonic()
    check(probe.http_probe("slow.example", 80, 0.3)[0] is False and time.monotonic() - t0 < 1.0,
          "the http name lookup must not outlast --timeout")
    slots = {}
    t0 = time.monotonic()
    res, gw = probe.run_round([("tcp", "127.0.0.1", closed_port), ("http", "slow.example", 80)],
                              f"127.0.0.1:{closed_port}", 0.3, "one.one.one.one", slots)
    took = time.monotonic() - t0
    check(took < 1.2 and res[("http", "slow.example:80")] == (False, None), f"the round must not wait for a slow http lookup: {took:.2f}s {res}")
finally:
    socket.getaddrinfo = real_getaddrinfo
# While the previous http probe still runs, no new thread starts for it.
gate = threading.Event()
busy = threading.Thread(target=gate.wait, daemon=True)
busy.start()
key = ("http", "slow.example:80")
slot = {"thread": busy, "result": None}
slots = {key: slot}
before = threading.active_count()
res, gw = probe.run_round([("http", "slow.example", 80)], f"127.0.0.1:{closed_port}", 0.3, "one.one.one.one", slots)
check(slots[key] is slot and res[key] == (False, None) and threading.active_count() <= before,
      "a still-running http probe must be skipped, not stacked")
gate.set()

# ---------------------------------------------------------- LAN targets ---
LANS = [("opnsense", "10.0.0.114", 80), ("sw", "10.0.0.100", 80)]


def lan_good(**over):
    r = good()
    r[("lan", "opnsense")] = over.get("opnsense", (True, 0.002))
    r[("lan", "sw")] = over.get("sw", (True, 0.003))
    return r


def lan_rows(d):
    return sqlite3.connect(os.path.join(d, "outages.db")).execute(
        "SELECT site, name, target, start, end, duration, closed_by FROM lan_outages ORDER BY id").fetchall()


def make_lan(name, confirm=2):
    d = os.path.join(TMP, name)
    logs = []
    led = probe.Ledger(d)
    mon = fake(probe.Monitor("home", TARGETS, led, confirm, 5, log=logs.append, lan_targets=LANS))
    mon.startup(T0)
    return mon, led, logs, d


DOWN = (False, None)
mon, led, logs, d = make_lan("lan-blip")
for i, res in enumerate([lan_good(), lan_good(opnsense=DOWN), lan_good(), lan_good()]):
    feed(mon, T0 + 5 * i, res, GW)
check(events(logs) == ["lan_blip"] and "name=opnsense" in logs[0] and "target=10.0.0.114:80" in logs[0]
      and "duration_seconds=5" in logs[0] and led.writes == 0 and lan_rows(d) == [], f"a LAN blip is logged, not stored: {logs}")

mon, led, logs, d = make_lan("lan-outage")
chg = []
for i, res in enumerate([lan_good(), lan_good(opnsense=DOWN), lan_good(opnsense=DOWN), lan_good(opnsense=DOWN), lan_good()]):
    chg.append(feed(mon, T0 + 5 * i, res, GW))
    if i == 3:
        mid = take(mon, T0 + 15)
check(events(logs) == ["lan_down", "lan_up"], f"LAN events: {events(logs)}")
check(logs[0] == f"lan_down site=home name=opnsense target=10.0.0.114:80 start={probe._iso(T0 + 5)}", f"lan_down line: {logs[0]}")
check(logs[1] == (f"lan_up site=home name=opnsense target=10.0.0.114:80 start={probe._iso(T0 + 5)} "
                  f"end={probe._iso(T0 + 20)} duration_seconds=15 closed_by=probe"), f"lan_up line: {logs[1]}")
check(lan_rows(d) == [("home", "opnsense", "10.0.0.114:80", T0 + 5, T0 + 20, 15, "probe")], f"LAN ledger row: {lan_rows(d)}")
check(rows(d) == [] and led.writes == 2, "a LAN outage writes the LAN table only, twice (start, end)")
check(chg == [False, False, True, False, True], f"LAN transitions request an immediate write: {chg}")
check(mid["up"] == 1 and mid["outage_active"] == 0 and mid["failed_rounds"] == 0 and mid["outages"] == 0,
      "a LAN outage must never change the internet metrics")
lanm = {l["name"]: l for l in mid["lan"]}
check(lanm["opnsense"]["up"] == 0 and lanm["opnsense"]["active"] == 1 and lanm["sw"]["up"] == 1 and lanm["sw"]["active"] == 0,
      f"per-target LAN state: {lanm}")
text = probe.render("home", mid)
check('internet_probe_lan_outage_active{site="home",name="opnsense"} 1' in text and
      'internet_probe_lan_up{site="home",name="opnsense",target="10.0.0.114:80"} 0' in text, "LAN series rendered with name and target")
check(not any(ln.startswith("internet_probe_lan_rtt_seconds") and 'name="opnsense"' in ln for ln in text.splitlines()),
      "LAN RTT omitted when failed")
check(take(mon, T0 + 21)["lan"][0]["up"] == 0 and take(mon, T0 + 40)["lan"][0]["up"] == 1, "lan_up window is textfile_every")

# The internet failing does not touch the LAN state either.
mon, led, logs, d = make_lan("lan-independent")
for i in range(4):
    r = bad()
    r[("lan", "opnsense")] = (True, 0.001)
    r[("lan", "sw")] = (True, 0.001)
    feed(mon, T0 + 5 * i, r, GW)
check(events(logs) == ["outage_start"] and lan_rows(d) == [], "an internet outage must not open a LAN outage")

# Restart closes an open LAN row; counters restored per name.
mon, led, logs, d = make_lan("lan-restart")
for i, res in enumerate([lan_good(), lan_good(sw=DOWN), lan_good(sw=DOWN), lan_good(sw=DOWN), lan_good(),
                         lan_good(opnsense=DOWN), lan_good(opnsense=DOWN), lan_good(opnsense=DOWN)]):
    feed(mon, T0 + 5 * i, res, GW)
check([r[1] for r in lan_rows(d)] == ["sw", "opnsense"] and lan_rows(d)[1][4] is None, f"one closed, one open: {lan_rows(d)}")
led2 = probe.Ledger(d)
logs2 = []
mon2 = fake(probe.Monitor("home", TARGETS, led2, 2, 5, log=logs2.append, lan_targets=LANS))
mon2.startup(T0 + 100)
r = lan_rows(d)
check(r[1][4] == T0 + 100 and r[1][5] == 75 and r[1][6] == "restart" and r[0][6] == "probe", f"restart closes the open LAN row: {r}")
check(len(logs2) == 1 and logs2[0].startswith("lan_up ") and "closed_by=restart" in logs2[0] and "name=opnsense" in logs2[0], f"restart line: {logs2}")
t2 = {l["name"]: l for l in take(mon2, T0 + 100)["lan"]}
check(t2["sw"]["outages"] == 1 and t2["sw"]["down_seconds"] == 15 and t2["opnsense"]["outages"] == 1
      and t2["opnsense"]["down_seconds"] == 75, f"LAN counters restored per name: {t2}")
for i, res in enumerate([lan_good(sw=DOWN), lan_good(sw=DOWN)]):
    feed(mon2, T0 + 200 + 5 * i, res, GW)
t3 = {l["name"]: l for l in take(mon2, T0 + 230)["lan"]}
check(t3["sw"]["outages"] == 2 and t3["sw"]["down_seconds"] == 15 + 30 and t3["opnsense"]["down_seconds"] == 75,
      f"the open LAN outage counts in its own total only: {t3}")

# Clock step and ledger errors follow the same rules as the internet.
mon, led, logs, d = make_lan("lan-clock")
mon._t = 0
mon.on_round(T0, lan_good(sw=DOWN), GW)
mon._t = 5
mon.on_round(T0 + 5, lan_good(sw=DOWN), GW)
mon._t = 10
mon.on_round(T0 - 20, lan_good(), GW)
r = lan_rows(d)
check(r[0][5] == 10 and r[0][5] >= 0 and r[0][4] == r[0][3] + 10, f"LAN duration from the monotonic clock: {r}")
d = os.path.join(TMP, "lan-locked")
logs = []
led = probe.Ledger(d, timeout=0.1)
mon = fake(probe.Monitor("home", TARGETS, led, 2, 5, log=logs.append, lan_targets=LANS))
mon.startup(T0)
other = sqlite3.connect(os.path.join(d, "outages.db"), isolation_level=None)
other.execute("BEGIN EXCLUSIVE")
for i, res in enumerate([lan_good(sw=DOWN), lan_good(sw=DOWN), lan_good()]):
    feed(mon, T0 + 5 * i, res, GW)
check(mon.ledger_ok == 0 and any("op=lan_insert" in ln for ln in logs) and events(logs).count("lan_down") == 1
      and events(logs).count("lan_up") == 1, f"a locked ledger does not stop the LAN machine: {logs}")
other.execute("ROLLBACK")
other.close()
# ledger_ok recovers without a transition, and a healthy ledger is checked rarely.
for i in range(3, 12):
    feed(mon, T0 + 5 * i, lan_good(), GW)
check(mon.ledger_ok == 0, "no health check before 60 s")
for i in range(12, 16):
    feed(mon, T0 + 5 * i, lan_good(), GW)
check(mon.ledger_ok == 1, "ledger_ok must recover by itself after the lock is gone")
calls = []
led.health = lambda: calls.append(1)
for i in range(16, 16 + 100):
    feed(mon, T0 + 5 * i, lan_good(), GW)
check(len(calls) <= 1, f"a healthy ledger is checked at most every 10 minutes: {len(calls)} checks in 500 s")

# Bounded probes: real sockets, refused is up, one in flight per target.
srv2 = socket.socket()
srv2.bind(("127.0.0.1", 0))
srv2.listen(1)
res, gw = probe.run_round([("tcp", "127.0.0.1", closed_port)], f"127.0.0.1:{closed_port}", 0.5, "one.one.one.one", {},
                          [("up", "127.0.0.1", srv2.getsockname()[1]), ("refused", "127.0.0.1", closed_port),
                           ("gone", "nonexistent.invalid", 80)])
check(res[("lan", "up")][0] and res[("lan", "refused")][0] and res[("lan", "refused")][1] is not None,
      f"a listening or refusing LAN target is up: {res}")
check(res[("lan", "gone")] == (False, None), "an unresolvable LAN target is down")
srv2.close()
gate = threading.Event()
busy = threading.Thread(target=gate.wait, daemon=True)
busy.start()
slot = {"thread": busy, "result": None}
slots = {("lan", "x"): slot}
before = threading.active_count()
res, gw = probe.run_round([], f"127.0.0.1:{closed_port}", 0.3, "one.one.one.one", slots, [("x", "127.0.0.1", closed_port)])
check(slots[("lan", "x")] is slot and res[("lan", "x")] == (False, None) and threading.active_count() <= before,
      "a LAN probe still running is skipped, not stacked")
gate.set()

# --report lan on a fixture, with an ongoing outage.
d = os.path.join(TMP, "lan-report")
probe.Ledger(d)
db = sqlite3.connect(os.path.join(d, "outages.db"))
db.executemany("INSERT INTO lan_outages(site,name,target,start,end,duration,closed_by) VALUES('home',?,?,?,?,?,?)", [
    ("er605", "10.0.0.1:80", at(25, 10), at(25, 10, 5), 300, "probe"),
    ("er605", "10.0.0.1:80", at(27, 9), at(27, 9, 10), 600, "restart"),
    ("sw", "10.0.0.100:80", at(27, 11, 30), None, None, None),
    ("old", "10.0.0.9:22", at(1, 10), at(1, 10, 1), 60, "probe"),
])
db.commit()
db.close()
DBL = os.path.join(d, "outages.db")
out = probe.report_lan(DBL, 7, NOW)
check("availability_%" in out and "ongoing" in out and "0:30:00" in out, f"lan report: {out}")
er = [ln for ln in out.splitlines() if ln.startswith("er605")][0].split()
check(er[2] == "2" and er[3] == "0:15:00", f"er605: 2 outages, 15 min down: {er}")
check(er[4] == f"{100 * (1 - 900 / (7 * 86400)):.3f}", f"availability over the window: {er}")
check("old" not in out, "outages older than --days are left out")
check("restart" in out and "probe" in out, "closed_by is listed")
probe.Ledger(os.path.join(TMP, "lan-report-empty"))
check("no LAN outages in the last" in probe.report_lan(os.path.join(TMP, "lan-report-empty", "outages.db"), 7, NOW), "an empty ledger says so")
d0 = os.path.join(TMP, "lan-report-oldschema")
os.makedirs(d0)
c = sqlite3.connect(os.path.join(d0, "outages.db"))
c.execute("CREATE TABLE outages(id INTEGER PRIMARY KEY)")
c.commit()
c.close()
check("no LAN outages recorded" in probe.report_lan(os.path.join(d0, "outages.db"), 7, NOW), "a ledger without the LAN table reads empty")
err = io.StringIO()
sys.argv = ["internet-probe.py", "--data-dir", d, "--report", "lan", "--days", "7"]
out_buf = io.StringIO()
with contextlib.redirect_stdout(out_buf):
    rc = probe.main()
sys.argv = old_argv
check(rc == 0 and "name" in out_buf.getvalue(), "--report lan runs through main()")

# The flag parser.
check(probe.parse_lan_target("er605=10.10.0.1:80") == ("er605", "10.10.0.1", 80), "parse a LAN target")
check(probe.parse_lan_target("a-b-1=host.example:443") == ("a-b-1", "host.example", 443), "parse a hostname target")
for bad_spec in ["Bad_Name=1.2.3.4:80", "=1.2.3.4:80", "noequals", "x=hostonly", "x=h:notaport", "UPPER=1.2.3.4:80"]:
    try:
        probe.parse_lan_target(bad_spec)
        check(False, f"{bad_spec!r} must be rejected")
    except ValueError:
        pass
for argv in (["--site", "t", "--lan-target", "Bad Name=1.2.3.4:80"], ["--site", "t", "--lan-target", "a=1.1.1.1:1", "--lan-target", "a=2.2.2.2:2"]):
    sys.argv = ["internet-probe.py", "--data-dir", os.path.join(TMP, "badflag")] + argv
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            probe.main()
        check(False, f"main() must reject {argv}")
    except SystemExit as e:
        check(e.code == 2 and "lan-target" in err.getvalue(), f"argparse rejects {argv}: {e.code} {err.getvalue()!r}")
    sys.argv = old_argv

# ------------------------------------------- role validation (rendered) ---
import jinja2

TASKS_TEXT = (ROLE / "tasks" / "main.yml").read_text()
assert_block = TASKS_TEXT.split("Check the per-host settings", 1)[1].split("- name:", 1)[0]
conds = re.findall(r"^\s+- (internet_probe_\S.*)$", assert_block, re.M)
jenv = jinja2.Environment()
jenv.tests["match"] = lambda v, pat: re.match(pat, v) is not None
jenv.filters["regex_replace"] = lambda v, a, b="": re.sub(a, b, v)
jenv.tests["regex"] = lambda v, pat: re.search(pat, v) is not None
jenv.filters["select"] = lambda seq, test, *a: [x for x in seq if jenv.tests[test](x, *a)]
jenv.filters["map"] = lambda seq, f, *a: [jenv.filters[f](x, *a) for x in seq]


def accepts(site, lans, paths=()):
    return all(jenv.compile_expression(c)(internet_probe_site=site, internet_probe_lan_targets=list(lans),
                                          internet_probe_paths=list(paths)) for c in conds)


check(len(conds) == 5, f"the settings assert must have the site, two LAN and two path conditions: {conds}")
check(accepts("home", []) and accepts("home", ["er605=10.10.0.1:80", "tailnet-x=100.1.1.1:22", "a=host.example:443"]),
      "valid LAN targets must pass the role assert")
for bad_lan in (["Bad=1.2.3.4:80"], ["noequals"], ["x=hostonly"], ["x=h:port"], ["x=1.2.3.4:80", "x=5.6.7.8:80"],
                ["x=1.2.3.4 :80"], ["=1.2.3.4:80"], ["a b=1.2.3.4:80"]):
    check(not accepts("home", bad_lan), f"the role assert must reject {bad_lan}")
check(not accepts("Home", []), "the role assert must reject a bad site")
# The same strings the role accepts are the ones the script accepts, and the reverse for the bad ones.
for good_lan in ["er605=10.10.0.1:80", "a=host.example:443"]:
    probe.parse_lan_target(good_lan)
check("until:" in TASKS_TEXT and "internet_probe_marker" in TASKS_TEXT and
      TASKS_TEXT.index("internet_probe_marker") < TASKS_TEXT.index("flush_handlers") < TASKS_TEXT.index("until:"),
      "the post-start check must wait for a timestamp newer than a marker taken before the restart")
until_expr = TASKS_TEXT.split("until:", 1)[1].split("retries", 1)[0]
check("internet_probe_timestamp_seconds" in until_expr and re.search(r"> \(internet_probe_marker\.stdout \| float\)", until_expr),
      "the wait condition must compare internet_probe_timestamp_seconds with the marker taken before the restart")

# ------------------------------------------------------------ named paths ---
PATHS = [("wan1", [("tcp", "1.0.0.1", 443), ("dns", "8.8.4.4", 53)]), ("wan2", [("tcp", "149.112.112.112", 443)])]


def pres(w1=(True, True), w2=True, internet=True):
    """A round: w1 is (tcp ok, dns ok) for path wan1, w2 the single target of wan2."""
    r = good() if internet else bad()
    r[("path", "wan1", "tcp", "1.0.0.1:443")] = (w1[0], 0.01 if w1[0] else None)
    r[("path", "wan1", "dns", "8.8.4.4:53")] = (w1[1], 0.02 if w1[1] else None)
    r[("path", "wan2", "tcp", "149.112.112.112:443")] = (w2, 0.03 if w2 else None)
    return r


def path_rows(d):
    return sqlite3.connect(os.path.join(d, "outages.db")).execute(
        "SELECT site, name, start, end, duration, closed_by FROM path_outages ORDER BY id").fetchall()


def make_path(name, confirm=2):
    d = os.path.join(TMP, name)
    logs = []
    led = probe.Ledger(d)
    mon = fake(probe.Monitor("home", TARGETS, led, confirm, 5, log=logs.append, paths=PATHS))
    mon.startup(T0)
    return mon, led, logs, d


NO = (False, False)
# Up when ANY target answers; down only when all fail.
mon, led, logs, d = make_path("path-anyof")
for i, res in enumerate([pres(), pres(w1=(True, False)), pres(w1=(False, True)), pres(w1=(True, False)), pres()]):
    feed(mon, T0 + 5 * i, res, GW)
check(events(logs) == [] and path_rows(d) == [] and led.writes == 0, f"one answering target keeps a path up: {logs}")
# Blip, then outage.
mon, led, logs, d = make_path("path-blip")
for i, res in enumerate([pres(), pres(w1=NO), pres(), pres()]):
    feed(mon, T0 + 5 * i, res, GW)
check(events(logs) == ["path_blip"] and logs[0] == f"path_blip site=home name=wan1 start={probe._iso(T0 + 5)} duration_seconds=5"
      and path_rows(d) == [] and led.writes == 0, f"a path blip is logged, not stored: {logs}")
mon, led, logs, d = make_path("path-outage")
chg = []
for i, res in enumerate([pres(), pres(w1=NO), pres(w1=NO), pres(w1=NO), pres()]):
    chg.append(feed(mon, T0 + 5 * i, res, GW))
    if i == 3:
        mid = take(mon, T0 + 15)
check(events(logs) == ["path_down", "path_up"], f"path events: {events(logs)}")
check(logs[0] == f"path_down site=home name=wan1 start={probe._iso(T0 + 5)}", f"path_down line: {logs[0]}")
check(logs[1] == (f"path_up site=home name=wan1 start={probe._iso(T0 + 5)} end={probe._iso(T0 + 20)} "
                  f"duration_seconds=15 closed_by=probe"), f"path_up line: {logs[1]}")
check(path_rows(d) == [("home", "wan1", T0 + 5, T0 + 20, 15, "probe")] and rows(d) == [] and led.writes == 2,
      f"path ledger row, internet table untouched: {path_rows(d)}")
check(chg == [False, False, True, False, True], f"path transitions request an immediate write: {chg}")
check(mid["up"] == 1 and mid["outage_active"] == 0 and mid["failed_rounds"] == 0 and mid["outages"] == 0,
      "a path outage must never change the internet metrics")
pm = {x["name"]: x for x in mid["paths"]}
check(pm["wan1"]["up"] == 0 and pm["wan1"]["active"] == 1 and pm["wan2"]["up"] == 1 and pm["wan2"]["active"] == 0,
      f"per-path state: {pm}")
text = probe.render("home", mid)
check('internet_probe_path_up{site="home",name="wan1"} 0' in text and 'internet_probe_path_outage_active{site="home",name="wan1"} 1' in text
      and 'internet_probe_path_failures_total{site="home",name="wan1",target="1.0.0.1:443"} 3' in text, "path series rendered")
check(not any(ln.startswith("internet_probe_path_rtt_seconds") and 'name="wan1"' in ln for ln in text.splitlines())
      and 'internet_probe_path_rtt_seconds{site="home",name="wan2",target="149.112.112.112:443"}' in text,
      "path RTT omitted per failed target only")
check(take(mon, T0 + 21)["paths"][0]["up"] == 0 and take(mon, T0 + 40)["paths"][0]["up"] == 1, "path_up window is textfile_every")
# The internet failing does not open a path outage, and the reverse.
mon, led, logs, d = make_path("path-independent")
for i in range(4):
    feed(mon, T0 + 5 * i, pres(internet=False), GW)
check(events(logs) == ["outage_start"] and path_rows(d) == [], "an internet outage must not open a path outage")
# Restart closes an open path row; counters restored per name.
mon, led, logs, d = make_path("path-restart")
seq = [pres(), pres(w2=False), pres(w2=False), pres(w2=False), pres(),
       pres(w1=NO), pres(w1=NO), pres(w1=NO)]
for i, res in enumerate(seq):
    feed(mon, T0 + 5 * i, res, GW)
led2 = probe.Ledger(d)
logs2 = []
mon2 = fake(probe.Monitor("home", TARGETS, led2, 2, 5, log=logs2.append, paths=PATHS))
mon2.startup(T0 + 100)
r = path_rows(d)
check([x[1] for x in r] == ["wan2", "wan1"] and r[1][3] == T0 + 100 and r[1][4] == 75 and r[1][5] == "restart" and r[0][5] == "probe",
      f"restart closes the open path row: {r}")
check(len(logs2) == 1 and logs2[0].startswith("path_up ") and "closed_by=restart" in logs2[0] and "name=wan1" in logs2[0], f"restart line: {logs2}")
pm = {x["name"]: x for x in take(mon2, T0 + 100)["paths"]}
check(pm["wan2"]["outages"] == 1 and pm["wan2"]["down_seconds"] == 15 and pm["wan1"]["outages"] == 1 and pm["wan1"]["down_seconds"] == 75,
      f"path counters restored per name: {pm}")
for i in range(2):
    feed(mon2, T0 + 200 + 5 * i, pres(w2=False), GW)
pm = {x["name"]: x for x in take(mon2, T0 + 230)["paths"]}
check(pm["wan2"]["outages"] == 2 and pm["wan2"]["down_seconds"] == 15 + 30 and pm["wan1"]["down_seconds"] == 75,
      f"the open path outage counts in its own total: {pm}")
# Clock step: duration from the monotonic clock.
mon, led, logs, d = make_path("path-clock")
mon._t = 0
mon.on_round(T0, pres(w2=False), GW)
mon._t = 5
mon.on_round(T0 + 5, pres(w2=False), GW)
mon._t = 10
mon.on_round(T0 - 20, pres(), GW)
r = path_rows(d)
check(r[0][4] == 10 and r[0][3] == r[0][2] + 10, f"path duration from the monotonic clock: {r}")
# Real sockets: path probes run through run_round, bounded one per target.
res, gw = probe.run_round([("tcp", "127.0.0.1", closed_port)], f"127.0.0.1:{closed_port}", 0.5, "one.one.one.one", {}, (),
                          [("p", [("tcp", "127.0.0.1", closed_port), ("tcp", "nonexistent.invalid", 80)])])
check(res[("path", "p", "tcp", f"127.0.0.1:{closed_port}")] == (False, None) and res[("path", "p", "tcp", "nonexistent.invalid:80")] == (False, None),
      f"a closed port is a failed path target (refused is not up for a path): {res}")
gate = threading.Event()
busy = threading.Thread(target=gate.wait, daemon=True)
busy.start()
pk = ("path", "p", "tcp", f"127.0.0.1:{closed_port}")
slot = {"thread": busy, "result": None}
slots = {pk: slot}
before = threading.active_count()
res, gw = probe.run_round([], f"127.0.0.1:{closed_port}", 0.3, "one.one.one.one", slots, (), [("p", [("tcp", "127.0.0.1", closed_port)])])
check(slots[pk] is slot and res[pk] == (False, None) and threading.active_count() <= before, "a path probe still running is skipped, not stacked")
gate.set()
# --report paths on a fixture, with an ongoing outage.
d = os.path.join(TMP, "path-report")
probe.Ledger(d)
db = sqlite3.connect(os.path.join(d, "outages.db"))
db.executemany("INSERT INTO path_outages(site,name,start,end,duration,closed_by) VALUES('home',?,?,?,?,?)", [
    ("wan1", at(25, 10), at(25, 10, 5), 300, "probe"),
    ("wan1", at(27, 9), at(27, 9, 10), 600, "restart"),
    ("wan2", at(27, 11, 30), None, None, None),
    ("old", at(1, 10), at(1, 10, 1), 60, "probe"),
])
db.commit()
db.close()
out = probe.report_paths(os.path.join(d, "outages.db"), 7, NOW)
w1 = [ln for ln in out.splitlines() if ln.startswith("wan1")][0].split()
check("availability_%" in out and "ongoing" in out and "0:30:00" in out and "old" not in out and "target" not in out, f"paths report: {out}")
check(w1[1] == "2" and w1[2] == "0:15:00" and w1[3] == f"{100 * (1 - 900 / (7 * 86400)):.3f}", f"wan1 row: {w1}")
check("no path outages recorded" in probe.report_paths(os.path.join(TMP, "lan-report-oldschema", "outages.db"), 7, NOW), "a ledger without the path table reads empty")
sys.argv = ["internet-probe.py", "--data-dir", d, "--report", "paths"]
out_buf = io.StringIO()
with contextlib.redirect_stdout(out_buf):
    rc = probe.main()
sys.argv = old_argv
check(rc == 0 and "wan1" in out_buf.getvalue(), "--report paths runs through main()")
# Flag parser.
check(probe.parse_path("wan1=tcp:1.0.0.1:443,dns:8.8.4.4:53") == ("wan1", [("tcp", "1.0.0.1", 443), ("dns", "8.8.4.4", 53)]), "parse a path")
for bad_spec in ["Wan=tcp:1.1.1.1:1", "w=", "w", "w=http:h:80", "w=ftp:h:1", "w=tcp:h", "w=tcp:h:x", "=tcp:h:1", "w=tcp:h:1,"]:
    try:
        probe.parse_path(bad_spec)
        check(False, f"{bad_spec!r} must be rejected")
    except ValueError:
        pass
for argv in (["--site", "t", "--path", "W=tcp:1.1.1.1:1"], ["--site", "t", "--path", "a=tcp:1.1.1.1:1", "--path", "a=dns:2.2.2.2:53"]):
    sys.argv = ["internet-probe.py", "--data-dir", os.path.join(TMP, "badflag")] + argv
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            probe.main()
        check(False, f"main() must reject {argv}")
    except SystemExit as e:
        check(e.code == 2 and "--path" in err.getvalue(), f"argparse rejects {argv}: {e.code} {err.getvalue()!r}")
    sys.argv = old_argv
# Role validation.
GOOD_PATHS = ["wan1=tcp:1.0.0.1:443,dns:8.8.4.4:53", "wan2=dns:208.67.222.222:53"]
check(accepts("home", [], GOOD_PATHS) and accepts("home", [], []), "valid paths pass the role assert")
for bad_p in (["Wan=tcp:1.1.1.1:1"], ["w=http:h:80"], ["w=tcp:h"], ["w=tcp:h:1,ftp:h:2"], ["w=tcp:h:1,"], ["w="], ["w"],
              ["a=tcp:h:1", "a=dns:h:53"], ["a b=tcp:h:1"], ["a=tcp:h:1 dns:h:2"]):
    check(not accepts("home", [], bad_p), f"the role assert must reject {bad_p}")
for gp in GOOD_PATHS:
    probe.parse_path(gp)
tmpl = (ROLE / "templates" / "internet-probe.service.j2").read_text()
check("--path {{ t }}" in tmpl and "internet_probe_paths" in (ANSIBLE / "docs" / "reference" / "variables.md").read_text(), "the paths variable is documented and in the unit")

# ------------------------------------------------------------- role tasks ---
tasks = (ROLE / "tasks" / "main.yml").read_text()
removal = tasks.split("Remove the internet probe where it is not enabled", 1)[1]
check("{{ internet_probe_textfile }}" in removal and "daemon_reload: true" in removal,
      "the removal block must delete the metrics file and reload systemd")
check("REPLACES" in (ROLE / "defaults" / "main.yml").read_text() and "setting any replaces the built-in set" in
      (ANSIBLE / "docs" / "reference" / "variables.md").read_text(), "targets docs must say they replace the built-in set")

# ------------------------------------------------------------------ unit ---
unit = (ROLE / "templates" / "internet-probe.service.j2").read_text()
check(re.search(r"^CapabilityBoundingSet=\s*$", unit, re.M) is not None, "the unit must have an empty CapabilityBoundingSet")
check("ExecStart=/usr/bin/python3 " in unit, "the unit must use /usr/bin/python3")
check("venv" not in unit and "LoadCredential" not in unit, "no virtualenv and no credential")
check(not re.search(r"(?i)password|secret|token|api[_-]?key", unit), "the unit must carry no secrets")
check(unit.startswith("# Managed by ansible/roles/internet_probe. Do not edit by hand."), "template header")
check("MemoryMax=64M" in unit and "Restart=always" in unit and "StateDirectory=" in unit, "unit limits")

# ------------------------------------------------------------------ docs ---
defaults = set(re.findall(rf"^({PREFIX}\w+):", (ROLE / "defaults" / "main.yml").read_text(), re.M))
variables = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
documented = set(re.findall(rf"^\| `({PREFIX}\w+)`", variables, re.M))
check(defaults == documented, f"variables.md and defaults differ: {sorted(defaults ^ documented)}")
check("import_playbook: internet-probe.yml" in (ANSIBLE / "playbooks" / "site.yml").read_text(), "playbook missing from site.yml")
playbook = (ANSIBLE / "playbooks" / "internet-probe.yml").read_text()
check("gather_facts: false" in playbook, "the playbook must set gather_facts: false")
check("`internet-probe.yml`" in (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text(), "playbook missing from playbooks.md")
check("internet_probe_lan_targets" in variables and "internet_probe_lan_targets" in
      (ROLE / "templates" / "internet-probe.service.j2").read_text() and "--lan-target" in
      (ROLE / "templates" / "internet-probe.service.j2").read_text(), "the LAN targets variable is documented and in the unit")
check("`internet-probe.yml`" in (ANSIBLE / "README.md").read_text(), "playbook missing from the ansible README")
metrics_doc = (REPO / "monitoring" / "docs" / "reference" / "metrics.md").read_text()
for metric in probe.HELP:
    check(f"`{metric}" in metrics_doc, f"{metric} is not in monitoring/docs/reference/metrics.md")

script_src = SCRIPT.read_text()
parser_flags = set(re.findall(r'add_argument\("(--[a-z-]+)"', script_src))
playbooks_doc = (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text()
role_section = playbooks_doc.split("### `internet_probe`", 1)[1].split("\n### ", 1)[0]
metrics_section = metrics_doc.split("## Internet probe", 1)[1].split("\n## ", 1)[0]
check("policy route" in role_section and "failover" in role_section and "one WAN" in role_section and "distinct" in role_section,
      "playbooks.md must explain how a path measures one WAN (policy route, failover off, distinct targets, one WAN case)")
for flag in set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", role_section + metrics_section)):
    check(flag in parser_flags, f"the docs show {flag}, which the poller does not accept")
for flag in set(re.findall(r"^  (--[a-z-]+)", unit, re.M)):
    check(flag in parser_flags, f"the unit passes {flag}, which the poller does not accept")
report_choices = set(re.search(r'"--report", choices=\[([^\]]+)\]', script_src).group(1).replace('"', "").replace(" ", "").split(","))
shown = set(re.findall(r"--report (\w+)", role_section))
check(shown and shown <= report_choices, f"documented --report values not accepted: {shown - report_choices}")
for kind in report_choices:
    check(f"--report {kind}" in role_section, f"--report {kind} is not documented in playbooks.md")
check("sudo /usr/bin/python3 /opt/internet-probe/internet-probe.py --data-dir /var/lib/internet-probe --report outages" in role_section,
      "the documented --report command line must match the role's paths")

if DASHBOARD.exists():
    dash = json.loads(DASHBOARD.read_text())
    queried = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            queried.update(re.findall(r"\b(internet_probe_\w+)", o))

    walk(dash)
    check(queried <= set(probe.HELP), f"dashboard queries unknown metrics: {sorted(queried - set(probe.HELP))}")
    check(dash.get("uid") == "internet-usage", "dashboard uid")
else:
    print("note: monitoring/grafana/dashboards/fleet/internet-usage.json is absent; dashboard check skipped")

if errors:
    print("FAIL")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("ok: internet_probe")
