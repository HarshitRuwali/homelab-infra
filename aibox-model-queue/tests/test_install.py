"""install.sh keeps an installed unit's bind and probes every bound address.

Run:  python tests/test_install.py
Exit 0 = all pass. Needs bash and sed; uses a temp HOME, a fake systemctl and a fake curl.
"""
import os
import pathlib
import stat
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve().parent.parent
FAILS = []


def check(cond, label):
    print(("  [ ok ] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def run(home, env_extra, curl_ok_for=None):
    bindir = home / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    # Fake curl: answers 200 for the status URL unless the host is not in curl_ok_for.
    ok = curl_ok_for if curl_ok_for is not None else ["*"]
    (bindir / "curl").write_text(
        "#!/bin/sh\nfor a; do u=\"$a\"; done\n"
        "echo \"$u\" >> \"$HOME/probes.log\"\n"
        "case \"$*\" in *'%{http_code}'*) "
        + "".join(f"case \"$u\" in *{h}*) printf 200; exit 0;; esac; " for h in ok)
        + "printf 000; exit 0;; esac\necho '{}'\n")
    for f in bindir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    py = bindir / "py"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MODEL_PROXY_", "XDG_"))}
    env.update(HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}",
               MODEL_QUEUE_PYTHON=str(py), MODEL_QUEUE_INSTALL_DIR=str(home / "inst"))
    env.update(env_extra)
    return subprocess.run(["bash", str(HERE / "install.sh")], env=env, capture_output=True,
                          text=True, timeout=60)


def unit_text(home):
    return (home / ".config/systemd/user/hermes-model-queue.service").read_text()


def seed_unit(home, host, port):
    d = home / ".config/systemd/user"
    d.mkdir(parents=True)
    (d / "hermes-model-queue.service").write_text(
        f"[Service]\nEnvironment=MODEL_PROXY_UPSTREAM=http://x:1\n"
        f"Environment=MODEL_PROXY_HOST={host}\nEnvironment=MODEL_PROXY_PORT={port}\n")


# The probe loop sleeps between retries, so failing probes take ~20 s; keep those cases few.
with tempfile.TemporaryDirectory() as t:
    home = pathlib.Path(t)
    r = run(home, {})
    check(r.returncode == 0, "fresh install succeeds")
    u = unit_text(home)
    check("MODEL_PROXY_HOST=127.0.0.1\n" in u and "MODEL_PROXY_PORT=8099\n" in u,
          "fresh install defaults to 127.0.0.1:8099")

with tempfile.TemporaryDirectory() as t:
    home = pathlib.Path(t)
    seed_unit(home, "127.0.0.1,100.88.214.113", "8123")
    r = run(home, {})
    u = unit_text(home)
    check(r.returncode == 0, "re-run over an existing unit succeeds")
    check("MODEL_PROXY_HOST=127.0.0.1,100.88.214.113\n" in u, "re-run keeps the installed bind list")
    check("MODEL_PROXY_PORT=8123\n" in u, "re-run keeps the installed port")
    check("127.0.0.1,100.88.214.113 port 8123" in r.stdout, "ok line prints the effective bind list and port")
    probes = (home / "probes.log").read_text()
    check("100.88.214.113:8123" in probes and "127.0.0.1:8123" in probes,
          "every bound address is probed")

with tempfile.TemporaryDirectory() as t:
    home = pathlib.Path(t)
    seed_unit(home, "127.0.0.1,100.88.214.113", "8123")
    r = run(home, {"MODEL_PROXY_HOST": "10.0.0.5", "MODEL_PROXY_PORT": "9000"})
    u = unit_text(home)
    check("MODEL_PROXY_HOST=10.0.0.5\n" in u and "MODEL_PROXY_PORT=9000\n" in u,
          "explicit environment values override the installed unit")

with tempfile.TemporaryDirectory() as t:
    home = pathlib.Path(t)
    seed_unit(home, "127.0.0.1,100.88.214.113", "8099")
    r = run(home, {}, curl_ok_for=["127.0.0.1"])
    check(r.returncode == 1 and "100.88.214.113" in r.stderr,
          "a bound address that does not answer fails the install and is named")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED")
    raise SystemExit(1)
print("all install tests passed")
