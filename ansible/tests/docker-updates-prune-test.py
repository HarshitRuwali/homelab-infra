#!/usr/bin/env python3
"""Test the pruning stage of roles/docker_updates' fleet-docker-update.sh.

Needs python3 with jinja2 and pyyaml; run from anywhere:

    python3 -I ansible/tests/docker-updates-prune-test.py

The script template is rendered with the role defaults (plus overrides), then
the rendered script is run end to end in a temp dir with a fake `docker` on
PATH. The fake records its argv and prints the output format real Docker 29
uses for each subcommand the script calls, so the run reaches the prune stage.

Checks:
  * defaults: the build cache age is "168h", and the rendered script has no
    Jinja left over
  * builder prune runs with `-f --filter until=168h`, after image prune, and
    never with --all
  * it does not run when the age is empty, when pruning is off, or when the
    update already failed
  * fleet_docker_image_bytes_reclaimed adds both prunes (3.427GB + 0B ->
    3427000000), and parses both `Total:` and `Total reclaimed space:` lines
  * a failing builder prune sets fleet_docker_update_failed to 1
"""
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
ROLE = ANSIBLE / "roles" / "docker_updates"
errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)


def role_defaults(out_dir, **override):
    values = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    values["alloy_textfile_dir"] = str(out_dir)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for k, v in list(values.items()):
        if isinstance(v, str) and "{{" in v:
            values[k] = env.from_string(v).render(**values)
    values.update(override)
    return values


def render(values):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters["ternary"] = lambda c, a, b=None: a if c else b
    text = env.from_string((ROLE / "templates" / "fleet-docker-update.sh.j2").read_text()).render(
        inventory_hostname="testhost", **values)
    # Docker's own Go templates ({{.Image}}) legitimately survive rendering;
    # a leftover Jinja expression names a role variable or is a block/comment.
    check(not re.search(r"\{\{\s*(docker_update|update_metrics|inventory)|\{%|\{#", text),
          "rendered script still has Jinja")
    return text


FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "ps "*)
    [ -z "${FAKE_PS_FAIL:-}" ] || { echo "cannot connect to the Docker daemon" >&2; exit 1; }
    exit 0 ;;
  "image prune")
    printf 'Deleted Images:\ndeleted: sha256:abc\n\nTotal reclaimed space: %s\n' "${FAKE_IMAGE_TOTAL:-0B}"
    exit 0 ;;
  "builder prune")
    [ -z "${FAKE_BUILDER_FAIL:-}" ] || { echo "builder prune failed" >&2; exit 1; }
    printf 'ID\t\t\t\t\t\tRECLAIMABLE\tSIZE\t\tLAST ACCESSED\nabcd1234\t\t\t\ttrue\t\t1.2GB\t\t2 weeks ago\nTotal:\t%s\n' "${FAKE_BUILDER_TOTAL:-0B}"
    exit 0 ;;
esac
echo "fake docker: unexpected: $*" >&2
exit 1
"""


def run(values, **env_extra):
    """Run the rendered script; return (exit code, docker calls, metrics, stdout)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        out = tmp / "metrics"
        out.mkdir()
        bindir = tmp / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        values = dict(values, update_metrics_dir=str(out))
        script = tmp / "fleet-docker-update.sh"
        script.write_text(render(values))
        log = tmp / "docker.log"
        log.write_text("")
        env = dict(os.environ, PATH=str(bindir) + ":" + os.environ["PATH"], FAKE_LOG=str(log))
        env.pop("FAKE_PS_FAIL", None)
        env.update(env_extra)
        p = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
        calls = log.read_text().splitlines()
        prom = out / values["docker_update_metrics_file"]
        metrics = {}
        if prom.exists():
            for line in prom.read_text().splitlines():
                if line and not line.startswith("#"):
                    k, v = line.split()
                    metrics[k] = v
        return p.returncode, calls, metrics, p.stdout + p.stderr


def builder_calls(calls):
    return [c for c in calls if c.startswith("builder prune")]


with tempfile.TemporaryDirectory() as t:
    defaults = role_defaults(t)
check(defaults.get("docker_update_build_cache_max_age") == "168h",
      "docker_update_build_cache_max_age default is not 168h")

RECLAIMED = "fleet_docker_image_bytes_reclaimed"
FAILED = "fleet_docker_update_failed"

# Defaults: image prune then builder prune with the age filter.
rc, calls, m, out = run(defaults, FAKE_IMAGE_TOTAL="0B", FAKE_BUILDER_TOTAL="3.427GB")
check(rc == 0, "default run exited %s: %s" % (rc, out))
check(builder_calls(calls) == ["builder prune -f --filter until=168h"],
      "builder prune calls with defaults: %r" % builder_calls(calls))
check(not any("--all" in c or " -a" in c for c in calls), "a prune used --all or -a: %r" % calls)
prunes = [c for c in calls if " prune" in c]
check(prunes[:2] == ["image prune -f", "builder prune -f --filter until=168h"],
      "image prune must come first, then builder prune: %r" % prunes)
check(m.get(RECLAIMED) == "3427000000", "3.427GB + 0B gave %s" % m.get(RECLAIMED))
check(m.get(FAILED) == "0", "failed metric is %s on a good run" % m.get(FAILED))
check("build cache: Total:" in out and "3.427GB" in out, "build cache total not logged: " + out)

# Both prunes add up, and both line formats parse.
rc, calls, m, out = run(defaults, FAKE_IMAGE_TOTAL="512MB", FAKE_BUILDER_TOTAL="3.427GB")
check(m.get(RECLAIMED) == "3939000000", "512MB + 3.427GB gave %s" % m.get(RECLAIMED))
rc, calls, m, out = run(defaults, FAKE_IMAGE_TOTAL="1.5kB", FAKE_BUILDER_TOTAL="0B")
check(m.get(RECLAIMED) == "1500", "1.5kB + 0B gave %s" % m.get(RECLAIMED))

# Age empty: no builder prune, image prune bytes only.
rc, calls, m, out = run(dict(defaults, docker_update_build_cache_max_age=""),
                        FAKE_IMAGE_TOTAL="2MB", FAKE_BUILDER_TOTAL="3.427GB")
check(rc == 0, "empty-age run exited %s: %s" % (rc, out))
check(builder_calls(calls) == [], "builder prune ran with an empty age: %r" % calls)
check("image prune -f" in calls, "image prune did not run with an empty age")
check(m.get(RECLAIMED) == "2000000", "empty age reclaimed %s" % m.get(RECLAIMED))

# Pruning off: neither prune runs.
rc, calls, m, out = run(dict(defaults, docker_update_prune=False), FAKE_BUILDER_TOTAL="3.427GB")
check(not any(" prune" in c for c in calls), "a prune ran with docker_update_prune off: %r" % calls)
check(m.get(RECLAIMED) == "0", "prune off reclaimed %s" % m.get(RECLAIMED))

# Update failed earlier (cannot read containers): neither prune runs.
rc, calls, m, out = run(defaults, FAKE_PS_FAIL="1", FAKE_BUILDER_TOTAL="3.427GB")
check(rc != 0 and m.get(FAILED) == "1", "failed update: rc=%s failed=%s" % (rc, m.get(FAILED)))
check(not any(" prune" in c for c in calls), "a prune ran after a failed update: %r" % calls)

# A failing builder prune fails the run.
rc, calls, m, out = run(defaults, FAKE_BUILDER_FAIL="1", FAKE_IMAGE_TOTAL="1MB")
check(builder_calls(calls) != [], "builder prune was not attempted")
check(m.get(FAILED) == "1" and rc == 1, "builder prune failure: rc=%s failed=%s" % (rc, m.get(FAILED)))
check("ERROR: build cache pruning failed" in out, "builder prune failure not logged: " + out)
check(m.get(RECLAIMED) == "1000000", "reclaimed after builder failure: %s" % m.get(RECLAIMED))

if errors:
    print("FAIL:")
    for e in errors:
        print("  " + e)
    sys.exit(1)
print("ok: docker_updates prune stage")
