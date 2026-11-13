#!/usr/bin/env python3
"""Test the collector footprint changes: Alloy config and memory limit, the
fact-free playbooks, the single apt simulation and the pve-guests config read.

Needs jinja2 and pyyaml (both come with ansible-core); run from anywhere:

    python3 -I ansible/tests/alloy-footprint-test.py

Checks:
  * config.alloy.j2 renders for an LXC Docker guest, a bare-metal host and a
    guest with the drops switched off: the unix collectors are disabled only
    for the `lxc` group, systemd start_time and task_metrics are off, the
    activating/deactivating states are dropped, the Alloy self-metric keep rule
    is present, cAdvisor disables disk, diskIO and cpuLoad, and Docker logs go
    through loki.process with a drop stage
  * no dashboard, alert rule or doc under monitoring/ and no grafana_alerting
    template reads anything those drops remove: collector metric prefixes, the
    systemd states, the cAdvisor and Alloy self-metric names, and node_hwmon_*
    only for hypervisor and Pi hosts
  * the memory limit: the real set_fact expression from tasks/main.yml gives
    the clamped percentage for `auto`, a fixed value verbatim and 0 for off;
    alloy.env.j2 sets GOMEMLIMIT only when the limit is above 0; the MemoryHigh
    drop-in is a percentage above it
  * every playbook imported by site.yml sets gather_facts: false, and no role
    those plays use reads an ansible_* fact; the gpu_exporter role gathers its
    own architecture
  * the update_metrics script, run with a fake apt-get, simulates the upgrade
    once and still reports both the by-origin and the security counts
  * the pve-guests script, run against a fake /etc/pve tree and a fake pvesh,
    calls pvesh once however many guests there are, reads the live section of
    each config (not snapshots or pending), and keeps the previous output when
    a guest's file is missing
  * every new role default has a row in docs/reference/variables.md
"""
import ast
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile

import jinja2
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
REPO = ANSIBLE.parent
ROLES = ANSIBLE / "roles"
errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


def load_defaults(role):
    return yaml.safe_load((ROLES / role / "defaults" / "main.yml").read_text())


def env():
    e = jinja2.Environment(keep_trailing_newline=True, undefined=jinja2.StrictUndefined)
    e.filters["to_json"] = json.dumps
    e.filters["bool"] = lambda v: str(v).lower() in ("1", "true", "yes")
    return e


def render_str(text, variables):
    """Render a defaults value the way Ansible would: until no braces remain."""
    e = env()
    for _ in range(5):
        if not isinstance(text, str) or "{{" not in text:
            return text
        text = e.from_string(text).render(**variables)
        if text.startswith("["):
            # Ansible turns a templated string that is a list literal into a list.
            text = ast.literal_eval(text)
    return text


def host_vars(group_names, **extra):
    """The role defaults resolved for a host in the given groups."""
    d = load_defaults("alloy_collector")
    v = {
        "group_names": group_names,
        "groups": {"wazuh_manager": []},
        "inventory_hostname": "testhost",
        "monitor_role": "test",
        "alloy_textfile_dir": "/var/lib/node_exporter/textfile_collector",
        "alloy_docker_effective": True,
        "collector_ingest_ca_file": "",
        "gpu_exporter_available": False,
        "alloy_gomemlimit_effective_mib": 96,
        "prometheus_remote_write_url": "https://example.invalid/api/v1/write",
        "loki_write_url": "https://example.invalid/loki/api/v1/push",
        "monitor_hostname": "testhost",
        "collector_basic_auth_user": "collector",
        "collector_basic_auth_password": "not-a-secret",
    }
    v.update(d)
    v.update(extra)
    for k in list(d):
        v[k] = render_str(v[k], v)
    return v


def render_template(name, variables):
    path = ROLES / "alloy_collector" / "templates" / name
    return env().from_string(path.read_text()).render(**variables)


# --- 1. the rendered Alloy config ------------------------------------------
lxc = render_template("config.alloy.j2", host_vars(["lxc", "lxc_debian", "monitored"]))
metal = render_template("config.alloy.j2", host_vars(["metal", "monitored"], alloy_docker_effective=False))
lxc_none = render_template(
    "config.alloy.j2",
    host_vars(["lxc", "monitored"], alloy_unix_disable_collectors=[], alloy_unix_drop_metrics_regex=""),
)

for name, text in (("lxc", lxc), ("metal", metal), ("lxc-none", lxc_none)):
    for tag in ("{{", "{%", "{#"):
        check(tag not in text, name + ": rendered config still contains " + tag)
    check("start_time      = false" in text, name + ": systemd start_time must be off")
    check("task_metrics    = false" in text, name + ": systemd task_metrics must be off")
    check("node_systemd_unit_state;(activating|deactivating)" in text,
          name + ": activating/deactivating states are not dropped")
    check("forward_to      = [prometheus.relabel.host_unix.receiver]" in text,
          name + ": the host scrape does not go through the host_unix relabel")
    check("prometheus_remote_storage_.+" in text and "action        = \"keep\"" in text,
          name + ": the Alloy self-metric keep rule is missing")
    check('regex         = ".+_bucket"' in text, name + ": histogram buckets are not dropped")

m = re.search(r"disable_collectors = \[(.*?)\]", lxc)
check(bool(m), "lxc: no disable_collectors line")
if m:
    got = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    want = {"thermal_zone", "hwmon", "cpufreq", "zfs", "softnet", "schedstat"}
    check(want <= got, "lxc: missing collectors " + str(sorted(want - got)))
    check("cpu" not in got and "meminfo" not in got and "filesystem" not in got and "netdev" not in got,
          "lxc: disabled a collector the dashboards need")
check("node_cpu_(core|package)_throttles_total" in lxc, "lxc: thermal-throttle counters not dropped")
check("disable_collectors" not in metal, "metal: must keep every collector")
check("throttles_total" not in metal, "metal: must not drop the throttle counters")
check("disable_collectors" not in lxc_none, "lxc with an empty override still disables collectors")

for name, text in (("lxc", lxc),):
    check('disabled_metrics = ["percpu"' in text and '"disk"' in text and '"diskIO"' in text
          and '"cpuLoad"' in text, name + ": cAdvisor disabled_metrics missing disk, diskIO or cpuLoad")
    check("forward_to      = [prometheus.relabel.cadvisor.receiver]" in text,
          name + ": cAdvisor scrape does not go through its relabel")
    check("forward_to = [loki.process.docker.receiver]" in text.replace("  forward_to  ", "  forward_to "),
          name + ": Docker logs do not go through loki.process")
    check('older_than          = "160h"' in text, name + ": no older_than drop stage for Docker logs")
check("loki.process" not in metal, "metal (no Docker): a docker log pipeline was rendered")
no_age = render_template("config.alloy.j2", host_vars(["lxc"], alloy_loki_max_entry_age=""))
check("stage.drop" not in no_age, "empty alloy_loki_max_entry_age still renders a drop stage")


# --- 2. nothing the drops remove is read anywhere ---------------------------
def monitoring_sources():
    files = []
    for base in (REPO / "monitoring", ROLES / "grafana_alerting", ROLES / "grafana_dashboards"):
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in (".json", ".yaml", ".yml", ".j2", ".md"):
                files.append(p)
    return files


SOURCES = [(p, p.read_text(errors="replace")) for p in monitoring_sources()]
# Doc pages describe metrics in prose; only machine-read files count as readers.
READERS = [(p, t) for p, t in SOURCES if p.suffix != ".md"]

PREFIX = {
    "zfs": r"node_zfs_", "softnet": r"node_softnet_", "schedstat": r"node_schedstat_",
    "cpufreq": r"node_cpu_(frequency|scaling)_", "thermal_zone": r"node_thermal_zone_",
    "edac": r"node_edac_", "xfs": r"node_xfs_", "nvme": r"node_nvme_",
    "powersupplyclass": r"node_power_supply_", "rapl": r"node_rapl_",
}
d = load_defaults("alloy_collector")
for coll in d["alloy_lxc_disabled_collectors"]:
    if coll in PREFIX:
        for p, t in READERS:
            check(not re.search(PREFIX[coll], t), "%s reads %s metrics but the lxc default disables %s"
                  % (p.relative_to(REPO), PREFIX[coll], coll))
    check(coll in PREFIX or coll in ("hwmon", "diskstats"), "lxc collector %s has no read check" % coll)

ROLE_OK = re.compile(r'role(=|=~)\\?"[^"]*(hypervisor|pi)')
for p, t in READERS:
    # node_hwmon_* only for hypervisor and Pi hosts (an LXC guest no longer has it).
    # Comment lines and YAML descriptions are not queries.
    for line in t.splitlines():
        if "node_hwmon_" in line and ("expr" in line or "count by" in line or "max by" in line):
            check(p.name == "t7920.json" or ROLE_OK.search(line.replace('\\"', '"')) is not None,
                  "%s reads node_hwmon_* without a hypervisor/pi role: %s" % (p.relative_to(REPO), line.strip()[:120]))
    if "node_disk_" in t:
        check(p.name == "t7920.json", "%s reads node_disk_*, only the hypervisor dashboard may"
              % p.relative_to(REPO))

# systemd states
for p, t in READERS:
    check(not re.search(r"node_systemd_unit_state\{[^}]*(activating|deactivating)", t),
          "%s queries a dropped systemd state" % p.relative_to(REPO))
    check("node_systemd_unit_start_time_seconds" not in t and "node_systemd_unit_tasks" not in t,
          "%s reads a systemd metric the collector no longer emits" % p.relative_to(REPO))

# cAdvisor names
drop = re.compile(d["alloy_cadvisor_drop_metrics_regex"].strip())
for p, t in READERS:
    for name in set(re.findall(r"\bcontainer_[a-z_]+", t)):
        check(not drop.fullmatch(name), "%s reads %s, which the cAdvisor drop removes"
              % (p.relative_to(REPO), name))
# Alloy self metrics
keep = re.compile(d["alloy_self_metrics_keep_regex"].strip())
for p, t in SOURCES:
    for name in set(re.findall(r"\b(?:loki_write|prometheus_remote_storage)_[a-z_]+", t)):
        check(keep.fullmatch(name) or name.endswith("_"), "%s names %s, which the self-metric keep rule drops"
              % (p.relative_to(REPO), name))
check(keep.fullmatch("prometheus_remote_storage_samples_failed_total") is not None,
      "keep rule drops the remote-write health metric")
check(keep.fullmatch("go_gc_gomemlimit_bytes") is not None, "keep rule drops go_gc_gomemlimit_bytes")
check(keep.fullmatch("go_memstats_heap_inuse_bytes") is not None, "keep rule drops heap in use")
check(keep.fullmatch("prometheus_target_interval_length_seconds_sum") is None, "keep rule keeps an unused family")

# --- 3. the memory limit ------------------------------------------------------
tasks = yaml.safe_load((ROLES / "alloy_collector" / "tasks" / "main.yml").read_text())
expr = next(t for t in tasks if t.get("name") == "Resolve the Alloy memory limit")["ansible.builtin.set_fact"][
    "alloy_gomemlimit_effective_mib"]
base = load_defaults("alloy_collector")


def limit(memtotal, **over):
    v = dict(base)
    v.update(over)
    v["alloy_memtotal"] = type("R", (), {"stdout": str(memtotal)})()
    # `alloy_memtotal.stdout` is an attribute lookup on a dict in Ansible
    v["alloy_memtotal"] = {"stdout": str(memtotal)}
    return int(env().from_string(expr).render(**v).strip())


for mem, want in ((256, 96), (512, 96), (1024, 122), (2048, 245), (7400, 256), (251000, 256)):
    check(limit(mem) == want, "auto limit for %d MiB: got %s want %d" % (mem, limit(mem), want))
check(limit(2048, alloy_gomemlimit_mib=160) == 160, "a fixed limit is not used verbatim")
check(limit(2048, alloy_gomemlimit_mib=0) == 0, "0 does not turn the limit off")

for mib, present in ((96, True), (0, False)):
    e = render_template("alloy.env.j2", host_vars(["lxc"], alloy_gomemlimit_effective_mib=mib))
    check(("GOMEMLIMIT=" in e) == present, "GOMEMLIMIT presence wrong for limit %d" % mib)
check('GOMEMLIMIT="96MiB"' in render_template("alloy.env.j2", host_vars(["lxc"])), "GOMEMLIMIT value format")

dropin = next(t for t in tasks if t.get("name") == "Cap Alloy memory softly with MemoryHigh")
content = env().from_string(dropin["ansible.builtin.copy"]["content"]).render(
    **dict(base, alloy_gomemlimit_effective_mib=96, alloy_env_path="/etc/default/alloy"))
check("MemoryHigh=144M" in content, "MemoryHigh is not 150% of a 96 MiB limit")
check("MemoryMax" not in json.dumps(tasks) and "MemoryMax" not in content, "MemoryMax must not be set")
check(dropin.get("notify") == "Restart alloy", "the memory drop-in does not restart alloy")

# --- 4. fact-free playbooks ------------------------------------------------
site = yaml.safe_load((ANSIBLE / "playbooks" / "site.yml").read_text())
imported = [x["ansible.builtin.import_playbook"] for x in site]
FACT = re.compile(r"\bansible_(?!check_mode\b|host\b|user\b|port\b|connection\b|python_interpreter\b|ssh_)\w+")
for pb in imported:
    for play in yaml.safe_load((ANSIBLE / "playbooks" / pb).read_text()):
        check(play.get("gather_facts") is False, "%s: play %r still gathers facts" % (pb, play.get("name")))
        for r in play.get("roles", []):
            rname = r if isinstance(r, str) else r.get("role")
            for f in (ROLES / rname).rglob("*"):
                if f.is_file() and f.suffix in (".yml", ".yaml", ".j2", ".sh", ""):
                    for hit in FACT.findall(f.read_text(errors="replace")):
                        if f.parts[-3:-1] == ("gpu_exporter", "tasks"):
                            continue
                        check(False, "%s (used by a fact-free play) reads %s" % (f.relative_to(ANSIBLE), hit))
gpu = (ROLES / "gpu_exporter" / "tasks" / "main.yml").read_text()
check(gpu.index("ansible.builtin.setup") < gpu.index("ansible_architecture"),
      "gpu_exporter reads ansible_architecture before running setup")


# --- 5. update_metrics: one simulation -------------------------------------
def run_script(rendered, tmp, extra_path_files):
    bindir = pathlib.Path(tmp) / "bin"
    bindir.mkdir()
    for name, body in extra_path_files.items():
        f = bindir / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return bindir


um = load_defaults("update_metrics")
um["alloy_textfile_dir"] = "/unused"
with tempfile.TemporaryDirectory() as tmp:
    um["update_metrics_dir"] = tmp
    um["update_metrics_use_needrestart"] = False
    um["group_names"] = ["autoupdate"]
    script = env().from_string((ROLES / "update_metrics" / "templates" / "fleet-update-metrics.sh.j2").read_text()
                               ).render(**um)
    sp = pathlib.Path(tmp) / "run.sh"
    sp.write_text(script)
    calls = pathlib.Path(tmp) / "calls"
    bindir = run_script(script, tmp, {
        "apt-get": 'echo x >> "%s"\n'
                   'echo "Inst libc6 [2.36] (2.37 Debian-Security:12/stable-security [amd64])"\n'
                   'echo "Inst vim [9.0] (9.1 Debian:12/stable [amd64])"\n'
                   'echo "Inst curl [7] (8 Debian-Security:12/stable-security [amd64])"\n' % calls,
    })
    r = subprocess.run(["bash", str(sp)], env={"PATH": str(bindir) + ":/usr/bin:/bin", "HOME": tmp},
                       capture_output=True, text=True)
    check(r.returncode == 0, "update-metrics script failed: " + r.stderr[:200])
    check(calls.read_text().count("x") == 1 if calls.exists() else False,
          "update-metrics must run apt-get --just-print exactly once")
    out = (pathlib.Path(tmp) / um["update_metrics_file"]).read_text()
    # The origin label is the last word inside the parentheses, as before this
    # change; the point here is that the three lines are all counted.
    check('apt_upgrades_pending{origin="[amd64]"} 3' in out, "by-origin count wrong: " + out)
    check("apt_upgrades_security_pending 2" in out, "security count wrong")
    # apt failing: both series must still be written, as zero
    (bindir / "apt-get").write_text("#!/bin/sh\nexit 100\n")
    r = subprocess.run(["bash", str(sp)], env={"PATH": str(bindir) + ":/usr/bin:/bin", "HOME": tmp},
                       capture_output=True, text=True)
    out = (pathlib.Path(tmp) / um["update_metrics_file"]).read_text()
    check('apt_upgrades_pending{origin=""} 0' in out and "apt_upgrades_security_pending 0" in out,
          "a failing apt-get must report zero pending")

# --- 6. pve-guests: one pvesh call ------------------------------------------
pve = load_defaults("pve_guest_metrics")
with tempfile.TemporaryDirectory() as tmp:
    tmpd = pathlib.Path(tmp)
    pve["alloy_textfile_dir"] = str(tmpd / "out")
    pve = {k: render_str(v, pve) for k, v in pve.items()}
    src = env().from_string((ROLES / "pve_guest_metrics" / "templates" / "fleet-pve-guests.j2").read_text()
                            ).render(**pve)
    for tag in ("{{", "{%"):
        check(tag not in src, "pve script still contains " + tag)
    sp = tmpd / "pve.py"
    sp.write_text(src)
    calls = tmpd / "pvesh-calls"
    guests = [
        {"type": "qemu", "vmid": 100, "node": "n1", "name": "fw"},
        {"type": "lxc", "vmid": 101, "node": "n1", "name": "ct"},
        {"type": "qemu", "vmid": 102, "node": "n2", "name": "elsewhere"},
        {"type": "qemu", "vmid": 103, "node": "n1", "name": "tmpl", "template": 1},
    ]
    fake = tmpd / "pvesh"
    # Any call but the guest list gets an empty config, so a per-guest call shows
    # up as a wrong count and missing NICs rather than as a crash.
    fake.write_text("#!/bin/sh\necho x >> '%s'\ncase \"$2\" in /cluster/resources) cat <<'EOF'\n%s\nEOF\n;; *) echo '{}';; esac\n"
                    % (calls, json.dumps(guests)))
    fake.chmod(0o755)
    nodes = tmpd / "pve" / "n1"
    (nodes / "qemu-server").mkdir(parents=True)
    (nodes / "lxc").mkdir()
    (nodes / "qemu-server" / "100.conf").write_text(
        "#a comment\nname: OPNsense\nnet0: virtio=AA:BB,bridge=vmbr0,firewall=1\nnet1: virtio=CC:DD,bridge=vmbr1\n\n"
        "[snap1]\nname: old\nnet5: virtio=EE:FF,bridge=vmbrOLD\n\n[PENDING]\nnet0: virtio=AA:BB,bridge=vmbrPENDING\n")
    (nodes / "lxc" / "101.conf").write_text("hostname: grafana\nnet0: name=eth0,bridge=vmbr0,hwaddr=11:22\n")

    ld = importlib.machinery.SourceFileLoader("pveguests", str(sp))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("pveguests", ld))
    ld.exec_module(mod)
    mod.PVESH = str(fake)
    mod.PVE_NODES_DIR = str(tmpd / "pve")
    mod.socket.gethostname = lambda: "n1.example"
    check(mod.main() == 0, "pve script failed on good fixtures")
    out = (tmpd / "out" / pve["pve_guest_metrics_file"]).read_text()
    check(calls.read_text().count("x") == 1, "pvesh must be called once, got %d" % calls.read_text().count("x"))
    check('device="tap100i0"' in out and 'guest="OPNsense"' in out and 'bridge="vmbr0"' in out, "qemu nic missing")
    check('device="tap100i1"' in out and 'bridge="vmbr1"' in out, "second qemu nic missing")
    check('device="veth101i0"' in out and 'guest="grafana"' in out, "lxc nic missing")
    check("vmbrOLD" not in out and "vmbrPENDING" not in out and "tap100i5" not in out,
          "a snapshot or pending section leaked into the output")
    check("elsewhere" not in out and "tmpl" not in out, "other-node or template guest was emitted")
    check("fleet_pve_guest_nics_total 3" in out, "nic total wrong")
    # a missing config file keeps the previous output and fails the run
    before = out
    (nodes / "lxc" / "101.conf").unlink()
    check(mod.main() == 1, "a missing guest config must fail the run")
    check((tmpd / "out" / pve["pve_guest_metrics_file"]).read_text() == before, "previous output was not kept")

# --- 7. docs name every new default -----------------------------------------
variables_md = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
for name in d:
    if name.startswith(("alloy_gomemlimit", "alloy_memory_high", "alloy_lxc_", "alloy_unix_", "alloy_self_",
                        "alloy_cadvisor_", "alloy_loki_")):
        check("`" + name + "`" in variables_md, name + " has no row in docs/reference/variables.md")

if errors:
    print("FAILED: %d problem(s)" % len(errors))
    for e in errors:
        print("  - " + e)
    sys.exit(1)
print("ok")
