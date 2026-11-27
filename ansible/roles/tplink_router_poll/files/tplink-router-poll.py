#!/usr/bin/env python3
"""Per-client traffic from a TP-Link router (DEV2 web API), for Grafana.

Runs on a host on the router's LAN. Every --interval seconds it logs in, reads
the client list and the traffic counters, logs out, and then:

  * writes Prometheus textfile metrics to --textfile (atomically), which the
    host's Alloy already scrapes and ships to central Prometheus in batches,
    buffering in its write-ahead log while the link is down;
  * appends the same sample as one JSON line to --data-dir/YYYY-MM-DD.jsonl
    (UTC dates), the host's own copy. Older days are gzipped and days past
    --keep-days are deleted;
  * books it into the usage ledger, --data-dir/usage.db (SQLite, kept
    forever): bytes per client per hour, and every connection's start, end
    and bytes. See the ledger section below. A consistent copy,
    usage-snapshot.db, is written once a day for the batch transfer off the
    host. A missing ledger is rebuilt from the JSONL files at start;
  * prints one logfmt line per client with traffic, and one per session start
    and end, to the journal, which Alloy ships to Loki.

The password is read from --password-file (systemd LoadCredential), never from
the command line or the environment.

Safety:
  * The router's MAC is checked against --expected-mac BEFORE the password is
    sent, so a laptop that joins another network with the same gateway address
    never offers the password to a stranger's router.
  * The router allows one admin session. If someone is logged in to the web UI
    (/cgi/getBusy reports isLogined=1), the poll is skipped rather than kicking
    them out.
  * Reads only: `go` and `gl` operations, plus login and logout.

  tplink-router-poll.py --once --name blr ...   one poll, print the metrics, write nothing
  tplink-router-poll.py --report day [--since 2026-11-01] [--client corex] [--csv]
  tplink-router-poll.py --report sessions       every connection, ~ marks an estimated start
  tplink-router-poll.py --rebuild-ledger        rebuild usage.db from the JSONL files
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.request

ROUTER_ACTS = [
    ("go", "DEV2_DEV_INFO"),
    ("gl", "DEV2_HOST_ENTRY"),
    ("gl", "DEV2_STAT_ENTRY"),
    ("gl", "DEV2_WIFI_ASSOC_DEV"),
    ("gl", "DEV2_WIFI_ASSOC_DEV_STAT"),
    ("gl", "DEV2_IP_INTF_STATS"),
    ("go", "DEV2_ONLINESTATUS"),
    ("go", "DEV2_PROC_STATUS"),
    ("go", "DEV2_MEM_STATUS"),
    ("go", "DEV2_SYSLOG_CFG"),
]

# Fields kept from DEV2_DEV_INFO. The rest (serial number, ACS signature, ...)
# is never written anywhere.
INFO_FIELDS = ("modelName", "X_TP_DeviceModel", "hardwareVersion", "softwareVersion", "upTime")


# ---------------------------------------------------------------- parsing ---

def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _mac(v):
    v = str(v or "").strip().lower().replace("-", ":")
    return v if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", v) else ""


def _ip_from_int(v):
    n = _int(v, -1)
    if 0 <= n <= 0xFFFFFFFF:
        return socket.inet_ntoa(n.to_bytes(4, "big"))
    return str(v or "")


def _band(conn: str) -> str:
    c = (conn or "").lower().replace(" ", "")
    if c.startswith("2.4"):
        return "2.4GHz"
    if c.startswith("5"):
        return "5GHz"
    if c.startswith("6"):
        return "6GHz"
    if c in ("ethernet", "wired", "lan"):
        return "wired"
    return "other"


def _stack_head(stack) -> int:
    return _int(str(stack or "").split(",")[0], -1)


def normalise(raw: dict, now: float) -> dict:
    """Turn the router's act replies into one sample. Pure, so it is testable
    without a router."""
    info = raw.get("DEV2_DEV_INFO") or {}
    hosts = raw.get("DEV2_HOST_ENTRY") or []
    stats = raw.get("DEV2_STAT_ENTRY") or []
    assoc = raw.get("DEV2_WIFI_ASSOC_DEV") or []
    assoc_stat = raw.get("DEV2_WIFI_ASSOC_DEV_STAT") or []
    ifstats = raw.get("DEV2_IP_INTF_STATS") or []
    online = raw.get("DEV2_ONLINESTATUS") or {}
    proc = raw.get("DEV2_PROC_STATUS") or {}
    mem = raw.get("DEV2_MEM_STATUS") or {}
    logcfg = raw.get("DEV2_SYSLOG_CFG") or {}

    clients = {}
    for h in hosts if isinstance(hosts, list) else []:
        mac = _mac(h.get("physAddress"))
        if not mac:
            continue
        clients[mac] = {
            "mac": mac,
            "ip": str(h.get("IPAddress") or ""),
            "name": str(h.get("hostName") or "").strip() or "Unknown",
            "band": _band(str(h.get("interfaceType") or "")),
            "active": 1 if str(h.get("active")) == "1" else 0,
        }

    # Per-IP statistics engine. Keyed by MAC when the row has one.
    stat_rows = []
    for s in stats if isinstance(stats, list) else []:
        stat_rows.append({
            "ip": _ip_from_int(s.get("ipAddress")),
            "mac": _mac(s.get("macAddress")),
            "bytes": _int(s.get("totalBytes")),
            "packets": _int(s.get("totalPkts")),
        })

    # Wi-Fi association table and its counters line up by `stack`.
    by_stack = {str(s.get("stack")): s for s in assoc_stat if isinstance(s, dict)}
    wifi = []
    for a in assoc if isinstance(assoc, list) else []:
        mac = _mac(a.get("MACAddress"))
        if not mac:
            continue
        st = by_stack.get(str(a.get("stack")), {})
        known = clients.get(mac, {})
        band = known.get("band") or "other"
        if band in ("other", "wired"):
            band = {1: "2.4GHz", 2: "2.4GHz", 3: "5GHz", 4: "5GHz"}.get(_stack_head(a.get("stack")), "other")
        wifi.append({
            "mac": mac,
            "band": band,
            # The router is the sender: bytesSent went TO the client.
            "download_bytes": _int(st.get("bytesSent")),
            "upload_bytes": _int(st.get("bytesReceived")),
            "download_packets": _int(st.get("packetsSent")),
            "upload_packets": _int(st.get("packetsReceived")),
            "retransmissions": _int(st.get("retransCount")),
            "signal_dbm": _int(a.get("signalStrength") or a.get("RSSI")),
            # Link rates are reported in kbit/s.
            "download_rate_bps": _int(a.get("lastDataDownlinkRate")) * 1000,
            "upload_rate_bps": _int(a.get("lastDataUplinkRate")) * 1000,
            "standard": str(a.get("operatingStandard") or ""),
            # Seconds since the client (re)joined; dates the start of a
            # session first seen part-way through.
            "connected_seconds": _int(a.get("associationTime"), -1),
        })

    # The WAN is the IP interface ONLINESTATUS points at, e.g.
    # "Device.IP.Interface.6." -> the row whose stack starts with 6.
    wan_idx = _int(re.sub(r"\D", "", str(online.get("interfaceV4") or "")), -1)
    wan = None
    for row in ifstats if isinstance(ifstats, list) else []:
        if _stack_head(row.get("stack")) == wan_idx:
            wan = {"download_bytes": _int(row.get("bytesReceived")),
                   "upload_bytes": _int(row.get("bytesSent"))}

    return {
        "ts": round(now, 3),
        "router": {k: str(info.get(k) or "") for k in INFO_FIELDS},
        "uptime_seconds": _int(info.get("upTime")),
        "cpu_percent": _int(proc.get("CPUUsage"), -1),
        "memory_bytes": {"total": _int(mem.get("total")) * 1024, "free": _int(mem.get("free")) * 1024},
        "wan_online": {"ipv4": 1 if online.get("onlineStatusV4") == "online" else 0,
                       "ipv6": 1 if online.get("onlineStatusV6") == "online" else 0},
        "wan": wan,
        # Where the router sends its own log; "here" is set by poll_once.
        "log_forward": {"enabled": 1 if str(logcfg.get("logToRemote")) == "1" else 0,
                        "server": str(logcfg.get("serverIP") or ""), "port": _int(logcfg.get("serverPort"))},
        "clients": sorted(clients.values(), key=lambda c: c["mac"]),
        "stats": stat_rows,
        "wifi": wifi,
    }


# -------------------------------------------------------------- rendering ---

def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**kv) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in kv.items()) + "}"


HELP = {
    "tplink_router_poll_success": ("gauge", "1 if the last poll read the router, 0 if it failed or was skipped."),
    "tplink_router_poll_skipped": ("gauge", "1 if the last poll was skipped on purpose (reason label)."),
    "tplink_router_poll_timestamp_seconds": ("gauge", "Unix time of the last poll attempt."),
    "tplink_router_poll_duration_seconds": ("gauge", "Wall time of the last poll."),
    "tplink_router_info": ("gauge", "Router model and firmware, value always 1."),
    "tplink_router_uptime_seconds": ("gauge", "Router uptime; counters below reset when it restarts."),
    "tplink_router_cpu_percent": ("gauge", "Router CPU use."),
    "tplink_router_memory_bytes": ("gauge", "Router memory by state."),
    "tplink_router_wan_online": ("gauge", "1 if the internet connection is up, by IP family."),
    "tplink_router_wan_bytes_total": ("counter", "Bytes over the internet connection since it came up."),
    "tplink_router_client_info": ("gauge", "Every client the router knows; value 1 if active now."),
    "tplink_router_client_bytes_total": ("counter", "Bytes per client IP from the router's traffic statistics (up and down together)."),
    "tplink_router_client_packets_total": ("counter", "Packets per client IP from the router's traffic statistics."),
    "tplink_router_wifi_client_bytes_total": ("counter", "Bytes per Wi-Fi client by direction; resets when the client reconnects."),
    "tplink_router_wifi_client_packets_total": ("counter", "Packets per Wi-Fi client by direction."),
    "tplink_router_wifi_client_retransmissions_total": ("counter", "Wi-Fi retransmissions to the client."),
    "tplink_router_wifi_client_signal_dbm": ("gauge", "Signal strength of the client as the router hears it."),
    "tplink_router_wifi_client_link_rate_bits": ("gauge", "Last data rate on the Wi-Fi link, by direction."),
    "tplink_router_log_forwarding": ("gauge", "1 if the router's remote log points at this poller (address and port), else 0."),
    "tplink_router_log_messages_total": ("counter", "Router log messages received since the poller started, by severity."),
    "tplink_router_log_rejected_total": ("counter", "Datagrams on the log port from any address but the router's, dropped."),
}

# Router log counters, filled by the listener thread, rendered with the metrics.
LOG_COUNTS: dict[str, int] = {}
LOG_REJECTED = [0]


def render(router: str, sample: dict | None, status: dict) -> str:
    out: dict[str, list[str]] = {k: [] for k in HELP}

    def add(metric, value, **labels):
        out[metric].append(f"{metric}{_labels(router=router, **labels)} {value}")

    add("tplink_router_poll_success", 1 if status["result"] == "ok" else 0)
    add("tplink_router_poll_skipped", 1 if status["result"] in ("busy", "wrong_network") else 0,
        reason=status["result"] if status["result"] in ("busy", "wrong_network") else "")
    add("tplink_router_poll_timestamp_seconds", f'{status["ts"]:.3f}')
    add("tplink_router_poll_duration_seconds", f'{status["duration"]:.3f}')
    for sev, n in sorted(LOG_COUNTS.items()):
        add("tplink_router_log_messages_total", n, severity=sev)
    if LOG_COUNTS or LOG_REJECTED[0]:
        add("tplink_router_log_rejected_total", LOG_REJECTED[0])

    if sample:
        r = sample["router"]
        add("tplink_router_info", 1, model=r.get("modelName") or r.get("X_TP_DeviceModel"),
            hardware=r.get("hardwareVersion"), firmware=r.get("softwareVersion"))
        add("tplink_router_uptime_seconds", sample["uptime_seconds"])
        if sample["cpu_percent"] >= 0:
            add("tplink_router_cpu_percent", sample["cpu_percent"])
        for state, v in sample["memory_bytes"].items():
            add("tplink_router_memory_bytes", v, state=state)
        lf = sample.get("log_forward")
        if lf and "here" in lf:
            add("tplink_router_log_forwarding", lf["here"], server=lf["server"], port=lf["port"])
        for fam, v in sample["wan_online"].items():
            add("tplink_router_wan_online", v, family=fam)
        if sample["wan"]:
            add("tplink_router_wan_bytes_total", sample["wan"]["download_bytes"], direction="download")
            add("tplink_router_wan_bytes_total", sample["wan"]["upload_bytes"], direction="upload")
        names = {c["mac"]: c for c in sample["clients"]}
        for c in sample["clients"]:
            add("tplink_router_client_info", c["active"], mac=c["mac"], ip=c["ip"], name=c["name"], band=c["band"])
        for s in sample["stats"]:
            c = names.get(s["mac"], {})
            name = c.get("name") or next((x["name"] for x in sample["clients"] if x["ip"] == s["ip"]), "Unknown")
            add("tplink_router_client_bytes_total", s["bytes"], ip=s["ip"], mac=s["mac"], name=name)
            add("tplink_router_client_packets_total", s["packets"], ip=s["ip"], mac=s["mac"], name=name)
        for w in sample["wifi"]:
            c = names.get(w["mac"], {})
            lab = dict(mac=w["mac"], name=c.get("name", "Unknown"), ip=c.get("ip", ""), band=w["band"])
            add("tplink_router_wifi_client_bytes_total", w["download_bytes"], direction="download", **lab)
            add("tplink_router_wifi_client_bytes_total", w["upload_bytes"], direction="upload", **lab)
            add("tplink_router_wifi_client_packets_total", w["download_packets"], direction="download", **lab)
            add("tplink_router_wifi_client_packets_total", w["upload_packets"], direction="upload", **lab)
            add("tplink_router_wifi_client_retransmissions_total", w["retransmissions"], **lab)
            add("tplink_router_wifi_client_signal_dbm", w["signal_dbm"], **lab)
            add("tplink_router_wifi_client_link_rate_bits", w["download_rate_bps"], direction="download", **lab)
            add("tplink_router_wifi_client_link_rate_bits", w["upload_rate_bps"], direction="upload", **lab)

    lines = []
    for metric, rows in out.items():
        if rows:
            typ, text = HELP[metric]
            lines += [f"# HELP {metric} {text}", f"# TYPE {metric} {typ}"] + rows
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- the router ---

def _post_plain(url: str, path: str, timeout: float) -> str:
    req = urllib.request.Request(url.rstrip("/") + path, data=b"", method="POST",
                                 headers={"Referer": url.rstrip("/") + "/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def router_busy(url: str, timeout: float) -> bool:
    """True if someone holds the single admin session (pre-auth endpoint)."""
    body = _post_plain(url, "/cgi/getBusy", timeout)
    m = re.search(r"isLogined\s*=\s*(\d+)", body)
    return bool(m and m.group(1) != "0")


def local_ip_towards(ip: str) -> str:
    """This host's address on the route to `ip` (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ip, 9))
            return s.getsockname()[0]
    except OSError:
        return ""


def gateway_mac(ip: str) -> str:
    """MAC for `ip` from the kernel ARP table (filled by the getBusy request)."""
    try:
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip:
                    return _mac(parts[3])
    except OSError:
        pass
    return ""


def read_router(url: str, password: str, username: str, timeout: float) -> dict:
    from tplinkrouterc6u import TPLinkEXClient  # imported late so tests need no deps

    client = TPLinkEXClient(url, password, username=username, timeout=timeout)
    client.authorize()
    raw = {}
    try:
        for op, oid in ROUTER_ACTS:
            _, vals = client.req_act([TPLinkEXClient.ActItem(op, oid)])
            raw[oid] = vals[0] if vals else None
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - logout is best effort; the session also times out
            pass
    return raw


# ------------------------------------------------------------------ files ---

def write_atomic(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


APPEND_LOCK = threading.Lock()


def append_sample(data_dir: str, record: dict, keep_days: int, now: float) -> None:
    with APPEND_LOCK:
        _append_sample(data_dir, record, keep_days, now)


def _append_sample(data_dir: str, record: dict, keep_days: int, now: float) -> None:
    os.makedirs(data_dir, exist_ok=True)
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    with open(os.path.join(data_dir, f"{today.isoformat()}.jsonl"), "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    cutoff = today - dt.timedelta(days=keep_days)
    for name in os.listdir(data_dir):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?", name)
        if not m:
            continue
        day = dt.date.fromisoformat(m.group(1))
        path = os.path.join(data_dir, name)
        if day < cutoff:
            os.remove(path)
        elif day < today and not m.group(2):
            with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(path)


# ----------------------------------------------------------------- ledger ---
#
# The permanent record of who used how much, and when. SQLite in the data dir,
# a few MB a year (estimate: one row per client per hour). Built only from the Wi-Fi counters, the one
# source that matched a measured download (see the role's docs).
#
#   usage     bytes per client per UTC hour. kind 'measured' is traffic seen
#             between two polls; kind 'carried' is traffic a client already had
#             on its counter when first seen (since it joined, or across a gap
#             in polling), booked to the hour it was found.
#   sessions  one row per connection: start (estimated from the router's
#             association time when first seen part-way), end, bytes.
#   names     every (mac, name, ip, band) a client has had, first and last seen.
#   counters  last counter values per client, for the next delta.

GAP_SECONDS = 15 * 60  # longer than this between polls: the gap's traffic is "carried"
SESSION_IDLE_SECONDS = 5 * 60  # gone this long: the session ended when it was last seen
LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  hour TEXT NOT NULL, mac TEXT NOT NULL, kind TEXT NOT NULL,
  download_bytes INTEGER NOT NULL DEFAULT 0, upload_bytes INTEGER NOT NULL DEFAULT 0,
  seconds_seen INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (hour, mac, kind));
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, mac TEXT NOT NULL, start REAL NOT NULL, start_estimated INTEGER NOT NULL,
  end REAL, download_bytes INTEGER NOT NULL DEFAULT 0, upload_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS names (
  mac TEXT NOT NULL, name TEXT NOT NULL, ip TEXT NOT NULL, band TEXT NOT NULL,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY (mac, name, ip, band));
CREATE TABLE IF NOT EXISTS counters (
  mac TEXT PRIMARY KEY, ts REAL NOT NULL, download INTEGER NOT NULL, upload INTEGER NOT NULL,
  session_id INTEGER);
CREATE INDEX IF NOT EXISTS usage_mac ON usage (mac, hour);
CREATE INDEX IF NOT EXISTS sessions_mac ON sessions (mac, start);
CREATE TABLE IF NOT EXISTS events (
  ts REAL NOT NULL, severity TEXT NOT NULL, facility TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


def _hour(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:00Z")


def _iso(ts) -> str:
    return "" if ts is None else dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _logfmt(event: str, **kv) -> str:
    def q(v):
        v = str(v)
        return f'"{v}"' if (not v or re.search(r'[\s"=]', v)) else v
    return event + " " + " ".join(f"{k}={q(str(v).replace(chr(34), chr(39)))}" for k, v in kv.items())


class Ledger:
    def __init__(self, path: str):
        import sqlite3
        self.db = sqlite3.connect(path)
        self.db.executescript(LEDGER_SCHEMA)

    def _add_usage(self, ts, mac, kind, down, up, seen):
        self.db.execute(
            "INSERT INTO usage VALUES (?,?,?,?,?,?) ON CONFLICT (hour, mac, kind) DO UPDATE SET "
            "download_bytes = download_bytes + excluded.download_bytes, upload_bytes = upload_bytes + excluded.upload_bytes, "
            "seconds_seen = seconds_seen + excluded.seconds_seen", (_hour(ts), mac, kind, down, up, seen))

    def update(self, sample: dict) -> list[str]:
        """Book one successful poll. Returns log lines describing it."""
        now = sample["ts"]
        lines = []
        names = {c["mac"]: c for c in sample.get("clients", [])}
        present = set()
        for w in sample.get("wifi", []):
            mac, down, up = w["mac"], w["download_bytes"], w["upload_bytes"]
            present.add(mac)
            ident = names.get(mac, {"name": "Unknown", "ip": "", "band": w.get("band", "other")})
            label = dict(mac=mac, name=ident["name"], ip=ident["ip"], band=ident.get("band") or w.get("band", ""))
            self.db.execute(
                "INSERT INTO names VALUES (?,?,?,?,?,?) ON CONFLICT DO UPDATE SET last_seen = excluded.last_seen",
                (mac, label["name"], label["ip"], label["band"], now, now))
            prev = self.db.execute("SELECT ts, download, upload, session_id FROM counters WHERE mac = ?", (mac,)).fetchone()
            reset = prev is not None and (down < prev[1] or up < prev[2])
            gap = prev is not None and now - prev[0] > GAP_SECONDS
            session_id = prev[3] if prev else None
            if prev is None or (reset and gap):
                # First sight, or a reconnect hidden inside a polling gap: what
                # the counter holds happened before we could see it.
                kind, d_down, d_up, seen = "carried", down, up, 0
                session_id = self._new_session(mac, now, w.get("connected_seconds", -1), prev, lines, label)
            elif reset:
                # Reconnected between two polls: the counter restarted, so the
                # new value is the traffic since then.
                kind, d_down, d_up, seen = "measured", down, up, int(now - prev[0])
                session_id = self._new_session(mac, now, w.get("connected_seconds", -1), prev, lines, label)
            else:
                if gap:
                    kind, d_down, d_up, seen = "carried", down - prev[1], up - prev[2], 0
                else:
                    kind, d_down, d_up, seen = "measured", down - prev[1], up - prev[2], int(now - prev[0])
                if session_id is None:
                    # Missing from one or more polls but never disconnected
                    # (its counter did not restart): carry on the last session.
                    last = self.db.execute("SELECT id FROM sessions WHERE mac = ? ORDER BY start DESC LIMIT 1",
                                           (mac,)).fetchone()
                    session_id = last[0] if last else self._new_session(
                        mac, now, w.get("connected_seconds", -1), None, lines, label)
            self._add_usage(now, mac, kind, d_down, d_up, seen)
            self.db.execute("UPDATE sessions SET download_bytes = download_bytes + ?, upload_bytes = upload_bytes + ?, "
                            "end = NULL WHERE id = ?", (d_down, d_up, session_id))
            self.db.execute("INSERT OR REPLACE INTO counters VALUES (?,?,?,?,?)", (mac, now, down, up, session_id))
            if d_down or d_up:
                lines.append(_logfmt("usage", kind=kind, download_bytes=d_down, upload_bytes=d_up, seconds=seen, **label))
        # Clients that left: close their session at the last poll they were
        # seen, once they have been gone SESSION_IDLE_SECONDS. Phones in power
        # save drop out of the association table for a poll or two without
        # disconnecting; a real reconnect also restarts the counter, which
        # opens a new session above anyway.
        for mac, ts, sid in self.db.execute(
                "SELECT mac, ts, session_id FROM counters WHERE session_id IS NOT NULL").fetchall():
            if mac not in present and now - ts > SESSION_IDLE_SECONDS:
                self._close_session(sid, ts, lines, names.get(mac))
                self.db.execute("UPDATE counters SET session_id = NULL WHERE mac = ?", (mac,))
        self.db.commit()
        return lines

    def _new_session(self, mac, now, connected, prev, lines, label):
        if prev is not None and prev[3] is not None:
            self._close_session(prev[3], prev[0], lines, label)
        estimated = connected is not None and connected >= 0
        start = now - connected if estimated else now
        cur = self.db.execute("INSERT INTO sessions (mac, start, start_estimated) VALUES (?,?,?)",
                              (mac, start, 1 if estimated else 0))
        lines.append(_logfmt("session_start", start=_iso(start), **label))
        return cur.lastrowid

    def _close_session(self, sid, end, lines, label):
        row = self.db.execute("SELECT mac, start, download_bytes, upload_bytes FROM sessions WHERE id = ?", (sid,)).fetchone()
        if not row:
            return
        self.db.execute("UPDATE sessions SET end = ? WHERE id = ?", (end, sid))
        label = label or {"mac": row[0]}
        lines.append(_logfmt("session_end", start=_iso(row[1]), end=_iso(end), seconds=int(end - row[1]),
                             download_bytes=row[2], upload_bytes=row[3],
                             **{k: label.get(k, "") for k in ("mac", "name", "ip", "band")}))

    def snapshot(self, path: str) -> None:
        """A consistent copy for the nightly batch transfer."""
        import sqlite3
        tmp = path + ".tmp"
        dst = sqlite3.connect(tmp)
        self.db.backup(dst)
        dst.close()
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)


def jsonl_records(data_dir: str):
    """Every stored sample, oldest first, from the daily files."""
    for name in sorted(os.listdir(data_dir)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl(\.gz)?", name):
            continue
        path = os.path.join(data_dir, name)
        with (gzip.open(path, "rt") if name.endswith(".gz") else open(path)) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def rebuild_ledger(data_dir: str, db_path: str) -> int:
    """Rebuild the ledger from the raw files, e.g. after it was lost."""
    if os.path.exists(db_path):
        os.remove(db_path)
    ledger = Ledger(db_path)
    n = 0
    for rec in jsonl_records(data_dir):
        if rec.get("poll") == "ok" and "wifi" in rec:
            ledger.update(rec)
            n += 1
        elif isinstance(rec.get("router_log"), dict):
            e = rec["router_log"]
            ledger.db.execute("INSERT INTO events VALUES (?,?,?,?)",
                              (rec["ts"], e.get("severity", ""), e.get("facility", ""), e.get("message", "")))
    ledger.db.commit()
    return n


# ------------------------------------------------------------- router log ---
#
# The router's own log (Wi-Fi joins and leaves, DHCP, logins, reboots) lives
# only in its RAM. With "remote log" pointed at this host it sends each line
# as a UDP datagram. Its format is not RFC 3164: just "<PRI> text", with no
# timestamp or hostname, which is why this is received here rather than by
# Alloy's syslog parser (which rejects it). Only the router's address is
# accepted. Each line goes to the events table, the JSONL copy and the journal.

SEVERITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
FACILITIES = ["kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news", "uucp", "cron",
              "authpriv", "ftp", "ntp", "audit", "alert", "clock"] + [f"local{i}" for i in range(8)]


def parse_router_log(data: bytes) -> dict:
    text = data.decode("utf-8", "replace").strip().replace("\x00", "")
    m = re.match(r"<(\d{1,3})>\s*(.*)", text, re.S)
    if not m:
        return {"severity": "unknown", "facility": "unknown", "message": text}
    pri = int(m.group(1))
    fac = FACILITIES[pri >> 3] if (pri >> 3) < len(FACILITIES) else str(pri >> 3)
    return {"severity": SEVERITIES[pri & 7], "facility": fac, "message": " ".join(m.group(2).split())}


def handle_router_log(data: bytes, sender: str, router_ip: str, db_path: str, data_dir: str,
                      keep_days: int, now: float) -> dict | None:
    """One datagram: drop it unless the router sent it, else record it."""
    if sender != router_ip:
        LOG_REJECTED[0] += 1
        return None
    e = parse_router_log(data)
    LOG_COUNTS[e["severity"]] = LOG_COUNTS.get(e["severity"], 0) + 1
    import sqlite3
    db = sqlite3.connect(db_path, timeout=30)
    try:
        db.executescript(LEDGER_SCHEMA)
        db.execute("INSERT INTO events VALUES (?,?,?,?)", (now, e["severity"], e["facility"], e["message"]))
        db.commit()
    finally:
        db.close()
    if data_dir:
        append_sample(data_dir, {"ts": round(now, 3), "router_log": e}, keep_days, now)
    print(_logfmt("router_log", severity=e["severity"], facility=e["facility"], msg=e["message"]), flush=True)
    return e


def router_log_listener(port: int, router_ip: str, db_path: str, data_dir: str, keep_days: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    while True:
        data, (sender, _) = sock.recvfrom(4096)
        try:
            handle_router_log(data, sender, router_ip, db_path, data_dir, keep_days, time.time())
        except Exception as e:  # noqa: BLE001 - one bad datagram must not stop the listener
            print(f"router log: {type(e).__name__}: {str(e)[:200]}", flush=True)


# ----------------------------------------------------------------- report ---

def report(db_path: str, by: str, since: str, until: str, client: str, csv_out: bool) -> int:
    import sqlite3
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    latest = {mac: name for mac, name in db.execute(
        "SELECT mac, name FROM names n WHERE last_seen = (SELECT max(last_seen) FROM names WHERE mac = n.mac)")}
    match = lambda mac: not client or client.lower() in (mac + " " + latest.get(mac, "")).lower()  # noqa: E731
    if by == "log":
        head = ["time", "severity", "message"]
        rows = []
        for ts, sev, msg in db.execute("SELECT ts, severity, message FROM events ORDER BY ts"):
            day = _iso(ts)[:10]
            if (since and day < since) or (until and day > until) or (client and client.lower() not in msg.lower()):
                continue
            rows.append([_iso(ts), sev, msg])
    elif by == "sessions":
        # Visits: a client's connections less than SESSION_IDLE_SECONDS apart
        # are one visit (a phone hopping between the 2.4 and 5 GHz radios
        # reconnects, and its counter restarts, without really leaving).
        head = ["start", "end", "minutes", "client", "mac", "download_mb", "upload_mb", "connections"]
        visits = []
        for mac, start, est, end, down, up in db.execute(
                "SELECT mac, start, start_estimated, end, download_bytes, upload_bytes FROM sessions ORDER BY mac, start"):
            v = visits[-1] if visits and visits[-1]["mac"] == mac else None
            if v and v["end"] is not None and start - v["end"] <= SESSION_IDLE_SECONDS:
                v.update(end=end, down=v["down"] + down, up=v["up"] + up, n=v["n"] + 1)
            else:
                visits.append({"mac": mac, "start": start, "est": est, "end": end, "down": down, "up": up, "n": 1})
        rows = []
        for v in sorted(visits, key=lambda v: v["start"]):
            day = _iso(v["start"])[:10]
            if (since and day < since) or (until and day > until) or not match(v["mac"]):
                continue
            rows.append([("~" if v["est"] else "") + _iso(v["start"]), _iso(v["end"]) or "connected",
                         round(((v["end"] or time.time()) - v["start"]) / 60), latest.get(v["mac"], "Unknown"),
                         v["mac"], round(v["down"] / 1e6, 2), round(v["up"] / 1e6, 2), v["n"]])
    else:
        width = {"hour": 17, "day": 10, "month": 7}[by]
        head = [by, "client", "mac", "download_mb", "upload_mb", "total_mb", "carried_mb", "minutes_seen"]
        agg = {}
        for hour, mac, kind, down, up, seen in db.execute(
                "SELECT hour, mac, kind, download_bytes, upload_bytes, seconds_seen FROM usage ORDER BY hour"):
            day = hour[:10]
            if (since and day < since) or (until and day > until) or not match(mac):
                continue
            a = agg.setdefault((hour[:width], mac), [0, 0, 0, 0])
            a[0] += down
            a[1] += up
            a[2] += (down + up) if kind == "carried" else 0
            a[3] += seen
        rows = [[k[0], latest.get(k[1], "Unknown"), k[1], round(v[0] / 1e6, 2), round(v[1] / 1e6, 2),
                 round((v[0] + v[1]) / 1e6, 2), round(v[2] / 1e6, 2), round(v[3] / 60)]
                for k, v in sorted(agg.items(), key=lambda kv: (kv[0][0], -(kv[1][0] + kv[1][1])))]
    if csv_out:
        import csv
        w = csv.writer(sys.stdout)
        w.writerow(head)
        w.writerows(rows)
    else:
        widths = [max(len(str(x)) for x in col) for col in zip(head, *rows)] if rows else [len(h) for h in head]
        for r in [head] + rows:
            print("  ".join(str(x).ljust(wd) for x, wd in zip(r, widths)))
    return 0


# ------------------------------------------------------------------- main ---

def poll_once(args, password: str) -> tuple[dict | None, dict]:
    start = time.time()
    status = {"ts": start, "result": "error", "error": ""}
    sample = None
    try:
        busy = router_busy(args.router, args.timeout)
        host = re.sub(r"^https?://", "", args.router).split("/")[0].split(":")[0]
        seen = gateway_mac(host)
        if args.expected_mac and seen != _mac(args.expected_mac):
            status["result"] = "wrong_network"
            status["error"] = f"router MAC {seen or 'unknown'} is not the expected one"
        elif busy:
            status["result"] = "busy"
        else:
            sample = normalise(read_router(args.router, password, args.username, args.timeout), time.time())
            if getattr(args, "syslog_port", 0):
                lf = sample["log_forward"]
                lf["here"] = int(bool(lf["enabled"]) and lf["server"] == local_ip_towards(host)
                                 and lf["port"] == args.syslog_port)
            status["result"] = "ok"
    except Exception as e:  # noqa: BLE001 - any failure becomes poll_success 0
        status["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    status["duration"] = time.time() - start
    return sample, status


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--router", default="http://192.168.0.1", help="router base URL")
    p.add_argument("--name", help="value of the router label, e.g. blr; required when polling")
    p.add_argument("--username", default="admin")
    p.add_argument("--password-file", default=os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", ""), "password"))
    p.add_argument("--expected-mac", default="", help="router LAN MAC; the password is only sent to it")
    p.add_argument("--textfile", default="", help="metrics file for the textfile collector")
    p.add_argument("--data-dir", default="/var/lib/tplink-router-poll",
                   help="daily JSONL files, the usage ledger (usage.db) and its nightly snapshot")
    p.add_argument("--keep-days", type=int, default=365, help="days of JSONL kept; the ledger is kept forever")
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--timeout", type=float, default=20)
    p.add_argument("--once", action="store_true", help="one poll, print metrics, write nothing")
    p.add_argument("--syslog-port", type=int, default=0,
                   help="UDP port to receive the router's own log on (its remote log setting); 0 is off")
    p.add_argument("--report", choices=["hour", "day", "month", "sessions", "log"],
                   help="print usage from the ledger instead of polling")
    p.add_argument("--since", default="", help="report: first UTC day, YYYY-MM-DD")
    p.add_argument("--until", default="", help="report: last UTC day, YYYY-MM-DD")
    p.add_argument("--client", default="",
                   help="report: only clients whose name or MAC contains this (log: messages containing it)")
    p.add_argument("--csv", action="store_true", help="report: CSV instead of a table")
    p.add_argument("--rebuild-ledger", action="store_true", help="rebuild usage.db from the JSONL files and exit")
    args = p.parse_args()
    db_path = os.path.join(args.data_dir, "usage.db")

    if args.report:
        return report(db_path, args.report, args.since, args.until, args.client, args.csv)
    if args.rebuild_ledger:
        print(f"rebuilt {db_path} from {rebuild_ledger(args.data_dir, db_path)} samples")
        return 0
    if not args.name:
        p.error("--name is required when polling")

    with open(args.password_file) as f:
        password = f.read().strip()

    ledger = None
    snapshot_day = None
    if not args.once:
        os.makedirs(args.data_dir, exist_ok=True)
        if not os.path.exists(db_path) and any(n.endswith((".jsonl", ".jsonl.gz")) for n in os.listdir(args.data_dir)):
            print(f"ledger missing: rebuilt from {rebuild_ledger(args.data_dir, db_path)} stored samples", flush=True)
        ledger = Ledger(db_path)
        if args.syslog_port:
            router_ip = re.sub(r"^https?://", "", args.router).split("/")[0].split(":")[0]
            threading.Thread(target=router_log_listener, daemon=True, name="router-log",
                             args=(args.syslog_port, router_ip, db_path, args.data_dir, args.keep_days)).start()

    while True:
        sample, status = poll_once(args, password)
        text = render(args.name, sample, status)
        if args.once:
            sys.stdout.write(text)
            if status["error"]:
                print(f"# poll: {status['result']}: {status['error']}", file=sys.stderr)
            return 0 if status["result"] == "ok" else 1
        if args.textfile:
            write_atomic(args.textfile, text)
        record = sample if sample else {"ts": round(status["ts"], 3)}
        record = dict(record, poll=status["result"], error=status["error"] or None)
        append_sample(args.data_dir, record, args.keep_days, status["ts"])
        if sample:
            # One journal line per client with traffic, per session start and
            # end. Alloy ships the journal to Loki, where Grafana sums them.
            for line in ledger.update(record):
                print(line, flush=True)
        today = _iso(status["ts"])[:10]
        if today != snapshot_day:
            ledger.snapshot(os.path.join(args.data_dir, "usage-snapshot.db"))
            snapshot_day = today
        if status["result"] != "ok":
            print(f"poll {status['result']}: {status['error']}", flush=True)
        time.sleep(max(5.0, args.interval - status["duration"]))


if __name__ == "__main__":
    sys.exit(main())
