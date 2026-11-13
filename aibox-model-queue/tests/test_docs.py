"""Docs and code must agree on configuration and the upstream default.

Run:  python tests/test_docs.py
Exit 0 = all pass. Needs no third-party packages.
"""
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent.parent
FAILS = []


def check(cond, label):
    print(("  [ ok ] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


proxy_src = (HERE / "proxy.py").read_text()
docs = (HERE / "docs" / "index.md").read_text()
readme = (HERE / "README.md").read_text()
install = (HERE / "install.sh").read_text()
unit = (HERE / "systemd" / "hermes-model-queue.service").read_text()

env_vars = sorted(set(re.findall(r'environ\.get\("(MODEL_PROXY_[A-Z_]+)"', proxy_src)))
check(len(env_vars) >= 8, "found the proxy's environment variables")
for v in env_vars:
    check(f"`{v}`" in docs, f"docs/index.md documents {v}")

default = re.search(r'"MODEL_PROXY_UPSTREAM", "([^"]+)"', proxy_src).group(1)
check(default.endswith(":8090"), f"proxy default upstream is the llama-server port ({default})")
check(default in install, "install.sh default upstream matches proxy.py")
check(default in docs, "docs/index.md names the default upstream")
check(default in readme, "README.md names the default upstream")
for name, text in (("proxy.py", proxy_src), ("install.sh", install), ("docs", docs), ("README", readme)):
    check(("bon" + "sai") not in text.lower(), f"{name} does not mention the retired quantised model")

for placeholder in re.findall(r"__[A-Z_]+__", unit):
    check(f"s#{placeholder}#" in install, f"install.sh substitutes {placeholder}")

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    raise SystemExit(1)
print("\nall docs tests passed")
