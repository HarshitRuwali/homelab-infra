"""A request cancelled while releasing must still give its slot back.

release() used to decrement `active` only after taking the condition lock. A
handler cancelled while waiting for that lock (aiohttp cancels it when the
client hangs up) skipped the decrement, so the slot leaked forever. Observed
2026-09-25: active=20 with 4 live clients, and `current` pinned to one model,
which would deadlock any request for a different model.

Run:  python tests/test_release_cancel.py
Exit 0 = all pass. No pytest dependency, same as test_gate.py.
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


async def t_cancel_during_release():
    gate = ModelGate()
    await gate.acquire("A")
    check(gate.active == 1, "one request holds the box")

    # Someone else holds the lock, so release() has to wait for it.
    await gate._cond.acquire()
    rel = asyncio.create_task(gate.release())
    await asyncio.sleep(0.05)
    rel.cancel()  # the client hung up mid-release
    await asyncio.sleep(0.05)
    gate._cond.release()
    await asyncio.sleep(0.05)

    check(gate.active == 0, f"slot returned despite the cancel (active={gate.active})")
    check(gate.current is None, f"box no longer pinned to A (current={gate.current})")

    # The practical consequence: a different model must still get in.
    try:
        await asyncio.wait_for(gate.acquire("B"), 1)
        check(gate.current == "B", "a request for another model is admitted")
        await gate.release()
    except asyncio.TimeoutError:
        check(False, "a request for another model is admitted")


if __name__ == "__main__":
    asyncio.run(t_cancel_during_release())
    if FAILS:
        print(f"\n{len(FAILS)} FAILED")
        sys.exit(1)
    print("\nall release-cancel tests passed")
