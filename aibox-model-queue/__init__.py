"""Queue inference across a single-GPU LM Studio host instead of racing for it.

The problem
-----------
The AI box holds exactly one model resident. LM Studio JIT-evicts correctly
when asked for a different one (measured on this hardware: ~11 s to swap in a
16 GB 27B), so *sequential* switching is fine. What breaks is concurrency.

Two Hermes profiles bound to different models will, sooner or later, have a
turn in flight at the same moment. Reproduced deterministically::

    google/gemma-4-12b-qat  -> ok: 'OK'
    unsloth/qwen3.8-27b     -> ERROR: Model is unloaded.

One request wins the box and the other is evicted mid-flight. Upstream this
surfaces as ``Model is unloaded.`` or a 400 ``Engine protocol startup was
aborted``, which Hermes classifies as non-retryable and aborts the turn on.

Why not a model fallback
------------------------
``fallback_providers`` makes the loser retry on another model, so the turn
survives. But that means a room deliberately bound to the 27B silently answers
on the 12B exactly when it is busiest. That trades a visible failure for an
invisible quality regression, which is worse: the whole reason to bind a room
to a bigger model is to get the bigger model.

What this does instead
----------------------
Fronts the box with a serializing proxy. Requests naming the **same** model run
concurrently (LM Studio handles that well, and it is the common case). A
request naming a **different** model waits for the box to drain, then takes it.
Nobody is downgraded; the loser of a race just waits its turn.

FIFO fairness is enforced so a queued 27B request cannot be starved by an
endless stream of 12B traffic; see ``proxy.ModelGate._admissible``.

Deployment
----------
The proxy is also runnable standalone (``python -m`` / ``proxy.py``) and ships
a systemd unit in ``systemd/``. That is the preferred way to run it, because a
shared serializing resource should outlive any one Hermes process: the CLI and
cron reach the box too, not just the gateway.

This plugin therefore only starts an in-process instance when the port is
**not** already bound. If the systemd unit owns it, the plugin stays out of the
way. That makes the extension safe to enable either way.
"""

from __future__ import annotations

import logging
import os
import socket
import threading

logger = logging.getLogger(__name__)

PLUGIN_NAME = "aibox-model-queue"
_STARTED = "_aibox_model_queue_started"
_state: dict = {}


def _port_is_free(host: str, port: int) -> bool:
    """True when nothing is listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def _serve_forever(host: str, port: int, upstream: str) -> None:
    import asyncio

    from aiohttp import web

    from . import proxy as proxy_mod

    proxy_mod.UPSTREAM = upstream
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app = loop.run_until_complete(proxy_mod._make_app())
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, host, port).start())
        logger.info("%s: serving %s:%s -> %s", PLUGIN_NAME, host, port, upstream)
        loop.run_forever()
    except Exception:
        logger.exception("%s: proxy thread died", PLUGIN_NAME)


def register(ctx) -> None:
    """Start the queue proxy unless something already owns the port.

    Failures are logged and swallowed. A broken optional plugin must never take
    the gateway down. Without it, requests simply go back to racing, which is
    the pre-existing behaviour.
    """
    try:
        if not ctx.get_config("enabled", True):
            logger.info("%s: disabled by config", PLUGIN_NAME)
            return
        host = str(ctx.get_config("host", "127.0.0.1"))
        port = int(ctx.get_config("port", 8099))
        upstream = str(ctx.get_config(
            "upstream", os.environ.get(
                "MODEL_PROXY_UPSTREAM", "http://10.10.50.122:8080",
            ),
        ))

        if _state.get(_STARTED):
            return
        if not _port_is_free(host, port):
            logger.info(
                "%s: %s:%s already served (systemd unit?); not starting a "
                "second instance", PLUGIN_NAME, host, port,
            )
            _state[_STARTED] = True
            return

        t = threading.Thread(
            target=_serve_forever, args=(host, port, upstream),
            name="aibox-model-queue", daemon=True,
        )
        t.start()
        _state[_STARTED] = True
    except Exception as exc:
        logger.warning("%s: not started (%s)", PLUGIN_NAME, exc)
