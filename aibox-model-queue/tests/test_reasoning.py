"""Tests for the reasoning off-switch translation, unit and through the real handler.

Run:  python tests/test_reasoning.py
Exit 0 = all pass. No pytest dependency, same as test_gate.py.
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import proxy  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  [ ok ] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def t_unit():
    d = {"reasoning": {"enabled": False, "effort": "none"}}
    check(proxy.translate_reasoning(d) and d.get("reasoning_effort") == "none",
          "enabled=false adds reasoning_effort=none")
    d = {"reasoning": {"effort": "None"}}
    check(proxy.translate_reasoning(d) and d.get("reasoning_effort") == "none",
          "effort=none (any case) adds reasoning_effort=none")
    d = {"reasoning": {"enabled": True, "effort": "medium"}}
    check(not proxy.translate_reasoning(d) and "reasoning_effort" not in d,
          "thinking-on request is left alone")
    d = {"reasoning": {"enabled": False}, "reasoning_effort": "high"}
    check(not proxy.translate_reasoning(d) and d["reasoning_effort"] == "high",
          "explicit top-level reasoning_effort wins")
    d = {"messages": []}
    check(not proxy.translate_reasoning(d) and "reasoning_effort" not in d,
          "no reasoning key is left alone")


async def t_through_handler():
    """Drive proxy.handle() against a fake upstream and inspect what it received."""
    received = []

    async def upstream(request):
        received.append(await request.read())
        return web.json_response({"ok": True})

    up_app = web.Application()
    up_app.router.add_route("*", "/{tail:.*}", upstream)
    up = TestServer(up_app)
    await up.start_server()
    proxy.UPSTREAM = str(up.make_url("")).rstrip("/")

    client = TestClient(TestServer(await proxy._make_app()))
    await client.start_server()
    try:
        before = proxy.STATS["reasoning_off_translated"]
        off = {"model": "m", "messages": [], "reasoning": {"enabled": False, "effort": "none"}}
        r = await client.post("/v1/chat/completions", data=json.dumps(off))
        got = json.loads(received[-1])
        check(r.status == 200 and got.get("reasoning_effort") == "none",
              "handler forwards reasoning_effort=none upstream")
        check(proxy.STATS["reasoning_off_translated"] == before + 1,
              "status counter increments on translation")

        fwd = proxy.STATS["reasoning_off_forwarded"]
        direct = {"model": "m", "messages": [], "reasoning_effort": "none"}
        await client.post("/v1/chat/completions", data=json.dumps(direct))
        check(proxy.STATS["reasoning_off_forwarded"] == fwd + 1,
              "forwarded counter sees a client-sent reasoning_effort=none")

        on_raw = json.dumps({"model": "m", "reasoning": {"enabled": True, "effort": "medium"}},
                            separators=(",", ":")).encode()
        await client.post("/v1/chat/completions", data=on_raw)
        check(received[-1] == on_raw, "thinking-on body is relayed byte-for-byte")

        emb = {"model": "m", "input": "x", "reasoning": {"enabled": False}}
        await client.post("/v1/embeddings", data=json.dumps(emb))
        check("reasoning_effort" not in json.loads(received[-1]),
              "embeddings path is never rewritten")

        status = await (await client.get("/_proxy/status")).json()
        check("reasoning_off_translated" in status and "reasoning_off_forwarded" in status,
              "both counters are exposed on /_proxy/status")
    finally:
        await client.close()
        await up.close()


async def main():
    t_unit()
    await t_through_handler()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("all reasoning tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
