"""Load the viewer page in Chromium's headless shell over the DevTools pipe.
Run by tests/e2e.sh, not directly. Standard library only, no docker.

    e2e_page.py check BASE_URL            page checks against e2e.sh's scratch vault
    e2e_page.py bench BASE_URL NOTE RUNS  cold-load timings of NOTE, one JSON line per run

Every page opens in a fresh browser context, so the browser cache starts empty.
"""
import json
import sys
import time

import cdp

FAILS = []
LIST = "document.querySelectorAll('#list .row').length > 0"
FRAME_TEXT = "!!document.querySelector('body h1, body h2, body p')"
PAINT = "Object.fromEntries(performance.getEntriesByType('paint').map(e => [e.name, Math.round(e.startTime)]))"


def check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label, flush=True)
    if not cond:
        FAILS.append(label)


def title_is(page, text, timeout=10.0):
    return page.wait(lambda: page.eval("document.getElementById('title').textContent") == text, timeout)


def empty_msg(page):
    return page.eval("document.getElementById('empty').textContent")


def timed_load(browser, url, note_label):
    """Times from navigation to first paint, file list, note title and note text."""
    page = browser.new_page()
    out = {}
    try:
        t0 = time.monotonic()
        page.navigate(url)

        def since():
            return round((time.monotonic() - t0) * 1000)

        if page.wait(lambda: page.eval(LIST), 30):
            out["list_ms"] = since()
            if note_label and title_is(page, note_label, 30):
                out["title_ms"] = since()
                if page.wait(lambda: page.frame_eval(FRAME_TEXT), 30):
                    out["note_ms"] = since()
        else:
            out["fail"] = "no file list"
        out.update(page.eval(PAINT) or {})
        browser.pump(0.1)  # collect late script errors
        out["errors"] = list(page.errors)
    finally:
        page.close()
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
    page = browser.new_page()
    page.intercept("*/api/tree*", lambda req: page.HOLD)
    page.navigate(base + "/")
    page.wait(lambda: page.held, 5)   # the tree request is paused in the browser
    msg = empty_msg(page)
    check(msg.startswith("Loading the file list"), f"the stage says it is loading before the tree arrives ({msg!r})")
    page.release()
    check(page.wait(lambda: page.eval(LIST), 10) and title_is(page, "README"),
          "the home note opens once the tree arrives")
    page.close()

    print("failed first load")
    page = browser.new_page()
    refused = {"n": 0}

    def refuse_twice(req):
        if refused["n"] < 2:
            refused["n"] += 1
            return 403, "[]"
        return None

    page.intercept("*/api/tree*", refuse_twice)
    t0 = time.monotonic()
    page.navigate(base + "/#" + note.replace("/", "%2F").replace(" ", "%20"))
    shown = page.wait(lambda: "HTTP 403" in page.eval("document.getElementById('empty').textContent"), 5)
    check(shown, "a refused tree request is shown on the page with its status")
    recovered = title_is(page, "Deep Note", 8)
    check(recovered and time.monotonic() - t0 < 8,
          f"the page retries on its own and opens the note (after {time.monotonic() - t0:.1f} s, poll is 10 s)")
    page.close()

    run_obsidian_checks(browser, base)


def obsbar(page):
    return page.eval("document.getElementById('obsbar').classList.contains('hidden') ? '' : "
                     "document.getElementById('obsbar').textContent")


def run_obsidian_checks(browser, base):
    """The page cannot launch the app in the test browser (no handler for obsidian:), so every
    attempt must fall back to this page; "the app opened" is simulated with a blur event."""
    deep = base + "/#vault%2Fsub%2FDeep%20Note.md"
    print("obsidian")
    page = browser.new_page()
    page.navigate(deep)
    title_is(page, "Deep Note")
    href = page.eval("document.getElementById('obs').href")
    check(href == "obsidian://open?vault=vault&file=sub%2FDeep%20Note.md", f"the Obsidian button links the note ({href})")
    check(page.eval("document.getElementById('obsmode').textContent") == "Off", "the device setting starts off")
    page.eval("localStorage.setItem('vault-obsidian-names', JSON.stringify({vault: 'My Vault'})); updateObs()")
    href = page.eval("document.getElementById('obs').href")
    check(href == "obsidian://open?vault=My%20Vault&file=sub%2FDeep%20Note.md", f"a per-device vault name is used ({href})")
    page.eval("localStorage.removeItem('vault-obsidian-names'); updateObs()")

    # The button opened the app (the page lost focus): the page offers to remember it.
    page.eval("document.getElementById('obs').click(); setTimeout(() => dispatchEvent(new Event('blur')), 50)", gesture=True)
    offer = page.wait(lambda: "from now on" in obsbar(page), 3)
    check(offer, f"after the app opened, the page offers to open links there ({obsbar(page)!r})")
    page.eval("[...document.querySelectorAll('#obsbar button')].find(b => b.textContent === 'Yes').click()", gesture=True)
    check(page.eval("localStorage.getItem('vault-obsidian')") == "on"
          and page.eval("document.getElementById('obsmode').textContent") == "On", "Yes turns the setting on")
    # The button did not open the app: it says so.
    page.eval("document.getElementById('obs').click()", gesture=True)
    check(page.wait(lambda: "did not open" in obsbar(page), 6), "a button press that opens nothing says so")

    # With the setting on, a link opens the app; here nothing opens, so the page stays and says so.
    page.navigate("about:blank")
    page.navigate(deep)
    check(title_is(page, "Deep Note"), "with the setting on, the linked note still renders here")
    shown = page.wait(lambda: "did not open" in obsbar(page), 6)
    check(shown and page.eval("location.protocol") == "http:" and "Stop trying" in obsbar(page),
          f"the attempt falls back to this page ({obsbar(page)!r})")
    # A reload of the same link does not try again.
    page.call("Page.reload")
    title_is(page, "Deep Note")
    page.b.pump(5)
    check(obsbar(page) == "", "reloading the same link does not try again")
    # A file changed in the last 10 minutes stays here: the device may not have it yet.
    page.navigate("about:blank")
    page.navigate(base + "/#vault%2Fsub%2FFresh.md")
    title_is(page, "Fresh")
    check(page.wait(lambda: "may not have reached" in obsbar(page), 3), "a just-changed file stays here, with a note why")
    # Artifacts have no Obsidian button and are never sent there.
    page.navigate("about:blank")
    page.navigate(base + "/#vault%2Fart.html")
    title_is(page, "art.html")
    page.b.pump(5)
    check(page.eval("document.getElementById('obs').classList.contains('hidden')") and obsbar(page) == "",
          "an HTML artifact has no Obsidian button and stays here")
    page.close()


def main():
    mode, base = sys.argv[1], sys.argv[2].rstrip("/")
    chrome, problem = cdp.find_chrome()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    with cdp.Browser(chrome) as browser:
        if mode == "check":
            run_checks(browser, base)
        else:
            note, runs = sys.argv[3], int(sys.argv[4])
            label = note.rsplit("/", 1)[-1].removesuffix(".md")
            for form, url in {"encoded-slashes": base + "/#" + note.replace("/", "%2F"),
                              "plain-slashes": base + "/#" + note}.items():
                for run in range(runs):
                    print(json.dumps({"url": form, "run": run, **timed_load(browser, url, label)}), flush=True)
    if FAILS:
        print(f"{len(FAILS)} check(s) failed")
        return 1
    if mode == "check":
        print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
