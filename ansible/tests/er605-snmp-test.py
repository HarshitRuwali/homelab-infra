#!/usr/bin/env python3
"""Test roles/er605_snmp: its poller against a canned snmp_exporter body, the
compose file and tasks that deploy it, and the docs and dashboards that
describe it.

Standard library only; run from anywhere:

    python3 -I ansible/tests/er605-snmp-test.py

Checks:
  * build_sample(): the eight original counters, CPU per core, memory states,
    uptime, interface state and the PPPoE session are read by ifName and
    description; an answer without the original counters is an error
  * render(): the original metric names and labels are unchanged, every metric
    has HELP and TYPE, a failed poll writes status only
  * counter reset: a counter that went backwards books the new value, never a
    huge number
  * the hourly ledger: every byte of the WAN counters is booked once, across
    hours, a reset and a restart of the poller; one usage line per closed hour,
    none for the hour in progress, none replayed after a restart
  * PPPoE reconnect (last change moved, or session counters reset) and router
    reboot (uptime went down) are detected once, with the previous session
    length; a reboot is not a reconnect
  * import-csv is idempotent and never overwrites an hour the live poller booked
  * the raw CSV days: older than 2 days gzipped, past keep_days deleted,
    unrelated files left alone, gzipped days still readable
  * --report hour|day|month|reconnects print local time (IST)
  * the role never templates, copies or prints the auth file; the compose file
    drops every capability, is read-only and sets no-new-privileges for every
    service; the manual-container check names the exact command
  * every default has a row in docs/reference/variables.md and the reverse; the
    playbook is in site.yml, playbooks.md and the README; every metric the
    poller writes is in monitoring's metrics reference; every flag the docs show
    exists in the poller; every er605_ metric the dashboards query is one the
    poller writes; the dashboards select the container the compose file names
"""
import contextlib
import datetime as dt
import gzip
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import tempfile

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
REPO = ANSIBLE.parent
ROLE = ANSIBLE / "roles" / "er605_snmp"
SCRIPT = ROLE / "files" / "er605-poll.py"
DASH_DIR = REPO / "monitoring" / "grafana" / "dashboards" / "fleet"
PREFIX = "er605_snmp_"
errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


sys.dont_write_bytecode = True  # no __pycache__ next to the role's files
spec = importlib.util.spec_from_file_location("poller", SCRIPT)
poller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poller)

# A canned snmp_exporter answer in the shapes the ER605 gives. Made-up numbers.
BODY = """\
# HELP ifHCInOctets The total number of octets received on the interface
# TYPE ifHCInOctets counter
ifHCInOctets{ifAlias="",ifDescr="default/eth0",ifIndex="2",ifName="default/eth0"} 3.38e+11
ifHCInOctets{ifAlias="",ifDescr="default/eth1",ifIndex="3",ifName="default/eth1"} 2.9e+11
ifHCInOctets{ifAlias="",ifDescr="default/pe-wan1_poe",ifIndex="16",ifName="default/pe-wan1_poe"} 349674107
ifHCInOctets{ifAlias="",ifDescr="inf.vlan10",ifIndex="20",ifName="inf.vlan10"} 5
# TYPE ifHCOutOctets counter
ifHCOutOctets{ifAlias="",ifDescr="default/eth0",ifIndex="2",ifName="default/eth0"} 2.9e+11
ifHCOutOctets{ifAlias="",ifDescr="default/eth1",ifIndex="3",ifName="default/eth1"} 3.4e+11
ifHCOutOctets{ifAlias="",ifDescr="default/pe-wan1_poe",ifIndex="16",ifName="default/pe-wan1_poe"} 12582531
ifInErrors{ifAlias="",ifDescr="default/eth0",ifIndex="2",ifName="default/eth0"} 113
ifInErrors{ifAlias="",ifDescr="default/eth1",ifIndex="3",ifName="default/eth1"} 0
ifInDiscards{ifAlias="",ifDescr="default/eth0",ifIndex="2",ifName="default/eth0"} 0
ifInDiscards{ifAlias="",ifDescr="default/eth1",ifIndex="3",ifName="default/eth1"} 1257
ifOutDiscards{ifAlias="",ifDescr="default/eth1",ifIndex="3",ifName="default/eth1"} 7
ifOperStatus{ifAlias="",ifDescr="default/eth0",ifIndex="2",ifName="default/eth0"} 1
ifOperStatus{ifAlias="",ifDescr="default/eth1",ifIndex="3",ifName="default/eth1"} 1
ifOperStatus{ifAlias="",ifDescr="default/pe-wan1_poe",ifIndex="16",ifName="default/pe-wan1_poe"} 2
ifLastChange{ifAlias="",ifDescr="default/eth1",ifIndex="3",ifName="default/eth1"} 0
ifLastChange{ifAlias="",ifDescr="default/pe-wan1_poe",ifIndex="16",ifName="default/pe-wan1_poe"} 1.12938688e+08
hrProcessorLoad{hrDeviceDescr="",hrDeviceIndex="768",hrDeviceType="1.3.6.1.2.1.25.3.1.3"} 4
hrProcessorLoad{hrDeviceDescr="",hrDeviceIndex="769",hrDeviceType="1.3.6.1.2.1.25.3.1.3"} 5
hrProcessorLoad{hrDeviceDescr="",hrDeviceIndex="770",hrDeviceType="1.3.6.1.2.1.25.3.1.3"} 5
hrProcessorLoad{hrDeviceDescr="",hrDeviceIndex="771",hrDeviceType="1.3.6.1.2.1.25.3.1.3"} 6
hrStorageAllocationUnits{hrStorageDescr="Physical memory",hrStorageIndex="1",hrStorageType="1.3.6.1.2.1.25.2.1.2"} 1024
hrStorageAllocationUnits{hrStorageDescr="Cached memory",hrStorageIndex="7",hrStorageType="1.3.6.1.2.1.25.2.1.1"} 1024
hrStorageAllocationUnits{hrStorageDescr="Memory buffers",hrStorageIndex="6",hrStorageType="1.3.6.1.2.1.25.2.1.1"} 1024
hrStorageAllocationUnits{hrStorageDescr="/",hrStorageIndex="31",hrStorageType="1.3.6.1.2.1.25.2.1.4"} 4096
hrStorageSize{hrStorageDescr="Physical memory",hrStorageIndex="1",hrStorageType="1.3.6.1.2.1.25.2.1.2"} 254128
hrStorageSize{hrStorageDescr="Cached memory",hrStorageIndex="7",hrStorageType="1.3.6.1.2.1.25.2.1.1"} 29412
hrStorageSize{hrStorageDescr="/",hrStorageIndex="31",hrStorageType="1.3.6.1.2.1.25.2.1.4"} 31766
hrStorageUsed{hrStorageDescr="Physical memory",hrStorageIndex="1",hrStorageType="1.3.6.1.2.1.25.2.1.2"} 114288
hrStorageUsed{hrStorageDescr="Cached memory",hrStorageIndex="7",hrStorageType="1.3.6.1.2.1.25.2.1.1"} 29412
hrStorageUsed{hrStorageDescr="Memory buffers",hrStorageIndex="6",hrStorageType="1.3.6.1.2.1.25.2.1.1"} 4136
hrStorageUsed{hrStorageDescr="/",hrStorageIndex="31",hrStorageType="1.3.6.1.2.1.25.2.1.4"} 1504
hrSystemUptime 1.13209135e+08
sysUpTime 1.13209027e+08
snmp_scrape_duration_seconds 0.4
"""

s = poller.build_sample(BODY, 1000)
w1 = s["wans"]["wan1"]
check(w1["in"] == 290000000000 and w1["out"] == 340000000000, "WAN counters not read from default/eth1")
check(s["lan_in"] == 338000000000 and s["lan_out"] == 290000000000, "LAN counters not read from default/eth0")
check((w1["in_err"], s["lan_in_err"], w1["in_disc"], w1["out_disc"]) == (0, 113, 1257, 7), "errors or discards misread")
check(s["cpu"] == {"0": 4, "1": 5, "2": 5, "3": 6}, f"cpu per core wrong: {s['cpu']}")
check(s["mem"] == {"total": 254128 * 1024, "used": 114288 * 1024, "cached": 29412 * 1024, "buffers": 4136 * 1024}, f"memory wrong: {s['mem']}")
check(s["uptime_cs"] == 113209027 and s["hr_uptime_cs"] == 113209135, "sysUpTime and hrSystemUptime are read separately")
check(s["if_up"] == {"eth0": 1, "eth1": 1, "pe-wan1_poe": 0}, f"interface state wrong: {s['if_up']}")
check(w1["pppoe_data"] == {"change_cs": 112938688, "in": 349674107, "out": 12582531}, f"pppoe wrong: {w1['pppoe_data']}")
check(w1["up"] == 0, "a PPPoE WAN whose session interface is down (ifOperStatus 2) is not up")
no_sys = poller.build_sample(BODY.replace("sysUpTime", "x_sysUpTime"), 1000)
check(no_sys["uptime_cs"] == 113209135, "uptime should fall back to hrSystemUptime")
try:
    poller.build_sample(BODY.replace('ifName="default/eth0"', 'ifName="other"'), 1000)
    errors.append("a body without the LAN counters must raise")
except RuntimeError as e:
    check("incomplete scrape" in str(e), "incomplete scrape message changed")
gone = poller.build_sample(BODY.replace('ifName="default/eth1"', 'ifName="other"'), 1000)
check(gone["wans"]["wan1"]["present"] is False and gone["wans"]["wan1"]["up"] == 0,
      "a configured WAN missing from SNMP must be down, not an error")
bare = poller.build_sample("\n".join(l for l in BODY.splitlines() if not re.match(r"(hr|sysUp|ifOper|ifLast)", l)), 1000)
check(bare["cpu"] == {} and bare["mem"] == {} and bare["uptime_cs"] is None and bare["wans"]["wan1"]["pppoe_data"] is None,
      "optional readings must be absent, not errors")

# ----------------------------------------------------------------- render ---
text = poller.render(s, {"success": True, "duration": 0.5, "last_ok": 1000}, reconnects={"wan1": 3})
# The names and labels another dashboard (internet-usage.json) depends on.
for line in ('er605_if_octets_total{interface="eth1",role="wan",direction="in"} 290000000000',
             'er605_if_octets_total{interface="eth1",role="wan",direction="out"} 340000000000',
             'er605_if_octets_total{interface="eth0",role="lan",direction="in"} 338000000000',
             'er605_if_octets_total{interface="eth0",role="lan",direction="out"} 290000000000',
             'er605_if_errors_total{interface="eth1",role="wan",direction="in"} 0',
             'er605_if_errors_total{interface="eth0",role="lan",direction="in"} 113',
             'er605_if_discards_total{interface="eth1",role="wan",direction="in"} 1257',
             'er605_if_discards_total{interface="eth1",role="wan",direction="out"} 7',
             "er605_last_scrape_timestamp_seconds 1000",
             "# TYPE er605_if_octets_total counter", "# TYPE er605_last_scrape_timestamp_seconds gauge",
             'er605_cpu_percent{core="2"} 5', 'er605_memory_bytes{state="used"} 117030912',
             "er605_uptime_seconds 1132091.35", 'er605_if_up{interface="pe-wan1_poe",role="pppoe"} 0',
             'er605_pppoe_session_seconds{wan="wan1"} 2703.39', 'er605_pppoe_reconnects_total{wan="wan1"} 3',
             'er605_pppoe_octets_total{wan="wan1",direction="in"} 349674107',
             'er605_wan_info{wan="wan1",interface="eth1",pppoe="pe-wan1_poe",isp=""} 1', 'er605_wan_up{wan="wan1"} 0', "er605_poll_success 1", "er605_poll_duration_seconds 0.500"):
    check(line in text.splitlines(), f"render() lacks the line: {line}")
for metric, (kind, helptext) in poller.HELP.items():
    check(f"# HELP {metric} " in text and f"# TYPE {metric} {kind}" in text, f"{metric} lacks HELP or TYPE in the textfile")
check(text.count("# TYPE") == len(poller.HELP), "every metric in HELP must be written on a good poll")
fail = poller.render(None, {"success": False, "duration": 20.0, "last_ok": 990})
check("er605_poll_success 0" in fail and "er605_last_scrape_timestamp_seconds 990" in fail and "er605_if_octets_total" not in fail,
      "a failed poll must write status only")
check('"' not in poller._esc('a"b').replace('\\"', ""), "label values must be escaped")

# ---------------------------------------------------------- counter reset ---
check(poller.counter_delta(150, 100) == 50, "plain delta wrong")
check(poller.counter_delta(30, 100) == 30, "a counter that went backwards must book the new value")
check(poller.counter_delta(500, 100, reset=True) == 500, "a known reset books the new value")
check(poller.counter_delta(7, None) == 0, "the first reading books nothing")


# ---------------------------------------------------------------- ledger ---
def ts(day, hour, minute=0, second=0):
    return dt.datetime(2026, 11, day, hour, minute, second, tzinfo=dt.timezone.utc).timestamp()


def smp(t, wi, wo, up_cs=None, change_cs=None, pin=0, pout=0, hr=..., extra=None):
    """A poll of the default single WAN (wan1, eth1, PPPoE); `extra` adds WANs: {name: dict}."""
    w = {"interface": "eth1", "pppoe": "pe-wan1_poe", "isp": "", "present": True, "in": wi, "out": wo, "in_err": 0,
         "in_disc": 0, "out_disc": 0, "pppoe_data": None, "up": 1}
    if change_cs is not None:
        w["pppoe_data"] = {"change_cs": change_cs, "in": pin, "out": pout}
    v = {"ts": t, "lan_in": 0, "lan_out": 0, "lan_in_err": 0, "cpu": {}, "mem": {}, "uptime_cs": up_cs,
         "hr_uptime_cs": up_cs if hr is ... else hr, "if_up": {}, "wans": {"wan1": w}}
    v["wans"].update(extra or {})
    return v


def wan2(t_in, t_out, up=1, present=True):
    """A DHCP WAN on eth2."""
    w = {"interface": "eth2", "pppoe": "", "isp": "Second ISP", "present": present, "pppoe_data": None, "up": up}
    if present:
        w.update({"in": t_in, "out": t_out, "in_err": 0, "in_disc": 0, "out_disc": 0})
    return w


def booked(db):
    return {h: (d, u) for h, d, u in db.execute("SELECT hour, download_bytes, upload_bytes FROM usage WHERE wan = 'wan1'")}


tmp = pathlib.Path(tempfile.mkdtemp(prefix="er605-test-"))
led = poller.Ledger(str(tmp / "usage.db"))
t0 = ts(27, 10, 59, 30)
seq = [  # (time, in, out)
    (t0, 1000, 100),
    (t0 + 30, 1500, 150),          # 11:00:00 is in the new hour: booked there
    (t0 + 60, 2500, 250),
    (ts(27, 11, 59, 30), 9000, 900),
    (ts(27, 12, 0, 0), 9100, 910),  # closes hour 11
    (ts(27, 12, 0, 30), 40, 4),     # counter went backwards (reboot without uptime): books 40 and 4
    (ts(27, 12, 31, 0), 540, 54),
]
lines = []
for t, i, o in seq:
    lines += led.update(smp(t, i, o))
b = booked(led.db)
check(b.get("2026-11-27T10:00:00Z") == (500, 50), "the first reading books nothing; the step ending at 11:00:00 belongs to hour 10")
total_in = sum(v[0] for v in b.values())
total_out = sum(v[1] for v in b.values())
expected_in = (1500 - 1000) + (2500 - 1500) + (9000 - 2500) + (9100 - 9000) + 40 + (540 - 40)
expected_out = (150 - 100) + (250 - 150) + (900 - 250) + (910 - 900) + 4 + (54 - 4)
check(total_in == expected_in and total_out == expected_out,
      f"bytes lost or double counted: booked {total_in}/{total_out}, expected {expected_in}/{expected_out}")
check(b.get("2026-11-27T11:00:00Z") == ((2500 - 1500) + (9000 - 2500) + (9100 - 9000), 100 + 650 + 10),
      f"hour 11 wrong: {b.get('2026-11-27T11:00:00Z')}")
check(b.get("2026-11-27T12:00:00Z") == (40 + 500, 4 + 50), f"hour 12 wrong: {b.get('2026-11-27T12:00:00Z')}")
usage_lines = [l for l in lines if l.startswith("usage ")]
check(usage_lines == ["usage hour=2026-11-27T10:00:00Z wan=wan1 download_bytes=500 upload_bytes=50",
                      "usage hour=2026-11-27T11:00:00Z wan=wan1 download_bytes=7600 upload_bytes=760"],
      f"one usage line per closed hour expected: {usage_lines}")
# a restart: a new Ledger on the same file continues from the stored counters, replays no usage line
led.db.close()
led = poller.Ledger(str(tmp / "usage.db"))
more = led.update(smp(ts(27, 12, 32, 0), 600, 60))
check(not [l for l in more if l.startswith("usage ")], "a restart must not replay usage lines")
check(booked(led.db)["2026-11-27T12:00:00Z"] == (40 + 500 + 60, 4 + 50 + 6), "bytes across a poller restart were lost")
nxt = led.update(smp(ts(27, 13, 0, 0), 700, 70))
check([l for l in nxt if l.startswith("usage ")] == ["usage hour=2026-11-27T12:00:00Z wan=wan1 download_bytes=700 upload_bytes=70"],
      f"hour 12 must be logged once when it closes: {nxt}")
check(not [l for l in led.update(smp(ts(27, 13, 0, 30), 710, 71)) if l.startswith("usage ")], "a closed hour must be logged once")

# a fresh ledger never logs the hours already in it (an import) as usage lines
led2 = poller.Ledger(str(tmp / "fresh.db"))
led2.db.execute("INSERT INTO usage VALUES ('2026-11-27T08:00:00Z', 'wan1', 1, 1, 'csv')")
led2.db.commit()
check(not [l for l in led2.update(smp(ts(27, 10, 0, 0), 1, 1)) if l.startswith("usage ")], "imported hours must not be replayed into the journal")

# ------------------------------------------------ reconnect and reboot ---
led3 = poller.Ledger(str(tmp / "ppp.db"))
base = ts(27, 8, 0, 0)
up = 100_000_00  # 100000 s of uptime, in hundredths
log = []
log += led3.update(smp(base, 1000, 100, up, up - 3_600_00, 500, 50))                        # session 1 h old
log += led3.update(smp(base + 30, 1100, 110, up + 30_00, up - 3_600_00, 520, 52))            # same session
check(not log and led3.reconnect_counts().get("wan1", 0) == 0, f"no event expected inside one session: {log}")
reconn = led3.update(smp(base + 60, 1200, 120, up + 60_00, up + 50_00, 3, 1))                # session restarted 10 s ago
check(len(reconn) == 1 and reconn[0].startswith("pppoe_reconnect at=2026-11-27T08:00:50Z wan=wan1 previous_session_seconds="),
      f"reconnect not logged as expected: {reconn}")
prev = int(re.search(r"previous_session_seconds=(\d+)", reconn[0]).group(1))
check(3640 <= prev <= 3660, f"previous session should be about 3650 s, got {prev}")
check(led3.reconnect_counts()["wan1"] == 1, "reconnect not stored once")
check(not led3.update(smp(base + 90, 1300, 130, up + 90_00, up + 50_00, 30, 10)), "a reconnect must be logged once")
# counters reset with the last change unmoved still count
cnt = led3.update(smp(base + 120, 1400, 140, up + 120_00, up + 50_00, 4, 2))
check(len(cnt) == 1 and cnt[0].startswith("pppoe_reconnect"), f"a reset of the session counters is a reconnect: {cnt}")
check(led3.reconnect_counts()["wan1"] == 2, "reconnect count after the counter reset should be 2")
# reboot: uptime went down. Not a reconnect; every counter restarts, so its value is the traffic since boot.
rb = led3.update(smp(base + 150, 50, 5, 20_00, 10_00, 1, 1))
check(len(rb) == 1 and rb[0].startswith("router_reboot at=2026-11-27T08:02:10Z"), f"reboot not logged as expected: {rb}")
check(led3.reconnect_counts()["wan1"] == 2, "a router reboot must not count as a PPPoE reconnect")
check(led3.db.execute("SELECT count(*) FROM reboots").fetchone()[0] == 1, "reboot not stored once")
check(sum(v[0] for v in booked(led3.db).values()) == (1100 - 1000) + (1200 - 1100) + (1300 - 1200) + (1400 - 1300) + 50,
      "a counter that restarted (reboot) must book the new value, not a negative delta")
check(not led3.update(smp(base + 180, 60, 6, 50_00, 10_00, 2, 2)), "nothing to log after the reboot settled")

# Finding 1: only the host uptime (hrSystemUptime) is a reboot. sysUpTime going
# down (SNMP agent restart, or its 497 day wrap) with the counters continuing
# must book only the real delta and log nothing.
la = poller.Ledger(str(tmp / "agent.db"))
big_in, big_out = 291_000_000_000, 361_000_000_000
la.update(smp(base, big_in, big_out, 900_00, 800_00, 500, 50, hr=500_000_00))
ev = la.update(smp(base + 30, big_in + 1000, big_out + 2000, 10_00, 5_00, 520, 52, hr=500_030_00))   # agent restarted
check(ev == [], f"an SNMP agent restart is not a reboot, nor a reconnect: {ev}")
check(la.db.execute("SELECT count(*) FROM reboots").fetchone()[0] == 0, "an agent restart must not store a reboot")
check(sum(v[0] for v in booked(la.db).values()) == 1000 and sum(v[1] for v in booked(la.db).values()) == 2000,
      f"uptime reset with the counters continuing must book only the real delta: {booked(la.db)}")
ev = la.update(smp(base + 60, big_in + 1500, big_out + 2500, 40_00, 5_00, 530, 53, hr=500_060_00))
check(ev == [] and sum(v[0] for v in booked(la.db).values()) == 1500, "polls after the agent restart must stay quiet")
# a real reboot: hrSystemUptime down and the counters restart
ev = la.update(smp(base + 90, 70, 7, 10_00, 5_00, 1, 1, hr=20_00))
check(len(ev) == 1 and ev[0].startswith("router_reboot at="), f"hrSystemUptime going down is a reboot: {ev}")

# Finding 2: a poller gap is spread over the hours it covers, not piled into the current hour.
lg = poller.Ledger(str(tmp / "gap.db"))
lg.update(smp(ts(27, 1, 0, 0), 1000, 100))
lg.update(smp(ts(27, 4, 0, 0), 1000 + 3000, 100 + 600))            # 3 h gap, 1000 and 200 per hour
check(booked(lg.db) == {"2026-11-27T01:00:00Z": (1000, 200), "2026-11-27T02:00:00Z": (1000, 200), "2026-11-27T03:00:00Z": (1000, 200)},
      f"a gap must be spread over its hours: {booked(lg.db)}")
lg.update(smp(ts(27, 6, 30, 0), 4000 + 2501, 700 + 77))            # odd totals: no byte lost to rounding
check(sum(v[0] for v in booked(lg.db).values()) == 3000 + 2501 and sum(v[1] for v in booked(lg.db).values()) == 600 + 77,
      "rounding must not lose bytes across a gap")
check(poller.split_hours(ts(27, 1, 30), ts(27, 2, 30), 100) == {"2026-11-27T01:00:00Z": 50, "2026-11-27T02:00:00Z": 50},
      "a step across an hour edge is split by time")
# a failed update rolls back; nothing partial commits with the next poll
lr = poller.Ledger(str(tmp / "rollback.db"))
lr.update(smp(ts(27, 1, 0, 0), 1000, 100))
real_put = lr._put
def boom(**kw):
    raise RuntimeError("disk")
lr._put = boom
try:
    lr.update(smp(ts(27, 1, 0, 30), 1500, 150))
    errors.append("a failing update must raise")
except RuntimeError:
    pass
lr._put = real_put
check(booked(lr.db) == {}, f"a failed update must leave no partial booking: {booked(lr.db)}")
lr.update(smp(ts(27, 1, 1, 0), 1600, 160))
check(booked(lr.db) == {"2026-11-27T01:00:00Z": (600, 60)}, f"the next poll books from the last committed state: {booked(lr.db)}")

# Finding 3: pe-wan1_poe missing from a poll (redial) keeps the old session state.
lp = poller.Ledger(str(tmp / "redial.db"))
lp.update(smp(base, 1000, 100, 100_000_00, 90_000_00, 500, 50))
ev = lp.update(smp(base + 30, 1100, 110, 100_030_00))              # no pppoe in this poll
check(ev == [] and lp.state().get("wan1:pppoe_change") == 90_000_00, f"a missing pe-wan1_poe must not clear the PPPoE state: {ev} {lp.state()}")
ev = lp.update(smp(base + 60, 1200, 120, 100_060_00, 100_050_00, 2, 1))   # back, new session
check(len(ev) == 1 and ev[0].startswith("pppoe_reconnect") and lp.reconnect_counts()["wan1"] == 1, f"the reconnect must be counted when the interface returns: {ev}")

# ------------------------------------------------------------- multi-WAN ---
ETH2 = """\
ifHCInOctets{ifAlias="",ifDescr="default/eth2",ifIndex="4",ifName="default/eth2"} 5000
ifHCOutOctets{ifAlias="",ifDescr="default/eth2",ifIndex="4",ifName="default/eth2"} 700
ifInErrors{ifAlias="",ifDescr="default/eth2",ifIndex="4",ifName="default/eth2"} 2
ifOperStatus{ifAlias="",ifDescr="default/eth2",ifIndex="4",ifName="default/eth2"} 1
"""
TWO = [{"name": "wan1", "interface": "eth1", "pppoe": "pe-wan1_poe", "isp": "First ISP"},
       {"name": "wan2", "interface": "eth2", "pppoe": "", "isp": "Second ISP"}]
BODY_UP = BODY.replace('ifOperStatus{ifAlias="",ifDescr="default/pe-wan1_poe",ifIndex="16",ifName="default/pe-wan1_poe"} 2',
                       'ifOperStatus{ifAlias="",ifDescr="default/pe-wan1_poe",ifIndex="16",ifName="default/pe-wan1_poe"} 1')
two = poller.build_sample(BODY_UP + ETH2, 1000, TWO)
check(two["wans"]["wan1"]["up"] == 1 and two["wans"]["wan2"]["up"] == 1, "both WANs should be up")
check(two["wans"]["wan2"]["in"] == 5000 and two["wans"]["wan2"]["in_err"] == 2 and two["wans"]["wan2"]["pppoe_data"] is None,
      "the DHCP WAN on eth2 is read by its own interface, with no PPPoE")
t2 = poller.render(two, {"success": True, "duration": 0.5, "last_ok": 1000}, reconnects={"wan1": 1})
for line in ('er605_if_octets_total{interface="eth1",role="wan",direction="in"} 290000000000',
             'er605_if_octets_total{interface="eth2",role="wan",direction="in"} 5000',
             'er605_if_octets_total{interface="eth2",role="wan",direction="out"} 700',
             'er605_if_errors_total{interface="eth2",role="wan",direction="in"} 2',
             'er605_wan_info{wan="wan2",interface="eth2",pppoe="",isp="Second ISP"} 1', 'er605_wan_up{wan="wan1"} 1', 'er605_wan_up{wan="wan2"} 1',
             'er605_if_up{interface="eth2",role="wan"} 1', 'er605_pppoe_reconnects_total{wan="wan1"} 1'):
    check(line in t2.splitlines(), f"two-WAN render lacks: {line}")
check(not [l for l in t2.splitlines() if l.startswith("er605_pppoe") and 'wan="wan2"' in l], "a non-PPPoE WAN must have no PPPoE series")
# A configured WAN whose interface is missing: wan_up 0, no counters, one wan_down, no crash.
THREE = TWO + [{"name": "wan3", "interface": "eth9", "pppoe": "", "isp": ""}]
miss = poller.build_sample(BODY_UP + ETH2, 1000, THREE)
tm = poller.render(miss, {"success": True, "duration": 0.5, "last_ok": 1000})
check('er605_wan_up{wan="wan3"} 0' in tm.splitlines() and 'interface="eth9"' not in tm.replace('interface="eth9",pppoe', ""),
      "a missing WAN interface must report wan_up 0 and no counters")
lm = poller.Ledger(str(tmp / "missing.db"))
first = lm.update(miss)
second = lm.update(dict(miss, ts=1030))
check([l for l in first if l.startswith("wan_down")] == ["wan_down at=1970-01-01T00:16:40Z wan=wan3"] and not [l for l in second if l.startswith("wan_")],
      f"a missing WAN logs one wan_down, not one per poll: {first} {second}")

# Two WANs booked separately; totals equal the sum; one usage line per WAN per hour.
l2 = poller.Ledger(str(tmp / "two.db"))
def two_poll(t, a_in, a_out, b_in, b_out, **kw):
    return smp(t, a_in, a_out, extra={"wan2": wan2(b_in, b_out, **kw)})
log2 = []
for t, ai, ao, bi, bo in ((ts(27, 10, 59, 30), 1000, 100, 50, 5), (ts(27, 11, 0, 30), 1600, 160, 450, 45),
                          (ts(27, 11, 30, 0), 2600, 260, 950, 95), (ts(27, 12, 0, 30), 3000, 300, 1050, 105)):
    log2 += l2.update(two_poll(t, ai, ao, bi, bo))
per = {(h, w): (d, u) for h, w, d, u in l2.db.execute("SELECT hour, wan, download_bytes, upload_bytes FROM usage")}
check(sum(v[0] for (h, w), v in per.items() if w == "wan1") == 2000 and sum(v[0] for (h, w), v in per.items() if w == "wan2") == 1000,
      f"each WAN must book its own bytes: {per}")
check(sum(d for d, u in per.values()) == 3000 and sum(u for d, u in per.values()) == 200 + 100,
      "the total over the WANs must equal the sum of the WANs")
ul = [l for l in log2 if l.startswith("usage ")]
check(any("hour=2026-11-27T11:00:00Z wan=wan1 " in l for l in ul) and any("hour=2026-11-27T11:00:00Z wan=wan2 " in l for l in ul),
      f"one usage line per WAN per closed hour expected: {ul}")
# A WAN going down and up: wan_events and journal lines.
ev = []
ev += l2.update(two_poll(ts(27, 12, 1, 0), 3100, 310, 0, 0, up=0, present=False))     # wan2 interface vanishes
ev += l2.update(two_poll(ts(27, 12, 1, 30), 3200, 320, 0, 0, up=0, present=False))    # still down: nothing new
ev += l2.update(two_poll(ts(27, 12, 3, 0), 3300, 330, 1100, 110))                    # back
ev = [l for l in ev if l.startswith("wan_")]
check(ev == ["wan_down at=2026-11-27T12:01:00Z wan=wan2", "wan_up at=2026-11-27T12:03:00Z wan=wan2 duration_seconds=120"],
      f"wan_down and wan_up lines wrong: {ev}")
rows = l2.db.execute("SELECT wan, down_at, up_at, duration FROM wan_events").fetchall()
check(rows == [("wan2", ts(27, 12, 1, 0), ts(27, 12, 3, 0), 120.0)], f"wan_events wrong: {rows}")
wan2_total = sum(d for (h, w), (d, u) in per.items() if w == "wan2")
after = sum(d for h, w, d in l2.db.execute("SELECT hour, wan, download_bytes FROM usage") if w == "wan2")
check(wan2_total == 1000 and after == 1000 + 50, f"a WAN that went down and came back keeps its counter: nothing lost or double counted ({after})")
# A pppoe reconnect names its WAN.
lr2 = poller.Ledger(str(tmp / "twoppp.db"))
lr2.update(smp(base, 1, 1, 100_000_00, 90_000_00, 5, 5, extra={"wan2": wan2(1, 1)}))
rc = lr2.update(smp(base + 30, 2, 2, 100_030_00, 100_020_00, 1, 1, extra={"wan2": wan2(2, 2)}))
check(len(rc) == 1 and " wan=wan1 " in rc[0] and lr2.reconnect_counts() == {"wan1": 1}, f"reconnect must carry its WAN: {rc}")

# One WAN, the default: er605_if_octets_total is byte-identical to the single-WAN poller's output.
single = poller.render(poller.build_sample(BODY, 1000), {"success": True, "duration": 0.5, "last_ok": 1000})
OCTETS = ("# HELP er605_if_octets_total ER605 interface byte counter (IF-MIB ifHC*Octets) via SNMPv3.\n"
          "# TYPE er605_if_octets_total counter\n"
          'er605_if_octets_total{interface="eth1",role="wan",direction="in"} 290000000000\n'
          'er605_if_octets_total{interface="eth1",role="wan",direction="out"} 340000000000\n'
          'er605_if_octets_total{interface="eth0",role="lan",direction="in"} 338000000000\n'
          'er605_if_octets_total{interface="eth0",role="lan",direction="out"} 290000000000\n')
check(single.startswith(OCTETS), "the one-WAN default must write er605_if_octets_total exactly as before")
check("# HELP er605_if_errors_total" in single and 'er605_wan_up{wan="wan1"} 0' in single, "single WAN output lacks errors or wan_up")

# Migration of a ledger written before multi-WAN support (its exact old schema and state keys).
OLD_SCHEMA = """
CREATE TABLE usage (hour TEXT PRIMARY KEY, download_bytes INTEGER NOT NULL, upload_bytes INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'live');
CREATE TABLE reconnects (id INTEGER PRIMARY KEY, at REAL NOT NULL, previous_session_seconds REAL NOT NULL);
CREATE TABLE reboots (id INTEGER PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE state (key TEXT PRIMARY KEY, value);
"""
import sqlite3
oldp = str(tmp / "old.db")
od = sqlite3.connect(oldp)
od.executescript(OLD_SCHEMA)
old_usage = [("2026-11-26T10:00:00Z", 111, 11, "csv"), ("2026-11-26T11:00:00Z", 222, 22, "live"), ("2026-11-27T08:00:00Z", 333, 33, "live")]
od.executemany("INSERT INTO usage VALUES (?,?,?,?)", old_usage)
od.execute("INSERT INTO reconnects (at, previous_session_seconds) VALUES (1000.5, 3600)")
od.execute("INSERT INTO reboots (at) VALUES (900)")
old_state = {"ts": ts(27, 8, 0, 0), "wan_in": 5000, "wan_out": 500, "uptime_cs": 10000, "hr_uptime_cs": 10000, "logged_through": ts(27, 7, 0),
             "pppoe_change": 123, "pppoe_in": 7, "pppoe_out": 8, "pppoe_start": 99.5}
od.executemany("INSERT INTO state VALUES (?,?)", old_state.items())
od.commit()
od.close()
ml = poller.Ledger(oldp)
check(sorted(ml.db.execute("SELECT hour, wan, download_bytes, upload_bytes, source FROM usage")) ==
      sorted((h, "wan1", d, u, src) for h, d, u, src in old_usage), "migration lost or changed usage rows, or did not mark them wan1")
check(ml.db.execute("SELECT at, previous_session_seconds, wan FROM reconnects").fetchall() == [(1000.5, 3600.0, "wan1")], "migration lost reconnects")
check(ml.db.execute("SELECT at FROM reboots").fetchall() == [(900.0,)], "migration lost reboots")
mst = ml.state()
check(all(mst.get("wan1:" + k) == old_state[k] for k in ("ts", "wan_in", "wan_out", "pppoe_change", "pppoe_in", "pppoe_out", "pppoe_start"))
      and mst["uptime_cs"] == 10000 and mst["logged_through"] == ts(27, 7, 0) and "wan_in" not in mst, f"migration of the state keys wrong: {mst}")
check("wan_events" in [r[0] for r in ml.db.execute("SELECT name FROM sqlite_master")], "migration must create wan_events")
# the migrated ledger carries on: a poll continues from the stored counters, books only the real delta, and is idempotent to re-open
cont = ml.update(smp(ts(27, 8, 0, 30), 5600, 560, 10_030, 123, 8, 9, hr=10_030))
check(cont == [] and booked(ml.db)["2026-11-27T08:00:00Z"] == (333 + 600, 33 + 60), f"a migrated ledger must continue without a reset: {cont} {booked(ml.db)}")
ml.db.close()
check(len(poller.Ledger(oldp).db.execute("SELECT * FROM usage").fetchall()) == 3, "re-opening a migrated ledger must change nothing")
# a failed migration leaves the old ledger as it was
bad = str(tmp / "bad.db")
bd = sqlite3.connect(bad)
bd.executescript(OLD_SCHEMA + "CREATE TABLE usage_new (x);")
bd.execute("INSERT INTO usage VALUES ('2026-11-26T10:00:00Z', 1, 1, 'live')")
bd.commit()
bd.close()
try:
    poller.Ledger(bad)
    errors.append("a migration that cannot run must raise")
except sqlite3.OperationalError:
    pass
chk = sqlite3.connect(bad)
check([r[1] for r in chk.execute("PRAGMA table_info(usage)")] == ["hour", "download_bytes", "upload_bytes", "source"]
      and chk.execute("SELECT count(*) FROM usage").fetchone()[0] == 1, "a failed migration must roll back completely")
chk.close()

# --wan parsing
check(poller.parse_wan("wan2=eth2,pppoe=pe-wan2_poe,isp=Second ISP") == {"name": "wan2", "interface": "eth2", "pppoe": "pe-wan2_poe", "isp": "Second ISP"}, "parse_wan wrong")
check(poller.parse_wan("wan2=eth2") == {"name": "wan2", "interface": "eth2", "pppoe": "", "isp": ""}, "parse_wan defaults wrong")
for bad_spec in ("Wan2=eth2", "wan2", "wan2=", "wan2=eth2,speed=1"):
    try:
        poller.parse_wan(bad_spec)
        errors.append(f"parse_wan must reject {bad_spec!r}")
    except ValueError:
        pass
try:
    poller.check_wans([poller.parse_wan("a=eth1"), poller.parse_wan("b=eth1")])
    errors.append("two WANs on one interface must be rejected")
except ValueError:
    pass
check(poller.DEFAULT_WANS == [{"name": "wan1", "interface": "eth1", "pppoe": "pe-wan1_poe", "isp": ""}], "the default WAN changed")

# ---------------------------------------------------------- raw CSV days ---
raw = tmp / "raw"
raw.mkdir()
now = ts(27, 12, 0, 0)
for day, in_ in ((27, 1), (26, 2), (25, 3), (24, 4), (23, 5), (10, 6)):
    (raw / f"er605-2026-11-{day:02d}.csv").write_text(
        "ts_epoch,%s\n" % ",".join(poller.COLS) + "".join(
            f"{int(ts(day, 0, m * 1))},{in_ * 1000 * (m + 1)},{in_ * 10 * (m + 1)},0,0,0,0,0,0\n" for m in range(3)))
(raw / "notes.txt").write_text("keep me")
(raw / "er605-2026-11-01.csv.bak").write_text("keep me too")
poller.housekeep(str(raw), 15, now)
names = sorted(os.listdir(raw))
check("er605-2026-11-27.csv" in names and "er605-2026-11-26.csv" in names and "er605-2026-11-25.csv" in names,
      f"days up to 2 old must stay plain: {names}")
check("er605-2026-11-24.csv.gz" in names and "er605-2026-11-24.csv" not in names, f"a 3 day old file must be gzipped: {names}")
check("er605-2026-11-23.csv.gz" in names, "older days must be gzipped")
check(not any("11-10" in n for n in names), f"a day past keep_days must be deleted: {names}")
check("notes.txt" in names and "er605-2026-11-01.csv.bak" in names, "unrelated files must be left alone")
with gzip.open(raw / "er605-2026-11-24.csv.gz", "rt") as f:
    check(f.readline().startswith("ts_epoch,wan_in"), "the gzipped day must keep its header")
poller.housekeep(str(raw), 15, now)
check(sorted(os.listdir(raw)) == names, "housekeeping must be idempotent")
poller.append_csv(str(raw), smp(now, 1, 2))
check(len(poller.csv_rows(str(raw))) == 3 * 5 + 1, "csv_rows must read plain and gzipped days")
today_csv = (raw / "er605-2026-11-27.csv").read_text().splitlines()
check(today_csv[-1] == f"{int(now)},1,2,0,0,0,0,0,0", f"CSV row format changed: {today_csv[-1]}")

# ------------------------------------------------------------ import-csv ---
imp = tmp / "imp"
imp.mkdir()
for n in ("er605-2026-11-23.csv.gz", "er605-2026-11-24.csv.gz"):
    (imp / n).write_bytes((raw / n).read_bytes())
(imp / "er605-2026-11-25.csv").write_text((raw / "er605-2026-11-25.csv").read_text())
db = str(tmp / "import.db")
w1, s1 = poller.import_csv(db, str(imp))
first = booked(poller.Ledger(db).db)
w2, s2 = poller.import_csv(db, str(imp))
second = booked(poller.Ledger(db).db)
check(first == second and w1 == w2 and s1 == s2 == 0, "import-csv is not idempotent")
rows = poller.csv_rows(str(imp))
check(sum(v[0] for v in first.values()) == sum(poller.counter_delta(b["wan_in"], a["wan_in"]) for (_, a), (_, b) in zip(rows, rows[1:])),
      "import-csv booked a different total than the CSV deltas")
live = poller.Ledger(db)
live._book("2026-11-25T00:00:00Z", "wan1", 5, 5)
live.db.commit()
poller.import_csv(db, str(imp))
check(booked(poller.Ledger(db).db)["2026-11-25T00:00:00Z"] == (first["2026-11-25T00:00:00Z"][0] + 5, first["2026-11-25T00:00:00Z"][1] + 5),
      "import-csv must not replace an hour the live poller booked")
with contextlib.redirect_stdout(io.StringIO()):
    cli_status = poller.main(["--data-dir", str(tmp / "cli"), "import-csv", str(imp)])
check(cli_status == 0, "import-csv subcommand failed")

# --------------------------------------------------------------- reports ---
rep = tmp / "rep"
rep.mkdir()
rl = poller.Ledger(str(rep / "usage.db"))
rl._book("2026-11-27T18:00:00Z", "wan1", 2_000_000_000, 1_000_000_000)   # 23:30 IST on the 27th
rl._book("2026-11-27T19:00:00Z", "wan1", 1_000_000_000, 500_000_000)     # 00:30 IST on the 28th
rl.db.execute("INSERT INTO reconnects (at, previous_session_seconds) VALUES (?,?)", (ts(27, 18, 30), 3725))
rl.db.execute("INSERT INTO reboots (at) VALUES (?)", (ts(27, 19, 0),))
rl.db.commit()
real_time = poller.time.time
poller.time.time = lambda: ts(28, 6, 0)
try:
    def run_report(kind, days):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            poller.ledger_report(str(rep), kind, days)
        return out.getvalue()
    hour = run_report("hour", 2)
    day = run_report("day", 30)
    month = run_report("month", 30)
    rec = run_report("reconnects", 30)
finally:
    poller.time.time = real_time
rl._book("2026-11-27T18:00:00Z", "wan2", 500_000_000, 250_000_000)
rl.db.execute("INSERT INTO wan_events (wan, down_at, up_at, duration) VALUES ('wan2', ?, ?, 185)", (ts(27, 18, 10), ts(27, 18, 13) + 5))
rl.db.commit()
poller.time.time = lambda: ts(28, 6, 0)
try:
    day2 = run_report("day", 30)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        poller.ledger_report(str(rep), "day", 30, "wan2")
    day_wan2 = out.getvalue()
    wanrep = run_report("wan", 30)
finally:
    poller.time.time = real_time
check("wan1_down_gb" in day2 and "wan2_down_gb" in day2 and "2.500" in day2, f"day report must have totals and per-WAN columns: {day2}")
check("wan1_down_gb" not in day_wan2 and "wan2_down_gb" in day_wan2 and "0.500" in day_wan2, f"--wan must limit the report: {day_wan2}")
check("wan2" in wanrep and "0:03:05" in wanrep and "1 down events" in wanrep, f"--report wan wrong: {wanrep}")
check("2026-11-27 23:30" in hour and "2026-11-28 00:30" in hour, f"hour report must show IST: {hour}")
check("2026-11-27" in day and "2026-11-28" in day and "2.000" in day, f"day report wrong: {day}")
check("2026-11" in month and "4.500" in month, f"month report wrong: {month}")
check("2026-11-28 00:00:00" in rec and "1:02:05" in rec and "router reboots: 2026-11-28 00:30:00" in rec, f"reconnect report wrong: {rec}")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    poller.window_report(str(raw), "2026-11-24 05:30", "2026-11-24 06:30")
check("window 2026-11-24 05:30 to 2026-11-24 06:30 IST" in out.getvalue(), "the window report must keep working")

# ----------------------------------------------------- role, compose, docs ---
tasks = (ROLE / "tasks" / "main.yml").read_text()
compose = (ROLE / "templates" / "compose.yml.j2").read_text()
role_text = "\n".join(p.read_text() for p in ROLE.rglob("*") if p.is_file() and p.suffix in (".yml", ".j2"))
check(compose.startswith("# Managed by ansible/roles/er605_snmp. Do not edit by hand."), "compose template lacks the managed header")
check("ansible_facts" not in role_text and not re.search(r"\bansible_(?!check_mode)\w+", role_text), "the role must not use ansible_* facts")
check(not any(p.name.startswith("auth") for p in ROLE.rglob("*") if p.is_file()), "the role must not ship an auth file or template")
check(not re.search(r"(template|copy):[^\n]*\n(?:[ \t]+[^\n]*\n){0,6}?[ \t]+(dest|src):[^\n]*auth", tasks), "the role must never template or copy the auth file")
check(not re.search(r"\b(cat|slurp|lookup\('file'|include_vars)\b[^\n]*auth", tasks), "the role must not read the auth file")
check("--wan={{ w.name }}={{ w.interface }}" in compose and "er605_snmp_wans" in compose, "the compose file must pass every er605_snmp_wans entry as --wan")
check("auth.yml:/etc/snmp_exporter/auth.yml:ro" in compose, "the auth file must be mounted read-only")
services = re.findall(r"^  (er605-[a-z]+):$", compose, re.M)
check(services == ["er605-snmp", "er605-poll"], f"unexpected services: {services}")
check(compose.count("cap_drop: [ALL]") == len(services), "every service must drop all capabilities")
check(compose.count("read_only: true") == len(services), "every service must have a read-only root filesystem")
check(compose.count('no-new-privileges:true') == len(services), "every service must set no-new-privileges")
check("privileged" not in compose and "cap_add" not in compose, "no privileged mode or added capabilities")
check(re.search(r"^name: er605-snmp$", compose, re.M) and "container_name: er605-snmp" in compose and "container_name: er605-poll" in compose,
      "the compose project and container names must stay er605-snmp and er605-poll")
check("docker rm -f" in tasks and "er605_snmp_replace_manual" in tasks and "com.docker.compose.project=er605-snmp" in tasks,
      "the role must detect manual containers and print docker rm -f")
check(re.search(r"^\s+when:.*not \(er605_snmp_replace_manual \| bool\)", tasks, re.M), "the manual containers must block unless replace_manual is set")
check("0600" in tasks and "pw_name != 'root'" in tasks, "the auth file owner and mode must be checked")

defaults = set(re.findall(rf"^({PREFIX}\w+):", (ROLE / "defaults" / "main.yml").read_text(), re.M))
variables = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
documented = set(re.findall(rf"^\| `({PREFIX}\w+)`", variables, re.M))
check(defaults == documented, f"variables.md and defaults differ: {sorted(defaults ^ documented)}")
used = set(re.findall(rf"\b({PREFIX}\w+)\b", role_text))
check(used - defaults <= {"er605_snmp_manual", "er605_snmp_existing", "er605_snmp_secret", "er605_snmp_up", "er605_snmp_first", "er605_snmp_leftover"},
      f"variables used but not in defaults: {sorted(used - defaults)}")
check("import_playbook: er605-snmp.yml" in (ANSIBLE / "playbooks" / "site.yml").read_text(), "playbook missing from site.yml")
check("`er605-snmp.yml`" in (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text(), "playbook missing from playbooks.md")
check("`er605-snmp.yml`" in (ANSIBLE / "README.md").read_text(), "playbook missing from the ansible README")
playbook = (ANSIBLE / "playbooks" / "er605-snmp.yml").read_text()
check("gather_facts: false" in playbook and "er605_snmp" in playbook, "the playbook must set gather_facts: false and run the role")
metrics_doc = (REPO / "monitoring" / "docs" / "reference" / "metrics.md").read_text()
for metric in poller.HELP:
    check(f"`{metric}" in metrics_doc, f"{metric} is not in monitoring/docs/reference/metrics.md")
for event in ("usage", "pppoe_reconnect", "wan_down", "wan_up", "router_reboot", "poll_error"):
    check(f"| `{event}` |" in metrics_doc, f"journal line {event} is not in the metrics reference")

script_src = SCRIPT.read_text()
parser_flags = set(re.findall(r'add_argument\("(--[a-z-]+)"', script_src))
playbooks_doc = (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text()
role_section = playbooks_doc.split("### `er605_snmp`", 1)[1].split("\n### ", 1)[0]
for ln in role_section.splitlines():
    if "er605-poll.py" in ln:
        for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", ln):
            check(flag in parser_flags, f"playbooks.md shows {flag}, which the poller does not accept")
for sub in re.findall(r"er605-poll\.py (import-csv|report|run)\b", role_section):
    check(f'add_parser("{sub}")' in script_src, f"playbooks.md shows the subcommand {sub}, which the poller does not have")
report_choices = set(re.search(r'"--report", choices=\[([^\]]+)\]', script_src).group(1).replace('"', "").replace(" ", "").split(","))
for kind in report_choices:
    check(f"--report {kind}" in role_section, f"--report {kind} is not documented in playbooks.md")
check("docker rm -f er605-snmp er605-poll" in role_section, "playbooks.md must show the command that removes the manual containers")
check("sudo install -m 0600 -o root -g root" in role_section and "/etc/er605-snmp/auth.yml" in role_section, "playbooks.md must show the one-time auth move")

home = json.loads((DASH_DIR / "home-isp.json").read_text())
check(home["uid"] == "home-isp" and home["title"] == "Home ISP", "dashboard uid or title changed")
check(set(home["tags"]) == {"network", "isp", "router", "er605"}, f"dashboard tags wrong: {home['tags']}")
check({l["url"] for l in home["links"]} == {"/d/internet-usage/internet-and-usage", "/d/router-tplink-blr/router-tplink-blr"}, "dashboard links wrong")
for p in home["panels"]:
    if p["type"] != "row":
        check(p.get("description"), f"panel '{p['title']}' has no description")
check([p["title"] for p in home["panels"] if p["type"] == "row"] ==
      ["Overview", "Usage", "Quality", "Router", "WAN $wan", "Outages (30 days, Loki)", "History, 30 days (Loki)",
       "Monthly history (permanent)"], "dashboard rows changed")
for old in ("WAN download now", "WAN upload now", "Downloaded, last 24 h", "Uploaded, last 24 h", "Poller last read",
            "Household WAN throughput (ER605 WAN1, eth1)", "Household vs t7920, download", "Household vs t7920, upload",
            "Daily volume", "WAN and LAN errors and discards"):
    check(old in {p["title"] for p in home["panels"]}, f"the old research dashboard's panel '{old}' is gone")
written = set(poller.HELP)
for name in ("home-isp.json", "internet-usage.json"):
    queried = set()
    for p in json.loads((DASH_DIR / name).read_text())["panels"]:
        for t in p.get("targets", []):
            queried |= set(re.findall(r"\b(er605_\w+)", t.get("expr", "")))
    check(queried and queried <= written, f"{name} queries er605 metrics the poller does not write: {sorted(queried - written)}")
exprs = " ".join(t.get("expr", "") for p in home["panels"] for t in p.get("targets", []))
check('container="er605-poll"' in exprs and 'host="ubuntu-dev"' in exprs, "the Loki queries must select the er605-poll container on ubuntu-dev")

import shutil
shutil.rmtree(tmp, ignore_errors=True)

if errors:
    print("FAIL")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print("ok: er605_snmp")
