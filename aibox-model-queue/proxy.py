#!/usr/bin/env python3
"""Serializing proxy in front of the AI box's llama.cpp ``llama-server``.

Current upstream
----------------
Since 2026-11-11 the box serves chat from llama.cpp ``llama-server`` on
``:8090`` (one resident model, ``qwen3.8-27b``, several parallel slots) and
embeddings from ``llama-embed`` on ``:8080``. LM Studio is disabled. With one
model and no swaps the gate below never queues anything; what the proxy still
provides is the tool-schema cleaning, the reasoning off-switch translation, a
stable URL, and (if multiple models are ever configured) the serialisation. The
gate is kept as is, it costs nothing. The rest of this docstring records why the
gate exists and was measured against LM Studio, hence the LM Studio wording.

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

Reasoning off-switch
--------------------
Hermes asks a custom provider to stop thinking with an OpenRouter-style body
field, ``reasoning: {"enabled": false}``. LM Studio ignores that field and
keeps thinking. It honours a top-level ``reasoning_effort: "none"`` instead
(measured 2026-09-21 on qwen3.8-27b: 53 -> 4 completion tokens). The proxy
translates the off case only; an explicit top-level ``reasoning_effort`` always
wins, and requests that leave thinking on are relayed byte-for-byte.

Tool-schema cleaning
--------------------
LM Studio compiles every tool's JSON schema into a llama.cpp grammar, and that
compiler rejects some valid schemas, failing the whole request with
``Failed to initialize samplers: failed to parse grammar``. Measured 2026-10-09:
a ``pattern`` using ``\\S``/``\\s`` (T3 Code's MCP tools) gives "unknown escape",
and an array of strings with ``maxLength`` >= 2000 (Hermes' ``clarify``) gives
"number of repetitions exceeds sane defaults". The proxy drops the validation-only
keywords ``pattern``, ``minLength``, ``maxLength``, ``minItems`` and ``maxItems`` from
tool parameter schemas. The tool's own server still validates its arguments.
The walk follows schema structure, so a parameter *named* ``pattern`` survives.

LM Studio also rejects a tool whose ``parameters`` is an object schema without
``properties`` (400 ``invalid_type ... "properties" Required``, measured
2026-10-09 with Pi under T3 Code, whose no-argument tools send just
``{"type": "object"}``). The proxy adds an empty ``properties`` there, and a
minimal object schema when ``parameters`` is missing.
"""
from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import logging
import os
from collections import deque

from aiohttp import ClientError, ClientSession, ClientTimeout, web

# llama.cpp llama-server (chat, qwen3.8-27b). :8080 is the bge embeddings server, not chat.
UPSTREAM = os.environ.get("MODEL_PROXY_UPSTREAM", "http://10.10.50.122:8090")
# Comma-separated, so the proxy can serve localhost and one tailnet address
# without listening on every interface.
BIND_HOSTS = [h.strip() for h in os.environ.get("MODEL_PROXY_HOST", "127.0.0.1").split(",") if h.strip()]
BIND_PORT = int(os.environ.get("MODEL_PROXY_PORT", "8099"))
# Cancel a request's handler when its client hangs up mid-stream. Without this aiohttp
# keeps waiting on the upstream for nobody and the gate slot stays held.
RUNNER_KWARGS = {"handler_cancellation": True}
# Time a queued request will wait for the box before giving up.
ACQUIRE_TIMEOUT = float(os.environ.get("MODEL_PROXY_ACQUIRE_TIMEOUT", "900"))
# Longest silence tolerated from the upstream (seconds between bytes, not total request
# time). A non-streaming completion sends nothing until it is finished, so this must
# exceed the longest such generation; 1800 s, lower it to free a hung slot sooner.
READ_TIMEOUT = float(os.environ.get("MODEL_PROXY_READ_TIMEOUT", "1800"))
# Largest request body accepted, in MiB. Real chat bodies are KB to a few MB.
MAX_BODY_MB = float(os.environ.get("MODEL_PROXY_MAX_BODY_MB", "32"))
# Optional shared secret. When set, every request (including /_proxy/status) must send
# ``Authorization: Bearer <token>``. Unset or empty means no authentication.
TOKEN = os.environ.get("MODEL_PROXY_TOKEN", "")
# Bytes of an upstream error body written to the log when the upstream answers >= 400.
ERROR_LOG_BYTES = 300

# Paths that actually pin a model and therefore must be serialized.
GATED_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/embeddings",
               "/api/v0/chat/completions", "/api/v0/completions")

logging.basicConfig(
    level=os.environ.get("MODEL_PROXY_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("model-queue")


# Inference paths whose body may carry Hermes' ``reasoning`` off-switch.
REASONING_PATHS = ("/v1/chat/completions", "/v1/completions",
                   "/api/v0/chat/completions", "/api/v0/completions")
# reasoning_off_translated: off-switches this proxy rewrote.
# reasoning_off_forwarded:  requests sent upstream with thinking off, by any route (the wire truth).
STATS = {"reasoning_off_translated": 0, "reasoning_off_forwarded": 0,
         "tool_schema_keywords_dropped": 0}

# Validation-only schema keywords the llama.cpp grammar compiler (used by LM Studio and
# llama-server alike) can choke on.
GRAMMAR_HOSTILE = ("pattern", "minLength", "maxLength", "minItems", "maxItems")
# Keywords whose value is one sub-schema, a list of them, or a name -> schema map.
_SUB_ONE = ("items", "additionalProperties", "not", "contains", "propertyNames")
_SUB_LIST = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUB_MAP = ("properties", "$defs", "definitions", "patternProperties")


def _clean_schema(node) -> int:
    """Drop GRAMMAR_HOSTILE keywords from a JSON schema in place. Returns how many."""
    if isinstance(node, list):  # legacy tuple-form "items"
        return sum(_clean_schema(n) for n in node)
    if not isinstance(node, dict):
        return 0
    dropped = 0
    for key in GRAMMAR_HOSTILE:
        if key in node:
            del node[key]
            dropped += 1
    for key in _SUB_ONE:
        if key in node:
            dropped += _clean_schema(node[key])
    for key in _SUB_LIST:
        for sub in node.get(key) or ():
            dropped += _clean_schema(sub)
    for key in _SUB_MAP:
        sub = node.get(key)
        if isinstance(sub, dict):
            for v in sub.values():
                dropped += _clean_schema(v)
    return dropped


def clean_tool_schemas(data: dict) -> int:
    """Clean every ``tools[].function.parameters`` schema in *data*. Mutates; returns count of fixes.

    Drops GRAMMAR_HOSTILE keywords, and gives a top-level object schema the
    ``properties`` key LM Studio's request validator required (no-argument tools often omit
    it; harmless on llama-server)."""
    fixed = 0
    for tool in data.get("tools") or ():
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            fn["parameters"] = params = {"type": "object", "properties": {}}
            fixed += 1
        elif params.get("type", "object") == "object" and "properties" not in params:
            params["properties"] = {}
            fixed += 1
        fixed += _clean_schema(params)
    return fixed


def translate_reasoning(data: dict) -> bool:
    """Add ``reasoning_effort: "none"`` when *data* asks for thinking off. Mutates; True if changed."""
    if "reasoning_effort" in data:
        return False
    r = data.get("reasoning")
    if not isinstance(r, dict):
        return False
    if r.get("enabled") is False or str(r.get("effort", "")).strip().lower() == "none":
        data["reasoning_effort"] = "none"
        return True
    return False


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
        # Give the slot back before the first await: a handler cancelled while
        # waiting for the lock (client hung up) must not leak it.
        self.active = max(0, self.active - 1)
        if self.active == 0:
            self.current = None
        await asyncio.shield(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()


GATE = ModelGate()
HOP_BY_HOP = {"content-length", "transfer-encoding", "connection",
              "keep-alive", "content-encoding"}


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Require ``Authorization: Bearer <MODEL_PROXY_TOKEN>`` when a token is configured."""
    if TOKEN:
        supplied = request.headers.get("Authorization", "")
        expected = "Bearer " + TOKEN
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            return web.json_response(
                {"error": {"message": "missing or invalid bearer token", "type": "unauthorized"}},
                status=401, headers={"WWW-Authenticate": "Bearer"})
    return await handler(request)


async def handle(request: web.Request) -> web.StreamResponse:
    path = request.rel_url.path
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        log.warning("rejected %s %s: body exceeds %s MiB", request.method, path, MAX_BODY_MB)
        return web.json_response(
            {"error": {"message": f"request body exceeds the {MAX_BODY_MB:g} MiB limit "
                                  "(MODEL_PROXY_MAX_BODY_MB)",
                       "type": "request_entity_too_large"}}, status=413)

    model = None
    if path in GATED_PATHS and body:
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            model = data.get("model")
            if path in REASONING_PATHS:
                dropped = clean_tool_schemas(data)
                if dropped:
                    STATS["tool_schema_keywords_dropped"] += dropped
                    log.debug("dropped %d grammar-hostile schema keywords (model=%s)", dropped, model)
                translated = translate_reasoning(data)
                if translated:
                    STATS["reasoning_off_translated"] += 1
                    log.debug("reasoning off -> reasoning_effort=none (model=%s)", model)
                if translated or dropped:
                    body = json.dumps(data).encode("utf-8")
                if data.get("reasoning_effort") == "none":
                    STATS["reasoning_off_forwarded"] += 1

    if model:
        await GATE.acquire(model)
    try:
        # The proxy token is for this proxy only; never forward it to the upstream.
        drop = ("host", "content-length") + (("authorization",) if TOKEN else ())
        headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
        session: ClientSession = request.app["session"]
        try:
            upstream_cm = session.request(
                request.method, UPSTREAM + str(request.rel_url),
                data=body or None, headers=headers,
            )
            upstream = await upstream_cm.__aenter__()
        except asyncio.TimeoutError:
            log.warning("upstream timed out before responding: %s %s", request.method, path)
            return web.json_response(
                {"error": {"message": "upstream timed out", "type": "gateway_timeout"}}, status=504)
        except ClientError as exc:
            log.warning("upstream unreachable: %s %s: %s", request.method, path, exc)
            return web.json_response(
                {"error": {"message": "upstream unreachable", "type": "bad_gateway"}}, status=502)
        try:
            out = web.StreamResponse(status=upstream.status)
            for k, v in upstream.headers.items():
                if k.lower() not in HOP_BY_HOP:
                    out.headers[k] = v
            await out.prepare(request)
            logged = upstream.status < 400
            async for chunk in upstream.content.iter_chunked(65536):
                if not logged:
                    logged = True
                    log.warning("upstream %s for %s %s (model=%s): %s", upstream.status,
                                request.method, path, model,
                                chunk[:ERROR_LOG_BYTES].decode("utf-8", "replace"))
                await out.write(chunk)
            await out.write_eof()
            return out
        finally:
            await upstream_cm.__aexit__(None, None, None)
    finally:
        if model:
            await GATE.release()


async def status(request: web.Request) -> web.Response:
    return web.json_response({
        "upstream": UPSTREAM,
        "auth": bool(TOKEN),
        "current_model": GATE.current,
        "active": GATE.active,
        "queued": [m for _, m in GATE._waiters],
        **STATS,
    })


async def _make_app() -> web.Application:
    app = web.Application(client_max_size=int(MAX_BODY_MB * 1024 * 1024),
                          middlewares=[auth_middleware])
    app.router.add_get("/_proxy/status", status)
    app.router.add_route("*", "/{tail:.*}", handle)

    async def _startup(a):
        # No total timeout: long generations must not be cut off. sock_read bounds a silent
        # upstream (READ_TIMEOUT) so a hung one cannot hold a gate slot forever.
        a["session"] = ClientSession(
            timeout=ClientTimeout(total=None, connect=30, sock_read=READ_TIMEOUT))

    async def _cleanup(a):
        await a["session"].close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app


if __name__ == "__main__":
    log.info("model-queue proxy %s:%s -> %s", ",".join(BIND_HOSTS), BIND_PORT, UPSTREAM)
    web.run_app(_make_app(), host=BIND_HOSTS, port=BIND_PORT, print=None, **RUNNER_KWARGS)
