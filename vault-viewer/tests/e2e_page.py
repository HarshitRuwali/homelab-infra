"""Load the viewer page in headless Chromium. Run by tests/e2e.sh inside the
Playwright container, never directly.

    e2e_page.py check BASE_URL            page checks against e2e.sh's scratch vault
    e2e_page.py bench BASE_URL NOTE RUNS  cold-load timings of NOTE, one JSON line per run

Every run opens a fresh browser context, so the browser cache starts empty.
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

FAILS = []
LIST = "document.querySelectorAll('#list .row').length > 0"


def check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label, flush=True)
    if not cond:
        FAILS.append(label)


def title_is(page, text, timeout=10000):
    """Poll from here: the page's CSP has no 'unsafe-eval', which
    wait_for_function needs to pass an argument."""
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        if page.evaluate("document.getElementById('title').textContent") == text:
            return True
        time.sleep(0.05)
    return False


def empty_msg(page):
    return page.evaluate("document.getElementById('empty').textContent")


def timed_load(browser, url, note_label):
    """Times from navigation to first paint, file list, note title and note text."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    t0 = time.monotonic()
    out = {}
    try:
        page.goto(url, wait_until="commit")
        page.wait_for_function(LIST, timeout=30000)
        out["list_ms"] = round((time.monotonic() - t0) * 1000)
        if note_label and title_is(page, note_label, 30000):
            out["title_ms"] = round((time.monotonic() - t0) * 1000)
            page.frame_locator("#frame").locator("body h1, body h2, body p").first.wait_for(timeout=30000)
            out["note_ms"] = round((time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        out["fail"] = type(e).__name__
    out.update(page.evaluate(
        "Object.fromEntries(performance.getEntriesByType('paint').map(e => [e.name, Math.round(e.startTime)]))"))
    out["errors"] = errors
    ctx.close()
    return out


def run_checks(browser, base):
    note = "vault/sub/Deep Note.md"
    print("page loads")
    for name, url in {"home": base + "/",
                      "hash with %2F": base + "/#" + note.replace("/", "%2F").replace(" ", "%20"),
                      "hash with plain slashes": base + "/#vault/sub/Deep%20Note.md"}.items():
        r = timed_load(browser, url, None if name == "home" else "Deep Note")
        ok = "list_ms" in r and (name == "home" or "note_ms" in r) and not r["errors"]
        check(ok, f"{name}: list and note render, no script errors ({json.dumps(r)})")

    print("loading state")
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route("**/api/tree", lambda route: (time.sleep(1.5), route.continue_()))
    page.goto(base + "/", wait_until="domcontentloaded")
    check(empty_msg(page).startswith("Loading the file list"), "the stage says it is loading before the tree arrives")
    page.wait_for_function(LIST, timeout=10000)
    check(title_is(page, "README"), "the home note opens once the tree arrives")
    ctx.close()

    print("failed first load")
    ctx = browser.new_context()
    page = ctx.new_page()
    refused = {"n": 0}

    def refuse_twice(route):
        if refused["n"] < 2:
            refused["n"] += 1
            return route.fulfill(status=403, body="[]", content_type="application/json")
        return route.continue_()

    page.route("**/api/tree", refuse_twice)
    t0 = time.monotonic()
    page.goto(base + "/#" + note.replace("/", "%2F").replace(" ", "%20"), wait_until="domcontentloaded")
    try:
        page.wait_for_function("document.getElementById('empty').textContent.includes('HTTP 403')", timeout=5000)
        shown = True
    except Exception:  # noqa: BLE001
        shown = False
    check(shown, "a refused tree request is shown on the page with its status")
    recovered = title_is(page, "Deep Note", 8000)
    check(recovered and time.monotonic() - t0 < 8,
          f"the page retries on its own and opens the note (after {time.monotonic() - t0:.1f} s, poll is 10 s)")
    ctx.close()


def main():
    mode, base = sys.argv[1], sys.argv[2].rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        if mode == "check":
            run_checks(browser, base)
        else:
            note, runs = sys.argv[3], int(sys.argv[4])
            label = note.rsplit("/", 1)[-1].removesuffix(".md")
            for form, url in {"encoded-slashes": base + "/#" + note.replace("/", "%2F"),
                              "plain-slashes": base + "/#" + note}.items():
                for run in range(runs):
                    print(json.dumps({"url": form, "run": run, **timed_load(browser, url, label)}), flush=True)
        browser.close()
    if FAILS:
        print(f"{len(FAILS)} check(s) failed")
        return 1
    if mode == "check":
        print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
