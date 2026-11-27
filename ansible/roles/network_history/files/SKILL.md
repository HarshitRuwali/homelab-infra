---
name: network-history
description: Answer questions about the homelab and home networks from permanent PostgreSQL history. Use for internet uptime and outages, ISP or per-WAN usage, who or which guest or client used how much, inter-LAN and segment traffic, tailnet traffic, PPPoE reconnects, and monthly trends or "since when" questions that reach back further than the 7-day Prometheus store.
---

# Network history

Permanent, aggregated network data lives in PostgreSQL (database `net_history`,
schema `net`), one row per IST day per key, kept forever, with monthly views on
top. The `nethist` command reads it with a read-only role. It needs Docker and
the password file `~/.config/net-history/reader.password`; nothing else.

## The three tiers

| Tier | Holds | Reach for it when |
|---|---|---|
| Prometheus (central, port 9090) | every metric, 15 s, 7 days | "right now", the last few days, anything outside the network set |
| prometheus-network (127.0.0.1:9091 on the monitoring host, Grafana datasource `prometheus-90d`) | the network metrics, 30 s, 90 days | per-minute detail up to 90 days back |
| PostgreSQL `net_history` (this skill) | daily rows forever, monthly views | totals per day or month, long-term trends, outage history |

Loki keeps 30 days of logs; outages older than that survive only in `net.outage_events`.

## How to use it

1. Discover: `nethist catalog` lists every table and view. `nethist catalog monthly_wan`
   shows its columns and an example query. `nethist coverage` shows the first and last
   day each table holds: check it before saying "since when" or "how long".
2. Fetch: `nethist month <table> [--months N]`, `nethist day <table> [--days N]`,
   `nethist outages [--days N] [--site S] [--lan|--all]`, or `nethist sql "SELECT ..."`.
   Tables: internet, lan, path, wan, segment, segflow, client, guest, tailnet.
3. Aggregate with SQL (`nethist sql`), then tell the answer in plain words with units.
   Add `--json` or `--csv` to feed a program.

Facts to get right:
- Days are IST (Asia/Kolkata). Today's row is partial (`window_seconds` says how much of
  the day it covers). A month's `days` column below the days in the month means gaps or a partial month.
- Every daily row has `coverage` (0 to 1): the share of that IST day the data source covered. Below 1 the day is
  partial and its bytes and counts are understated; the monthly views count such days in `partial_days`. Say so
  when you quote a month with `partial_days > 0`. A stored row is never replaced by one of lower coverage.
- Availability is `1 - down_seconds / window_seconds`, recomputed from sums in the monthly views.
  Never average the daily ratios.
- `net.monthly_wan` has one row per WAN per month plus a row with `wan = 'total'`.
  Add `WHERE wan <> 'total'` when summing yourself, or you count the household twice.
- Bytes are raw bytes; `pg_size_pretty(bigint)` makes them readable. `guest_daily.sent_bytes`
  is what the guest sent. `segment_daily.received_bytes` is what the segment sent into the firewall.
- Only SELECT works (the role is read-only, 30 s statement timeout).

## Example questions

1. "How was the home internet in the last 3 months?"
   `nethist month internet --months 3` then read `availability` (fraction, 0.999 = 99.9 %), `down_seconds`, `outages` for site `home`.
2. "How much data did we use each month, per ISP?"
   `nethist month wan --months 6`: the `total` row is the household, the others are WAN1/WAN2.
3. "Who used the most Wi-Fi data in Bangalore last month?"
   `nethist sql "SELECT name, pg_size_pretty(total_bytes) FROM net.monthly_client WHERE router='blr' AND month = date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata' - interval '1 month')::date ORDER BY total_bytes DESC LIMIT 10"`
4. "List every outage in the last 30 days."
   `nethist outages --days 30` (add `--lan` for inter-LAN targets, `--all` for everything), or `nethist --json outages --days 30`.
5. "Which guest moved the most traffic this year?"
   `nethist sql "SELECT guest, pg_size_pretty(sum(total_bytes)) FROM net.monthly_guest WHERE month >= date_trunc('year', now())::date GROUP BY guest ORDER BY sum(total_bytes) DESC LIMIT 10"`

## When the answer is not here

- Sub-day detail within the last 90 days: query `prometheus-90d` (127.0.0.1:9091 on the monitoring host, or the Grafana datasource of that uid).
- Anything not a network metric, or the last minutes: central Prometheus.
- A table that is empty means its collector has not been deployed or has produced nothing: `nethist coverage`.
