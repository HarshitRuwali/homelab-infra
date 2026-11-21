"""Optional bearer token, upstream read timeout, request body limit, upstream error logging.

Run:  python tests/test_limits.py
Exit 0 = all pass. No pytest dependency, same as test_gate.py.
"""
import asyncio
import json
import logging
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import proxy  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  [ ok ] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def t_defaults():
    """Defaults in a clean environment: chat server :8090, loopback bind, no token."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_PROXY_")}
    out = subprocess.run(
        [sys.executable, "-c",
         "import json,proxy;print(json.dumps([proxy.UPSTREAM,proxy.BIND_HOSTS,proxy.TOKEN,"
         "proxy.READ_TIMEOUT,proxy.MAX_BODY_MB]))"],
        cwd=HERE, env=env, capture_output=True, text=True).stdout
    up, hosts, token, read_to, max_mb = json.loads(out)
    check(up == "http://10.10.50.122:8090", f"default upstream is llama-server :8090 (got {up})")
    check(hosts == ["127.0.0.1"], f"default bind is loopback only (got {hosts})")
    check(token == "", "no token by default")
    check(read_to == 1800.0 and max_mb == 32.0, "default read timeout 1800 s, body limit 32 MiB")
    init_src = (HERE / "__init__.py").read_text()
    check("10.10.50.122:8080" not in init_src and "10.10.50.122:8090" in init_src,
          "plugin __init__ default upstream is :8090")


async def with_proxy(upstream_handler, body):
    up_app = web.Application()
    up_app.router.add_route("*", "/{tail:.*}", upstream_handler)
    up = TestServer(up_app)
    await up.start_server()
    proxy.UPSTREAM = str(up.make_url("")).rstrip("/")
    client = TestClient(TestServer(await proxy._make_app()))
    await client.start_server()
    try:
        await body(client)
    finally:
        await client.close()
        await up.close()


async def t_auth():
    async def ok(request):
        return web.json_response({"data": []})

    async def body(client):
        r = await client.get("/v1/models")
        check(r.status == 200, "no token configured: request passes without credentials")
        proxy.TOKEN = "s3cret"
        try:
            r = await client.get("/v1/models")
            check(r.status == 401, "token configured: missing credentials gives 401")
            r = await client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
            check(r.status == 401, "token configured: wrong token gives 401")
            r = await client.get("/_proxy/status")
            check(r.status == 401, "token configured: /_proxy/status is protected too")
            r = await client.get("/v1/models", headers={"Authorization": "Bearer s3cret"})
            check(r.status == 200, "token configured: correct token passes")
            st = await (await client.get("/_proxy/status",
                                         headers={"Authorization": "Bearer s3cret"})).json()
            check(st.get("auth") is True and "s3cret" not in json.dumps(st),
                  "status reports auth on and never echoes the token")
        finally:
            proxy.TOKEN = ""

    await with_proxy(ok, body)


async def t_auth_not_forwarded():
    seen = []

    async def ok(request):
        seen.append(request.headers.get("Authorization"))
        return web.json_response({"data": []})

    async def body(client):
        await client.get("/v1/models", headers={"Authorization": "Bearer s3cret"})
        check(seen[-1] == "Bearer s3cret", "no token configured: Authorization passes through")
        proxy.TOKEN = "s3cret"
        try:
            await client.get("/v1/models", headers={"Authorization": "Bearer s3cret"})
            check(seen[-1] is None, "token configured: Authorization is not forwarded upstream")
        finally:
            proxy.TOKEN = ""

    await with_proxy(ok, body)


async def t_body_limit():
    seen = []

    async def ok(request):
        seen.append(await request.read())
        return web.json_response({"ok": True})

    old = proxy.MAX_BODY_MB
    proxy.MAX_BODY_MB = 1  # 1 MiB so the test stays small
    try:
        async def body(client):
            r = await client.post("/v1/chat/completions", data=b"x" * (2 * 1024 * 1024))
            payload = await r.json()
            check(r.status == 413, f"oversized body gets 413 (got {r.status})")
            check("MODEL_PROXY_MAX_BODY_MB" in payload["error"]["message"],
                  "413 message names the setting")
            check(not seen, "oversized body never reaches the upstream")
            r = await client.post("/v1/chat/completions",
                                  data=json.dumps({"model": "m", "messages": []}))
            check(r.status == 200, "normal body still passes")

        await with_proxy(ok, body)
    finally:
        proxy.MAX_BODY_MB = old


async def t_read_timeout():
    async def stalls(request):
        await asyncio.sleep(10)  # never answers within the test's timeout
        return web.json_response({})

    old = proxy.READ_TIMEOUT
    proxy.READ_TIMEOUT = 0.5
    try:
        async def body(client):
            t0 = asyncio.get_running_loop().time()
            r = await client.post("/v1/chat/completions",
                                  data=json.dumps({"model": "m", "messages": []}))
            took = asyncio.get_running_loop().time() - t0
            check(r.status == 504, f"silent upstream gives 504 (got {r.status})")
            check(took < 5, f"timeout fires near READ_TIMEOUT (took {took:.1f}s)")
            check(proxy.GATE.active == 0, "gate slot released after the timeout")

        await with_proxy(stalls, body)
    finally:
        proxy.READ_TIMEOUT = old


async def t_unreachable():
    app = await proxy._make_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    proxy.UPSTREAM = "http://127.0.0.1:1"  # nothing listens here
    try:
        r = await client.get("/v1/models")
        check(r.status == 502, f"unreachable upstream gives 502 (got {r.status})")
    finally:
        await client.close()


async def t_error_logging():
    """A 400 from the upstream is relayed unchanged and its body is logged."""
    err = b'{"error":{"code":400,"message":"Failed to initialize samplers","type":"invalid_request_error"}}'

    async def bad(request):
        return web.Response(status=400, body=err, content_type="application/json")

    cap = Capture()
    proxy.log.addHandler(cap)
    try:
        async def body(client):
            r = await client.post("/v1/chat/completions",
                                  data=json.dumps({"model": "m", "messages": []}))
            check(r.status == 400 and await r.read() == err, "upstream 400 relayed unchanged")
            check(any("upstream 400" in ln and "Failed to initialize samplers" in ln
                      for ln in cap.lines), "upstream error body is logged")
            n = len(cap.lines)
            ok_resp = await client.get("/_proxy/status")
            check(ok_resp.status == 200 and len(cap.lines) == n, "successful responses are not logged")

        await with_proxy(bad, body)
    finally:
        proxy.log.removeHandler(cap)


async def main():
    t_defaults()
    for t in (t_auth, t_auth_not_forwarded, t_body_limit, t_read_timeout, t_unreachable, t_error_logging):
        print(t.__name__)
        await t()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("all limits tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
