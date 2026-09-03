#!/usr/bin/env python3
"""Serializing proxy in front of LM Studio on the AI box.

Why this exists
---------------
The box holds exactly one model at a time. LM Studio JIT-evicts correctly when
asked for a different model (measured: ~11 s for a 16 GB 27B), but two
*concurrent* requests for *different* models race: one wins and the other dies
mid-flight with ``Model is unloaded.`` / ``Engine protocol startup was
aborted.``. That is what killed the Hermes ``dev`` room on 2026-09-03.

The wrong fix is a model fallback: it answers a dev/jobs request on the small
model, which defeats the reason those rooms exist. The right fix is to make the
loser of the race *wait* instead of degrade.

Semantics
---------
* Requests naming the **same** model run concurrently (LM Studio handles that
  fine, and it is the common case).
* A request naming a **different** model waits until every in-flight request
  finishes, then takes the box.
* FIFO fairness: a waiting different-model request is never starved by an
  endless stream of same-model requests. Same-model requests may only join the
  current batch if no earlier waiter wants something else.
* Non-inference endpoints (``/v1/models`` etc.) bypass the gate entirely.

The gate is released only after the response body is fully relayed, so
streaming responses hold the box for their whole lifetime.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from collections import deque

from aiohttp import ClientSession, ClientTimeout, web

UPSTREAM = os.environ.get("MODEL_PROXY_UPSTREAM", "http://10.10.50.122:8080")
BIND_HOST = os.environ.get("MODEL_PROXY_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("MODEL_PROXY_PORT", "8099"))
# Time a queued request will wait for the box before giving up.
ACQUIRE_TIMEOUT = float(os.environ.get("MODEL_PROXY_ACQUIRE_TIMEOUT", "900"))

# Paths that actually pin a model and therefore must be serialized.
GATED_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings",
               "/api/v0/chat/completions", "/api/v0/completions")

logging.basicConfig(
    level=os.environ.get("MODEL_PROXY_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("model-queue")


class ModelGate:
    """Admission control keyed by model name, with FIFO fairness."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._tickets = itertools.count()
        self._waiters: deque[tuple[int, str]] = deque()
        self.current: str | None = None
        self.active = 0

    def _admissible(self, ticket: int, model: str) -> bool:
        if self.active == 0:
            # Box is idle: strict FIFO, the oldest waiter goes first.
            return self._waiters and self._waiters[0][0] == ticket
        if model != self.current:
            return False
        # Same model as the running batch: join only if nobody ahead of us is
        # waiting for a different model (otherwise they would starve).
        for t, m in self._waiters:
            if t == ticket:
                return True
            if m != self.current:
                return False
        return False

    async def acquire(self, model: str) -> None:
        ticket = next(self._tickets)
        async with self._cond:
            self._waiters.append((ticket, model))
            try:
                if not self._admissible(ticket, model):
                    swap = self.current is not None and self.current != model
                    if swap:
                        log.info("queueing %s behind in-flight %s (%d active)",
                                 model, self.current, self.active)
                    await asyncio.wait_for(
                        self._cond.wait_for(lambda: self._admissible(ticket, model)),
                        timeout=ACQUIRE_TIMEOUT,
                    )
            finally:
                try:
                    self._waiters.remove((ticket, model))
                except ValueError:
                    pass
            if self.current != model:
                log.info("switching box to %s", model)
            self.current = model
            self.active += 1

    async def release(self) -> None:
        async with self._cond:
            self.active = max(0, self.active - 1)
            if self.active == 0:
                self.current = None
            self._cond.notify_all()


GATE = ModelGate()
HOP_BY_HOP = {"content-length", "transfer-encoding", "connection",
              "keep-alive", "content-encoding"}


async def handle(request: web.Request) -> web.StreamResponse:
    path = request.rel_url.path
    body = await request.read()

    model = None
    if path in GATED_PATHS and body:
        try:
            model = json.loads(body).get("model")
        except (ValueError, AttributeError):
            model = None

    if model:
        await GATE.acquire(model)
    try:
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ("host", "content-length")}
        session: ClientSession = request.app["session"]
        async with session.request(
            request.method, UPSTREAM + str(request.rel_url),
            data=body or None, headers=headers,
        ) as upstream:
            out = web.StreamResponse(status=upstream.status)
            for k, v in upstream.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    out.headers[k] = v
            await out.prepare(request)
            async for chunk in upstream.content.iter_chunked(65536):
                await out.write(chunk)
            await out.write_eof()
            return out
    finally:
        if model:
            await GATE.release()


async def status(request: web.Request) -> web.Response:
    return web.json_response({
        "upstream": UPSTREAM,
        "current_model": GATE.current,
        "active": GATE.active,
        "queued": [m for _, m in GATE._waiters],
    })


async def _make_app() -> web.Application:
    app = web.Application(client_max_size=1024 ** 3)
    app.router.add_get("/_proxy/status", status)
    app.router.add_route("*", "/{tail:.*}", handle)

    async def _startup(a):
        # No total timeout: model loads and long generations must not be cut off.
        a["session"] = ClientSession(timeout=ClientTimeout(total=None, connect=30))

    async def _cleanup(a):
        await a["session"].close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


if __name__ == "__main__":
    log.info("model-queue proxy %s:%s -> %s", BIND_HOST, BIND_PORT, UPSTREAM)
    web.run_app(_make_app(), host=BIND_HOST, port=BIND_PORT, print=None)
