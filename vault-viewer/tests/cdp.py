"""A small DevTools-protocol client over Chromium's --remote-debugging-pipe
(fds 3 and 4, NUL-separated JSON), standard library only. Used by e2e_page.py.

The pipe handling follows PDF_RENDER in artifacts.py (posix_spawn with fd
dup2, a buffered NUL reader, send/call), kept as a separate copy so the tests
do not depend on the program's embedded string.

Chromium runs with its own sandbox (never --no-sandbox). Unlike the PDF
renderer, the network is not blocked: the viewer under test is on loopback.
"""
import base64
import fcntl
import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

PDF_RUNTIME_HINT = "install it with: python3 artifacts.py --install-pdf-runtime"


def find_chrome(program="artifacts.py"):
    """E2E_CHROME, else <runtime dir>/<pdf rev>/chrome/chrome-headless-shell.
    Returns (path, None) or (None, message)."""
    env = os.environ.get("E2E_CHROME")
    if env:
        return (env, None) if os.access(env, os.X_OK) else (None, f"E2E_CHROME is not executable: {env}")
    runtime = os.environ.get("ARTIFACTS_PDF_RUNTIME") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "artifacts", "pdf-runtime")
    try:
        rev = subprocess.run(["python3", program, "--pdf-rev"], capture_output=True, text=True,
                             check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        return None, f"cannot read the PDF revision from {program}: {e}"
    path = os.path.join(runtime, rev, "chrome", "chrome-headless-shell")
    if not os.access(path, os.X_OK):
        return None, f"headless shell not found at {path}\n{PDF_RUNTIME_HINT}"
    return path, None


def high(fd):  # off 0-9, so the dup2 onto 3 and 4 never maps an fd onto itself
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved


class Browser:
    def __init__(self, chrome):
        self.profile = tempfile.mkdtemp(prefix="e2e-chrome-")
        flags = [
            "--headless", "--remote-debugging-pipe", "--no-startup-window",
            "--user-data-dir=" + self.profile, "--disable-dev-shm-usage", "--hide-scrollbars", "--mute-audio",
            "--disable-background-networking", "--disable-breakpad", "--disable-component-update",
            "--disable-default-apps", "--disable-extensions", "--disable-sync", "--no-default-browser-check",
            "--no-first-run", "--password-store=basic",
        ]
        cmd_r, self.cmd_w = map(high, os.pipe())
        self.out_r, out_w = map(high, os.pipe())
        null = high(os.open(os.devnull, os.O_RDWR))
        self.buf, self.scanned, self.last_id = bytearray(), 0, 0
        self.replies = {}  # replies read while another call was waiting, by id
        self.queue = []  # events read while waiting for something else
        self.pages = {}  # sessionId -> Page
        self.pid = None
        try:
            self.pid = os.posix_spawn(chrome, [chrome, *flags], os.environ, setsid=True, file_actions=[
                (os.POSIX_SPAWN_DUP2, null, 0), (os.POSIX_SPAWN_DUP2, null, 1), (os.POSIX_SPAWN_DUP2, null, 2),
                (os.POSIX_SPAWN_DUP2, cmd_r, 3), (os.POSIX_SPAWN_DUP2, out_w, 4)])
        finally:
            for fd in (cmd_r, out_w, null):
                os.close(fd)

    # transport
    def read_msg(self, deadline):
        while True:
            end = self.buf.find(b"\0", self.scanned)
            if end >= 0:
                msg = json.loads(self.buf[:end])
                del self.buf[:end + 1]
                self.scanned = 0
                return msg
            self.scanned = len(self.buf)
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.out_r], [], [], left)[0]:
                raise TimeoutError
            chunk = os.read(self.out_r, 1 << 20)
            if not chunk:
                raise RuntimeError("Chromium exited")
            self.buf.extend(chunk)

    def send(self, method, params=None, session=None):
        self.last_id += 1
        msg = {"id": self.last_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        view = memoryview(json.dumps(msg).encode() + b"\0")
        while view:
            view = view[os.write(self.cmd_w, view):]
        return self.last_id

    def handle(self, m):
        if "id" in m:  # a reply, read by pump() or a nested call: keep it for its caller
            self.replies[m["id"]] = m
            return
        page = self.pages.get(m.get("sessionId"))
        if page is not None:
            page.on_event(m)

    def call(self, method, params=None, session=None, timeout=30):
        i, deadline = self.send(method, params, session), time.monotonic() + timeout
        while True:
            # An event handler may make calls of its own while this one waits (an
            # interceptor answering a paused request): a reply read by a nested call
            # is kept in self.replies for the call it belongs to, never dropped.
            m = self.replies.pop(i, None) or self.read_msg(deadline)
            if m.get("id") == i:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
            self.handle(m)

    def pump(self, seconds):
        """Dispatch events for up to SECONDS."""
        deadline = time.monotonic() + seconds
        try:
            while True:
                self.handle(self.read_msg(deadline))
        except TimeoutError:
            pass

    # pages
    def new_page(self, width=1400, height=900):
        """A page in a fresh browser context: an empty cache and storage."""
        ctx = self.call("Target.createBrowserContext")["browserContextId"]
        target = self.call("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
        sid = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        page = Page(self, sid, ctx)
        self.pages[sid] = page
        page.call("Page.enable")
        page.call("Runtime.enable")
        page.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1,
                                                         "mobile": False})
        # frames in other processes arrive as child sessions
        page.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        return page

    def close(self):
        """Kill the browser's process group and remove the profile; safe to repeat."""
        if self.pid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.pid, sig)
                except ProcessLookupError:
                    break
                for _ in range(30):
                    try:
                        if os.waitpid(self.pid, os.WNOHANG)[0]:
                            self.pid = None
                            break
                    except ChildProcessError:
                        self.pid = None
                        break
                    time.sleep(0.1)
                if self.pid is None:
                    break
            self.pid = None
            for fd in (self.cmd_w, self.out_r):
                try:
                    os.close(fd)
                except OSError:
                    pass
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Page:
    def __init__(self, browser, session, context):
        self.b, self.session, self.context = browser, session, context
        self.errors = []  # script exceptions and console errors
        self.children = {}  # child session id -> target info (out-of-process frames)
        self.interceptor = None

    def call(self, method, params=None, session=None, timeout=30):
        return self.b.call(method, params, session or self.session, timeout)

    def on_event(self, m):
        method, p = m.get("method"), m.get("params", {})
        if method == "Runtime.exceptionThrown":
            d = p["exceptionDetails"]
            self.errors.append((d.get("exception") or {}).get("description") or d.get("text", "exception"))
        elif method == "Runtime.consoleAPICalled" and p.get("type") == "error":
            self.errors.append("console.error: " + " ".join(str(a.get("value", a.get("description", "")))
                                                            for a in p.get("args", [])))
        elif method == "Fetch.requestPaused" and self.interceptor:
            self.interceptor(p)
        elif method == "Target.attachedToTarget":
            child = p["sessionId"]
            self.children[child] = p["targetInfo"]
            self.b.pages[child] = self  # route the child's events here too
        elif method == "Target.detachedFromTarget":
            self.children.pop(p["sessionId"], None)

    HOLD = object()

    def intercept(self, pattern, handler):
        """Pause requests matching PATTERN (e.g. "*/api/tree*"). HANDLER(req) returns None to let the
        request through, (status, body) to answer it itself, or Page.HOLD to keep it paused until
        release(). Holding, not sleeping, delays a request: a sleep here would also stall this
        client, so the test would look at the page only after the request had gone through."""
        self.held = []

        def on_paused(p):
            rid = p["requestId"]
            out = handler(p["request"])
            if out is Page.HOLD:
                self.held.append(rid)
            elif out is None:
                self.call("Fetch.continueRequest", {"requestId": rid})
            else:
                status, body = out
                self.call("Fetch.fulfillRequest", {
                    "requestId": rid, "responseCode": status,
                    "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
                    "body": base64.b64encode(body.encode()).decode()})
        self.interceptor = on_paused
        self.call("Fetch.enable", {"patterns": [{"urlPattern": pattern}]})

    def release(self):
        """Let every held request through."""
        for rid in self.held:
            self.call("Fetch.continueRequest", {"requestId": rid})
        self.held = []

    def navigate(self, url):
        r = self.call("Page.navigate", {"url": url})
        if r.get("errorText"):
            raise RuntimeError("navigation failed: " + r["errorText"])

    def eval(self, expr, session=None, context_id=None, gesture=False):
        params = {"expression": expr, "returnByValue": True, "userGesture": gesture}
        if context_id:
            params["contextId"] = context_id
        r = self.call("Runtime.evaluate", params, session)
        if "exceptionDetails" in r:
            raise RuntimeError("evaluate: " + str(r["exceptionDetails"].get("text")))
        return r["result"].get("value")

    def frame_eval(self, expr):
        """EXPR evaluated inside the page's single child iframe (it may be an out-of-process frame).
        Returns None while there is no frame document yet."""
        for sid, info in list(self.children.items()):
            if info.get("type") == "iframe":
                try:
                    return self.eval(expr, session=sid)
                except RuntimeError:
                    return None
        tree = self.call("Page.getFrameTree")["frameTree"]
        for child in tree.get("childFrames", []):
            try:
                world = self.call("Page.createIsolatedWorld", {"frameId": child["frame"]["id"],
                                                               "worldName": "e2e"})["executionContextId"]
                return self.eval(expr, context_id=world)
            except RuntimeError:
                return None
        return None

    def wait(self, cond, timeout=10.0, interval=0.03):
        """Poll COND() (called between event pumps) until it is truthy; returns its value or False."""
        end = time.monotonic() + timeout
        while True:
            self.b.pump(interval)
            try:
                v = cond()
            except RuntimeError:
                v = None
            if v:
                return v
            if time.monotonic() >= end:
                return False

    def close(self):
        try:
            self.b.call("Target.disposeBrowserContext", {"browserContextId": self.context}, timeout=5)
        except (RuntimeError, TimeoutError):
            pass
