#!/usr/bin/env python3
"""Offline test of security/opnsense/pfstats, the producer of the firewall's pfstats line.

Standard library plus PyYAML; no network, no firewall, nothing installed. The
firewall's tools are replaced by shims on PATH that print fixture output in the
shapes FreeBSD prints. Run from anywhere:

    python3 -I ansible/tests/pfstats-test.py

Checks:
  * `sh -n` on pfstats-log.sh and install.sh
  * pfstats-log.sh, run on fixture pfctl, netstat and sysctl output with
    PFSTATS_STDOUT=1, prints exactly the expected line: every key, in order,
    with the value parsed from the right field; without PFSTATS_STDOUT it
    sends the same line with `logger -t pfstats`
  * the line against its readers: every regexp the tenant rules and the
    dashboard apply to job="pfstats" matches the line once and captures the
    value of the key it names
  * install.sh: a dry run (the default) on a fake root changes nothing and runs
    nothing; it refuses a root with no /usr/local/opnsense and a bad argument;
    --apply installs the three files with the documented modes, restarts
    configd, runs the cron script from /usr/local/opnsense/mvc, reloads cron,
    and fails when the line lacks a key
  * the configd action, the syslog program name and the cron command agree, and
    the wiring doc and the security README name what the installer does
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: run this from the Ansible controller environment")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "security" / "opnsense" / "pfstats"
LOG_SH = SRC / "pfstats-log.sh"
INSTALL_SH = SRC / "install.sh"
ACTIONS = SRC / "actions_pfstats.conf"
INC = SRC / "pfstats.inc"
CRON_PHP = SRC / "add-pfstats-cron.php"
WIRING = ROOT / "security" / "docs" / "wiring" / "index.md"
SEC_README = ROOT / "security" / "README.md"
RULES = ROOT / "monitoring" / "grafana" / "provisioning" / "alerting" / "rules-tenant.yaml"
DASH = ROOT / "monitoring" / "grafana" / "dashboards" / "fleet" / "tenant-activity.json"

errors = []


def fail(msg):
    errors.append(msg)


# --- fixtures, in the shapes the real tools print ---------------------------------------------
PFCTL_SI = """Status: Enabled for 0 days 02:03:04           Debug: Urgent

State Table                          Total             Rate
  current entries                      323
  searches                        12345678         1672.7/s
  inserts                           123456           16.7/s
  removals                          123133           16.7/s
Counters
  match                             300000           40.6/s
  bad-offset                             0            0.0/s
  fragment                               0            0.0/s
  short                                  0            0.0/s
  normalize                              0            0.0/s
  memory                                 0            0.0/s
  bad-timestamp                          0            0.0/s
  congestion                             0            0.0/s
  ip-option                              0            0.0/s
  proto-cksum                            0            0.0/s
  state-mismatch                        10            0.0/s
  state-insert                           0            0.0/s
  state-limit                            4            0.0/s
  src-limit                              2            0.0/s
  synproxy                               0            0.0/s
  map-failed                             0            0.0/s
"""
PFCTL_SM = """states        hard limit   405300
src-nodes     hard limit   405300
frags         hard limit     5000
table-entries hard limit  1000000
"""
PFCTL_SS = """10.10.90.5 -> 0.0.0.0 ( states 12, connections 0, rate 0.0/0.0s )
10.10.60.20 -> 0.0.0.0 ( states 40, connections 0, rate 0.0/0.0s )
"""
VIRUSPROT = "   10.10.90.9\n"
NETSTAT = """Name    Mtu Network       Address              Ipkts Ierrs Idrop     Ibytes    Opkts Oerrs     Obytes  Coll
vtnet0  1500 <Link#1>      bc:24:11:00:00:01  1000001     0     0  100000001   900001     0   90000001     0
vtnet0     - 192.168.1.5/24 192.168.1.5        1000000     -     -  100000000   900000     -   90000000     -
vtnet1  1500 <Link#2>      bc:24:11:00:00:02  1100002     0     0  110000002   910002     0   91000002     0
vtnet2  1500 <Link#3>      bc:24:11:47:7b:cf  5140724     0     0 3925897956  4915999     0 4153120928     0
vtnet2     - 10.10.60.1/24 10.10.60.1          5140000     -     - 3925000000  4915000     - 4153000000     -
vtnet3  1500 <Link#4>      bc:24:11:00:00:04  1300004     0     0  130000004   930004     0   93000004     0
vtnet4  1500 <Link#5>      bc:24:11:00:00:05  1400005     0     0  140000005   940005     0   94000005     0
vtnet5  1500 <Link#6>      bc:24:11:00:00:06  1500006     0     7  150000006   950006     0   95000006     0
lo0    16384 <Link#7>                          66666     0     0    6666666    66666     0    6666666     0
pflog0 33160 <Link#8>                              0     0     0          0        0     0          0     0
"""
LOADAVG = "{ 0.99 0.82 0.75 }\n"
FREE_PAGES = "115456\n"
PAGESIZE = "4096\n"

EXPECTED = (
    "states=323 states_max=405300 state_limit_hits=4 src_limit_hits=2 src_nodes=2 banned=1 load1=0.99 "
    "mem_free_mb=451"
    " vtnet0_ipkts=1000001 vtnet0_opkts=900001 vtnet0_ibytes=100000001 vtnet0_obytes=90000001 vtnet0_idrop=0"
    " vtnet1_ipkts=1100002 vtnet1_opkts=910002 vtnet1_ibytes=110000002 vtnet1_obytes=91000002 vtnet1_idrop=0"
    " vtnet2_ipkts=5140724 vtnet2_opkts=4915999 vtnet2_ibytes=3925897956 vtnet2_obytes=4153120928 vtnet2_idrop=0"
    " vtnet3_ipkts=1300004 vtnet3_opkts=930004 vtnet3_ibytes=130000004 vtnet3_obytes=93000004 vtnet3_idrop=0"
    " vtnet4_ipkts=1400005 vtnet4_opkts=940005 vtnet4_ibytes=140000005 vtnet4_obytes=94000005 vtnet4_idrop=0"
    " vtnet5_ipkts=1500006 vtnet5_opkts=950006 vtnet5_ibytes=150000006 vtnet5_obytes=95000006 vtnet5_idrop=7"
)

# Where install.sh puts each file, and with what mode. The apply test and the docs table both read this.
DEST = {
    "pfstats-log.sh": ("usr/local/sbin/pfstats-log.sh", 0o755),
    "actions_pfstats.conf": ("usr/local/opnsense/service/conf/actions.d/actions_pfstats.conf", 0o644),
    "pfstats.inc": ("usr/local/etc/inc/plugins.inc.d/pfstats.inc", 0o644),
}

PATH_LINE = "PATH=/sbin:/usr/sbin:/bin:/usr/bin:/usr/local/bin:/usr/local/sbin"

SHIMS = {
    "pfctl": """#!/bin/sh
case "$*" in
    "-si") cat "$FIXDIR/si.txt" ;;
    "-sm") cat "$FIXDIR/sm.txt" ;;
    "-sS") cat "$FIXDIR/sS.txt" ;;
    "-t virusprot -T show") cat "$FIXDIR/virusprot.txt" ;;
    *) echo "pfctl shim: unexpected arguments: $*" >&2; exit 1 ;;
esac
""",
    "netstat": """#!/bin/sh
case "$*" in
    "-ibn") cat "$FIXDIR/netstat.txt" ;;
    *) echo "netstat shim: unexpected arguments: $*" >&2; exit 1 ;;
esac
""",
    "sysctl": """#!/bin/sh
case "$*" in
    "-n vm.loadavg") cat "$FIXDIR/loadavg.txt" ;;
    "-n vm.stats.vm.v_free_count") cat "$FIXDIR/free.txt" ;;
    "-n hw.pagesize") cat "$FIXDIR/pagesize.txt" ;;
    *) echo "sysctl shim: unexpected arguments: $*" >&2; exit 1 ;;
esac
""",
    "logger": """#!/bin/sh
printf '%s\\n' "$*" >> "$FIXDIR/logger.log"
""",
    # install.sh's own commands: record, never act
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


def write_fixture(fixdir, **overrides):
    files = {"si.txt": PFCTL_SI, "sm.txt": PFCTL_SM, "sS.txt": PFCTL_SS, "virusprot.txt": VIRUSPROT,
             "netstat.txt": NETSTAT, "loadavg.txt": LOADAVG, "free.txt": FREE_PAGES, "pagesize.txt": PAGESIZE}
    files.update(overrides)
    for name, text in files.items():
        (fixdir / name).write_text(text)


def make_shims(base):
    shims = base / "shims"
    shims.mkdir()
    for name, body in SHIMS.items():
        path = shims / name
        path.write_text(body)
        path.chmod(0o755)
    return shims


def env_for(shims, fixdir, **extra):
    env = dict(os.environ)
    env["PATH"] = f"{shims}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    env["FIXDIR"] = str(fixdir)
    env.pop("PFSTATS_STDOUT", None)
    env.pop("PFSTATS_ROOT", None)
    env.update(extra)
    return env


def patched_log_script(shims):
    """pfstats-log.sh sets its own PATH, which would hide the shims. Replace that one line."""
    text = LOG_SH.read_text()
    if text.count(PATH_LINE) != 1:
        fail(f"pfstats-log.sh no longer has the line {PATH_LINE!r} exactly once; update this test with it")
        return text
    return text.replace(PATH_LINE, f"PATH={shims}:/usr/bin:/bin")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def snapshot(tree):
    out = {}
    for path in sorted(tree.rglob("*")):
        rel = str(path.relative_to(tree))
        if path.is_dir():
            out[rel] = ("dir", stat.S_IMODE(path.stat().st_mode))
        else:
            st = path.stat()
            out[rel] = ("file", stat.S_IMODE(st.st_mode), st.st_mtime_ns,
                        hashlib.sha256(path.read_bytes()).hexdigest())
    return out


def kv(line):
    return dict(part.split("=", 1) for part in line.split())


# --- syntax -------------------------------------------------------------------------------------
for script in (LOG_SH, INSTALL_SH):
    p = run(["sh", "-n", str(script)])
    if p.returncode != 0:
        fail(f"sh -n {script.name} failed: {p.stderr.strip()}")

with tempfile.TemporaryDirectory() as tmp:
    base = pathlib.Path(tmp)
    shims = make_shims(base)
    fixdir = base / "fix"
    fixdir.mkdir()
    write_fixture(fixdir)

    # --- the producer ---------------------------------------------------------------------------
    patched = base / "pfstats-log.sh"
    patched.write_text(patched_log_script(shims))
    patched.chmod(0o755)

    p = run(["sh", str(patched)], env=env_for(shims, fixdir, PFSTATS_STDOUT="1"))
    got = p.stdout.strip()
    if p.returncode != 0:
        fail(f"pfstats-log.sh exited {p.returncode}: {p.stderr.strip()}")
    if got != EXPECTED:
        want, have = kv(EXPECTED), kv(got) if got and all("=" in x for x in got.split()) else {}
        diffs = [f"{k}: want {want[k]!r}, got {have.get(k)!r}" for k in want if have.get(k) != want[k]]
        extra = [k for k in have if k not in want]
        fail("pfstats-log.sh printed the wrong line: " + "; ".join(diffs + [f"unexpected key {k}" for k in extra])
             + ("" if diffs or extra else f" (order or spacing differs): {got!r}"))

    # sent to syslog when PFSTATS_STDOUT is unset
    (fixdir / "logger.log").unlink(missing_ok=True)
    p = run(["sh", str(patched)], env=env_for(shims, fixdir))
    logged = (fixdir / "logger.log").read_text().strip() if (fixdir / "logger.log").exists() else ""
    if p.stdout.strip():
        fail(f"pfstats-log.sh printed to stdout without PFSTATS_STDOUT: {p.stdout.strip()!r}")
    if logged != f"-t pfstats {EXPECTED}":
        fail(f"pfstats-log.sh did not send the line with `logger -t pfstats`: {logged!r}")

    # an empty ban list and no source nodes read 0, not blank
    write_fixture(fixdir, **{"virusprot.txt": "", "sS.txt": ""})
    p = run(["sh", str(patched)], env=env_for(shims, fixdir, PFSTATS_STDOUT="1"))
    have = kv(p.stdout.strip()) if p.stdout.strip() else {}
    if have.get("banned") != "0" or have.get("src_nodes") != "0":
        fail(f"an empty ban list and source table must read banned=0 src_nodes=0, got {have.get('banned')!r} "
             f"and {have.get('src_nodes')!r}")
    write_fixture(fixdir)

    # --- the line against the rules and the dashboard that read it -------------------------------
    line_kv = kv(EXPECTED)
    exprs = []
    rules = yaml.safe_load(RULES.read_text())
    for g in rules.get("groups") or []:
        for r in g.get("rules") or []:
            for q in r.get("data") or []:
                exprs.append((f"rule {r.get('uid')}", str((q.get("model") or {}).get("expr", ""))))
    for panel in json.loads(DASH.read_text()).get("panels") or []:
        for t in panel.get("targets") or []:
            exprs.append((f"panel {panel.get('id')} ({panel.get('title')})", str(t.get("expr", ""))))
    checked = 0
    for where, expr in exprs:
        if 'job="pfstats"' not in expr:
            continue
        for logql in re.findall(r'regexp "((?:[^"\\]|\\.)*)"', expr):
            pattern = logql.replace("\\\\", "\\")
            m = re.match(r"(?:\\b)?([a-z0-9_]+)=\(\?P<v>", pattern)
            if not m or m.group(1) not in line_kv:
                fail(f"{where}: regexp {pattern!r} names a key the pfstats line does not have")
                continue
            found = re.findall(pattern, EXPECTED)
            if len(found) != 1 or found[0] != line_kv[m.group(1)]:
                fail(f"{where}: regexp {pattern!r} finds {found} in the line, expected [{line_kv[m.group(1)]!r}]")
            checked += 1
    if checked < 8:
        fail(f"only {checked} pfstats regexps were checked against the line; the rules and dashboard use more")

    # --- install.sh: dry run, refusals, apply on a fake root ---------------------------------------
    def fake_root(name, with_opnsense=True):
        root = base / name
        for d in ("usr/local/sbin", "usr/local/opnsense/service/conf/actions.d", "usr/local/etc/inc/plugins.inc.d",
                  "usr/local/opnsense/mvc"):
            if with_opnsense or "opnsense" not in d:
                (root / d).mkdir(parents=True)
        return root

    calls = fixdir / "calls.log"
    root = fake_root("root-dry")
    before = snapshot(root)
    p = run(["sh", str(INSTALL_SH)], env=env_for(shims, fixdir, PFSTATS_ROOT=str(root)))
    if p.returncode != 0:
        fail(f"install.sh dry run exited {p.returncode}: {p.stderr.strip()}")
    if snapshot(root) != before:
        fail("install.sh changed the fake root in a dry run")
    if calls.exists() and calls.read_text().strip():
        fail(f"install.sh ran commands in a dry run: {calls.read_text().strip()}")
    for needle in ("would run:", "--apply", "nothing was changed"):
        if needle not in p.stdout:
            fail(f"the install.sh dry run output does not say {needle!r}")

    root = fake_root("root-none", with_opnsense=False)
    (root / "usr/local/sbin").mkdir(parents=True, exist_ok=True)
    before = snapshot(root)
    for args in ([], ["--apply"]):
        p = run(["sh", str(INSTALL_SH)] + args, env=env_for(shims, fixdir, PFSTATS_ROOT=str(root)))
        if p.returncode == 0 or "not an OPNsense firewall" not in p.stderr:
            fail(f"install.sh {' '.join(args)} must refuse a root with no /usr/local/opnsense, got exit "
                 f"{p.returncode}: {p.stderr.strip()!r}")
    if snapshot(root) != before:
        fail("install.sh changed a root that is not an OPNsense firewall")
    if calls.exists() and calls.read_text().strip():
        fail("install.sh ran commands on a root that is not an OPNsense firewall")

    p = run(["sh", str(INSTALL_SH), "--bogus"], env=env_for(shims, fixdir, PFSTATS_ROOT=str(fake_root("root-arg"))))
    if p.returncode != 2:
        fail(f"install.sh --bogus must exit 2, got {p.returncode}")

    # apply: run a COPY of the directory whose pfstats-log.sh uses the shims' PATH
    src = base / "src"
    shutil.copytree(SRC, src)
    (src / "pfstats-log.sh").write_text(patched_log_script(shims))
    (src / "pfstats-log.sh").chmod(0o755)
    root = fake_root("root-apply")
    calls.unlink(missing_ok=True)
    p = run(["sh", str(src / "install.sh"), "--apply"], env=env_for(shims, fixdir, PFSTATS_ROOT=str(root)))
    if p.returncode != 0:
        fail(f"install.sh --apply exited {p.returncode}: {p.stdout.strip()} {p.stderr.strip()}")
    for name, (dest, mode) in DEST.items():
        path = root / dest
        if not path.is_file():
            fail(f"install.sh --apply did not install {dest}")
            continue
        if stat.S_IMODE(path.stat().st_mode) != mode:
            fail(f"{dest} has mode {oct(stat.S_IMODE(path.stat().st_mode))}, expected {oct(mode)}")
        if path.read_bytes() != (src / name).read_bytes():
            fail(f"{dest} differs from {name}")
    expected_calls = [
        "service configd restart",
        f"php {src}/add-pfstats-cron.php apply (cwd {root}/usr/local/opnsense/mvc)",
        "configctl template reload OPNsense/Cron",
        "configctl cron restart",
    ]
    got_calls = calls.read_text().strip().splitlines() if calls.exists() else []
    if got_calls != expected_calls:
        fail(f"install.sh --apply ran {got_calls}, expected {expected_calls}")
    if "ok: the line carries" not in p.stdout:
        fail("install.sh --apply did not report the line check passing")

    # apply, but the script loses a key: the installer must refuse to call it good
    for key in ("states", "states_max", "src_limit_hits", "banned"):
        src_bad = base / f"src-no-{key}"
        shutil.copytree(SRC, src_bad)
        text = patched_log_script(shims)
        text, n = re.subn(rf"\b{key}=\$\{{[a-z_]+:-0\}} ", "", text, count=1)
        if n != 1:
            fail(f"could not remove {key}= from the line to test the installer's check")
            continue
        (src_bad / "pfstats-log.sh").write_text(text)
        (src_bad / "pfstats-log.sh").chmod(0o755)
        p = run(["sh", str(src_bad / "install.sh"), "--apply"],
                env=env_for(shims, fixdir, PFSTATS_ROOT=str(fake_root(f"root-no-{key}"))))
        if p.returncode == 0 or f"no {key}= field" not in p.stderr:
            fail(f"install.sh --apply accepted a line with no {key}= (exit {p.returncode}): {p.stderr.strip()!r}")

# --- the three pieces agree --------------------------------------------------------------------
actions = ACTIONS.read_text()
section = re.search(r"^\[(\w+)\]", actions, re.M)
if not section or ACTIONS.name != f"actions_pfstats.conf":
    fail("actions_pfstats.conf must be named actions_<name>.conf with a [section]")
else:
    action = f"pfstats {section.group(1)}"          # configd names an action <file prefix> <section>
    if f"'command' => '{action}'" not in CRON_PHP.read_text():
        fail(f"the cron job in add-pfstats-cron.php does not run the configd action {action!r}")
if "command:/usr/local/sbin/pfstats-log.sh" not in actions:
    fail("the configd action does not run /usr/local/sbin/pfstats-log.sh")
if "type:script" not in actions:
    fail("the configd action is not type:script")
if "logger -t pfstats" not in LOG_SH.read_text():
    fail("pfstats-log.sh no longer logs with the tag pfstats")
if "$logfacilities['pfstats'] = ['facility' => ['pfstats']];" not in INC.read_text():
    fail("pfstats.inc does not register the syslog program pfstats")
if not os.access(LOG_SH, os.X_OK) or not os.access(INSTALL_SH, os.X_OK):
    fail("pfstats-log.sh and install.sh must be executable")
if "tenantmon" in CRON_PHP.read_text():
    fail("add-pfstats-cron.php still names tenantmon-cron.php")

# --- docs ---------------------------------------------------------------------------------------
wiring = WIRING.read_text()
section_pf = wiring.split("### pfstats")[-1].split("\n### ")[0] if "### pfstats" in wiring else ""
if not section_pf:
    fail("security/docs/wiring/index.md has no pfstats section")
for needle in ("security/opnsense/pfstats/", "sh install.sh", "sh install.sh --apply", "csh", "PFSTATS_STDOUT=1",
               "/usr/local/sbin/pfstats-log.sh",
               "/usr/local/opnsense/service/conf/actions.d/actions_pfstats.conf",
               "/usr/local/etc/inc/plugins.inc.d/pfstats.inc", "add-pfstats-cron.php", "pfstats log",
               "**Forward the program.**", "pfstats.inc", "**Revert.**", "/usr/local/opnsense",
               "states_max", "src_limit_hits", "banned", "ansible/tests/pfstats-test.py"):
    if needle not in section_pf:
        fail(f"the wiring doc's pfstats section no longer mentions {needle}")
doc_line = re.search(r"^states=\S+ .*$", section_pf, re.M)
if doc_line:
    doc_keys = [part.split("=", 1)[0] for part in doc_line.group(0).split()]
    want_keys = [part.split("=", 1)[0] for part in EXPECTED.split()][:13]
    if doc_keys != want_keys:
        fail(f"the example line in the wiring doc has keys {doc_keys}, the script writes {want_keys}")
else:
    fail("the wiring doc's pfstats section has no example line")
for name, (dest, mode) in DEST.items():
    if not re.search(rf"\| `{re.escape(name)}` \| `/{re.escape(dest)}` \| {mode:04o} \|", section_pf):
        fail(f"the wiring doc's table does not give {name} the path /{dest} and mode {mode:04o}")
if "opnsense/pfstats/" not in SEC_README.read_text():
    fail("security/README.md does not list opnsense/pfstats/")
if not re.search(r"^mkdocs|^nav", (ROOT / "security" / "mkdocs.yml").read_text(), re.M):
    fail("security/mkdocs.yml has no nav")

EM_DASH = chr(0x2014)
for path in sorted(SRC.iterdir()) + [pathlib.Path(__file__)]:
    if EM_DASH in path.read_text():
        fail(f"{path.name} contains an em dash")

if errors:
    print("\n".join(f"FAIL: {e}" for e in errors))
    sys.exit(1)
print("ok: pfstats producer, installer and docs agree with the rules that read the line")
