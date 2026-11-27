#!/usr/bin/env python3
"""nethist: query the permanent network history (PostgreSQL schema net) from a shell.

Read-only. Runs psql from a pinned postgres container, so nothing is installed
on the host except Docker. The reader password is read from a file and handed to
psql through a temporary pgpass file (mode 0600, deleted afterwards); it is never
on the command line or in the environment.

Output is an aligned text table by default; --json and --csv for programs.
"""
import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

CONFIG = os.path.expanduser("~/.config/net-history/nethist.conf")
DEFAULTS = {
    "host": "10.10.50.126",
    "port": "5432",
    "database": "net_history",
    "user": "net_history_reader",
    "password_file": os.path.expanduser("~/.config/net-history/reader.password"),
    "image": "postgres:16-alpine",
}
MONTH_TABLES = {  # name -> monthly view
    "internet": "monthly_internet", "lan": "monthly_lan", "path": "monthly_path", "wan": "monthly_wan",
    "segment": "monthly_segment", "segflow": "monthly_segflow", "client": "monthly_client",
    "guest": "monthly_guest", "tailnet": "monthly_tailnet",
}
DAY_TABLES = {k: v.replace("monthly_", "") + "_daily" for k, v in MONTH_TABLES.items()}
SAFE = re.compile(r"^[A-Za-z0-9_.:-]+$")

EPILOG = """\
examples:
  nethist catalog                        every table and view, one line each
  nethist catalog monthly_wan            its columns, plus an example query
  nethist coverage                       first and last day each table holds
  nethist month internet --months 6      availability, downtime, outages per site per month
  nethist month wan                      household usage per WAN per month, plus a total row
  nethist month path                     availability of each per-WAN probe path per month
  nethist day client --days 7            Wi-Fi usage per client per day
  nethist outages --days 30              internet, per-WAN and path outages (add --lan for LAN)
  nethist sql "SELECT month, wan, pg_size_pretty(total_bytes) FROM net.monthly_wan ORDER BY 1 DESC LIMIT 6"
  nethist --json month guest --months 3  machine-readable

Tables for month/day: %s.
Days are IST (Asia/Kolkata) calendar days; today's row is a partial day.
The coverage column (day tables) and partial_days (month tables) say how much of a day a row covers.
""" % ", ".join(sorted(MONTH_TABLES))


def load_config():
    cfg = dict(DEFAULTS)
    parser = configparser.ConfigParser()
    if os.path.exists(CONFIG):
        parser.read(CONFIG)
        cfg.update({k: os.path.expanduser(v) for k, v in parser["nethist"].items()} if parser.has_section("nethist") else {})
    return cfg


def sql_str(value):
    if not SAFE.match(value):
        raise SystemExit("nethist: %r has characters outside letters, digits and _ . : -" % value)
    return "'" + value + "'"


def build_query(args):
    """Subcommand -> SQL text (already validated)."""
    if args.cmd == "catalog":
        if args.object:
            obj = sql_str(args.object)
            return ("SELECT column_name, data_type, description FROM net.catalog_columns WHERE object_name = %s "
                    "UNION ALL SELECT 'example', NULL, example_query FROM net.catalog WHERE object_name = %s "
                    "AND example_query IS NOT NULL" % (obj, obj))
        return "SELECT object_name, kind, description FROM net.catalog"
    if args.cmd == "coverage":
        return "SELECT * FROM net.coverage"
    if args.cmd == "month":
        return ("SELECT * FROM net.%s WHERE month > (now() AT TIME ZONE 'Asia/Kolkata')::date - make_interval(months => %d) "
                "ORDER BY month DESC, 2" % (MONTH_TABLES[args.table], args.months))
    if args.cmd == "day":
        return ("SELECT * FROM net.%s WHERE day > (now() AT TIME ZONE 'Asia/Kolkata')::date - %d ORDER BY day DESC, 2"
                % (DAY_TABLES[args.table], args.days))
    if args.cmd == "outages":
        kinds = ["'lan'"] if args.lan else (["'internet'", "'path'", "'wan'", "'lan'"] if args.all else ["'internet'", "'path'", "'wan'"])
        where = "started_at > now() - interval '%d days' AND kind IN (%s)" % (args.days, ", ".join(kinds))
        if args.site:
            where += " AND site = %s" % sql_str(args.site)
        return ("SELECT site, kind, name, started_at, ended_at, round(duration_seconds) AS seconds, cause, closed_by "
                "FROM net.outage_events WHERE %s ORDER BY started_at DESC" % where)
    if args.cmd == "sql":
        return args.query.strip().rstrip(";")
    raise SystemExit("nethist: unknown command")


def psql_argv(cfg, query, fmt, pgpass):
    uid_gid = "%d:%d" % (os.getuid(), os.getgid())
    base = ["docker", "run", "--rm", "--network", "host", "--user", uid_gid,
            "-v", "%s:/pgpass:ro" % pgpass, "-e", "PGPASSFILE=/pgpass",
            "-e", "PGHOST=" + cfg["host"], "-e", "PGPORT=" + cfg["port"], "-e", "PGDATABASE=" + cfg["database"],
            "-e", "PGUSER=" + cfg["user"], "-e", "PGCONNECT_TIMEOUT=10",
            "-e", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=30000",
            cfg["image"], "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-P", "pager=off"]
    if fmt == "json":
        wrapped = "SELECT coalesce(json_agg(row_to_json(q)), '[]'::json) FROM (%s) q" % query
        return base + ["-A", "-t", "-c", wrapped]
    if fmt == "csv":
        return base + ["--csv", "-c", query]
    return base + ["-c", query]


def write_pgpass(cfg):
    try:
        password = open(cfg["password_file"]).read().strip()
    except OSError as exc:
        raise SystemExit("nethist: cannot read %s: %s" % (cfg["password_file"], exc))
    if os.stat(cfg["password_file"]).st_mode & 0o077:
        print("nethist: warning: %s is readable by others; chmod 600 it" % cfg["password_file"], file=sys.stderr)
    fd, path = tempfile.mkstemp(prefix="nethist-pgpass-")  # created 0600
    with os.fdopen(fd, "w") as fh:
        esc = lambda v: v.replace("\\", "\\\\").replace(":", "\\:")
        fh.write("%s:%s:%s:%s:%s\n" % (esc(cfg["host"]), cfg["port"], esc(cfg["database"]), esc(cfg["user"]), esc(password)))
    return path


def parser():
    common = argparse.ArgumentParser(add_help=False)
    out = common.add_argument_group("output")
    out.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print a JSON array of objects")
    out.add_argument("--csv", action="store_true", default=argparse.SUPPRESS, help="print CSV with a header row")
    for opt, help_ in (("--host", "database host"), ("--port", "database port"), ("--database", "database name"),
                       ("--user", "database role (a read-only one)"), ("--password-file", "file holding the password"),
                       ("--image", "postgres image that provides psql")):
        common.add_argument(opt, default=argparse.SUPPRESS, help=help_ + " (default from nethist.conf)")
    p = argparse.ArgumentParser(prog="nethist", description=__doc__.split("\n\n")[0], epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    c = sub.add_parser("catalog", parents=[common], help="list tables and views, or the columns of one")
    c.add_argument("object", nargs="?", help="a table or view name, for example monthly_wan")
    sub.add_parser("coverage", parents=[common], help="first day, last day and row count of each daily table")
    m = sub.add_parser("month", parents=[common], help="a monthly view")
    m.add_argument("table", choices=sorted(MONTH_TABLES))
    m.add_argument("--months", type=int, default=12, help="how many months back (default 12)")
    d = sub.add_parser("day", parents=[common], help="a daily table")
    d.add_argument("table", choices=sorted(DAY_TABLES))
    d.add_argument("--days", type=int, default=14, help="how many days back (default 14)")
    o = sub.add_parser("outages", parents=[common], help="stored outages: internet, per-WAN path and ER605 WAN (--lan for LAN)")
    o.add_argument("--days", type=int, default=90, help="how many days back (default 90)")
    o.add_argument("--site", help="only this site (home, blr, homelab)")
    g = o.add_mutually_exclusive_group()
    g.add_argument("--lan", action="store_true", help="only LAN target outages")
    g.add_argument("--all", action="store_true", help="every kind")
    s = sub.add_parser("sql", parents=[common], help="run one read-only SELECT")
    s.add_argument("query", help="the SQL; tables live in schema net")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    cfg = load_config()
    for key in ("host", "port", "database", "user", "password_file", "image"):
        val = getattr(args, key, None)
        if val:
            cfg[key] = os.path.expanduser(val)
    args.json, args.csv = getattr(args, "json", False), getattr(args, "csv", False)
    if args.json and args.csv:
        raise SystemExit("nethist: choose --json or --csv, not both")
    for opt in ("months", "days"):
        if getattr(args, opt, 1) < 1:
            raise SystemExit("nethist: --%s must be at least 1" % opt)
    query = build_query(args)
    fmt = "json" if args.json else "csv" if args.csv else "text"
    if shutil.which("docker") is None:
        raise SystemExit("nethist: docker is not installed or not on PATH")
    pgpass = write_pgpass(cfg)
    try:
        return subprocess.run(psql_argv(cfg, query, fmt, pgpass)).returncode
    finally:
        os.unlink(pgpass)


if __name__ == "__main__":
    sys.exit(main())
