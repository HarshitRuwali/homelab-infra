#!/usr/bin/env python3
"""Test roles/dell_fan_boost: its control logic, its script against a fake
sysfs tree, and the docs that describe it.

Standard library only; run from anywhere:

    python3 -I ansible/tests/dell-fan-boost-test.py

Checks:
  * the script template renders from defaults/main.yml with no Jinja left over,
    every variable it uses has a default, and the result compiles
  * Controller: boosts at the on threshold and not below, holds for the minimum
    time, releases at the off threshold and not above it, refuses or ends a
    boost while another package is at the guard, keeps the guard until it
    has dropped GUARD_RELEASE_C, and never boosts on an unreadable package
  * the real script, run against a fake hwmon tree: clears a leftover manual
    mode at start, boosts the configured channel, hands every channel back to
    automatic on release and on SIGTERM, writes its metrics, and --restore
    puts every channel back on automatic
  * the unit runs --restore in ExecStopPost and the script accepts it
  * every default has a row in docs/reference/variables.md, and the reverse;
    the playbook, role and metrics are named where the docs list them; the
    playbook is in site.yml and t7920 enables the role
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time

ANSIBLE = pathlib.Path(__file__).resolve().parent.parent
REPO = ANSIBLE.parent
ROLE = ANSIBLE / "roles" / "dell_fan_boost"
PREFIX = "dell_fan_boost_"
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


def load_script(metrics_dir):
    values = defaults()
    values["alloy_textfile_dir"] = metrics_dir
    values = {k: render(v, values) if "{{" in v else v for k, v in values.items()}
    text = render((ROLE / "templates" / "fleet-dell-fan-boost.j2").read_text(), values)
    for tag in ("{{", "{%", "{#"):
        check(tag not in text, "rendered script still contains " + tag)
    path = pathlib.Path(metrics_dir) / "fleet-dell-fan-boost"
    path.write_text(text)
    loader = importlib.machinery.SourceFileLoader("fanboost", str(path))
    spec = importlib.util.spec_from_loader("fanboost", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return path, mod


def test_controller(mod):
    on, off, guard, hold = 72, 60, 80, 120

    def ctl():
        return mod.Controller(package=1, on_c=on, off_c=off, min_hold=hold, guard_c=guard)

    c = ctl()
    check(c.decide({0: 50, 1: on - 1}, 0)[0] is False, "boosted below the on threshold")
    check(c.decide({0: 50, 1: on}, 1)[0] is True, "did not boost at the on threshold")
    check(c.decide({0: 50, 1: 40}, 1 + hold - 1)[0] is True, "released before the minimum hold")
    check(c.decide({0: 50, 1: off + 1}, 1 + hold)[0] is True, "released above the off threshold")
    check(c.decide({0: 50, 1: off}, 1 + hold + 1)[0] is False, "did not release at the off threshold")
    check(c.decide({0: 50, 1: on - 1}, 1 + hold + 2)[0] is False, "re-boosted below the on threshold")

    c = ctl()
    check(c.decide({0: guard, 1: 90}, 0)[0] is False, "boosted while another package was at the guard")
    check(c.decide({0: guard - 3, 1: 90}, 1)[0] is False, "guard released before dropping GUARD_RELEASE_C")
    check(c.decide({0: guard - mod.GUARD_RELEASE_C, 1: 90}, 2)[0] is True, "guard did not release")

    c = ctl()
    c.decide({0: 50, 1: 90}, 0)
    check(c.decide({0: guard, 1: 90}, 1)[0] is False, "guard did not end a boost inside the minimum hold")

    c = ctl()
    check(c.decide({0: 50}, 0)[0] is False, "boosted with the package unreadable")
    c.decide({0: 50, 1: 90}, 1)
    check(c.decide({0: 50}, 2)[0] is False, "kept boosting after the package became unreadable")


def fake_tree(root):
    core = root / "hwmon0"
    core.mkdir()
    (core / "name").write_text("coretemp\n")
    for i, pkg in ((1, 0), (2, 1)):
        (core / ("temp" + str(i) + "_label")).write_text("Package id " + str(pkg) + "\n")
        (core / ("temp" + str(i) + "_input")).write_text("50000\n")
    (core / "temp3_label").write_text("Core 0\n")
    (core / "temp3_input").write_text("99000\n")
    smm = root / "hwmon1"
    smm.mkdir()
    (smm / "name").write_text("dell_smm\n")
    for n in range(1, 5):
        (smm / ("pwm" + str(n))).write_text("0\n")
        (smm / ("pwm" + str(n) + "_enable")).write_text("2\n")
    return core, smm


def wait_for(pred, what, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    fail("timed out waiting for: " + what)
    return False


def test_script(script, channel):
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "hwmon"
        root.mkdir()
        metrics = pathlib.Path(tmp) / "metrics"
        core, smm = fake_tree(root)
        temp = core / "temp2_input"
        enables = sorted(smm.glob("pwm*_enable"))
        pwm = smm / ("pwm" + str(channel))
        prom = metrics / "fleet-dell-fan-boost.prom"

        def all_auto():
            return all(p.read_text().strip() == "2" for p in enables)

        def metric(name):
            if not prom.exists():
                return None
            m = re.search(r"^" + name + r" (\S+)$", prom.read_text(), re.M)
            return m.group(1) if m else None

        (smm / "pwm3_enable").write_text("1")  # left over from a killed run
        proc = subprocess.Popen(
            [sys.executable, "-I", str(script), "--hwmon-root", str(root), "--metrics-dir", str(metrics),
             "--poll", "0.05", "--min-hold", "0.3"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            wait_for(lambda: metric("fleet_dell_fan_boost_active") == "0", "first metrics write")
            check(all_auto(), "a leftover manual channel was not restored at start")

            temp.write_text("75000\n")
            wait_for(lambda: (smm / ("pwm" + str(channel) + "_enable")).read_text().strip() == "1"
                     and pwm.read_text().strip() == "255", "boost on the configured channel")
            wait_for(lambda: metric("fleet_dell_fan_boost_active") == "1", "active=1 in metrics")
            check(metric("fleet_dell_fan_boost_package_celsius") == "75.0", "package temperature not reported")

            temp.write_text("50000\n")
            wait_for(all_auto, "every channel back on automatic after release")
            wait_for(lambda: metric("fleet_dell_fan_boost_active") == "0", "active=0 after release")
            check(metric("fleet_dell_fan_boost_transitions_total") == "2", "transitions not counted")

            temp.write_text("75000\n")
            wait_for(lambda: pwm.read_text().strip() == "255" and not all_auto(), "second boost")
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=5)
            check(proc.returncode == 0, "SIGTERM exit code " + str(proc.returncode) + ": " + out)
            check(all_auto(), "SIGTERM left a channel in manual mode")
            check(metric("fleet_dell_fan_boost_active") == "0", "SIGTERM left active=1 in metrics")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        for p in enables:
            p.write_text("1")
        r = subprocess.run([sys.executable, "-I", str(script), "--restore", "--hwmon-root", str(root)],
                           capture_output=True, text=True)
        check(r.returncode == 0, "--restore exit code " + str(r.returncode))
        check(all_auto(), "--restore left a channel in manual mode")


def test_docs(script_text):
    d = defaults()
    unit = render((ROLE / "templates" / "fleet-dell-fan-boost.service.j2").read_text(), d)
    check(re.search(r"^ExecStopPost=\S+ --restore$", unit, re.M), "unit has no ExecStopPost=... --restore")
    check('"--restore"' in script_text, "script does not accept --restore")
    check("WATCHDOG=1" in script_text and re.search(r"^WatchdogSec=", unit, re.M),
          "watchdog is not wired on both sides")

    variables = (ANSIBLE / "docs" / "reference" / "variables.md").read_text()
    documented = set(re.findall(r"`(" + PREFIX + r"\w+)`", variables))
    for name in d:
        if name in ("dell_fan_boost_script", "dell_fan_boost_metrics_dir", "dell_fan_boost_metrics_file"):
            continue
        check(name in documented, "variables.md has no row for " + name)
    for name in documented:
        check(name in d, "variables.md names " + name + ", which is not a default")
    for name, value in d.items():
        m = re.search(r"^\| `" + name + r"` \| `([^`]*)` \|", variables, re.M)
        if m:
            check(m.group(1) == value, "variables.md shows " + name + "=" + m.group(1) + ", default is " + value)

    readme = (ANSIBLE / "README.md").read_text()
    playbooks = (ANSIBLE / "docs" / "reference" / "playbooks.md").read_text()
    check("`dell-fan-boost.yml`" in readme, "ansible/README.md does not list dell-fan-boost.yml")
    check("`dell_fan_boost`" in readme, "ansible/README.md does not list the role")
    check("| `dell-fan-boost.yml` |" in playbooks, "playbooks.md does not list dell-fan-boost.yml")
    check("### `dell_fan_boost`" in playbooks, "playbooks.md has no dell_fan_boost section")
    check((ANSIBLE / "playbooks" / "dell-fan-boost.yml").exists(), "playbooks/dell-fan-boost.yml is missing")
    check("import_playbook: dell-fan-boost.yml" in (ANSIBLE / "playbooks" / "site.yml").read_text(),
          "site.yml does not import dell-fan-boost.yml")
    check(re.search(r"^dell_fan_boost_enabled: true$",
                    (ANSIBLE / "inventory" / "host_vars" / "t7920.yml").read_text(), re.M),
          "host_vars/t7920.yml does not enable the role")

    metrics_doc = (REPO / "monitoring" / "docs" / "reference" / "metrics.md").read_text()
    emitted = set(re.findall(r'"(fleet_dell_fan_boost_\w+) ', script_text))
    check(emitted, "found no metric names in the script")
    documented_metrics = set(re.findall(r"`(fleet_dell_fan_boost_\w+)`", metrics_doc))
    check(emitted == documented_metrics,
          "metrics.md and the script disagree: only in script " + str(sorted(emitted - documented_metrics))
          + ", only in docs " + str(sorted(documented_metrics - emitted)))


def main():
    d = defaults()
    with tempfile.TemporaryDirectory() as tmp:
        script, mod = load_script(tmp)
        compile(script.read_text(), str(script), "exec")
        test_controller(mod)
        test_script(script, int(d[PREFIX + "channel"]))
        test_docs(script.read_text())
    if errors:
        for e in errors:
            print("FAIL: " + e)
        return 1
    print("ok: dell_fan_boost logic, script and docs agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
