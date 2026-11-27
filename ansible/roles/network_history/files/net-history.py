#!/usr/bin/env python3
"""net-history: roll the network metrics up into permanent daily rows in PostgreSQL.

Reads the 90-day Prometheus tier (and Loki for outage events), computes one
value per day per key with increase() evaluated at the end of each IST day, and
upserts it into the `net` schema of the net_history database. Daily rows are
never deleted; the monthly views (net.monthly_*) group them. Runs hourly from
net-history.timer and is idempotent: it recomputes today and the previous two
days every time.

  net-history.py                          hourly run: today and the two days before (a first run
                                          with no --backfill and no seed back-fills 90 days)
  net-history.py --backfill 90            recompute the last 90 days now
  net-history.py --import-wan-daily f.csv seed wan_daily from the ER605 ledger
                                          (day,download_bytes,upload_bytes[,wan][,coverage])
  net-history.py --report month           print the monthly views
  net-history.py --report month --table wan

Needs the pure-python driver pg8000 (the role installs it in a virtualenv). The
password is read from --password-file, never from the command line.

Tables (schema net), one row per IST day per key, all documented with COMMENT
and listed by `SELECT * FROM net.catalog`:
  internet_daily, lan_daily, path_daily, wan_daily, segment_daily, segflow_daily,
  client_daily, guest_daily, tailnet_daily, outage_events
Views: monthly_internet, monthly_lan, monthly_path, monthly_wan, monthly_segment,
  monthly_segflow, monthly_client, monthly_guest, monthly_tailnet, coverage,
  catalog, catalog_columns.

Coverage: the 90-day store and the central Prometheus only know the days since their first
sample. For every table and day the aggregator takes the store that covers most of the day
(the 90-day store wins ties), stores the covered share in the row's `coverage`, and never replaces
a stored row by one with lower coverage (today's row is always recomputed). Days with coverage
below 1 are counted in the monthly views' partial_days. Rows imported from the ER605 ledger win
over lower-coverage Prometheus rows.

A source that has no data (a collector not deployed yet) skips its table with
one `skip` log line; the run still succeeds. A failing Prometheus, Loki or
database makes the run fail (exit 1, net_history_run_success 0).

Log lines are logfmt, first word the event: skip, loki, run.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
DAY = 86400
RECENT_DAYS = 2            # days before today recomputed on every run
BACKFILL_DEFAULT = 90      # first run: as far as the 90-day store goes
LOKI_RECENT_DAYS = 2
LOKI_BACKFILL_DAYS = 30    # Loki retention
RANGE_CHUNK_DAYS = 31
LOKI_LIMIT = 5000
SCHEMA = "net"

TABLE_NAMES = {  # --table / report name -> daily table, monthly view
    "internet": ("internet_daily", "monthly_internet"),
    "lan": ("lan_daily", "monthly_lan"),
    "path": ("path_daily", "monthly_path"),
    "wan": ("wan_daily", "monthly_wan"),
    "segment": ("segment_daily", "monthly_segment"),
    "segflow": ("segflow_daily", "monthly_segflow"),
    "client": ("client_daily", "monthly_client"),
    "guest": ("guest_daily", "monthly_guest"),
    "tailnet": ("tailnet_daily", "monthly_tailnet"),
}

DEFAULT_WAN = "wan1"   # the label used while the ER605 poller has no wan label yet
SEGMENTS_RE = "LAN|TENANT|DMZ|LAB|LEASE|WAN"
GUEST_DEV = "(tap|veth)[0-9]+i[0-9]+"

# ------------------------------------------------------------------ schema ---

# (table, description, [(column, type, description)], primary key)
DAILY = [
    ("internet_daily",
     "One row per IST day per site: confirmed internet downtime and outages, from the internet-probe counters.",
     [("site", "text NOT NULL", "Site label of the probing host: home, blr or homelab."),
      ("window_seconds", "double precision NOT NULL", "Seconds the day covers: 86400, or the elapsed part for today."),
      ("down_seconds", "double precision NOT NULL", "Seconds of confirmed outage in the window (blips excluded)."),
      ("outages", "integer NOT NULL", "Confirmed outages that started in the window."),
      ("availability", "double precision NOT NULL", "1 - down_seconds / window_seconds, between 0 and 1.")],
     ("day", "site")),
    ("lan_daily",
     "One row per IST day per site and LAN target: inter-LAN reachability from the internet-probe hosts.",
     [("site", "text NOT NULL", "Site label of the probing host."),
      ("name", "text NOT NULL", "LAN target name, for example er605 or opnsense-wan."),
      ("window_seconds", "double precision NOT NULL", "Seconds the day covers: 86400, or the elapsed part for today."),
      ("down_seconds", "double precision NOT NULL", "Seconds the target was confirmed unreachable."),
      ("outages", "integer NOT NULL", "Confirmed LAN outages in the window.")],
     ("day", "site", "name")),
    ("path_daily",
     "One row per IST day per site and named probe path: downtime of a path that the router sends out one WAN (a per-WAN probe).",
     [("site", "text NOT NULL", "Site label of the probing host."),
      ("name", "text NOT NULL", "Path name, normally the WAN it measures (wan1, wan2)."),
      ("window_seconds", "double precision NOT NULL", "Seconds the day covers: 86400, or the elapsed part for today."),
      ("down_seconds", "double precision NOT NULL", "Seconds the path was confirmed down."),
      ("outages", "integer NOT NULL", "Confirmed path outages in the window."),
      ("availability", "double precision NOT NULL", "1 - down_seconds / window_seconds, between 0 and 1.")],
     ("day", "site", "name")),
    ("wan_daily",
     "One row per IST day per ER605 WAN: internet volume at that WAN port and its PPPoE reconnects.",
     [("wan", "text NOT NULL", "WAN name from the ER605 poller (wan1, wan2); wan1 for history before the poller labelled WANs."),
      ("download_bytes", "bigint", "Bytes received on the WAN interface."),
      ("upload_bytes", "bigint", "Bytes sent on the WAN interface."),
      ("pppoe_reconnects", "integer", "PPPoE reconnects seen by the ER605 poller for this WAN."),
      ("source", "text NOT NULL DEFAULT 'prometheus'", "prometheus, or import for rows seeded from the ER605 ledger CSV.")],
     ("day", "wan")),
    ("segment_daily",
     "One row per IST day per OPNsense interface: bytes routed for each segment.",
     [("interface", "text NOT NULL", "OPNsense interface label: LAN, TENANT, DMZ, LAB, LEASE or WAN."),
      ("received_bytes", "bigint", "Bytes the segment's hosts sent into the firewall."),
      ("transmitted_bytes", "bigint", "Bytes the firewall delivered to the segment.")],
     ("day", "interface")),
    ("segflow_daily",
     "One row per IST day per segment pair: NetFlow bytes, packets and flows between OPNsense segments.",
     [("src_segment", "text NOT NULL", "Segment the traffic came from."),
      ("dst_segment", "text NOT NULL", "Segment the traffic went to."),
      ("bytes", "bigint", "Bytes of flows from src_segment to dst_segment."),
      ("packets", "bigint", "Packets of the same flows."),
      ("flows", "bigint", "NetFlow records (a long connection is several).")],
     ("day", "src_segment", "dst_segment")),
    ("client_daily",
     "One row per IST day per Wi-Fi client of a TP-Link router: download and upload bytes.",
     [("router", "text NOT NULL", "Router label, for example blr."),
      ("mac", "text NOT NULL", "Client MAC address."),
      ("name", "text", "Client name the router reported (the one with the most bytes that day)."),
      ("download_bytes", "bigint", "Bytes the client downloaded over Wi-Fi."),
      ("upload_bytes", "bigint", "Bytes the client uploaded over Wi-Fi.")],
     ("day", "router", "mac")),
    ("guest_daily",
     "One row per IST day per Proxmox guest: bytes crossing its tap or veth NICs (the Guest Traffic dashboard join).",
     [("guest", "text NOT NULL", "Guest name from fleet_pve_guest_nic_info."),
      ("sent_bytes", "bigint", "Bytes the guest sent (received on its host-side NIC)."),
      ("received_bytes", "bigint", "Bytes the guest received (transmitted on its host-side NIC).")],
     ("day", "guest")),
    ("tailnet_daily",
     "One row per IST day per host: bytes on the tailscale0 interface.",
     [("host", "text NOT NULL", "Host label."),
      ("received_bytes", "bigint", "Bytes received on tailscale0."),
      ("transmitted_bytes", "bigint", "Bytes transmitted on tailscale0.")],
     ("day", "host")),
]
DAY_COMMENT = "IST calendar day (Asia/Kolkata) the row covers."
UPDATED_COMMENT = "When this row was last written by net-history."

# monthly view: (view, source table, key columns, [(column, expression, description)], view description)
MONTHLY = [
    ("monthly_internet", "internet_daily", ["site"],
     [("window_seconds", "sum(window_seconds)", "Seconds observed in the month."),
      ("down_seconds", "sum(down_seconds)", "Confirmed outage seconds in the month."),
      ("outages", "sum(outages)::integer", "Confirmed outages in the month."),
      ("availability", "greatest(0, 1 - sum(down_seconds) / nullif(sum(window_seconds), 0))",
       "1 - down_seconds / window_seconds, recomputed from the monthly sums (not an average of daily ratios).")],
     "Internet availability, downtime and outages per site per month."),
    ("monthly_lan", "lan_daily", ["site", "name"],
     [("window_seconds", "sum(window_seconds)", "Seconds observed in the month."),
      ("down_seconds", "sum(down_seconds)", "Confirmed unreachable seconds in the month."),
      ("outages", "sum(outages)::integer", "Confirmed LAN outages in the month."),
      ("availability", "greatest(0, 1 - sum(down_seconds) / nullif(sum(window_seconds), 0))",
       "1 - down_seconds / window_seconds from the monthly sums.")],
     "Inter-LAN target availability and outages per site and target per month."),
    ("monthly_path", "path_daily", ["site", "name"],
     [("window_seconds", "sum(window_seconds)", "Seconds observed in the month."),
      ("down_seconds", "sum(down_seconds)", "Confirmed path-down seconds in the month."),
      ("outages", "sum(outages)::integer", "Confirmed path outages in the month."),
      ("availability", "greatest(0, 1 - sum(down_seconds) / nullif(sum(window_seconds), 0))",
       "1 - down_seconds / window_seconds from the monthly sums.")],
     "Availability of each per-WAN probe path per month."),
    ("monthly_wan", "wan_daily", ["wan"],
     [("download_bytes", "sum(download_bytes)::bigint", "Download bytes in the month."),
      ("upload_bytes", "sum(upload_bytes)::bigint", "Upload bytes in the month."),
      ("total_bytes", "(sum(download_bytes) + sum(upload_bytes))::bigint", "Download plus upload."),
      ("pppoe_reconnects", "sum(pppoe_reconnects)::integer", "PPPoE reconnects in the month.")],
     "Household internet volume and PPPoE reconnects per WAN per month, plus a row with wan = 'total' that sums all WANs."),
    ("monthly_segment", "segment_daily", ["interface"],
     [("received_bytes", "sum(received_bytes)::bigint", "Bytes the segment sent into the firewall."),
      ("transmitted_bytes", "sum(transmitted_bytes)::bigint", "Bytes the firewall delivered to the segment."),
      ("total_bytes", "(sum(received_bytes) + sum(transmitted_bytes))::bigint", "Received plus transmitted.")],
     "Bytes routed per OPNsense segment per month."),
    ("monthly_segflow", "segflow_daily", ["src_segment", "dst_segment"],
     [("bytes", "sum(bytes)::bigint", "Bytes from src_segment to dst_segment."),
      ("packets", "sum(packets)::bigint", "Packets of those flows."),
      ("flows", "sum(flows)::bigint", "NetFlow records.")],
     "Traffic between OPNsense segments per month."),
    ("monthly_client", "client_daily", ["router", "mac"],
     [("name", "(array_agg(name ORDER BY day DESC) FILTER (WHERE name IS NOT NULL))[1]", "Most recent name the router reported."),
      ("download_bytes", "sum(download_bytes)::bigint", "Bytes downloaded in the month."),
      ("upload_bytes", "sum(upload_bytes)::bigint", "Bytes uploaded in the month."),
      ("total_bytes", "(sum(download_bytes) + sum(upload_bytes))::bigint", "Download plus upload.")],
     "Wi-Fi usage per client of a TP-Link router per month."),
    ("monthly_guest", "guest_daily", ["guest"],
     [("sent_bytes", "sum(sent_bytes)::bigint", "Bytes the guest sent in the month."),
      ("received_bytes", "sum(received_bytes)::bigint", "Bytes the guest received in the month."),
      ("total_bytes", "(sum(sent_bytes) + sum(received_bytes))::bigint", "Sent plus received.")],
     "Traffic per Proxmox guest per month."),
    ("monthly_tailnet", "tailnet_daily", ["host"],
     [("received_bytes", "sum(received_bytes)::bigint", "Bytes received on tailscale0."),
      ("transmitted_bytes", "sum(transmitted_bytes)::bigint", "Bytes transmitted on tailscale0."),
      ("total_bytes", "(sum(received_bytes) + sum(transmitted_bytes))::bigint", "Received plus transmitted.")],
     "Tailnet traffic per host per month."),
]
WITH_TOTAL = {"monthly_wan": "wan"}   # view -> key column that gets a 'total' row
PARTIAL_DAYS_COMMENT = ("Days in the month whose daily row had coverage below 1 (the data source did not span the whole day) or unknown. "
                        "A month with partial_days > 0 understates volumes and counts.")
COVERAGE_COMMENT = ("Share of the IST day (0 to 1, or of the elapsed part for today) that the data source actually covered when the row was "
                    "computed; below 1 means the day is partial. A stored row is never replaced by one with lower coverage, except today's.")
MONTH_COMMENT = "First day of the IST calendar month."
DAYS_COMMENT = "Number of daily rows that went into the month (fewer than the days in the month means gaps or a partial month)."

OUTAGE_COLUMNS = [
    ("site", "text NOT NULL", "Site label of the probing host."),
    ("kind", "text NOT NULL CHECK (kind IN ('internet', 'lan', 'path', 'wan'))",
     "internet: the site lost the internet; lan: a LAN target was unreachable; path: a per-WAN probe path was down; wan: an ER605 WAN port or PPPoE session was down."),
    ("name", "text NOT NULL", "internet for internet outages; the LAN target, path or WAN name otherwise."),
    ("target", "text NOT NULL DEFAULT ''", "HOST:PORT of the LAN target; empty for the other kinds."),
    ("started_at", "timestamptz NOT NULL", "When the outage started (first failed round)."),
    ("ended_at", "timestamptz", "When the outage ended."),
    ("duration_seconds", "double precision", "Outage length in seconds."),
    ("cause", "text", "Probe's cause label for internet outages (for example gateway or upstream); NULL for the other kinds."),
    ("closed_by", "text", "probe, or restart when the poller restarted during the outage (the end is then the restart time); NULL for wan."),
    ("recorded_at", "timestamptz NOT NULL DEFAULT now()", "When net-history stored or last updated the event."),
]

EXAMPLES = {
    "internet_daily": "SELECT day, site, round(down_seconds::numeric / 60, 1) AS down_min, outages, round(availability::numeric * 100, 3) AS pct, coverage FROM net.internet_daily WHERE outages > 0 ORDER BY day DESC LIMIT 30",
    "lan_daily": "SELECT day, site, name, down_seconds, outages FROM net.lan_daily WHERE outages > 0 ORDER BY day DESC LIMIT 30",
    "path_daily": "SELECT day, site, name, down_seconds, outages, round(availability::numeric * 100, 3) AS pct FROM net.path_daily WHERE outages > 0 ORDER BY day DESC LIMIT 30",
    "wan_daily": "SELECT day, wan, pg_size_pretty(download_bytes) AS down, pg_size_pretty(upload_bytes) AS up, coverage, source FROM net.wan_daily ORDER BY day DESC, wan LIMIT 14",
    "segment_daily": "SELECT day, interface, pg_size_pretty(received_bytes + transmitted_bytes) AS total FROM net.segment_daily ORDER BY day DESC, 3 DESC LIMIT 20",
    "segflow_daily": "SELECT day, src_segment, dst_segment, pg_size_pretty(bytes) FROM net.segflow_daily ORDER BY day DESC, bytes DESC LIMIT 20",
    "client_daily": "SELECT day, name, pg_size_pretty(download_bytes + upload_bytes) AS total FROM net.client_daily ORDER BY day DESC, download_bytes + upload_bytes DESC LIMIT 20",
    "guest_daily": "SELECT day, guest, pg_size_pretty(sent_bytes + received_bytes) AS total FROM net.guest_daily ORDER BY day DESC, sent_bytes + received_bytes DESC LIMIT 20",
    "tailnet_daily": "SELECT day, host, pg_size_pretty(received_bytes + transmitted_bytes) AS total FROM net.tailnet_daily ORDER BY day DESC, 3 DESC LIMIT 20",
    "outage_events": "SELECT site, kind, name, started_at, ended_at, round(duration_seconds) AS seconds, cause FROM net.outage_events WHERE kind IN ('internet', 'wan') ORDER BY started_at DESC LIMIT 20",
    "monthly_internet": "SELECT month, site, outages, round(down_seconds::numeric / 3600, 2) AS down_hours, round(availability::numeric * 100, 3) AS pct FROM net.monthly_internet ORDER BY month DESC, site",
    "monthly_lan": "SELECT month, site, name, outages, round(availability::numeric * 100, 3) AS pct FROM net.monthly_lan ORDER BY month DESC, site, name",
    "monthly_path": "SELECT month, site, name, outages, round(availability::numeric * 100, 3) AS pct FROM net.monthly_path ORDER BY month DESC, site, name",
    "monthly_wan": "SELECT month, wan, pg_size_pretty(download_bytes) AS down, pg_size_pretty(upload_bytes) AS up, pppoe_reconnects, partial_days FROM net.monthly_wan ORDER BY month DESC, wan",
    "monthly_segment": "SELECT month, interface, pg_size_pretty(total_bytes) FROM net.monthly_segment ORDER BY month DESC, total_bytes DESC",
    "monthly_segflow": "SELECT month, src_segment, dst_segment, pg_size_pretty(bytes) FROM net.monthly_segflow ORDER BY month DESC, bytes DESC LIMIT 20",
    "monthly_client": "SELECT month, name, pg_size_pretty(total_bytes) FROM net.monthly_client ORDER BY month DESC, total_bytes DESC LIMIT 20",
    "monthly_guest": "SELECT month, guest, pg_size_pretty(total_bytes) FROM net.monthly_guest ORDER BY month DESC, total_bytes DESC LIMIT 20",
    "monthly_tailnet": "SELECT month, host, pg_size_pretty(total_bytes) FROM net.monthly_tailnet ORDER BY month DESC, total_bytes DESC LIMIT 20",
    "coverage": "SELECT * FROM net.coverage",
    "catalog": "SELECT object_name, kind, description FROM net.catalog",
    "catalog_columns": "SELECT column_name, data_type, description FROM net.catalog_columns WHERE object_name = 'monthly_wan'",
    "schema_version": "SELECT * FROM net.schema_version",
    "meta": "SELECT key, value FROM net.meta",
}


def q(text):
    return "'" + str(text).replace("'", "''") + "'"


def monthly_statements(partial):
    """CREATE VIEW + COMMENT statements of the monthly_* views; `partial` adds the partial_days column (schema 2)."""
    s = []
    for view, src, keys, measures, vdesc in MONTHLY:
        month = "date_trunc('month', day)::date"
        sel = [month + " AS month"] + keys + ["count(DISTINCT day)::integer AS days"]
        if partial:
            sel.append("(count(DISTINCT day) FILTER (WHERE coverage IS NULL OR coverage < 1))::integer AS partial_days")
        sel += ["%s AS %s" % (expr, c) for c, expr, _ in measures]
        grp = ", ".join([month] + keys)
        if view in WITH_TOTAL:  # one extra row per month, key = 'total', summing every key
            sel = [x if x != WITH_TOTAL[view] else "coalesce(%s, 'total') AS %s" % (x, x) for x in sel]
            grp = "GROUPING SETS ((%s), (%s))" % (grp, month)
        s.append("CREATE VIEW net.%s AS SELECT %s FROM net.%s GROUP BY %s" % (view, ", ".join(sel), src, grp))
        s.append("COMMENT ON VIEW net.%s IS %s" % (view, q(vdesc + " Built from net." + src + ".")))
        s.append("COMMENT ON COLUMN net.%s.month IS %s" % (view, q(MONTH_COMMENT)))
        keydesc = {c: d for t, _, cs, _ in DAILY for c, _, d in cs if t == src}
        for k in keys:
            s.append("COMMENT ON COLUMN net.%s.%s IS %s" % (view, k, q(keydesc[k])))
        s.append("COMMENT ON COLUMN net.%s.days IS %s" % (view, q(DAYS_COMMENT)))
        if partial:
            s.append("COMMENT ON COLUMN net.%s.partial_days IS %s" % (view, q(PARTIAL_DAYS_COMMENT)))
        for c, _, d in measures:
            s.append("COMMENT ON COLUMN net.%s.%s IS %s" % (view, c, q(d)))
    return s


def catalog_statements():
    """The net.catalog view and its comments (recreated by every migration that changes an example)."""
    s = []
    values = ", ".join("(%s, %s)" % (q(k), q(v)) for k, v in sorted(EXAMPLES.items()))
    s.append(
        "CREATE VIEW net.catalog AS SELECT c.relname::text AS object_name, "
        "CASE c.relkind WHEN 'r' THEN 'table' ELSE 'view' END AS kind, "
        "obj_description(c.oid, 'pg_class') AS description, e.example_query "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'net' "
        "LEFT JOIN (VALUES " + values + ") AS e(object_name, example_query) ON e.object_name = c.relname "
        "WHERE c.relkind IN ('r', 'v') ORDER BY CASE c.relkind WHEN 'r' THEN 0 ELSE 1 END, c.relname")
    s.append("COMMENT ON VIEW net.catalog IS " + q("Every table and view in schema net with a one-line description and an example query. The place to start."))
    for c, d in [("object_name", "Table or view name inside schema net."), ("kind", "table or view."),
                 ("description", "What the object holds."), ("example_query", "A query to copy and adapt.")]:
        s.append("COMMENT ON COLUMN net.catalog.%s IS %s" % (c, q(d)))
    return s


def migration_1():
    """The initial schema: tables, monthly views, coverage, catalog; every object commented."""
    s = []
    s.append("CREATE SCHEMA IF NOT EXISTS net")
    s.append("COMMENT ON SCHEMA net IS " + q(
        "Permanent network history rolled up from Prometheus and Loki by net-history on the monitoring host. "
        "Start with SELECT * FROM net.catalog."))
    s.append("CREATE TABLE IF NOT EXISTS net.meta (key text PRIMARY KEY, value text NOT NULL, "
             "updated_at timestamptz NOT NULL DEFAULT now())")
    s.append("COMMENT ON TABLE net.meta IS " + q("Key/value facts about the aggregator: last_run, last_success, backfilled_at, schema_version."))
    s.append("COMMENT ON COLUMN net.meta.key IS " + q("Fact name."))
    s.append("COMMENT ON COLUMN net.meta.value IS " + q("Fact value as text (timestamps are ISO 8601 UTC)."))
    s.append("COMMENT ON COLUMN net.meta.updated_at IS " + q("When the fact was last written."))
    for name, desc, cols, pk in DAILY:
        defs = ["day date NOT NULL"] + ["%s %s" % (c, t) for c, t, _ in cols]
        defs.append("updated_at timestamptz NOT NULL DEFAULT now()")
        defs.append("PRIMARY KEY (%s)" % ", ".join(pk))
        s.append("CREATE TABLE net.%s (%s)" % (name, ", ".join(defs)))
        s.append("COMMENT ON TABLE net.%s IS %s" % (name, q(desc)))
        s.append("COMMENT ON COLUMN net.%s.day IS %s" % (name, q(DAY_COMMENT)))
        for c, _, d in cols:
            s.append("COMMENT ON COLUMN net.%s.%s IS %s" % (name, c, q(d)))
        s.append("COMMENT ON COLUMN net.%s.updated_at IS %s" % (name, q(UPDATED_COMMENT)))
    defs = ["%s %s" % (c, t) for c, t, _ in OUTAGE_COLUMNS] + ["PRIMARY KEY (site, kind, name, started_at)"]
    s.append("CREATE TABLE net.outage_events (%s)" % ", ".join(defs))
    s.append("COMMENT ON TABLE net.outage_events IS " + q(
        "Every closed internet, LAN, per-WAN path and ER605 WAN outage, kept forever, collected from the internet-probe and er605-poll journal lines in Loki (unique per site, kind, name, started_at)."))
    for c, _, d in OUTAGE_COLUMNS:
        s.append("COMMENT ON COLUMN net.outage_events.%s IS %s" % (c, q(d)))
    s += monthly_statements(partial=False)
    cov = " UNION ALL ".join(
        "SELECT '%s'::text AS table_name, min(day) AS first_day, max(day) AS last_day, count(DISTINCT day)::integer AS days, count(*)::bigint AS row_count FROM net.%s"
        % (t, t) for t, _, _, _ in DAILY)
    s.append("CREATE VIEW net.coverage AS " + cov)
    s.append("COMMENT ON VIEW net.coverage IS " + q("What each daily table holds: first and last day, days with data and rows. Check it before answering 'how long' or 'since when'."))
    for c, d in [("table_name", "Daily table name."), ("first_day", "Earliest day stored."), ("last_day", "Latest day stored (today is a partial day)."),
                 ("days", "Distinct days stored."), ("row_count", "Rows stored.")]:
        s.append("COMMENT ON COLUMN net.coverage.%s IS %s" % (c, q(d)))
    s += catalog_statements()
    s.append(
        "CREATE VIEW net.catalog_columns AS SELECT c.relname::text AS object_name, a.attname::text AS column_name, "
        "format_type(a.atttypid, a.atttypmod) AS data_type, col_description(c.oid, a.attnum) AS description "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'net' "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
        "WHERE c.relkind IN ('r', 'v') ORDER BY c.relname, a.attnum")
    s.append("COMMENT ON VIEW net.catalog_columns IS " + q("Every column of every table and view in schema net with its type and description."))
    for c, d in [("object_name", "Table or view name."), ("column_name", "Column name."),
                 ("data_type", "PostgreSQL type."), ("description", "What the column holds and its unit.")]:
        s.append("COMMENT ON COLUMN net.catalog_columns.%s IS %s" % (c, q(d)))
    return s


def migration_2():
    """coverage on every daily table, partial_days on the monthly views, source er605-ledger instead of import."""
    s = []
    for name, _desc, _cols, _pk in DAILY:
        s.append("ALTER TABLE net.%s ADD COLUMN IF NOT EXISTS coverage numeric(4,3) CHECK (coverage BETWEEN 0 AND 1)" % name)
        s.append("COMMENT ON COLUMN net.%s.coverage IS %s" % (name, q(COVERAGE_COMMENT)))
    s.append("UPDATE net.wan_daily SET source = 'er605-ledger', coverage = 1 WHERE source = 'import'")
    s.append("COMMENT ON COLUMN net.wan_daily.source IS " + q(
        "prometheus, or er605-ledger for rows imported from the ER605 ledger (which win over Prometheus rows of lower coverage)."))
    for view, *_ in MONTHLY:
        s.append("DROP VIEW net.%s" % view)
    s += monthly_statements(partial=True)
    s.append("DROP VIEW net.catalog")
    s += catalog_statements()
    return s


MIGRATIONS = [(1, "initial schema: daily tables, monthly views, coverage, catalog", migration_1),
              (2, "coverage on the daily tables, partial_days on the monthly views", migration_2)]

SCHEMA_VERSION_DDL = ("CREATE TABLE IF NOT EXISTS net.schema_version (version integer PRIMARY KEY, "
                      "description text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
SCHEMA_VERSION_COMMENTS = [
    "COMMENT ON TABLE net.schema_version IS " + q("Migrations applied by net-history, one row per version."),
    "COMMENT ON COLUMN net.schema_version.version IS " + q("Migration number."),
    "COMMENT ON COLUMN net.schema_version.description IS " + q("What the migration did."),
    "COMMENT ON COLUMN net.schema_version.applied_at IS " + q("When it was applied."),
]


def migrate(conn, upto=None):
    """Apply pending migrations in one transaction under an advisory lock (`upto`: tests apply only the first N)."""
    cur = conn.cursor()
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (7410203,))
    cur.execute("CREATE SCHEMA IF NOT EXISTS net")
    cur.execute("SELECT to_regclass('net.schema_version') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("SELECT coalesce(max(version), 0) FROM net.schema_version")
        current = cur.fetchone()[0]
    else:
        cur.execute(SCHEMA_VERSION_DDL)
        for stmt in SCHEMA_VERSION_COMMENTS:
            cur.execute(stmt)
        current = 0
    applied = []
    for version, desc, stmts in MIGRATIONS:
        if version <= current or (upto is not None and version > upto):
            continue
        for stmt in stmts():
            cur.execute(stmt)
        cur.execute("INSERT INTO net.schema_version (version, description) VALUES (%s, %s)", (version, desc))
        applied.append(version)
    conn.commit()
    return applied


def schema_version():
    return max(v for v, _, _ in MIGRATIONS)


# --------------------------------------------------------------- utilities ---

def logfmt(event, **kv):
    def quote(v):
        v = str(v).replace('"', "'")
        return '"%s"' % v if (not v or any(c in v for c in ' "=')) else v
    return event + " " + " ".join("%s=%s" % (k, quote(v)) for k, v in kv.items())


def log(event, **kv):
    print(logfmt(event, **kv), flush=True)


class RunError(Exception):
    pass


def day_start(day):
    """Midnight IST at the start of `day`."""
    return dt.datetime(day.year, day.month, day.day, tzinfo=IST)


def day_end(day):
    return day_start(day + dt.timedelta(days=1))


def ist_day(ts):
    """The IST day an instant falls in."""
    return dt.datetime.fromtimestamp(ts, IST).date()


def days_to_compute(now, back):
    today = now.astimezone(IST).date()
    return [today - dt.timedelta(days=i) for i in range(back, -1, -1)]


def parse_when(text):
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = dt.datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def to_int(v):
    return None if v is None else int(round(v))


# -------------------------------------------------------------- HTTP clients ---

def http_json(url, params, timeout=120):
    full = url + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(full, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RunError("request to %s failed: %s" % (url, exc))


class Prom:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _check(self, body):
        if body.get("status") != "success":
            raise RunError("prometheus: %s" % body.get("error", body))
        return body["data"]["result"]

    def instant(self, query, when):
        res = self._check(http_json(self.base + "/api/v1/query", {"query": query, "time": "%.3f" % when}))
        return [(r["metric"], float(r["value"][1])) for r in res]

    def range(self, query, start, end, step):
        res = self._check(http_json(self.base + "/api/v1/query_range",
                                    {"query": query, "start": "%.3f" % start, "end": "%.3f" % end, "step": "%d" % step}))
        return [(r["metric"], [(float(t), float(v)) for t, v in r["values"]]) for r in res]


class Fetcher:
    """Evaluates one query template per IST day: range queries for complete days,
    an instant query over the elapsed part for today. __W__ is the query window.

    `span[day]` is the window the query covers; `window[day]` is the part of it the
    source really had data for (span x coverage), which the availability tables
    divide by."""

    def __init__(self, prom, days, now, cov=None):
        self.prom = prom
        self.now = now.timestamp()
        self.days = sorted(days)
        self.complete = [d for d in self.days if day_end(d).timestamp() <= self.now]
        self.partial = [d for d in self.days if d not in self.complete]
        self.span = {}
        for d in self.complete:
            self.span[d] = float(DAY)
        for d in self.partial:
            self.span[d] = max(0.0, self.now - day_start(d).timestamp())
        cov = cov or {}
        self.window = {d: self.span[d] * cov.get(d, 1.0) for d in self.span}

    def _runs(self):
        """Contiguous runs of complete days (a source may be chosen for scattered days)."""
        runs = []
        for d in self.complete:
            if runs and d - runs[-1][-1] == dt.timedelta(days=1):
                runs[-1].append(d)
            else:
                runs.append([d])
        return runs

    def fetch(self, template):
        """-> {day: [(labels, value)]}; days without data are absent."""
        out = {}
        ends = {int(round(day_end(d).timestamp())): d for d in self.complete}
        for run_days in self._runs():
            lo = run_days[0]
            while lo <= run_days[-1]:
                hi = min(run_days[-1], lo + dt.timedelta(days=RANGE_CHUNK_DAYS - 1))
                series = self.prom.range(template.replace("__W__", "%ds" % DAY),
                                         day_end(lo).timestamp(), day_end(hi).timestamp(), DAY)
                for labels, values in series:
                    for ts, val in values:
                        d = ends.get(int(round(ts)))
                        if d is not None and val == val:
                            out.setdefault(d, []).append((labels, val))
                lo = hi + dt.timedelta(days=1)
        for d in self.partial:
            win = int(self.span[d])
            if win < 60:
                continue
            for labels, val in self.prom.instant(template.replace("__W__", "%ds" % win), self.now):
                if val == val:
                    out.setdefault(d, []).append((labels, val))
        return out


# ---------------------------------------------------------------- coverage ---
# Each daily table is computed from one source metric. A Prometheus store only knows
# the days since its first sample, so a day is computed from the store that covers
# most of it, and the share it covered is stored with the row.
ANCHORS = {
    "internet_daily": "internet_probe_down_seconds_total",
    "lan_daily": "internet_probe_lan_down_seconds_total",
    "path_daily": "internet_probe_path_down_seconds_total",
    "wan_daily": 'er605_if_octets_total{role="wan"}',
    "segment_daily": "opnsense_interfaces_received_bytes_total",
    "segflow_daily": "opnsense_segflow_bytes_total",
    "client_daily": "tplink_router_wifi_client_bytes_total",
    "guest_daily": 'node_network_receive_bytes_total{device=~"%s"}' % GUEST_DEV,
    "tailnet_daily": 'node_network_receive_bytes_total{device="tailscale0"}',
}
EVERYTHING = float("-inf")  # the source had data before the window we looked at


def first_sample(prom, anchor, now_ts, since_ts):
    """Unix time of the first sample of `anchor` at or after since_ts (EVERYTHING if data
    exists at since_ts already, None if there is none): a coarse hourly scan, then 30 s
    steps through the hour before the first hit."""
    step = max(3600, int((now_ts - since_ts) / 10000) + 1)
    series = prom.range("count(%s)" % anchor, since_ts, now_ts, step)
    pts = sorted(t for _, vals in series for t, _v in vals)
    if not pts:
        return None
    if pts[0] <= since_ts + 1:
        return EVERYTHING
    fine = prom.range("count(%s)" % anchor, pts[0] - step, pts[0], 30)
    fpts = sorted(t for _, vals in fine for t, _v in vals)
    return fpts[0] if fpts else pts[0]


def coverage_of(first, day, now):
    """Share (0..1, 3 decimals) of the IST `day`, or of its elapsed part for today, that a
    source whose first sample is `first` covers."""
    if first is None:
        return 0.0
    start = day_start(day).timestamp()
    end = min(day_end(day).timestamp(), now.timestamp())
    if end <= start:
        return 0.0
    covered = end - max(start, first)
    return round(min(1.0, max(0.0, covered / (end - start))), 3)


# ------------------------------------------------------------- row builders ---

def by_key(entries, *label_names):
    return {tuple(l.get(n, "") for n in label_names): v for l, v in entries}


def build_internet(f):
    down = f.fetch("sum by (site) (increase(internet_probe_down_seconds_total[__W__]))")
    outs = f.fetch("sum by (site) (increase(internet_probe_outages_total[__W__]))")
    rows = []
    for d in sorted(set(down) | set(outs)):
        dv, ov = by_key(down.get(d, []), "site"), by_key(outs.get(d, []), "site")
        for (site,) in sorted(set(dv) | set(ov)):
            win = f.window[d]
            if not site or win <= 0:
                continue
            secs = round(max(0.0, dv.get((site,), 0.0)), 3)
            rows.append({"day": d, "site": site, "window_seconds": win, "down_seconds": min(secs, win),
                         "outages": to_int(max(0.0, ov.get((site,), 0.0))),
                         "availability": round(max(0.0, 1 - secs / win), 6)})
    return rows


def build_lan(f):
    down = f.fetch("sum by (site, name) (increase(internet_probe_lan_down_seconds_total[__W__]))")
    outs = f.fetch("sum by (site, name) (increase(internet_probe_lan_outages_total[__W__]))")
    rows = []
    for d in sorted(set(down) | set(outs)):
        dv, ov = by_key(down.get(d, []), "site", "name"), by_key(outs.get(d, []), "site", "name")
        for (site, name) in sorted(set(dv) | set(ov)):
            win = f.window[d]
            if not site or not name or win <= 0:
                continue
            rows.append({"day": d, "site": site, "name": name, "window_seconds": win,
                         "down_seconds": min(round(max(0.0, dv.get((site, name), 0.0)), 3), win),
                         "outages": to_int(max(0.0, ov.get((site, name), 0.0)))})
    return rows


def scalar(entries):
    return entries[0][1] if entries else None


def build_path(f):
    down = f.fetch("sum by (site, name) (increase(internet_probe_path_down_seconds_total[__W__]))")
    outs = f.fetch("sum by (site, name) (increase(internet_probe_path_outages_total[__W__]))")
    rows = []
    for d in sorted(set(down) | set(outs)):
        dv, ov = by_key(down.get(d, []), "site", "name"), by_key(outs.get(d, []), "site", "name")
        for (site, name) in sorted(set(dv) | set(ov)):
            win = f.window[d]
            if not site or not name or win <= 0:
                continue
            secs = round(max(0.0, dv.get((site, name), 0.0)), 3)
            rows.append({"day": d, "site": site, "name": name, "window_seconds": win, "down_seconds": min(secs, win),
                         "outages": to_int(max(0.0, ov.get((site, name), 0.0))),
                         "availability": round(max(0.0, 1 - secs / win), 6)})
    return rows


def wan_octets(f, direction):
    """{day: {wan: bytes}}. Joined on interface to the er605_wan_info series seen anywhere in the window;
    days without any count the whole role="wan" total as DEFAULT_WAN (the poller before it labelled WANs)."""
    sel = 'er605_if_octets_total{role="wan",direction="%s"}' % direction
    # er605_wan_info is read over the whole window, not at its end: a poller that was failing around
    # IST midnight must not turn every WAN into wan1.
    joined = f.fetch("sum by (wan) (increase(%s[__W__]) * on (interface) group_left (wan) "
                     "max by (interface, wan) (last_over_time(er605_wan_info[__W__])))" % sel)
    total = f.fetch("sum(increase(%s[__W__]))" % sel)
    out = {}
    for d in set(joined) | set(total):
        if joined.get(d):
            out[d] = {(lab.get("wan") or DEFAULT_WAN): val for lab, val in joined[d]}
        else:
            out[d] = {DEFAULT_WAN: scalar(total[d])}
    return out


def build_wan(f):
    down, up = wan_octets(f, "in"), wan_octets(f, "out")
    rec = f.fetch("sum by (wan) (increase(er605_pppoe_reconnects_total[__W__]))")
    rows = []
    for d in sorted(set(down) | set(up)):
        rv = {}
        for lab, val in rec.get(d, []):  # the old unlabelled series and the new wan="wan1" one are the same WAN
            key = lab.get("wan") or DEFAULT_WAN
            rv[key] = rv.get(key, 0.0) + val
        for wan in sorted(set(down.get(d, {})) | set(up.get(d, {}))):
            rows.append({"day": d, "wan": wan, "download_bytes": to_int(down.get(d, {}).get(wan)),
                         "upload_bytes": to_int(up.get(d, {}).get(wan)), "pppoe_reconnects": to_int(rv.get(wan)),
                         "source": "prometheus"})
    return rows


def build_segment(f):
    rx = f.fetch('sum by (interface) (increase(opnsense_interfaces_received_bytes_total{interface=~"%s"}[__W__]))' % SEGMENTS_RE)
    tx = f.fetch('sum by (interface) (increase(opnsense_interfaces_transmitted_bytes_total{interface=~"%s"}[__W__]))' % SEGMENTS_RE)
    rows = []
    for d in sorted(set(rx) | set(tx)):
        rv, tv = by_key(rx.get(d, []), "interface"), by_key(tx.get(d, []), "interface")
        for (iface,) in sorted(set(rv) | set(tv)):
            rows.append({"day": d, "interface": iface, "received_bytes": to_int(rv.get((iface,))),
                         "transmitted_bytes": to_int(tv.get((iface,)))})
    return rows


def build_segflow(f):
    parts = {c: f.fetch("sum by (src_segment, dst_segment) (increase(opnsense_segflow_%s_total[__W__]))" % c)
             for c in ("bytes", "packets", "flows")}
    rows = []
    for d in sorted(set().union(*[set(v) for v in parts.values()])):
        maps = {c: by_key(parts[c].get(d, []), "src_segment", "dst_segment") for c in parts}
        for key in sorted(set().union(*[set(m) for m in maps.values()])):
            rows.append({"day": d, "src_segment": key[0], "dst_segment": key[1], "bytes": to_int(maps["bytes"].get(key)),
                         "packets": to_int(maps["packets"].get(key)), "flows": to_int(maps["flows"].get(key))})
    return rows


def build_client(f):
    dl = f.fetch('sum by (router, mac, name) (increase(tplink_router_wifi_client_bytes_total{direction="download"}[__W__]))')
    ul = f.fetch('sum by (router, mac, name) (increase(tplink_router_wifi_client_bytes_total{direction="upload"}[__W__]))')
    rows = []
    for d in sorted(set(dl) | set(ul)):
        agg = {}
        for direction, entries in (("download_bytes", dl.get(d, [])), ("upload_bytes", ul.get(d, []))):
            for lab, val in entries:
                key = (lab.get("router", ""), lab.get("mac", ""))
                rec = agg.setdefault(key, {"download_bytes": 0.0, "upload_bytes": 0.0, "names": {}})
                rec[direction] += val
                rec["names"][lab.get("name", "")] = rec["names"].get(lab.get("name", ""), 0.0) + val
        for (router, mac), rec in sorted(agg.items()):
            if not mac:
                continue
            name = max(rec["names"].items(), key=lambda kv: kv[1])[0] or None
            rows.append({"day": d, "router": router, "mac": mac, "name": name,
                         "download_bytes": to_int(rec["download_bytes"]), "upload_bytes": to_int(rec["upload_bytes"])})
    return rows


def guest_query(metric):
    # The Guest Traffic dashboard join: 5 minute increases of the host-side tap or veth NIC, labelled with the
    # guest from fleet_pve_guest_nic_info, summed over the window. Federation copies no staleness markers, so
    # when a guest NIC is replaced the 90-day store briefly holds the old and the new info series for the same
    # device and the plain join fails ("duplicate series for the match group"). Keep, per host and device, the
    # info series with the newest sample: topk over timestamp(), turned back into 1 with `> bool 0`.
    latest = "((topk by (host, device) (1, timestamp(fleet_pve_guest_nic_info))) > bool 0)"
    return ("sum by (guest) (sum_over_time((increase(%s{device=~\"%s\"}[5m]) "
            "* on (host, device) group_left (guest) %s)[__W__:5m]))" % (metric, GUEST_DEV, latest))


def build_guest(f):
    sent = f.fetch(guest_query("node_network_receive_bytes_total"))
    recv = f.fetch(guest_query("node_network_transmit_bytes_total"))
    rows = []
    for d in sorted(set(sent) | set(recv)):
        sv, rv = by_key(sent.get(d, []), "guest"), by_key(recv.get(d, []), "guest")
        for (guest,) in sorted(set(sv) | set(rv)):
            if guest:
                rows.append({"day": d, "guest": guest, "sent_bytes": to_int(sv.get((guest,))), "received_bytes": to_int(rv.get((guest,)))})
    return rows


def build_tailnet(f):
    rx = f.fetch('sum by (host) (increase(node_network_receive_bytes_total{device="tailscale0"}[__W__]))')
    tx = f.fetch('sum by (host) (increase(node_network_transmit_bytes_total{device="tailscale0"}[__W__]))')
    rows = []
    for d in sorted(set(rx) | set(tx)):
        rv, tv = by_key(rx.get(d, []), "host"), by_key(tx.get(d, []), "host")
        for (host,) in sorted(set(rv) | set(tv)):
            rows.append({"day": d, "host": host, "received_bytes": to_int(rv.get((host,))), "transmitted_bytes": to_int(tv.get((host,)))})
    return rows


# table -> (builder, key columns, value columns)
BUILDERS = [
    ("internet_daily", build_internet, ("day", "site"), ("window_seconds", "down_seconds", "outages", "availability")),
    ("lan_daily", build_lan, ("day", "site", "name"), ("window_seconds", "down_seconds", "outages")),
    ("path_daily", build_path, ("day", "site", "name"), ("window_seconds", "down_seconds", "outages", "availability")),
    ("wan_daily", build_wan, ("day", "wan"), ("download_bytes", "upload_bytes", "pppoe_reconnects", "source")),
    ("segment_daily", build_segment, ("day", "interface"), ("received_bytes", "transmitted_bytes")),
    ("segflow_daily", build_segflow, ("day", "src_segment", "dst_segment"), ("bytes", "packets", "flows")),
    ("client_daily", build_client, ("day", "router", "mac"), ("name", "download_bytes", "upload_bytes")),
    ("guest_daily", build_guest, ("day", "guest"), ("sent_bytes", "received_bytes")),
    ("tailnet_daily", build_tailnet, ("day", "host"), ("received_bytes", "transmitted_bytes")),
]


def upsert(cur, table, keys, values, rows, today):
    """Insert or update rows carrying a `coverage`. An existing row is replaced only by one with
    higher coverage, or equal coverage (the source does not matter, except that a row imported from
    the ER605 ledger is kept), or when it is today's row, which is always recomputed."""
    cols = list(keys) + list(values) + ["coverage"]
    sets = ", ".join("%s = COALESCE(EXCLUDED.%s, net.%s.%s)" % (c, c, table, c) for c in values)
    tie = " AND net.%s.source <> 'er605-ledger'" % table if table == "wan_daily" else ""
    sql = ("INSERT INTO net.%s (%s) VALUES (%s) ON CONFLICT (%s) DO UPDATE SET %s, coverage = EXCLUDED.coverage, updated_at = now() "
           "WHERE net.%s.coverage IS NULL OR net.%s.coverage < EXCLUDED.coverage OR (net.%s.coverage = EXCLUDED.coverage%s) OR net.%s.day = %%s"
           % (table, ", ".join(cols), ", ".join(["%s"] * len(cols)), ", ".join(keys), sets, table, table, table, tie, table))
    for row in rows:
        cur.execute(sql, [row.get(c) for c in cols] + [today])


# ------------------------------------------------------------ outage events ---

LOGFMT = re.compile(r'(\w+)=("([^"]*)"|\S*)')


def parse_outage_line(line):
    """One journal line -> an outage_events dict, or None. Handles the internet-probe
    lines outage_end, lan_up and path_up and the er605-poll line wan_up."""
    line = line.strip()
    event = line.split(" ", 1)[0]
    if event not in ("outage_end", "lan_up", "path_up", "wan_up"):
        return None
    kv = {m.group(1): (m.group(3) if m.group(3) is not None else m.group(2)) for m in LOGFMT.finditer(line)}
    try:
        dur = float(kv["duration_seconds"]) if kv.get("duration_seconds") else None
        if event == "wan_up":  # at= is the recovery time; the outage started duration_seconds earlier
            ended = parse_when(kv["at"])
            if dur is None or not kv.get("wan"):
                return None
            return {"site": kv.get("site", "home"), "kind": "wan", "name": kv["wan"], "target": "",
                    "started_at": ended - dt.timedelta(seconds=dur), "ended_at": ended, "duration_seconds": dur,
                    "cause": None, "closed_by": None}
        started = parse_when(kv["start"])
        ended = parse_when(kv["end"]) if kv.get("end") else None
        site = kv["site"]
    except (KeyError, ValueError):
        return None
    if event == "outage_end":
        return {"site": site, "kind": "internet", "name": "internet", "target": "", "started_at": started, "ended_at": ended,
                "duration_seconds": dur, "cause": kv.get("cause"), "closed_by": kv.get("closed_by")}
    if not kv.get("name"):
        return None
    return {"site": site, "kind": "lan" if event == "lan_up" else "path", "name": kv["name"], "target": kv.get("target", ""),
            "started_at": started, "ended_at": ended, "duration_seconds": dur, "cause": None, "closed_by": kv.get("closed_by")}


def fetch_outage_events(queries, loki_url, now, days):
    """queries: LogQL selectors with line filters. -> deduplicated event dicts."""
    events = {}
    for logql in queries:
        start = int((now - dt.timedelta(days=days)).timestamp() * 1e9)
        end = int(now.timestamp() * 1e9) + 1
        while True:
            body = http_json(loki_url.rstrip("/") + "/loki/api/v1/query_range",
                             {"query": logql, "start": start, "end": end, "limit": LOKI_LIMIT, "direction": "forward"})
            if body.get("status") != "success":
                raise RunError("loki: %s" % body.get("error", body))
            lines = sorted((int(ts), line) for s in body["data"]["result"] for ts, line in s["values"])
            for _, line in lines:
                ev = parse_outage_line(line)
                if ev:
                    events[(ev["site"], ev["kind"], ev["name"], ev["started_at"])] = ev
            if len(lines) < LOKI_LIMIT:
                break
            start = lines[-1][0] + 1
    return list(events.values())


OUTAGE_COLS = ["site", "kind", "name", "target", "started_at", "ended_at", "duration_seconds", "cause", "closed_by"]


def upsert_outages(cur, events):
    sql = ("INSERT INTO net.outage_events (%s) VALUES (%s) ON CONFLICT (site, kind, name, started_at) DO UPDATE SET "
           "target = EXCLUDED.target, ended_at = EXCLUDED.ended_at, duration_seconds = EXCLUDED.duration_seconds, "
           "cause = EXCLUDED.cause, closed_by = EXCLUDED.closed_by, recorded_at = now()"
           % (", ".join(OUTAGE_COLS), ", ".join(["%s"] * len(OUTAGE_COLS))))
    for ev in events:
        cur.execute(sql, [ev[c] for c in OUTAGE_COLS])


# -------------------------------------------------------------- WAN import ---

def import_wan_daily(conn, path, force, wan=DEFAULT_WAN):
    """Seed wan_daily from the ER605 ledger: a CSV with day,download_bytes,upload_bytes and optionally
    wan (default `wan`) and coverage (default 1). An existing row is replaced only if its coverage is
    lower (or unknown); --force replaces it whatever it holds. -> (inserted, replaced_partial, skipped)."""
    cur = conn.cursor()
    inserted = replaced = skipped = 0
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        need = {"day", "download_bytes", "upload_bytes"}
        if not reader.fieldnames or not need <= set(reader.fieldnames):
            raise RunError("%s needs the columns day,download_bytes,upload_bytes (and optionally wan, coverage)" % path)
        for rec in reader:
            day = dt.date.fromisoformat(rec["day"].strip())
            dl, ul = int(float(rec["download_bytes"])), int(float(rec["upload_bytes"]))
            row_wan = (rec.get("wan") or "").strip() or wan
            cov = round(float(rec["coverage"]), 3) if (rec.get("coverage") or "").strip() else 1.0
            if not 0 <= cov <= 1:
                raise RunError("coverage %s on %s is outside 0..1" % (cov, day))
            guard = "" if force else " WHERE net.wan_daily.coverage IS NULL OR net.wan_daily.coverage < EXCLUDED.coverage"
            cur.execute("INSERT INTO net.wan_daily (day, wan, download_bytes, upload_bytes, source, coverage) "
                        "VALUES (%s, %s, %s, %s, 'er605-ledger', %s) ON CONFLICT (day, wan) DO UPDATE SET "
                        "download_bytes = EXCLUDED.download_bytes, upload_bytes = EXCLUDED.upload_bytes, source = 'er605-ledger', "
                        "coverage = EXCLUDED.coverage, updated_at = now()" + guard + " RETURNING (xmax = 0)",
                        (day, row_wan, dl, ul, cov))
            got = cur.fetchall()
            if not got:
                skipped += 1
            elif got[0][0]:
                inserted += 1
            else:
                replaced += 1
    conn.commit()
    return inserted, replaced, skipped


# ----------------------------------------------------------------- textfile ---

def write_textfile(path, now, success, rows):
    lines = [
        "# HELP net_history_last_run_timestamp_seconds Unix time the last net-history run finished.",
        "# TYPE net_history_last_run_timestamp_seconds gauge",
        "net_history_last_run_timestamp_seconds %d" % int(now.timestamp()),
        "# HELP net_history_run_success 1 if the last net-history run succeeded, else 0.",
        "# TYPE net_history_run_success gauge",
        "net_history_run_success %d" % (1 if success else 0),
    ]
    if rows:
        lines += ["# HELP net_history_rows Rows stored in each net-history table.",
                  "# TYPE net_history_rows gauge"]
        lines += ['net_history_rows{table="%s"} %d' % (t, n) for t, n in sorted(rows.items())]
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ------------------------------------------------------------------- report ---

def format_table(cols, rows):
    text = [["" if v is None else str(v) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in text]) for i, c in enumerate(cols)]
    numeric = [all(isinstance(r[i], (int, float)) or r[i] is None for r in rows) and rows != [] for i in range(len(cols))]
    def line(cells):
        return "  ".join(c.rjust(w) if numeric[i] else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths))).rstrip()
    return "\n".join([line(cols), "  ".join("-" * w for w in widths)] + [line(r) for r in text])


def report_month(conn, tables):
    cur = conn.cursor()
    out = []
    for key in tables:
        view = TABLE_NAMES[key][1]
        cur.execute("SELECT * FROM net.%s ORDER BY 1 DESC, 2" % view)
        cols = [d[0] for d in cur.description]
        out.append("== net.%s\n%s" % (view, format_table(cols, cur.fetchall())))
    return "\n\n".join(out)


# --------------------------------------------------------------------- main ---

def connect(args):
    try:
        import pg8000.dbapi
    except ImportError:
        raise RunError("pg8000 is not installed; run this with /opt/net-history/venv/bin/python")
    try:
        password = open(args.password_file).read().strip()
    except OSError as exc:
        raise RunError("cannot read the password file: %s" % exc)
    try:
        return pg8000.dbapi.connect(user=args.db_user, password=password, host=args.db_host, port=args.db_port,
                                    database=args.db_name, timeout=30, application_name="net-history")
    except Exception as exc:  # pg8000 raises several types
        raise RunError("cannot connect to %s:%s/%s as %s: %s" % (args.db_host, args.db_port, args.db_name, args.db_user, exc))


def get_meta(cur, key):
    cur.execute("SELECT value FROM net.meta WHERE key = %s", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_meta(cur, key, value):
    cur.execute("INSERT INTO net.meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, value))


def table_counts(conn):
    cur = conn.cursor()
    counts = {}
    for name, *_ in BUILDERS:
        cur.execute("SELECT count(*) FROM net.%s" % name)
        counts[name] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM net.outage_events")
    counts["outage_events"] = cur.fetchone()[0]
    return counts


def loki_queries(args):
    return ['{unit="internet-probe.service"} |~ "^(outage_end|lan_up|path_up) "',
            '%s |~ "^wan_up "' % args.er605_selector]


def sources(args):
    """[(name, Prom)]: the 90-day store first (it wins ties), then the central Prometheus."""
    out = [("90d", Prom(args.prometheus))]
    central = (args.central_prometheus or "").rstrip("/")
    if central and central != args.prometheus.rstrip("/"):
        out.append(("central", Prom(central)))
    return out


def plan_days(table, days, srcs, now, lookback_since):
    """{day: (source index, coverage)} choosing, per day, the source that covers most of it."""
    anchor = ANCHORS[table]
    firsts = []
    for name, prom in srcs:
        try:
            firsts.append(first_sample(prom, anchor, now.timestamp(), lookback_since))
        except RunError as exc:
            if name == "90d":
                raise
            log("source_unavailable", source=name, table=table, error=exc)
            firsts.append(None)
    plan = {}
    for d in days:
        covs = [coverage_of(f, d, now) if f is not None else 0.0 for f in firsts]
        best = max(range(len(covs)), key=lambda i: (covs[i], -i))
        if covs[best] > 0:
            plan[d] = (best, covs[best])
    return plan


def fill_unknown_coverage(conn, srcs, now):
    """Rows written before the coverage column existed (coverage NULL): give each the best coverage any
    source has for its day. A row imported from the ledger keeps coverage 1 (the migration set it)."""
    cur = conn.cursor()
    filled = 0
    for table, _b, _k, _v in BUILDERS:
        cur.execute("SELECT DISTINCT day FROM net.%s WHERE coverage IS NULL" % table)
        todo = [r[0] for r in cur.fetchall()]
        if not todo:
            continue
        since = day_start(min(todo)).timestamp() - 3600
        firsts = []
        for name, prom in srcs:
            try:
                firsts.append(first_sample(prom, ANCHORS[table], now.timestamp(), since))
            except RunError as exc:
                if name == "90d":  # without the main store the coverage of old rows cannot be judged: stop, as plan_days does
                    raise
                log("source_unavailable", source=name, table=table, error=exc)
                firsts.append(None)
        for d in todo:
            cov = max([coverage_of(f, d, now) for f in firsts if f is not None] or [0.0])
            # Nothing covers this day any more: it came from a store that has since aged out, so it was complete.
            cur.execute("UPDATE net.%s SET coverage = %%s WHERE day = %%s AND coverage IS NULL" % table, (cov if cov > 0 else 1.0, d))
            filled += cur.rowcount
    conn.commit()
    return filled


def loki_queries(args):
    return ['{unit="internet-probe.service"} |~ "^(outage_end|lan_up|path_up) "',
            '%s |~ "^wan_up "' % args.er605_selector]


def run(args, conn, now):
    started = time.time()
    cur = conn.cursor()
    applied = migrate(conn)
    srcs = sources(args)
    filled = fill_unknown_coverage(conn, srcs, now)
    backfill = args.backfill
    if backfill is None:
        backfill = 0 if get_meta(cur, "backfilled_at") else BACKFILL_DEFAULT
    days = days_to_compute(now, max(RECENT_DAYS, backfill))
    today = now.astimezone(IST).date()
    since = day_start(days[0]).timestamp() - 3600
    upserted, skipped, partial = {}, [], {}
    for table, builder, keys, values in BUILDERS:
        plan = plan_days(table, days, srcs, now, since)
        rows = []
        for idx, (name, prom) in enumerate(srcs):
            mine = {d: c for d, (i, c) in plan.items() if i == idx}
            if not mine:
                continue
            for row in builder(Fetcher(prom, list(mine), now, mine)):
                row["coverage"] = mine[row["day"]]
                rows.append(row)
        if not rows:
            log("skip", table=table, reason="no_data")
            skipped.append(table)
            continue
        upsert(cur, table, keys, values, rows, today)
        conn.commit()
        upserted[table] = len(rows)
        partial[table] = sum(1 for r in rows if r["coverage"] < 1)
    loki_days = LOKI_BACKFILL_DAYS if backfill > RECENT_DAYS else LOKI_RECENT_DAYS
    events = fetch_outage_events(loki_queries(args), args.loki, now, loki_days)
    upsert_outages(cur, events)
    conn.commit()
    if backfill:
        set_meta(cur, "backfilled_at", now.astimezone(dt.timezone.utc).isoformat(timespec="seconds"))
    stamp = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    set_meta(cur, "last_run", stamp)
    set_meta(cur, "last_success", stamp)
    set_meta(cur, "schema_version", str(schema_version()))
    conn.commit()
    log("run", result="ok", days=len(days), backfill=backfill, loki_events=len(events),
        tables=",".join("%s:%d" % kv for kv in sorted(upserted.items())) or "none",
        partial_rows=sum(partial.values()), migrated=",".join(map(str, applied)) or "none", coverage_filled=filled,
        skipped=",".join(skipped) or "none", seconds="%.1f" % (time.time() - started))
    return True


def build_parser():
    p = argparse.ArgumentParser(description="Roll network metrics up into permanent daily rows in PostgreSQL.",
                                epilog="Password: read from --password-file, never from the command line.")
    p.add_argument("--prometheus", default="http://127.0.0.1:9091", help="Prometheus base URL (default: the 90-day tier)")
    p.add_argument("--central-prometheus", default="http://127.0.0.1:9090", metavar="URL",
                   help="the central Prometheus, used for days the 90-day store does not cover; empty disables it")
    p.add_argument("--loki", default="http://127.0.0.1:3100", help="Loki base URL")
    p.add_argument("--er605-selector", default='{host="ubuntu-dev", container="er605-poll"}',
                   help="Loki stream selector of the ER605 poller's journal lines (wan_up events)")
    p.add_argument("--db-host", default="127.0.0.1")
    p.add_argument("--db-port", type=int, default=5432)
    p.add_argument("--db-name", default="net_history")
    p.add_argument("--db-user", default="net_history_writer")
    p.add_argument("--password-file", default="/etc/net-history/writer.password")
    p.add_argument("--backfill", type=int, metavar="DAYS", help="recompute the last DAYS days (default: 90 if nothing was ever back-filled, else 2)")
    p.add_argument("--import-wan-daily", metavar="CSV", help="seed wan_daily from the ER605 ledger: a CSV with day,download_bytes,upload_bytes and optionally wan, coverage")
    p.add_argument("--wan", default=DEFAULT_WAN, help="with --import-wan-daily: the WAN for rows without a wan column (default wan1)")
    p.add_argument("--force", action="store_true", help="with --import-wan-daily: overwrite existing rows even when their coverage is not lower")
    p.add_argument("--report", choices=["month"], help="print the monthly views and exit")
    p.add_argument("--table", choices=sorted(TABLE_NAMES), action="append", help="with --report: only this table (repeatable)")
    p.add_argument("--textfile", metavar="PATH", help="write net_history_* metrics for Alloy's textfile collector")
    p.add_argument("--now", metavar="ISO8601", help=argparse.SUPPRESS)  # tests: pretend it is this instant
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    now = parse_when(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    conn = None
    success = False
    rows = None
    try:
        conn = connect(args)
        if args.report:
            migrate(conn)
            print(report_month(conn, args.table or sorted(TABLE_NAMES)))
            return 0
        if args.import_wan_daily:
            migrate(conn)
            ins, rep_, skip = import_wan_daily(conn, args.import_wan_daily, args.force, args.wan)
            log("import", table="wan_daily", inserted=ins, replaced_lower_coverage=rep_, skipped_existing=skip)
            return 0
        success = run(args, conn, now)
        rows = table_counts(conn)
    except RunError as exc:
        log("run", result="error", error=exc)
    except Exception as exc:  # last resort: never leave the timer silent
        log("run", result="error", error="%s: %s" % (type(exc).__name__, exc))
    finally:
        if args.textfile and not args.report and not args.import_wan_daily:
            try:
                write_textfile(args.textfile, dt.datetime.now(dt.timezone.utc) if not args.now else now, success, rows)
            except OSError as exc:
                log("textfile_error", error=exc)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
