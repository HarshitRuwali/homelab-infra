"""A client that hangs up mid-stream must free its slot instead of orphaning the upstream.

In production aiohttp only cancels a handler on client hang-up when the runner has
handler_cancellation (connection_lost otherwise just fails the request payload,
which a handler already past request.read() never touches). Observed 2026-09-25:
4 upstream requests and active=4 with 1 live client.

Caveat: under aiohttp.test_utils this test passes with or without the flag (the
harness cancels the handler itself), so it guards the behaviour, not the flag.
The flag's necessity rests on reading web_protocol.connection_lost, not on this test.

Run:  python tests/test_disconnect.py
Exit 0 = all pass. No pytest dependency, same as test_gate.py.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import proxy  # noqa: E402
from aiohttp import ClientSession, web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  [ ok ] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


async def t_hangup_mid_stream_frees_slot():
    upstream_done = asyncio.Event()

    async def queued_stream(request):
        # Headers and one chunk now, then silence: a stream stuck in LM Studio's queue.
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        try:
            await resp.write(b"data: first\n\n")
            await asyncio.sleep(30)
            await resp.write(b"data: late\n\n")
            return resp
        finally:
            upstream_done.set()

    up_app = web.Application()
    up_app.router.add_route("*", "/{tail:.*}", queued_stream)
    up = TestServer(up_app, handler_cancellation=True)
    await up.start_server()
    proxy.UPSTREAM = str(up.make_url("")).rstrip("/")

    # The same runner settings production uses, so the test cannot pass on a flag prod lacks.
    srv = TestServer(await proxy._make_app(), **proxy.RUNNER_KWARGS)
    await srv.start_server()
    try:
        async with ClientSession() as s:
            resp = await s.post(srv.make_url("/v1/chat/completions"),
                                json={"model": "m", "messages": [], "stream": True})
            await resp.content.readline()
            check(proxy.GATE.active == 1, "streaming request holds one slot")
            resp.close()  # the client hangs up mid-stream
        for _ in range(30):
            if proxy.GATE.active == 0:
                break
            await asyncio.sleep(0.1)
        check(proxy.GATE.active == 0, f"slot freed within 3s of the hang-up (active={proxy.GATE.active})")
        try:
            await asyncio.wait_for(upstream_done.wait(), 3)
            check(True, "upstream stream was abandoned, not left running")
        except asyncio.TimeoutError:
            check(False, "upstream stream was abandoned, not left running")
    finally:
        await srv.close()
        await up.close()


if __name__ == "__main__":
    asyncio.run(t_hangup_mid_stream_frees_slot())
    if FAILS:
        print(f"\n{len(FAILS)} FAILED")
        sys.exit(1)
    print("\nall disconnect tests passed")
