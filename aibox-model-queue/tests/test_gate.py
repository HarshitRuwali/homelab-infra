"""Behavioural tests for ModelGate, the whole point of the extension.

Run:  python tests/test_gate.py
Exit 0 = all pass. No pytest dependency so it runs anywhere.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from proxy import ModelGate  # noqa: E402

FAILS = []


def check(cond, label):
    print(("  [ ok ] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


async def _hold(gate, model, order, dur=0.05):
    await gate.acquire(model)
    order.append(("start", model))
    await asyncio.sleep(dur)
    order.append(("end", model))
    await gate.release()


async def t_same_model_is_concurrent():
    gate, order = ModelGate(), []
    await asyncio.gather(*(_hold(gate, "A", order) for _ in range(3)))
    starts = [i for i, (k, _) in enumerate(order) if k == "start"]
    # All three should start before any finishes -> genuine concurrency.
    check(max(starts) < order.index(("end", "A")), "same model runs concurrently")


async def t_different_model_serializes():
    gate, order = ModelGate(), []
    await asyncio.gather(_hold(gate, "A", order), _hold(gate, "B", order))
    # No interleaving: each model's start/end must be adjacent.
    seq = [m for _, m in order]
    check(seq in (["A", "A", "B", "B"], ["B", "B", "A", "A"]),
          f"different models serialize (got {seq})")


async def t_no_downgrade():
    """Every request is served by the model it asked for."""
    gate, served = ModelGate(), []

    async def one(m):
        await gate.acquire(m)
        served.append((m, gate.current))
        await gate.release()

    await asyncio.gather(*(one(m) for m in ("A", "B", "A", "B")))
    check(all(req == got for req, got in served),
          "no request is served by another model")


async def t_fifo_fairness():
    """A waiting B is not starved by a stream of A traffic."""
    gate, order = ModelGate(), []
    await gate.acquire("A")           # A batch holds the box
    b = asyncio.create_task(_hold(gate, "B", order))
    await asyncio.sleep(0.02)          # let B queue up
    late_a = asyncio.create_task(_hold(gate, "A", order))
    await asyncio.sleep(0.02)
    await gate.release()               # first A finishes
    await asyncio.gather(b, late_a)
    check(order and order[0] == ("start", "B"),
          f"queued B goes before a later A (got {order[:1]})")


async def main():
    for t in (t_same_model_is_concurrent, t_different_model_serializes,
              t_no_downgrade, t_fifo_fairness):
        await t()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("all gate tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
