#!/usr/bin/env python3
"""Test roles/pve_guest_metrics: the exporter script, run against fixtures.

Standard library only; run from anywhere:

    python3 -I ansible/tests/pve-guest-metrics-test.py

The template is rendered from defaults/main.yml and loaded as a module. Its
external tools are replaced, not its code: pvesh, iptables, tc and conntrack
become small executables that answer from fixture files (so the real
subprocess, streaming, exit-status and stderr handling runs), and the firewall
directory, sysfs and the output directory become temporary paths.

Checks:
  * the rendered script has no Jinja left over, compiles, and never puts the
    opening brace next to a percent sign or a hash
  * the exact exposition text for a node with tenant VMs: the tenant and
    lease segments, an uppercase tag, a /32 entry, a VM with no firewall
    file, an empty ipfilter set, an address under some other ipset, a tag
    that only contains "tenant", a container, a guest on another node and a
    template; conntrack counted by ORIGINAL source only and by whole address
    (10.10.60.21 does not collect 10.10.60.210); `conntrack -L` is run once
    for any number of tenants and not at all when there is nothing to count
  * the conntrack reading itself: output that is only streamed (a table of tens
    of thousands of lines is iterated, never read whole), lines that are not
    entries (the summary conntrack prints, blanks) ignored even on stdout, a
    successful exit with no entries is an empty table and reads 0, and a
    missing binary, a nonzero exit or a run past the timeout fails the run
  * the firewall counters: private and private_in by chain direction, smtp,
    two rules with one label summed, a chain that is absent giving no lines,
    non-DROP rules and dpt:2525 ignored, a counter above a million kept exact
  * the packet-rate counter, read from `tc -s filter show` text: the pref-5
    police filter's "dropped" figure (not overlimits, not the pref-50
    bandwidth limit on the same qdisc, not a pref-5 filter that is not a
    police), a zero kept, a tenant VM with no address still counted, no line
    for a stopped VM, an empty listing, or a tap that vanished between the
    sysfs check and tc; tc is asked only about running tenant VMs
  * the packet-limit gauge: 1 or 0 for every RUNNING VM with both tags tenant
    and lease, 0 for a filter that is absent and for a tap with no ingress
    qdisc at all, 1 for a filter that has dropped nothing, and no line for a
    work VM, a stopped VM or one whose tap vanished
  * the firewall-in-force gauge, for every RUNNING tenant VM of either segment:
    1 only when the datacenter option, net0 firewall=1, the VM file's enable: 1
    and the GROUP rule for the segment's group (tenant for work, lease for
    lease) and the tap<vmid>i0-OUT chain all hold; 0 for each way to miss one
    (including a rule switched off with a pipe and the wrong group), the
    datacenter option read exactly once and not at all without a running
    tenant VM, the chain asked about only when the rest holds, a stopped VM
    giving nothing, and the file parsers checked on their own
  * the dropped-packet counter in both families (iptables and ip6tables), told
    apart by the family label, each with its own private set, and a chain
    missing in one family only giving no line for that family
  * the role fails the play when /usr/sbin/conntrack is missing, before it
    installs anything, installs nothing itself, and names the same path as
    the script
  * a node with no tenant VMs and no group chains writes none of the four
    families, does not touch the conntrack table or tc, and still writes the
    NIC and timestamp lines
  * a failing iptables, ip6tables, tap-chain listing, datacenter option, tc or
    conntrack, and an unreadable firewall file, each fail the run and leave the
    previous file untouched
  * every fleet_pve_* metric the script emits is documented in
    docs/reference/variables.md, and the reverse
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import re
import socket
import stat
import subprocess
import sys
import tempfile

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
ROLE = ANSIBLE / "roles" / "pve_guest_metrics"
PREFIX = "pve_guest_metrics_"
GROUP_CHAINS = ("GROUP-tenant-IN", "GROUP-tenant-OUT", "GROUP-lease-IN", "GROUP-lease-OUT")
errors = []


def fail(msg):
    errors.append(msg)


def check(cond, msg):
    if not cond:
        fail(msg)


def defaults():
    out = {}
    for line in (ROLE / "defaults" / "main.yml").read_text().splitlines():
        m = re.match(r"^(" + PREFIX + r"\w+):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def render(text, values):
    def sub(m):
        name = m.group(1)
        if name not in values:
            fail("template uses " + name + ", which has no default")
            return ""
        return values[name]
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, text)


def render_script(metrics_dir):
    values = defaults()
    values["alloy_textfile_dir"] = metrics_dir
    values = {k: render(v, values) if "{{" in v else v for k, v in values.items()}
    template = (ROLE / "templates" / "fleet-pve-guests.j2").read_text()
    text = render(template, values)
    for tag in ("{{", "{%", "{#"):
        check(tag not in text, "rendered script still contains " + tag)
    # The template is rendered by Jinja before install, so these two-character
    # sequences must not appear in it even in a comment. The only braces that
    # may follow an opening brace are the two variable delimiters.
    stripped = re.sub(r"\{\{\s*\w+\s*\}\}", "", template)
    for tag in ("{%", "{#", "{{"):
        check(tag not in stripped, "template contains " + tag + " outside a variable")
    return text, values


def load_script(text, directory):
    path = pathlib.Path(directory) / "fleet-pve-guests"
    path.write_text(text)
    compile(text, str(path), "exec")
    loader = importlib.machinery.SourceFileLoader("pveguests", str(path))
    spec = importlib.util.spec_from_loader("pveguests", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_PVESH = """#!@PY@
import json, sys
fixture = json.load(open("@FIXTURE@"))
path = sys.argv[2]
with open("@FIXTURE@.calls", "a") as fh:
    fh.write(path + "\\n")
if path not in fixture:
    sys.stderr.write("no such path: " + path + "\\n")
    sys.exit(2)
print(json.dumps(fixture[path]))
"""

# Answers like `iptables -w 5 -nvxL <chain>`: the listing from <chain>.txt,
# the "no such chain" error (exit 1) when there is no file, or the contents of
# <chain>.fail on stderr with exit 4, as iptables does when it cannot get the
# xtables lock. Every call is appended to calls.log.
FAKE_IPTABLES = """#!@PY@
import os, sys
fixture = "@FIXTURE@"
if sys.argv[1:4] != ["-w", "5", "-nvxL"] or len(sys.argv) != 5:
    sys.stderr.write("unexpected arguments: " + " ".join(sys.argv[1:]) + "\\n")
    sys.exit(2)
chain = sys.argv[4]
with open(os.path.join(fixture, "calls.log"), "a") as fh:
    fh.write(chain + "\\n")
listing = os.path.join(fixture, chain + ".txt")
failing = os.path.join(fixture, chain + ".fail")
if os.path.exists(failing):
    sys.stderr.write(open(failing).read())
    sys.exit(4)
if not os.path.exists(listing):
    sys.stderr.write("iptables: No chain/target/match by that name.\\n")
    sys.exit(1)
sys.stdout.write(open(listing).read())
"""


# Answers like `tc -s filter show dev <tap> parent ffff:`: the listing from
# <tap>.txt, `Cannot find device` (exit 1) when there is none, or, for a tap
# with a <tap>.fail file, that file's first line as the exit status and the rest
# on stderr. Every call is appended to calls.log.
FAKE_TC = """#!@PY@
import os, sys
fixture = "@FIXTURE@"
argv = sys.argv[1:]
if len(argv) != 7 or argv[:3] != ["-s", "filter", "show"] or argv[3] != "dev" or argv[5:] != ["parent", "ffff:"]:
    sys.stderr.write("unexpected arguments: " + " ".join(argv) + "\\n")
    sys.exit(2)
tap = argv[4]
with open(os.path.join(fixture, "calls.log"), "a") as fh:
    fh.write(tap + "\\n")
failing = os.path.join(fixture, tap + ".fail")
listing = os.path.join(fixture, tap + ".txt")
if os.path.exists(failing):
    code, _, message = open(failing).read().partition("\\n")
    sys.stderr.write(message)
    sys.exit(int(code))
if not os.path.exists(listing):
    sys.stderr.write('Cannot find device "' + tap + '"\\n')
    sys.exit(1)
sys.stdout.write(open(listing).read())
"""


# Answers like `conntrack -L`: the fixture's stdout.txt on stdout and, as the
# real tool does, a summary line on STDERR. A `fail` file (first line the exit
# status, the rest stderr) makes it fail; a `hang` file (seconds) makes it sleep
# first, so a run with no timeout enforced still ends and the test can tell.
# Every call is appended to calls.log.
FAKE_CONNTRACK = """#!@PY@
import os, sys, time
fixture = "@FIXTURE@"
if sys.argv[1:] != ["-L"]:
    sys.stderr.write("unexpected arguments: " + " ".join(sys.argv[1:]) + "\\n")
    sys.exit(2)
with open(os.path.join(fixture, "calls.log"), "a") as fh:
    fh.write("call\\n")
failing = os.path.join(fixture, "fail")
if os.path.exists(failing):
    code, _, message = open(failing).read().partition("\\n")
    sys.stderr.write(message)
    sys.exit(int(code))
hang = os.path.join(fixture, "hang")
if os.path.exists(hang):
    time.sleep(float(open(hang).read()))
text = open(os.path.join(fixture, "stdout.txt")).read()
sys.stdout.write(text)
sys.stdout.flush()
shown = len([l for l in text.splitlines() if " src=" in l])
sys.stderr.write("conntrack v1.4.7 (conntrack-tools): " + str(shown) + " flow entries have been shown.\\n")
"""


class ProbedStream:
    """conntrack's stdout as the script sees it: iteration counts lines, and a
    bulk read is recorded, because the table must be streamed, not loaded."""

    def __init__(self, stream, probe):
        self._stream = stream
        self._probe = probe

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self._stream)
        self._probe.lines += 1
        return line

    def read(self, *args):
        self._probe.bulk_reads += 1
        return self._stream.read(*args)

    def readlines(self, *args):
        self._probe.bulk_reads += 1
        return self._stream.readlines(*args)

    def close(self):
        return self._stream.close()


class ProbedProc:
    def __init__(self, proc, probe):
        self._proc = proc
        self.stdout = ProbedStream(proc.stdout, probe)

    def __getattr__(self, name):
        return getattr(self._proc, name)


class Probe:
    """Stands in for the script's `subprocess` module. Everything is delegated;
    only Popen of the conntrack binary is wrapped to watch how its output is read."""

    def __init__(self, real, conntrack_path):
        self._real = real
        self._path = conntrack_path
        self.lines = 0
        self.bulk_reads = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Popen(self, args, *a, **k):
        proc = self._real.Popen(args, *a, **k)
        return ProbedProc(proc, self) if args and args[0] == self._path else proc


def write_exec(path, template, **subst):
    text = template.replace("@PY@", sys.executable)
    for key, value in subst.items():
        text = text.replace("@" + key + "@", str(value))
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


HEADER = "    pkts      bytes target     prot opt in     out     source               destination\n"

TENANT_IN = (
    "Chain GROUP-tenant-IN (1 references)\n" + HEADER
    + "      41      2460 PVEFW-SET-ACCEPT-MARK  tcp  --  *      *       10.10.60.1           0.0.0.0/0            tcp dpt:22 /* PVESIG:aaa */\n"
    + "       7       588 DROP       all  --  *      *       0.0.0.0/0            0.0.0.0/0            match-set PVEFW-0-private-v4 src /* PVESIG:bbb */\n"
)
TENANT_OUT = (
    "Chain GROUP-tenant-OUT (1 references)\n" + HEADER
    + "    9013   1086206 PVEFW-SET-ACCEPT-MARK  all  --  *      *       0.0.0.0/0            10.10.60.1           /* PVESIG:ccc */\n"
    + " 1234567 148148040 DROP       all  --  *      *       0.0.0.0/0            0.0.0.0/0            match-set PVEFW-0-private-v4 dst /* PVESIG:ddd */\n"
    + "       3       180 DROP       tcp  --  *      *       0.0.0.0/0            0.0.0.0/0            tcp dpt:25 /* PVESIG:eee */\n"
    # None of these are drops of a counted kind.
    + "       5       300 RETURN     tcp  --  *      *       0.0.0.0/0            0.0.0.0/0            tcp dpt:25\n"
    + "      99      9900 RETURN     all  --  *      *       0.0.0.0/0            0.0.0.0/0            match-set PVEFW-0-private-v4 dst\n"
    + "       2       120 DROP       tcp  --  *      *       0.0.0.0/0            0.0.0.0/0            tcp dpt:2525\n"
)
# The other family's private set must not count, whichever chain it is in.
TENANT_OUT += "     100      6000 DROP       all  --  *      *       0.0.0.0/0            0.0.0.0/0            match-set PVEFW-0-private-v6 dst\n"
# A zero counter is still a series, and two rules with one label add up.
LEASE_OUT = (
    "Chain GROUP-lease-OUT (1 references)\n" + HEADER
    + "       0         0 DROP       all  --  *      *       0.0.0.0/0            0.0.0.0/0            match-set PVEFW-0-private-v4 dst\n"
    + "       2       120 DROP       tcp  --  *      *       0.0.0.0/0            0.0.0.0/0            tcp dpt:25\n"
    + "       4       240 DROP       tcp  --  *      *       0.0.0.0/0            0.0.0.0/0            tcp dpt:25\n"
)

# The same chains in ip6tables: another set name, other addresses, other counters.
# GROUP-tenant-IN and GROUP-lease-OUT do not exist in this family: silence.
HEADER6 = "    pkts      bytes target     prot opt in     out     source                                    destination\n"
TENANT_OUT6 = (
    "Chain GROUP-tenant-OUT (1 references)\n" + HEADER6
    + "       4       320 PVEFW-SET-ACCEPT-MARK  all      *      *       ::/0                 fd00::1\n"
    + "      55      4400 DROP       all      *      *       ::/0                 ::/0                 match-set PVEFW-0-private-v6 dst\n"
    + "       2       160 DROP       tcp      *      *       ::/0                 ::/0                 tcp dpt:25\n"
    # the other family's set must not count here
    + "     200     16000 DROP       all      *      *       ::/0                 ::/0                 match-set PVEFW-0-private-v4 dst\n"
)
LEASE_IN6 = (
    "Chain GROUP-lease-IN (1 references)\n" + HEADER6
    + "       9       720 DROP       all      *      *       ::/0                 ::/0                 match-set PVEFW-0-private-v6 src\n"
)


def tap_chain(vmid):
    return "Chain tap" + str(vmid) + "i0-OUT (1 references)\n" + HEADER


# What tc prints for the packet-rate limit, as the hookscript installs it. Only
# the two figures that differ from a fresh filter are invented: "dropped" and
# "overlimits" are made different so the test can tell which one is read.
TC_RATE_LIMIT = """filter protocol all pref 5 matchall chain 0 handle 0x1
    not_in_hw (rule hit %(pkt)d)
    action order 1:  police 0x1 rate 0bit burst 0b mtu 4096Mb pkts_rate 20000 pkts_burst 40000 action drop overhead 0b
    ref 1 bind 1  installed 640 sec used 0 sec
    Action statistics:
    Sent %(bytes)d bytes %(pkt)d pkt (dropped %(dropped)d, overlimits %(over)d requeues 0)
"""
# Proxmox's own bandwidth limit on the same ingress qdisc, at pref 50, listed as
# a header line and again with its handle. Its drops are not the packet limit.
TC_PVE_LIMIT = """filter protocol all pref 50 basic chain 0
filter protocol all pref 50 basic chain 0 handle 0x1
    action order 1:  police 0x1 rate 12500Kbit burst 1280Kb mtu 64Kb action drop overhead 0b
    ref 1 bind 1  installed 640 sec used 3 sec
    Action statistics:
    Sent 123456 bytes 789 pkt (dropped 42, overlimits 42 requeues 0)
"""
# Someone else's filter at pref 5 that drops but is not a police.
TC_FOREIGN_PREF5 = """filter protocol all pref 5 matchall chain 0 handle 0x1
    not_in_hw (rule hit 10)
    action order 1: gact action drop
     random type none pass val 0
     index 1 ref 1 bind 1 installed 5 sec used 5 sec
    Action statistics:
    Sent 600 bytes 10 pkt (dropped 10, overlimits 0 requeues 0)
"""
# The filter exactly as printed for a fresh install: nothing dropped yet.
TC_FRESH = """filter protocol all pref 5 matchall chain 0 handle 0x1
    not_in_hw (rule hit 0)
    action order 1:  police 0x1 rate 0bit burst 0b mtu 4096Mb pkts_rate 20000 pkts_burst 40000 action drop overhead 0b
    ref 1 bind 1  installed 0 sec used 0 sec
    Action statistics:
    Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)
"""
TC_ALICE = TC_RATE_LIMIT % {"pkt": 1500, "bytes": 91236, "dropped": 1234, "over": 1300} + TC_PVE_LIMIT
TC_BOB = TC_FOREIGN_PREF5 + TC_PVE_LIMIT
TC_CAROL = TC_RATE_LIMIT % {"pkt": 80, "bytes": 4800, "dropped": 77, "over": 77}

def fw(addresses, group="tenant", enable="1", rule=None):
    """A VM firewall file as proxmox/pve-guest.sh writes it. rule replaces the
    GROUP line, so a test can leave it out or switch it off with a pipe."""
    rule = "GROUP " + group if rule is None else rule
    return ("# Managed by proxmox/pve-guest.sh: tenant VM.\n[OPTIONS]\nenable: " + enable
            + "\npolicy_in: DROP\nipfilter: 1\n\n"
            "[IPSET ipfilter-net0] # the only source address net0 may use\n" + addresses
            + "\n[RULES]\n" + rule + "\nIN ACCEPT -p tcp -dport 22\n")


def ct_line(proto, orig_src, orig_dst, reply_src, reply_dst):
    """One `conntrack -L` line, in the shapes the tool prints for each protocol."""
    if proto == "tcp":
        return ("tcp      6 51 TIME_WAIT src=" + orig_src + " dst=" + orig_dst + " sport=34324 dport=443 src="
                + reply_src + " dst=" + reply_dst + " sport=443 dport=34324 [ASSURED] mark=399 use=1\n")
    if proto == "udp":
        return ("udp      17 25 src=" + orig_src + " dst=" + orig_dst + " sport=51234 dport=53 [UNREPLIED] src="
                + reply_src + " dst=" + reply_dst + " sport=53 dport=51234 mark=0 use=1\n")
    return ("icmp     1 29 src=" + orig_src + " dst=" + orig_dst + " type=8 code=0 id=1 src="
            + reply_src + " dst=" + reply_dst + " type=0 code=0 id=1 mark=0 use=1\n")


CONNTRACK_TABLE = "".join(
    # alice, 10.10.60.21: four connections she opened, of three kinds
    [ct_line("tcp", "10.10.60.21", "93.184.216.34", "93.184.216.34", "192.168.1.5")] * 2
    + [ct_line("udp", "10.10.60.21", "9.9.9.9", "9.9.9.9", "192.168.1.5")]
    + [ct_line("icmp", "10.10.60.21", "9.9.9.9", "9.9.9.9", "192.168.1.5")]
    # her address as the REPLY source only: OPNsense reaching in over SSH
    + [ct_line("tcp", "10.10.60.1", "10.10.60.21", "10.10.60.21", "10.10.60.1")]
    # 10.10.60.210 is not 10.10.60.21
    + [ct_line("tcp", "10.10.60.210", "1.1.1.1", "1.1.1.1", "192.168.1.5")] * 2
    # bob, 10.10.60.22
    + [ct_line("tcp", "10.10.60.22", "1.1.1.1", "1.1.1.1", "192.168.1.5")] * 2
    # carol, 10.10.60.23, only ever the reply source
    + [ct_line("udp", "10.10.60.1", "10.10.60.23", "10.10.60.23", "10.10.60.1")]
    # a VM that is not a tenant
    + [ct_line("tcp", "10.10.60.99", "1.1.1.1", "1.1.1.1", "192.168.1.5")] * 5
    + [ct_line("tcp", "fd00::21", "2606:4700::1111", "2606:4700::1111", "fd00::21")]
    # lines that are not entries, whichever stream conntrack put them on
    + ["conntrack v1.4.7 (conntrack-tools): 20 flow entries have been shown.\n", "\n"]
)

# vmid, type, name, tags, bridge, firewall file body (None for no file), node
HOST = "NODE"
GUESTS = [
    (100, "qemu", "devbox", "work;uncapped", "vmbr0", fw("10.10.60.99\n"), HOST),
    (112, "lxc", "grafana", "tenant", "vmbr1", fw("10.10.60.12\n"), HOST),
    (201, "qemu", "alice-vm", "tenant", "vmbr2", fw("10.10.60.21\n"), HOST),
    # bob is a lease with the work group, which is the wrong group for a lease
    (202, "qemu", "bob-vm", "tenant;lease", "vmbr2", fw("10.10.60.22/32 # bob\n"), HOST),
    (203, "qemu", "carol-vm", "lease;Tenant", "vmbr2", fw("!10.10.60.200\n10.10.60.23\n", group="lease"), HOST),
    (204, "qemu", "dave-vm", "tenant", "vmbr2", None, HOST),
    (205, "qemu", "erin-vm", "tenant", "vmbr2",
     fw("# nothing yet\n\n[IPSET management]\n10.10.60.77\n"), HOST),
    (206, "qemu", "frank-vm", "tenants;nontenant", "vmbr2", fw("10.10.60.26\n"), HOST),
    (207, "qemu", "elsewhere", "tenant", "vmbr2", fw("10.10.60.27\n"), "otherpve"),
    (208, "qemu", "gina-vm", "tenant", "vmbr2", None, HOST),
    # Lease VMs with no address, to see the packet limit alone: 209 has a tap
    # with no ingress qdisc, 210 is stopped, 211 has the filter and no drops.
    (209, "qemu", "hana-vm", "tenant;lease", "vmbr2", None, HOST),
    (210, "qemu", "ivan-vm", "tenant;lease", "vmbr2", fw("", group="lease"), HOST),
    (211, "qemu", "june-vm", "lease;tenant", "vmbr2", None, HOST),
    # Ways for the hypervisor firewall NOT to be in force on a running work VM,
    # each with everything else sound: 212 has no firewall=1 on net0, 213 has
    # enable: 0 in its file, 214 has no GROUP rule, 215 has the rule switched off
    # with a pipe, 216 has no tap chain, 217 has the lease group though it is a
    # work VM. 204, 208, 209 and 211 above have no firewall file at all.
    (212, "qemu", "kim-vm", "tenant", "vmbr2", fw(""), HOST),
    (213, "qemu", "leo-vm", "tenant", "vmbr2", fw("", enable="0"), HOST),
    (214, "qemu", "mia-vm", "tenant", "vmbr2", fw("", rule="# no group here"), HOST),
    (215, "qemu", "ned-vm", "tenant", "vmbr2", fw("", rule="|GROUP tenant"), HOST),
    (216, "qemu", "ola-vm", "tenant", "vmbr2", fw(""), HOST),
    (217, "qemu", "pia-vm", "tenant", "vmbr2", fw("", group="lease"), HOST),
]
# net0 of 212 has no firewall=1; every other VM has it.
NO_NIC_FIREWALL = {212}
# Which taps exist on the host, with what `tc -s filter show` prints for each.
# 205 and 210 are stopped, so their taps do not exist; 100 and 206 are not tenants, so tc
# must not be asked about them.
TAPS = {
    "tap100i0": TC_ALICE,
    "tap201i0": TC_ALICE,
    "tap202i0": TC_BOB,
    "tap203i0": TC_CAROL,
    "tap204i0": TC_FRESH,
    "tap206i0": TC_ALICE,
    "tap208i0": "",
    "tap209i0": "",
    "tap211i0": TC_FRESH,
    "tap212i0": "",
    "tap213i0": "",
    "tap214i0": "",
    "tap215i0": "",
    "tap216i0": "",
    "tap217i0": "",
}
TC_ASKED = ["tap201i0", "tap202i0", "tap203i0", "tap204i0", "tap208i0", "tap209i0", "tap211i0",
            "tap212i0", "tap213i0", "tap214i0", "tap215i0", "tap216i0", "tap217i0"]
# The firewall's own chain for each tap: present for the VMs that are meant to
# have one, including the stopped ones, and absent for 216.
TAP_CHAINS = dict(("tap" + str(v) + "i0-OUT", tap_chain(v)) for v in (201, 203, 205, 210, 212, 213, 214, 215, 217))
# iptables is asked about the tap chain only for a VM whose other parts hold:
# 201 and 203 are fully in force and 216 has everything but the chain.
TAP_CHAINS_ASKED = ["tap201i0-OUT", "tap203i0-OUT", "tap216i0-OUT"]
TAP_CHAINS["tap216i0-OUT"] = None  # absent: no fixture


def guest(vmid):
    return next(g for g in GUESTS if g[0] == vmid)


TEMPLATE_VM = (9000, "qemu", "template", "tenant", "vmbr2", fw("10.10.60.90\n"), HOST)

NIC_HELP = ("# HELP fleet_pve_guest_nic_info The guest NIC behind each host-side tap or veth device, "
            "via roles/pve_guest_metrics.")


def nic_lines(guests):
    out = [NIC_HELP, "# TYPE fleet_pve_guest_nic_info gauge"]
    for vmid, gtype, name, _tags, bridge, _fw, node in sorted(guests):
        if node != HOST:
            continue
        device = ("tap" if gtype == "qemu" else "veth") + str(vmid) + "i0"
        out.append('fleet_pve_guest_nic_info{bridge="' + bridge + '",device="' + device + '",guest="'
                   + name + '",nic="net0",type="' + gtype + '",vmid="' + str(vmid) + '"} 1')
    return out


def nics_total(count):
    return ["# HELP fleet_pve_guest_nics_total Guest NICs named on this node.",
            "# TYPE fleet_pve_guest_nics_total gauge",
            "fleet_pve_guest_nics_total " + str(count),
            "# HELP fleet_pve_guests_collection_timestamp_seconds When this ran.",
            "# TYPE fleet_pve_guests_collection_timestamp_seconds gauge",
            "fleet_pve_guests_collection_timestamp_seconds <TS>"]


class Env:
    """One temporary node: fixtures on disk, the script's paths pointed at them."""

    def __init__(self, script_text, guests, chains, conntrack, taps=None, chains6=None, dc=None):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.metrics = root / "metrics"
        self.fw = root / "firewall"
        self.fixture = root / "iptables-fixtures"
        self.fixture6 = root / "ip6tables-fixtures"
        self.tc_fixture = root / "tc-fixtures"
        self.sys_net = root / "sys-net"
        for d in (self.fw, self.fixture, self.fixture6, self.tc_fixture, self.sys_net):
            d.mkdir()
        self.node = socket.gethostname().split(".")[0]
        self.conntrack_fixture = root / "conntrack-fixtures"
        self.conntrack_fixture.mkdir()

        fixture = {"/cluster/resources": [
            {"type": g[1], "vmid": g[0], "name": g[2], "node": self.node if g[6] == HOST else g[6],
             **({"template": 1} if g in (TEMPLATE_VM,) else {})}
            for g in guests]}
        for vmid, gtype, name, tags, bridge, fw, node in guests:
            net = ("virtio=BC:24:11:00:00:01,bridge=" + bridge + ("" if vmid in NO_NIC_FIREWALL else ",firewall=1")
                   if gtype == "qemu" else "name=eth0,bridge=" + bridge)
            fixture["/nodes/" + self.node + "/" + gtype + "/" + str(vmid) + "/config"] = {
                "name" if gtype == "qemu" else "hostname": name, "tags": tags, "net0": net}
            if fw is not None:
                (self.fw / (str(vmid) + ".fw")).write_text(fw)
        # The datacenter firewall option. dc=None leaves the path unanswered, as a
        # failing pvesh would; the default is the firewall on.
        if dc != "unanswered":
            fixture["/cluster/firewall/options"] = {"enable": 1} if dc is None else dc
        (root / "pvesh.json").write_text(json.dumps(fixture))
        self.pvesh_json = root / "pvesh.json"
        write_exec(root / "pvesh", FAKE_PVESH, FIXTURE=root / "pvesh.json")
        write_exec(root / "iptables", FAKE_IPTABLES, FIXTURE=self.fixture)
        for chain, text in {**TAP_CHAINS, **chains}.items():
            if text is not None:
                (self.fixture / (chain + ".txt")).write_text(text)
        write_exec(root / "ip6tables", FAKE_IPTABLES, FIXTURE=self.fixture6)
        for chain, text in (chains6 or {}).items():
            (self.fixture6 / (chain + ".txt")).write_text(text)
        # A tap exists in sysfs when it is a key. A string value is what tc
        # lists for it; a (code, message) tuple is a tc failure.
        write_exec(root / "tc", FAKE_TC, FIXTURE=self.tc_fixture)
        for tap, listing in (taps or {}).items():
            (self.sys_net / tap).mkdir()
            if isinstance(listing, tuple):
                (self.tc_fixture / (tap + ".fail")).write_text(str(listing[0]) + "\n" + listing[1])
            else:
                (self.tc_fixture / (tap + ".txt")).write_text(listing)

        # conntrack is the text the tool prints on stdout, or None for a host
        # where the binary is not installed.
        self.conntrack = root / "conntrack"
        if conntrack is not None:
            (self.conntrack_fixture / "stdout.txt").write_text(conntrack)
            write_exec(self.conntrack, FAKE_CONNTRACK, FIXTURE=self.conntrack_fixture)

        self.mod = load_script(script_text, root)
        self.mod.OUT_DIR = str(self.metrics)
        self.mod.PVESH = str(root / "pvesh")
        self.mod.IPTABLES = str(root / "iptables")
        self.mod.IP6TABLES = str(root / "ip6tables")
        self.mod.TC = str(root / "tc")
        self.mod.SYS_NET = str(self.sys_net)
        self.mod.FW_DIR = str(self.fw)
        self.mod.CONNTRACK = str(self.conntrack)

        self.probe = Probe(subprocess, str(self.conntrack))
        self.mod.subprocess = self.probe

    @property
    def prom(self):
        return self.metrics / self.mod.OUT_FILE

    def run(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.mod.main()
        return rc, err.getvalue()

    def lines(self):
        text = self.prom.read_text()
        return re.sub(r"(collection_timestamp_seconds) \d+$", r"\1 <TS>", text, flags=re.M).splitlines()

    def conntrack_calls(self):
        log = self.conntrack_fixture / "calls.log"
        return len(log.read_text().split()) if log.exists() else 0

    def tc_calls(self):
        log = self.tc_fixture / "calls.log"
        return log.read_text().split() if log.exists() else []

    def iptables_calls(self):
        log = self.fixture / "calls.log"
        return log.read_text().split() if log.exists() else []

    def ip6tables_calls(self):
        log = self.fixture6 / "calls.log"
        return log.read_text().split() if log.exists() else []

    def pvesh_calls(self):
        log = pathlib.Path(str(self.pvesh_json) + ".calls")
        return log.read_text().split() if log.exists() else []

    def close(self):
        self.tmp.cleanup()


def expect_lines(actual, expected, what):
    if actual == expected:
        return
    fail(what + " differs from the expected exposition text:")
    for i in range(max(len(actual), len(expected))):
        a = actual[i] if i < len(actual) else "<missing>"
        e = expected[i] if i < len(expected) else "<missing>"
        if a != e:
            fail("  line " + str(i + 1) + "\n    got:      " + a + "\n    expected: " + e)


def test_tenant_vms(script_text):
    guests = GUESTS + [TEMPLATE_VM]
    env = Env(script_text, guests,
              {"GROUP-tenant-IN": TENANT_IN, "GROUP-tenant-OUT": TENANT_OUT, "GROUP-lease-OUT": LEASE_OUT},
              CONNTRACK_TABLE, TAPS,
              chains6={"GROUP-tenant-OUT": TENANT_OUT6, "GROUP-lease-IN": LEASE_IN6})
    try:
        rc, err = env.run()
        check(rc == 0, "tenant-VM run exited " + str(rc) + ": " + err)
        if rc != 0:
            return
        expected = nic_lines(guests) + [
            "# HELP fleet_pve_tenant_conntrack_entries Conntrack entries whose original source is the "
            "address of a tenant VM (segment work or lease), via roles/pve_guest_metrics.",
            "# TYPE fleet_pve_tenant_conntrack_entries gauge",
            'fleet_pve_tenant_conntrack_entries{guest="alice-vm",ip="10.10.60.21",segment="work",vmid="201"} 4',
            'fleet_pve_tenant_conntrack_entries{guest="bob-vm",ip="10.10.60.22",segment="lease",vmid="202"} 2',
            'fleet_pve_tenant_conntrack_entries{guest="carol-vm",ip="10.10.60.23",segment="lease",vmid="203"} 0',
            "# HELP fleet_pve_tenant_policed_packets_total Packets dropped by the packet-rate limit on the "
            "tap device of a tenant VM (segment work or lease), via roles/pve_guest_metrics.",
            "# TYPE fleet_pve_tenant_policed_packets_total counter",
            'fleet_pve_tenant_policed_packets_total{guest="alice-vm",segment="work",vmid="201"} 1234',
            'fleet_pve_tenant_policed_packets_total{guest="carol-vm",segment="lease",vmid="203"} 77',
            'fleet_pve_tenant_policed_packets_total{guest="dave-vm",segment="work",vmid="204"} 0',
            'fleet_pve_tenant_policed_packets_total{guest="june-vm",segment="lease",vmid="211"} 0',
            "# HELP fleet_pve_tenant_packet_limit_active 1 when the packet-rate limit is on the tap device "
            "of a running lease VM, 0 when it has been lost, via roles/pve_guest_metrics.",
            "# TYPE fleet_pve_tenant_packet_limit_active gauge",
            'fleet_pve_tenant_packet_limit_active{guest="bob-vm",segment="lease",vmid="202"} 0',
            'fleet_pve_tenant_packet_limit_active{guest="carol-vm",segment="lease",vmid="203"} 1',
            'fleet_pve_tenant_packet_limit_active{guest="hana-vm",segment="lease",vmid="209"} 0',
            'fleet_pve_tenant_packet_limit_active{guest="june-vm",segment="lease",vmid="211"} 1',
            "# HELP fleet_pve_tenant_firewall_active 1 when the hypervisor firewall is fully in force for a "
            "running tenant VM (segment work or lease), 0 when any part is missing, "
            "via roles/pve_guest_metrics.",
            "# TYPE fleet_pve_tenant_firewall_active gauge",
            'fleet_pve_tenant_firewall_active{guest="alice-vm",segment="work",vmid="201"} 1',
            'fleet_pve_tenant_firewall_active{guest="bob-vm",segment="lease",vmid="202"} 0',
            'fleet_pve_tenant_firewall_active{guest="carol-vm",segment="lease",vmid="203"} 1',
            'fleet_pve_tenant_firewall_active{guest="dave-vm",segment="work",vmid="204"} 0',
            'fleet_pve_tenant_firewall_active{guest="gina-vm",segment="work",vmid="208"} 0',
            'fleet_pve_tenant_firewall_active{guest="hana-vm",segment="lease",vmid="209"} 0',
            'fleet_pve_tenant_firewall_active{guest="june-vm",segment="lease",vmid="211"} 0',
            'fleet_pve_tenant_firewall_active{guest="kim-vm",segment="work",vmid="212"} 0',
            'fleet_pve_tenant_firewall_active{guest="leo-vm",segment="work",vmid="213"} 0',
            'fleet_pve_tenant_firewall_active{guest="mia-vm",segment="work",vmid="214"} 0',
            'fleet_pve_tenant_firewall_active{guest="ned-vm",segment="work",vmid="215"} 0',
            'fleet_pve_tenant_firewall_active{guest="ola-vm",segment="work",vmid="216"} 0',
            'fleet_pve_tenant_firewall_active{guest="pia-vm",segment="work",vmid="217"} 0',
            "# HELP fleet_pve_firewall_dropped_packets_total Packets dropped by the tenant and lease "
            "firewall groups, by address family, via roles/pve_guest_metrics.",
            "# TYPE fleet_pve_firewall_dropped_packets_total counter",
            'fleet_pve_firewall_dropped_packets_total{chain="GROUP-lease-IN",family="ipv6",rule="private_in"} 9',
            'fleet_pve_firewall_dropped_packets_total{chain="GROUP-lease-OUT",family="ipv4",rule="private"} 0',
            'fleet_pve_firewall_dropped_packets_total{chain="GROUP-lease-OUT",family="ipv4",rule="smtp"} 6',
            'fleet_pve_firewall_dropped_packets_total{chain="GROUP-tenant-IN",family="ipv4",rule="private_in"} 7',
            'fleet_pve_firewall_dropped_packets_total{chain="GROUP-tenant-OUT",family="ipv4",rule="private"} 1234567',
            'fleet_pve_firewall_dropped_packets_total{chain="GROUP-tenant-OUT",family="ipv4",rule="smtp"} 3',
            'fleet_pve_firewall_dropped_packets_total{chain="GROUP-tenant-OUT",family="ipv6",rule="private"} 55',
            'fleet_pve_firewall_dropped_packets_total{chain="GROUP-tenant-OUT",family="ipv6",rule="smtp"} 2',
        ] + nics_total(len([g for g in guests if g[6] == HOST and g != TEMPLATE_VM]))
        # The template is skipped before the NIC lines too, so it is not in them.
        expected = [l for l in expected if "vmid=\"9000\"" not in l]
        expect_lines(env.lines(), expected, "tenant-VM exposition")

        text = "\n".join(env.lines())
        check('chain="GROUP-lease-IN",family="ipv4"' not in text, "the chain GROUP-lease-IN, absent in ipv4, produced ipv4 output")
        check('chain="GROUP-tenant-IN",family="ipv6"' not in text, "the chain GROUP-tenant-IN, absent in ipv6, produced ipv6 output")
        check('chain="GROUP-lease-OUT",family="ipv6"' not in text, "the chain GROUP-lease-OUT, absent in ipv6, produced ipv6 output")
        for vmid in ("100", "112", "204", "205", "206", "207"):
            check('vmid="' + vmid + '"} ' not in "".join(
                l for l in env.lines() if l.startswith("fleet_pve_tenant_conntrack_entries")),
                "VM " + vmid + " has a conntrack series, but it is not a tenant VM with an address")
        check(sorted(env.iptables_calls()) == sorted(list(GROUP_CHAINS) + TAP_CHAINS_ASKED),
              "iptables was asked about " + str(sorted(env.iptables_calls()))
              + ", not each group chain once and the tap chain of each VM whose other parts hold")
        check(sorted(env.ip6tables_calls()) == sorted(GROUP_CHAINS),
              "ip6tables was asked about " + str(sorted(env.ip6tables_calls())) + ", not each group chain once")
        check(env.pvesh_calls().count("/cluster/firewall/options") == 1,
              "the datacenter firewall option was read " + str(env.pvesh_calls().count("/cluster/firewall/options"))
              + " times, not once")
        firewall = [l for l in env.lines() if l.startswith("fleet_pve_tenant_firewall_active{")]
        for vmid in ("100", "112", "205", "206", "207", "210"):
            check(not any('vmid="' + vmid + '"' in l for l in firewall),
                  "VM " + vmid + " has a firewall gauge, but it is stopped or not a tenant VM")
        check(sorted(env.tc_calls()) == TC_ASKED,
              "tc was asked about " + str(sorted(env.tc_calls())) + ", not just the running tenant VMs " + str(TC_ASKED))
        policed = [l for l in env.lines() if l.startswith("fleet_pve_tenant_policed_packets_total")]
        for vmid in ("100", "112", "202", "205", "206", "207", "208", "209", "210"):
            check(not any('vmid="' + vmid + '"' in l for l in policed),
                  "VM " + vmid + " has a policed-packets series, but it is stopped, not a tenant or has no pref-5 police")
        active = [l for l in env.lines() if l.startswith("fleet_pve_tenant_packet_limit_active{")]
        for vmid in ("100", "112", "201", "204", "205", "206", "207", "208", "210"):
            check(not any('vmid="' + vmid + '"' in l for l in active),
                  "VM " + vmid + " has a packet-limit gauge, but it is a work VM, stopped or not a tenant")
        check(env.conntrack_calls() == 1,
              "conntrack -L was run " + str(env.conntrack_calls()) + " times for three tenants, not once")
        check(env.probe.bulk_reads == 0, "the conntrack output was read in bulk, not streamed")
        mode = stat.S_IMODE(env.prom.stat().st_mode)
        check(mode == 0o644, "metrics file mode is " + oct(mode) + ", not 0o644")
        check(not [p for p in env.metrics.iterdir() if p.name != env.mod.OUT_FILE],
              "the atomic write left a temporary file behind")
    finally:
        env.close()


def test_quiet_node(script_text):
    guests = [(100, "qemu", "devbox", "work", "vmbr0", fw("10.10.60.99\n"), HOST)]
    env = Env(script_text, guests, {}, "")  # no group chains; conntrack is installed but must not be run
    try:
        rc, err = env.run()
        check(rc == 0, "quiet node exited " + str(rc) + ": " + err)
        if rc != 0:
            return
        expect_lines(env.lines(), nic_lines(guests) + nics_total(1), "quiet-node exposition")
        text = "\n".join(env.lines())
        check("fleet_pve_tenant_conntrack_entries" not in text, "a node with no tenant VMs wrote conntrack lines")
        check("fleet_pve_firewall_dropped_packets_total" not in text,
              "a node with no group chains wrote dropped-packet lines (HELP and TYPE included)")
        check("fleet_pve_tenant_policed_packets_total" not in text, "a node with no tenant VMs wrote policed lines")
        check("fleet_pve_tenant_packet_limit_active" not in text, "a node with no tenant VMs wrote packet-limit lines")
        check(env.tc_calls() == [], "tc was asked about a tap although no VM is a tenant")
        check(env.conntrack_calls() == 0, "conntrack -L was run although no VM has an address to count")
        check("fleet_pve_tenant_firewall_active" not in text, "a node with no tenant VMs wrote firewall-in-force lines")
        check("/cluster/firewall/options" not in env.pvesh_calls(),
              "the datacenter firewall option was read although no tenant VM is running")
        check(sorted(env.iptables_calls()) == sorted(GROUP_CHAINS), "iptables was not asked about every group chain")
        check(sorted(env.ip6tables_calls()) == sorted(GROUP_CHAINS), "ip6tables was not asked about every group chain")
    finally:
        env.close()


def test_conntrack_reading(script_text):
    guests = [GUESTS[2]]  # alice, 10.10.60.21
    entry = ct_line("tcp", "10.10.60.21", "1.1.1.1", "1.1.1.1", "192.168.1.5")

    def count_for(table):
        env = Env(script_text, guests, {}, table)
        try:
            rc, err = env.run()
            if rc != 0:
                return None, err, env
            line = [l for l in env.lines() if l.startswith("fleet_pve_tenant_conntrack_entries{")]
            return (line[0].rsplit(" ", 1)[1] if line else None), err, env
        finally:
            env.close()

    # A valid empty table: exit 0 and nothing on stdout is zero, not a failure.
    value, err, _ = count_for("")
    check(value == "0", "an empty conntrack table gave " + str(value) + ", not 0: " + err)

    # Two entries around the summary line and a blank, both on stdout.
    noise = (entry + "conntrack v1.4.7 (conntrack-tools): 2 flow entries have been shown.\n" + "\n" + entry)
    value, err, _ = count_for(noise)
    check(value == "2", "a summary line on stdout changed the count to " + str(value) + ", not 2: " + err)

    # The reply direction is not the VM: 10.10.60.21 only as the second src=.
    inbound = ct_line("tcp", "10.10.60.1", "10.10.60.21", "10.10.60.21", "10.10.60.1")
    value, err, _ = count_for(inbound * 3 + entry)
    check(value == "1", "reply-direction sources were counted: " + str(value) + ", not 1: " + err)

    # Streaming: tens of thousands of lines go through iteration, none through a bulk read.
    other = ct_line("tcp", "10.10.70.9", "1.1.1.1", "1.1.1.1", "192.168.1.5")
    big = other * 50000 + entry * 7
    env = Env(script_text, guests, {}, big)
    try:
        rc, err = env.run()
        check(rc == 0, "a large conntrack table failed the run: " + err)
        check(env.probe.bulk_reads == 0, "the conntrack output was read in bulk, not streamed")
        check(env.probe.lines == 50007, "the script iterated " + str(env.probe.lines) + " of 50007 conntrack lines")
        if rc == 0:
            check(any(l.endswith('vmid="201"} 7') for l in env.lines()), "the large table did not count alice's 7")
    finally:
        env.close()


def test_datacenter_firewall(script_text):
    """Every VM is sound, so the datacenter option alone decides: 1 only when
    it says enable 1. A cluster that never set it answers with nothing."""
    guests = [guest(201), guest(203)]  # alice (work, group tenant) and carol (lease, group lease)
    taps = {"tap201i0": "", "tap203i0": ""}
    for dc, want in (({"enable": 1}, "1"), ({"enable": "1"}, "1"), ({"enable": 0}, "0"), ({}, "0"),
                     ({"policy_in": "DROP"}, "0")):
        env = Env(script_text, guests, {}, CONNTRACK_TABLE, taps, dc=dc)
        try:
            rc, err = env.run()
            check(rc == 0, "datacenter options " + json.dumps(dc) + " failed the run: " + err)
            got = [l.rsplit(" ", 1)[1] for l in env.lines() if l.startswith("fleet_pve_tenant_firewall_active{")]
            check(got == [want, want], "datacenter options " + json.dumps(dc) + " gave " + str(got) + ", not " + want + " for both VMs")
            check(env.pvesh_calls().count("/cluster/firewall/options") == 1, "the datacenter option was not read exactly once")
        finally:
            env.close()


def test_firewall_file_parsing(script_text):
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_script(script_text, tmp)

    def text(options="enable: 1", rules="GROUP tenant"):
        return "[OPTIONS]\n" + options + "\n\n[RULES]\n" + rules + "\n"

    check(mod.fw_enabled(text("enable: 1")), "enable: 1 was not read as enabled")
    check(mod.fw_enabled(text("policy_in: DROP\nENABLE:   1  # on")), "a trailing comment or odd spacing broke enable")
    check(not mod.fw_enabled(text("enable: 0")), "enable: 0 was read as enabled")
    check(not mod.fw_enabled(text("policy_in: DROP")), "a file with no enable line was read as enabled")
    check(not mod.fw_enabled(text("# enable: 1")), "a commented-out enable line was read as enabled")
    check(not mod.fw_enabled("[RULES]\nenable: 1\n"), "enable: 1 outside [OPTIONS] was read as enabled")
    check(not mod.fw_enabled(text("enable: 10")), "enable: 10 was read as enabled")

    check(mod.fw_has_group(text(), "tenant"), "GROUP tenant was not found")
    check(mod.fw_has_group(text(rules="GROUP tenant -i net0"), "tenant"), "GROUP tenant with options was not found")
    check(mod.fw_has_group(text(rules="IN ACCEPT -p tcp\nGROUP lease"), "lease"), "GROUP lease after another rule was not found")
    check(not mod.fw_has_group(text(rules="|GROUP tenant"), "tenant"), "a rule switched off with a pipe was found")
    check(not mod.fw_has_group(text(rules="GROUP tenants"), "tenant"), "GROUP tenants matched the group tenant")
    check(not mod.fw_has_group(text(rules="GROUP tenant"), "lease"), "GROUP tenant matched the group lease")
    check(not mod.fw_has_group(text(rules="# GROUP tenant"), "tenant"), "a commented-out GROUP rule was found")
    check(not mod.fw_has_group("[OPTIONS]\nGROUP tenant\n", "tenant"), "GROUP tenant outside [RULES] was found")

    check(mod.nic_has_firewall("virtio=BC:24:11:00:00:01,bridge=vmbr2,firewall=1"), "firewall=1 was not read")
    check(not mod.nic_has_firewall("virtio=BC:24:11:00:00:01,bridge=vmbr2,firewall=0"), "firewall=0 was read as on")
    check(not mod.nic_has_firewall("virtio=BC:24:11:00:00:01,bridge=vmbr2"), "a net0 without firewall was read as on")
    check(not mod.nic_has_firewall("virtio=BC:24:11:00:00:01,bridge=vmbr2,firewall=10"), "firewall=10 was read as on")
    check(not mod.nic_has_firewall(None), "a missing net0 was read as on")


def test_vanished_tap(script_text):
    """The VM stopped between the sysfs check and tc: silence, not a failure,
    and for a lease VM no packet-limit gauge either, since it is not running."""
    guests = [guest(201), guest(203)]  # alice (work) and carol (lease)
    gone = lambda tap: (1, 'Cannot find device "' + tap + '"\n')
    env = Env(script_text, guests, {}, CONNTRACK_TABLE,
              {"tap201i0": gone("tap201i0"), "tap203i0": gone("tap203i0")})
    try:
        rc, err = env.run()
        check(rc == 0, "a tap that vanished failed the run (" + str(rc) + "): " + err)
        if rc == 0:
            text = "\n".join(env.lines())
            check("fleet_pve_tenant_policed_packets_total" not in text, "a vanished tap produced a policed-packets line")
            check("fleet_pve_tenant_packet_limit_active" not in text,
                  "a lease VM whose tap vanished produced a packet-limit gauge")
            check("fleet_pve_tenant_firewall_active" not in text,
                  "a VM whose tap vanished produced a firewall-in-force gauge")
            check(sorted(env.tc_calls()) == ["tap201i0", "tap203i0"], "tc was not asked about both running taps")
    finally:
        env.close()


def test_failures_keep_the_previous_file(script_text):
    guests = [GUESTS[2], GUESTS[3]]  # alice and bob, both tenants
    chains = {"GROUP-tenant-OUT": TENANT_OUT}
    previous = "# previous run\nfleet_pve_guests_collection_timestamp_seconds 1\n"

    def attempt(what, setup, needle, **env_kw):
        env = Env(script_text, guests, chains, CONNTRACK_TABLE, **env_kw)
        try:
            env.metrics.mkdir()
            env.prom.write_text(previous)
            setup(env)
            rc, err = env.run()
            check(rc == 1, what + ": exit status " + str(rc) + ", not 1")
            check(env.prom.read_text() == previous, what + ": the previous file was modified")
            check("keeping the previous file" in err, what + ": stderr does not say it kept the previous file: " + err)
            check(needle in err, what + ": stderr does not name the cause (" + needle + "): " + err)
        finally:
            env.close()

    attempt("iptables failing", lambda e: (e.fixture / "GROUP-tenant-IN.fail").write_text(
        "Another app is currently holding the xtables lock. Perhaps you want to use the -w option?\n"),
        "xtables lock")
    attempt("conntrack missing", lambda e: e.conntrack.unlink(), "conntrack")

    def conntrack_fails(e):
        (e.conntrack_fixture / "fail").write_text(
            "1\nconntrack v1.4.7 (conntrack-tools): Operation not permitted.\n")
    attempt("conntrack exiting nonzero", conntrack_fails, "Operation not permitted")

    def conntrack_hangs(e):
        e.mod.CONNTRACK_TIMEOUT = 1
        (e.conntrack_fixture / "hang").write_text("5")
    attempt("conntrack past its timeout", conntrack_hangs, "timed out")
    attempt("firewall file unreadable", lambda e: ((e.fw / "201.fw").unlink(), (e.fw / "201.fw").mkdir()),
            "201")
    attempt("tc failing", lambda e: None, "tap201i0",
            taps={"tap201i0": (2, "Error: Dump terminated\n")})
    attempt("ip6tables failing", lambda e: (e.fixture6 / "GROUP-tenant-IN.fail").write_text(
        "ip6tables v1.8.11 (legacy): can't initialize ip6tables table `filter'\n"), "ip6tables")
    # alice is running here, with every other part of her firewall in place, so
    # the tap chain is asked about, and the datacenter option is read.
    attempt("iptables failing on a tap chain", lambda e: (e.fixture / "tap201i0-OUT.fail").write_text(
        "Another app is currently holding the xtables lock.\n"), "tap201i0-OUT", taps={"tap201i0": ""})
    attempt("datacenter firewall option unreadable", lambda e: None, "/cluster/firewall/options",
            taps={"tap201i0": ""}, dc="unanswered")


def test_role_tasks(script_text):
    """The play must fail, before it installs anything, on a hypervisor with no
    conntrack binary, and the role must never install one itself."""
    tasks = (ROLE / "tasks" / "main.yml").read_text()
    m = re.search(r'^CONNTRACK = "([^"]+)"', script_text, re.M)
    check(m is not None, "the script has no CONNTRACK constant")
    path = m.group(1) if m else "/usr/sbin/conntrack"

    enabled = tasks.find("when: pve_guest_metrics_enabled | bool")
    disabled = tasks.find("when: not (pve_guest_metrics_enabled | bool)")
    stat_at = tasks.find("- name: Look for conntrack")
    assert_at = tasks.find("- name: Fail if conntrack is not installed")
    check(enabled != -1 and disabled != -1 and enabled < stat_at < assert_at < disabled,
          "the conntrack check is not inside the enabled block of tasks/main.yml")
    first_change = min(i for i in (tasks.find("- name: Ensure the textfile collector directory exists"),
                                   tasks.find("- name: Install the guest metrics exporter script")) if i != -1)
    check(assert_at != -1 and assert_at < first_change, "the conntrack check does not come before the first change the role makes")

    stat_task = tasks[stat_at:assert_at] if -1 not in (stat_at, assert_at) else ""
    check("ansible.builtin.stat:" in stat_task and ("path: " + path) in stat_task,
          "the conntrack stat task does not look at " + path + ", the script's CONNTRACK")
    reg = re.search(r"register: (\w+)", stat_task)
    assert_task = tasks[assert_at:disabled] if -1 not in (assert_at, disabled) else ""
    assert_task = assert_task.split("\n    - name:")[0]
    check("ansible.builtin.assert:" in assert_task, "the conntrack check is not an assert")
    check(reg is not None and ("that: " + reg.group(1) + ".stat.exists") in assert_task,
          "the assert does not test the stat result's exists")
    check(path in assert_task and "does not install it" in assert_task and "apt install conntrack" in assert_task,
          "the failure message does not name the path, say how to install it and that the role does not")
    # Host packages are not this role's to install.
    check(not re.search(r"^\s+ansible\.builtin\.(apt|package|dnf|yum|pip|apt_key|apt_repository):", tasks, re.M),
          "tasks/main.yml uses a package module; this role must not install host packages")
    check("apt-get" not in tasks and "dpkg -i" not in tasks, "tasks/main.yml shells out to a package manager")


def test_docs(script_text):
    variables = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
    # conntrack comes from the netlink tool. The kernel has no proc file, so
    # neither the code nor a page may say that is where the table is read.
    code = [l for l in script_text.splitlines() if not l.lstrip().startswith("#")]
    check(not any("/proc/net" in l for l in code), "the script reads something under /proc/net")
    check('CONNTRACK = "/usr/sbin/conntrack"' in script_text, "the script does not run /usr/sbin/conntrack")
    check("`conntrack -L`" in variables and "/usr/sbin/conntrack -L" in variables,
          "variables.md does not say the table comes from conntrack -L")
    flat = " ".join(variables.split())
    # IPv6 is counted too, told apart by a label, from the other family's set.
    check("ip6tables" in variables and "`family`" in variables and "PVEFW-0-private-v6" in variables,
          "variables.md does not explain the family label, ip6tables and the private-v6 set")
    check("/cluster/firewall/options" in variables and "`tap<vmid>i0-OUT`" in variables,
          "variables.md does not say where fleet_pve_tenant_firewall_active reads its inputs")
    check("fails the play instead when `/usr/sbin/conntrack` is missing" in flat,
          "variables.md does not say the role fails the play when /usr/sbin/conntrack is missing")
    # The tag marks the owner's own work VMs too, so no metric calls a tenant VM leased.
    helps = [l for l in script_text.splitlines() if "# HELP fleet_pve_tenant" in l or '"# HELP' in l]
    check(helps, "found no HELP lines in the script")
    check(not any("leased" in h.lower() for h in helps), "a HELP line calls a tenant VM leased: " + str([h for h in helps if "leased" in h.lower()]))
    for name in ("conntrack_entries", "policed_packets_total"):
        text = next((l for l in script_text.splitlines() if "fleet_pve_tenant_" + name in l and "HELP" in l), "")
        nxt = script_text.split(text, 1)[1].split("\n", 2)[1] if text else ""
        check("tenant VM (segment work or lease)" in (text + nxt), "the HELP for " + name + " does not say 'tenant VM (segment work or lease)'")
    for rel in ("docs/reference/variables.md", "README.md", "docs/fleet/index.md", "roles/pve_guest_metrics/defaults/main.yml"):
        text = (ANSIBLE / rel).read_text()
        check(not re.search(r"leased VMs?'?s? (connections|conntrack|address|tap)", text), rel + " calls a tenant VM leased")
    for rel in ("docs/reference/variables.md", "README.md", "docs/fleet/index.md"):
        text = (ANSIBLE / rel).read_text()
        check(not re.search(r"(read|reads|from|in)\s+`?/proc/net/nf_conntrack", text), rel + " says conntrack is read from /proc")
    emitted = set(re.findall(r"# HELP (fleet_pve_\w+)", script_text))
    check(len(emitted) >= 5, "found too few fleet_pve_* metric names in the script: " + str(sorted(emitted)))
    # Each metric needs its row in the "What it writes" table; a mention in
    # passing is not documentation. Any other metric the page names must exist.
    in_table = set(re.findall(r"^\| `(fleet_pve_\w+)` \|", variables, re.M))
    named = set(re.findall(r"`(fleet_pve_\w+)`", variables))
    check(emitted == in_table,
          "the variables.md metric table and the script disagree: only in script " + str(sorted(emitted - in_table))
          + ", only in the table " + str(sorted(in_table - emitted)))
    check(named <= emitted, "variables.md names metrics the script does not emit: " + str(sorted(named - emitted)))
    for rule in ("private", "private_in", "smtp"):
        check("`" + rule + "`" in variables, "variables.md does not explain the rule label " + rule)
    for chain in GROUP_CHAINS:
        check("`" + chain + "`" in variables, "variables.md does not name the firewall chain " + chain)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        text, _values = render_script(tmp)
        test_tenant_vms(text)
        test_quiet_node(text)
        test_conntrack_reading(text)
        test_vanished_tap(text)
        test_role_tasks(text)
        test_datacenter_firewall(text)
        test_firewall_file_parsing(text)
        test_failures_keep_the_previous_file(text)
        test_docs(text)
    if errors:
        for e in errors:
            print("FAIL: " + e)
        return 1
    print("ok: pve_guest_metrics exporter output, failure handling and docs agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
