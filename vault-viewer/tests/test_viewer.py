"""Behavioural tests for the vault viewer (artifacts.py).

Run from vault-viewer/:  python3 tests/test_viewer.py
Exit 0 = all pass. Needs python3-markdown; no pytest. ARTIFACTS_PY=/path/to/copy.py
points the tests at another copy of the program (used to mutation-test the checks).
The PDF checks that need Chromium run only when ARTIFACTS_PDF_RUNTIME names a directory with an installed
runtime for this program's PDF_REV (python3 artifacts.py --install-pdf-runtime --pdf-runtime DIR, DIR ending in
artifacts/pdf-runtime so the AppArmor profile applies); the rest needs no browser and no network.
"""
import base64
import contextlib
import email.utils
import http.client
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import re
import hashlib
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import zipfile
import zlib

HERE = pathlib.Path(__file__).resolve().parent.parent
PROGRAM = pathlib.Path(os.environ.get("ARTIFACTS_PY", HERE / "artifacts.py"))

FAILS = []


def check(cond, label):
    print(("  [ ok ] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def skip(label):
    print(f"  [skip] {label}")


def load_program(path):
    loader = importlib.machinery.SourceFileLoader("artifacts_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


A = load_program(PROGRAM)

# 1x1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
MERMAID_NOTE = "# Diagram\n\n```mermaid\ngraph TD\n  A-->B\n```\n"
UNICODE_NAME = "Ünï café.md"
# An empty zip archive: just the end-of-central-directory record.
ZIP = b"PK\x05\x06" + bytes(18)


def build_vault(base):
    """vault/ (the served root) and outside/ (a secret the vault must not leak)."""
    vault, outside = base / "vault", base / "outside"
    (vault / "sub").mkdir(parents=True)
    (vault / ".hidden").mkdir()
    outside.mkdir()
    (vault / "Note.md").write_text(
        "---\ntitle: Hello\ntags: [a, b]\n---\n# Note\n\nSee [[Other]] and [[Other|the alias]].\n\n"
        "![[pic.png]]\n\n![rel](sub/pic2.png)\n\n![[leak.png]]\n\n![rel leak](leak.png)\n")
    (vault / "Other.md").write_text("# Other\n\nBack to [[Note]].\n")
    (vault / "Inline.md").write_text('# Inline\n\n<img src="Other.md" alt="x">\n')
    (vault / "Mermaid.md").write_text(MERMAID_NOTE)
    (vault / UNICODE_NAME).write_text(MERMAID_NOTE + "\nBody text with `inline code`.\n")
    (vault / "pic.png").write_bytes(PNG)
    (vault / "sub" / "pic2.png").write_bytes(PNG)
    (vault / "code.py").write_text("print('hi')\n")
    (vault / "Ünï export.zip").write_bytes(ZIP)
    (vault / "notes.xyz").write_text("unknown type\n")
    (vault / ".hidden" / "secret.md").write_text("# secret\n")
    (outside / "secret.png").write_bytes(b"\x89PNG-outside-secret")
    (outside / "secret.md").write_text("# outside\n")
    (vault / "leak.png").symlink_to(outside / "secret.png")
    (vault / "leak.md").symlink_to(outside / "secret.md")
    (outside / "secret.zip").write_bytes(ZIP)
    (vault / "leak.zip").symlink_to(outside / "secret.zip")
    return vault


def t_resolve(lib):
    print("Library.resolve")
    check(lib.resolve("vault/Note.md") == lib.roots[0] / "Note.md", "normal note resolves to its path")
    check(lib.resolve("vault/sub/pic2.png") is not None, "file in a subfolder resolves")
    check(lib.resolve("vault/../outside/secret.md") is None, ".. traversal -> None")
    check(lib.resolve("vault/sub/../../outside/secret.md") is None, "nested .. traversal -> None")
    check(lib.resolve("vault/.hidden/secret.md") is None, "hidden folder -> None")
    check(lib.resolve("vault/notes.xyz") is None, "unknown extension -> None")
    check(lib.resolve("vault/Ünï export.zip") is not None, "zip archive resolves")
    check(lib.resolve("vault/leak.zip") is None, "symlinked zip outside the root -> None")
    check({i["name"]: i["kind"] for i in lib.scan()}.get("Ünï export.zip") == "archive", "scan lists a zip as kind archive")
    check(lib.resolve("vault/leak.png") is None, "symlink to a file outside the root -> None")
    check(lib.resolve("vault/leak.md") is None, "symlinked note outside the root -> None")
    check(lib.resolve("vault//etc/passwd") is None, "absolute path under the root name -> None")
    check(lib.resolve("other/Note.md") is None, "unknown root name -> None")
    check(lib.resolve("vault") is None and lib.resolve("vault/") is None, "root itself -> None")
    check(lib.resolve("vault/missing.md") is None, "missing file -> None")


def render(lib, name, pdf):
    ident = "vault/" + name
    return A.render_note(lib, lib.resolve(ident), ident, pdf=pdf)


def t_render(lib):
    print("render_note")
    viewer = render(lib, "Note.md", pdf=False)
    check('class="props"' in viewer, "viewer: properties box kept")
    check('class="backlinks"' in render(lib, "Other.md", pdf=False), "viewer: backlinks kept")
    check('data-id="vault/Other.md"' in viewer, "viewer: wikilinks carry data-id")
    check('href="#vault/Other.md"' in viewer, "viewer: wikilinks carry an href")
    check('src="/raw/vault/pic.png' in viewer, "viewer: embeds point at /raw/")

    pdf = render(lib, "Note.md", pdf=True)
    check('class="props"' not in pdf, "pdf: no properties box")
    check('class="backlinks"' not in render(lib, "Other.md", pdf=True), "pdf: no backlinks")
    check("data-id=" not in pdf, "pdf: no data-id anywhere")
    check('href="#vault' not in pdf, "pdf: no vault href")
    check(len(re.findall(r'<a class="internal"\s*>', pdf)) >= 2, "pdf: wikilinks are plain <a class=\"internal\">")
    check("the alias" in pdf, "pdf: wikilink alias kept as text")
    check("data:image/png;base64," + base64.b64encode(PNG).decode() in pdf, "pdf: embedded .png inlined as a data: URL")
    check(pdf.count("data:image/png;base64,") >= 2, "pdf: markdown image inlined too")
    check("/raw/" not in pdf, "pdf: no /raw/ URL left")

    try:
        leak = render(lib, "Note.md", pdf=True)
        raised = False
    except Exception as e:  # noqa: BLE001
        leak, raised = "", repr(e)
    check(not raised, f"pdf: symlinked outside image does not raise ({raised or 'ok'})")
    secret = base64.b64encode(b"\x89PNG-outside-secret").decode()
    check(secret not in leak, "pdf: symlinked outside image is not inlined")
    check('alt="leak.png"' in leak and re.search(r'src="" alt="leak.png"', leak) is not None,
          "pdf: symlinked outside image has an empty src")

    inline = render(lib, "Inline.md", pdf=True)
    check('<img src="" alt="x">' in inline, 'pdf: <img src="Other.md"> (a note) is not inlined, src is empty')
    check("base64" not in inline, "pdf: nothing inlined for a non-image src")

    print("copy buttons")
    check("addCopyButtons();" in viewer and viewer.index("hljs.highlightElement") < viewer.index("addCopyButtons();"),
          "viewer: code blocks get copy buttons, after highlighting")
    check("addCopyButtons" not in pdf and "codewrap" not in pdf, "pdf: no copy buttons")
    check("addCopyButtons();" in A.render_text(lib.resolve("vault/code.py"), "vault/code.py"),
          "viewer: text files get a copy button too")
    check("type: 'vault-copy', text" in A.NOTE_JS and "'vault-copied'" in A.NOTE_JS and "e.source === parent" in A.NOTE_JS,
          "note: falls back to asking the viewer, and only trusts the reply from its parent")
    check('d.type === "vault-copy" && typeof d.text === "string" && d.text.length < 5e6' in A.PAGE
          and 'e.source !== $("frame").contentWindow' in A.PAGE,
          "viewer: copies only text, bounded, and only for its own frame")
    check(".copy { display: none; }" in A.NOTE_CSS, "print: copy buttons hidden")

    print("viewer page states (driven in a browser by tests/e2e.sh)")
    check('id="emptymsg">Loading the file list' in A.PAGE, "viewer: the stage says it is loading until the tree arrives")
    check('if (!r.ok) throw new Error("HTTP " + r.status);' in A.PAGE
          and "retryTimer = setTimeout(refresh, 2000)" in A.PAGE,
          "viewer: a failed first tree load is shown with its status and retried after 2 s")
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        skip("JavaScript syntax check (needs node)")
    else:
        scripts = {"note page": re.findall(r"<script>(.*?)</script>", viewer, re.S),
                   "viewer page": re.findall(r"<script>(.*?)</script>", A.PAGE, re.S)}
        for name, parts in scripts.items():
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write("\n;\n".join(parts))
            out = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
            os.unlink(f.name)
            check(parts and out.returncode == 0, f"{name}: inline scripts parse as JavaScript {out.stderr.strip()[:200]}")

    inline_fn = A.inliner(lib, {i["id"]: i for i in lib.scan()})
    check(inline_fn("vault/code.py") == "", "inline() of a text file is empty")
    check(inline_fn("vault/nope.png") == "", "inline() of an unknown id is empty")


def t_mermaid(lib):
    print("Mermaid")
    viewer = render(lib, "Mermaid.md", pdf=False)
    check("cdn.jsdelivr.net/npm/mermaid@11" in viewer, "viewer: Mermaid loads from the CDN")
    check("useMaxWidth: false" in viewer and "useMaxWidth: true" not in viewer, "viewer: useMaxWidth false")
    check("/lib/mermaid" not in viewer, "viewer: no PDF-image library path")

    pdf = render(lib, "Mermaid.md", pdf=True)
    check("/lib/mermaid/dist/mermaid.esm.min.mjs" in pdf, "pdf: Mermaid loads from the PDF image")
    check("useMaxWidth: true" in pdf and "useMaxWidth: false" not in pdf, "pdf: useMaxWidth true")
    check("cdn.jsdelivr.net" not in pdf, "pdf: no CDN reference")
    head, _, body = pdf.partition("<body>")
    check('dataset.ready = "1"' in head, "pdf mermaid: the head module sets dataset.ready")
    check("dataset.ready" not in body, "pdf mermaid: the body does not set ready early")

    plain = render(lib, "Other.md", pdf=True)
    check("dataset.ready" in plain.partition("<body>")[2], "pdf without Mermaid: ready is set inline")
    check('type="module"' not in plain, "pdf without Mermaid: no module script")


class Client:
    def __init__(self, port):
        self.port = port

    def get(self, path, method="GET", timeout=60, **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            c.request(method, path, headers=headers)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()


def t_http(lib):
    print("HTTP guards")
    A.Handler.log_message = lambda self, fmt, *args: None
    srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    port = srv.server_address[1]
    A.Handler.hosts = {f"127.0.0.1:{port}"}
    A.Handler.library = lib
    A.Handler.gate = A.TailnetGate(None)
    A.Handler.pdf_runtime = pathlib.Path(tempfile.gettempdir()) / "artifacts-no-such-runtime" / "artifacts" / "pdf-runtime"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(port)
    note = "/pdf/" + urllib.parse.quote("vault/Note.md")
    try:
        check(c.get("/", Host="evil.example")[0] == 403, "wrong Host on / -> 403")
        check(c.get(note, Host="evil.example")[0] == 403, "wrong Host on /pdf/ -> 403")
        check(c.get("/raw/vault/Note.md", Host=f"localhost:{port}")[0] == 403, "unlisted Host name on /raw/ -> 403")
        st, _, body = c.get("/")
        check(st == 200 and b'id="pdf"' in body, "index / is 200 and has the PDF button")
        check(c.get("/api/tree", **{"Sec-Fetch-Site": "cross-site"})[0] == 403, "/api/ cross-site -> 403")
        check(c.get("/api/tree", **{"Sec-Fetch-Site": "same-origin"})[0] == 200, "/api/ same-origin -> 200")

        check(c.get(note, **{"Sec-Fetch-Site": "cross-site"})[0] == 403, "/pdf/ Sec-Fetch-Site cross-site -> 403")
        check(c.get(note, **{"Sec-Fetch-Site": "same-site"})[0] == 403, "/pdf/ Sec-Fetch-Site same-site -> 403")
        check(c.get(note, method="HEAD")[0] == 405, "HEAD /pdf/<note> -> 405")
        check(c.get("/pdf/vault/pic.png")[0] == 404, "/pdf/<png> -> 404")
        check(c.get("/pdf/vault/%2e%2e/outside/secret.md")[0] == 404, "/pdf/ traversal -> 404")
        check(c.get("/pdf/vault/.hidden/secret.md")[0] == 404, "/pdf/ hidden file -> 404")
        check(c.get("/pdf/vault/leak.md")[0] == 404, "/pdf/ symlink outside the vault -> 404")
        check(c.get("/raw/vault/leak.png")[0] == 404, "/raw/ symlink outside the vault -> 404")
        check(c.get("/raw/vault/leak.zip")[0] == 404, "/raw/ zip symlink outside the vault -> 404")
        check(c.get("/pdf/" + urllib.parse.quote("vault/Ünï export.zip"))[0] == 404, "/pdf/<zip> -> 404")

        st, _, body = c.get(note)
        check(st == 503 and b"--install-pdf-runtime" in body,
              "/pdf/<note> without an installed runtime -> 503 naming --install-pdf-runtime")

        for prefix in ("/raw/", "/view/"):
            st, _, body = c.get(prefix + "vault/Note.md")
            check(st == 200, f"{prefix}<note> -> 200")
        st, hdr, body = c.get("/view/vault/Note.md")
        check("sandbox" in hdr.get("Content-Security-Policy", ""), "/view/<note> carries the sandbox CSP")
        for prefix in ("/raw/", "/view/"):
            st, hdr, body = c.get(prefix + urllib.parse.quote("vault/Ünï export.zip"))
            check(st == 200 and body == ZIP, f"{prefix}<zip> -> 200 with the file bytes")
            check(hdr.get("Content-Type") == "application/zip", f"{prefix}<zip> is application/zip")
            want = "filename*=UTF-8''" + urllib.parse.quote("Ünï export.zip")
            check(hdr.get("Content-Disposition", "").startswith("attachment;") and want in hdr["Content-Disposition"],
                  f"{prefix}<zip> is an attachment with the UTF-8 file name")
            check("sandbox" in hdr.get("Content-Security-Policy", ""), f"{prefix}<zip> carries the sandbox CSP")
        st, _, body = c.get("/")
        check(b'id="dllink"' in body, "index / has the archive download pane")

        print("end-to-end PDF")
        if real_runtime() is None:
            skip(NO_RUNTIME)
        else:
            A.Handler.pdf_runtime = real_runtime()
            ident = "/pdf/" + urllib.parse.quote("vault/" + UNICODE_NAME)
            st, hdr, body = c.get(ident, timeout=180)
            check(st == 200, f"GET /pdf/<mermaid note> -> 200 (got {st})")
            check(hdr.get("Content-Type") == "application/pdf", "Content-Type is application/pdf")
            check(body.startswith(b"%PDF"), "body starts with %PDF")
            want = "filename*=UTF-8''" + urllib.parse.quote(UNICODE_NAME[:-3]) + ".pdf"
            check(want in hdr.get("Content-Disposition", ""), "Content-Disposition carries the UTF-8 file name")
            check(b"/Subtype /Type3" not in body and b"/Subtype /CIDFontType2" in body,
                  "PDF text is in embedded TrueType fonts, no Type 3 (readers select and copy Type 3 text badly)")
    finally:
        srv.shutdown()
        srv.server_close()


def t_perf(base):
    print("cached scan, ETag/304, log suppression, file caching")
    vault = base / "perfvault"
    vault.mkdir()
    (vault / "a.md").write_text("# a\n\nfindme-alpha\n")
    (vault / "pic.png").write_bytes(PNG)
    (vault / "big.zip").write_bytes(ZIP + bytes(1 << 20))
    lib = A.Library([vault], ttl=3600)
    ids = lambda: [i["id"] for i in lib.scan()]  # noqa: E731
    check(ids() == ["perfvault/a.md", "perfvault/big.zip", "perfvault/pic.png"], "scan lists the vault")
    (vault / "new.md").write_text("# new\n")
    check("perfvault/new.md" not in ids(), "scan is cached within the TTL")
    lib.ttl = 0
    check("perfvault/new.md" in ids(), "a new file appears once the TTL has passed")
    (vault / "new.md").unlink()
    check("perfvault/new.md" not in ids(), "a deleted file disappears")
    old = {i["id"]: i["mtime"] for i in lib.scan()}["perfvault/a.md"]
    os.utime(vault / "a.md", (old + 50, old + 50))
    check({i["id"]: i["mtime"] for i in lib.scan()}["perfvault/a.md"] == old + 50, "an edit changes the mtime in the scan")
    check(lib.snapshot()[2].startswith('"'), "the tree ETag is a quoted strong validator")
    e1 = lib.snapshot()[2]
    os.utime(vault / "a.md", (old + 99, old + 99))
    check(lib.snapshot()[2] != e1, "the ETag changes when a file changes")
    check(lib.snapshot()[2] == lib.snapshot()[2], "the ETag is stable when nothing changed")
    (vault / "sub").mkdir()
    (vault / "sub" / "deep.md").write_text("# deep\n")
    (vault / ".dot").mkdir()
    (vault / ".dot" / "x.md").write_text("# x\n")
    check("perfvault/sub/deep.md" in ids() and not any(".dot" in i for i in ids()), "subfolders scanned, hidden folders skipped")

    check([h["id"] for h in A.search(lib, "findme-alpha")] == ["perfvault/a.md"], "search finds a note")
    (vault / "a.md").write_text("# a\n\nfindme-beta changed\n")
    check(A.search(lib, "findme-beta") != [] and A.search(lib, "findme-alpha") == [],
          "search sees an edited file (cache keyed by mtime and size)")
    lib.TEXT_CACHE_CHARS = 100
    A.search(lib, "zzzz")
    check(lib._texts_chars <= 100, "the search text cache stays within its bound")
    lib.TEXT_CACHE_CHARS = 48 << 20
    lib.ttl = 3600

    logged = []
    A.Handler.log_message = lambda self, fmt, *args: logged.append(fmt % args)
    srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    port = srv.server_address[1]
    A.Handler.hosts = {f"127.0.0.1:{port}"}
    A.Handler.library = lib
    A.Handler.gate = A.TailnetGate(None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(port)
    try:
        st, hdr, body = c.get("/api/tree")
        etag = hdr.get("ETag", "")
        check(st == 200 and etag.startswith('"') and not etag.startswith("W/"), "/api/tree sends a strong ETag")
        check(hdr.get("Cache-Control") == "private, no-cache", "/api/tree is private, no-cache")
        import json as _json
        check([i["id"] for i in _json.loads(body)["items"]] == ids(), "/api/tree body lists the scan")
        st, hdr, body = c.get("/api/tree", **{"If-None-Match": etag})
        check(st == 304 and body == b"" and hdr.get("ETag") == etag, "If-None-Match with the current ETag -> 304, no body")
        check(c.get("/api/tree", **{"If-None-Match": "W/" + etag})[0] == 304, "a weak form of the ETag also matches")
        check(c.get("/api/tree", **{"If-None-Match": '"stale"'})[0] == 200, "a stale ETag -> 200")
        check(c.get("/api/tree", Host="evil.example", **{"If-None-Match": etag})[0] == 403, "wrong Host is refused before any 304")
        check(c.get("/api/tree", **{"If-None-Match": etag, "Sec-Fetch-Site": "cross-site"})[0] == 403,
              "cross-site is refused before any 304")
        check(c.get("/api/tree", **{"If-Modified-Since": email.utils.formatdate(time.time() + 3600, usegmt=True)})[0] == 200,
              "/api/tree ignores If-Modified-Since (only If-None-Match)")
        A.Handler.title = "other-title"
        st, hdr, body = c.get("/api/tree", **{"If-None-Match": etag})
        check(st == 200 and hdr["ETag"] != etag and b"other-title" in body, "a different title changes the tree ETag")
        A.Handler.title = None
        lib.ttl = 0
        (vault / "later.md").write_text("# later\n")
        st, hdr, body = c.get("/api/tree", **{"If-None-Match": etag})
        check(st == 200 and b"later.md" in body and hdr["ETag"] != etag, "a new file changes the ETag and returns 200")
        lib.ttl = 3600
        etag = c.get("/api/tree")[1]["ETag"]
        check(c.get("/api/tree", **{"If-None-Match": etag})[0] == 304, "the tree ETag matches again after the change")
        check(c.get("/api/tree", **{"If-Modified-Since": email.utils.formatdate(time.time() + 3600, usegmt=True)})[0] == 200,
              "an IMS-only request after a vault change gets 200")

        st, hdr, body = c.get("/raw/perfvault/pic.png")
        check(st == 200 and body == PNG, "/raw/<png> serves the bytes")
        check(hdr.get("ETag", "").startswith('"') and "Last-Modified" in hdr and hdr.get("Cache-Control") == "private, no-cache",
              "/raw/<png> has ETag, Last-Modified and Cache-Control private")
        st, h2, body = c.get("/raw/perfvault/pic.png", **{"If-None-Match": hdr["ETag"]})
        check(st == 304 and body == b"", "/raw/<png> If-None-Match -> 304")
        st, _, body = c.get("/raw/perfvault/pic.png", **{"If-Modified-Since": hdr["Last-Modified"]})
        check(st == 304, "/raw/<png> If-Modified-Since -> 304")
        st, hdr, body = c.get("/raw/perfvault/big.zip")
        check(st == 200 and len(body) == (1 << 20) + len(ZIP) and hdr.get("Content-Length") == str(len(body)),
              "a zip larger than one chunk streams in full")
        st, hdr, body = c.get("/raw/perfvault/big.zip", method="HEAD")
        check(st == 200 and body == b"" and int(hdr["Content-Length"]) > 0, "HEAD on a file sends headers only")
        check(c.get("/raw/perfvault/missing.png")[0] == 404, "a missing file -> 404")

        logged.clear()
        c.get("/api/tree")
        c.get("/api/tree", **{"If-None-Match": etag})
        c.get("/api/search?q=findme")
        c.get("/raw/perfvault/pic.png", **{"If-None-Match": '"x"'})
        c.get("/raw/perfvault/pic.png", **{"If-None-Match": "*"})
        c.get("/api/nothing")
        c.get("/raw/perfvault/missing.png")
        check(not any("/api/tree" in m or "/api/search" in m for m in logged), "successful /api/ requests are not logged")
        check(not any(" 304 " in m for m in logged), "304 responses are not logged")
        check(any("/raw/perfvault/pic.png" in m and " 200 " in m for m in logged), "non-API requests are logged")
        check(any("/api/nothing" in m and " 404 " in m for m in logged), "API errors are logged")
        check(any("missing.png" in m and " 404 " in m for m in logged), "404s are logged")
    finally:
        srv.shutdown()
        srv.server_close()


def NO_SANDBOX(render):
    """The renderer with Chromium's sandbox off: tests that run a runtime outside the one path the AppArmor profile admits."""
    assert '"--headless",' in render
    return render.replace('"--headless",', '"--headless", "--no-sandbox",', 1)


def glob_homes():
    return [str(p) for p in pathlib.Path(tempfile.gettempdir()).glob("artifacts-pdf-*")]


def real_runtime():
    """The installed PDF runtime for this program's PDF_REV (env ARTIFACTS_PDF_RUNTIME), or None."""
    base = os.environ.get("ARTIFACTS_PDF_RUNTIME")
    if base and (pathlib.Path(base) / A.PDF_REV / "REV").is_file():
        return pathlib.Path(base)
    return None


NO_RUNTIME = ("no PDF runtime for this revision: set ARTIFACTS_PDF_RUNTIME to a directory holding "
              "<PDF_REV>/REV (python3 artifacts.py --install-pdf-runtime --pdf-runtime .../artifacts/pdf-runtime)")


def load_variant(source, name):
    """The program loaded from altered source text (never registered over A)."""
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader=None))
    exec(compile(source, name, "exec"), mod.__dict__)
    return mod


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def zip_bytes(members):
    """members: name -> bytes | (bytes, unix mode)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            data, mode = data if isinstance(data, tuple) else (data, 0o644)
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100000 | mode) << 16
            z.writestr(info, data)
    return buf.getvalue()


def tgz_bytes(members, extra=()):
    """members: name -> bytes; extra: ready TarInfo objects (links) appended."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        for info in extra:
            t.addfile(info)
    return buf.getvalue()


# What the installer must keep from the fixture archives (the layout the pins' rules expect),
# next to members it must drop.
CH = "chrome-headless-shell-linux64/"
FIXTURE_KEPT = {
    "chrome/chrome-headless-shell", "chrome/locales/en-US.pak", "chrome/libfoo.so", "chrome/resources.pak",
    "fonts/inter/Inter-Regular.ttf", "fonts/inter/Inter-SemiBoldItalic.ttf",
    "fonts/jetbrains-mono/JetBrainsMono-Bold.ttf",
    "fonts/dejavu/DejaVuSans.ttf", "fonts/dejavu/DejaVuSerif-Bold.ttf",
    "fonts/noto/NotoColorEmoji.ttf",
    "lib/mermaid/dist/mermaid.esm.min.mjs", "lib/mermaid/dist/chunks/mermaid.esm.min/a.mjs",
    "lib/@mermaid-js/layout-elk/dist/mermaid-layout-elk.esm.min.mjs",
    "lib/@mermaid-js/layout-elk/dist/chunks/mermaid-layout-elk.esm.min/sub/b.mjs",
    "lib/@highlightjs/cdn-assets/highlight.min.js", "lib/@highlightjs/cdn-assets/styles/github.min.css",
}


def fixture_downloads():
    """pin name -> archive bytes, small stand-ins laid out as the real downloads are."""
    return {
        "chrome": zip_bytes({
            CH + "chrome-headless-shell": (b"#!/bin/sh\n", 0o755), CH + "locales/en-US.pak": b"en",
            CH + "locales/de.pak": b"de", CH + "libfoo.so": b"foo", CH + "resources.pak": b"res",
            CH + "libvk_swiftshader.so": b"x", CH + "libvulkan.so.1": b"x", CH + "vk_swiftshader_icd.json": b"{}"}),
        "inter": zip_bytes({
            "extras/ttf/Inter-Regular.ttf": b"i", "extras/ttf/Inter-SemiBoldItalic.ttf": b"i",
            "extras/ttf/Inter-Black.ttf": b"x", "extras/otf/Inter-Regular.otf": b"x", "LICENSE.txt": b"x"}),
        "jetbrains-mono": zip_bytes({"fonts/ttf/JetBrainsMono-Bold.ttf": b"j", "fonts/ttf/JetBrainsMono-Thin.ttf": b"x",
                                     "fonts/webfonts/JetBrainsMono-Bold.woff2": b"x"}),
        "dejavu": zip_bytes({"dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf": b"d",
                             "dejavu-fonts-ttf-2.37/ttf/DejaVuSerif-Bold.ttf": b"d",
                             "dejavu-fonts-ttf-2.37/ttf/DejaVuSansCondensed.ttf": b"x",
                             "dejavu-fonts-ttf-2.37/LICENSE": b"x"}),
        "noto-emoji": b"emoji",
        "mermaid": tgz_bytes({
            "package/dist/mermaid.esm.min.mjs": b"m", "package/dist/mermaid.js": b"x",
            "package/dist/chunks/mermaid.esm.min/a.mjs": b"c", "package/dist/chunks/mermaid.esm.min/a.mjs.map": b"x",
            "package/dist/chunks/mermaid.core/z.mjs": b"x", "package/package.json": b"{}"}),
        "layout-elk": tgz_bytes({
            "package/dist/mermaid-layout-elk.esm.min.mjs": b"e",
            "package/dist/chunks/mermaid-layout-elk.esm.min/sub/b.mjs": b"c",
            "package/dist/chunks/mermaid-layout-elk.esm.min/sub/b.mjs.map": b"x", "package/dist/x.cjs": b"x"}),
        "highlightjs": tgz_bytes({
            "package/highlight.min.js": b"h", "package/styles/github.min.css": b"s",
            "package/styles/dark.min.css": b"x", "package/es/core.js": b"x"}),
    }


class FakeNet:
    """Stands in for pdf_download: serves fixture bytes by pin URL, records every call, and
    checks that the final runtime directory never exists before the install is complete."""

    def __init__(self, files, base=None, fail_on=None):
        self.files, self.calls, self.base, self.fail_on = dict(files), [], base, fail_on
        self.by_url = {A.PDF_PINS[n][0]: n for n in A.PDF_PINS}
        self.early = False
        self.dests = []

    def __call__(self, url, dest):
        name = self.by_url[url]
        self.calls.append(name)
        self.dests.append(pathlib.Path(dest))
        if self.base is not None and (self.base / A.PDF_REV).exists():
            self.early = True
        if name == self.fail_on:
            raise OSError("network down")
        pathlib.Path(dest).write_bytes(self.files[name])


def fixture_pins(files):
    return {n: (A.PDF_PINS[n][0], hashlib.sha256(files[n]).hexdigest()) for n in A.PDF_PINS}


def ents(root):
    """Directory entries except the installer's lock file."""
    return sorted(p.name for p in root.iterdir() if p.name != ".lock")


def tree(root):
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def run_main(*argv):
    sys.argv = ["artifacts.py", *argv]
    out, err = io.StringIO(), io.StringIO()
    rc = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            A.main()
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def pdf_unicode(pdf):
    """The characters the PDF's embedded fonts can map back to text (ToUnicode CMaps)."""
    chars = set()
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            data = zlib.decompress(m.group(1))
        except zlib.error:
            continue
        if b"beginbfchar" not in data and b"beginbfrange" not in data:
            continue
        for part in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
            for dst in re.findall(rb"<[0-9A-Fa-f]+>\s*<([0-9A-Fa-f]{4})>", part):
                chars.add(chr(int(dst, 16)))
        for part in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
            for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]{4})>", part):
                first = int(dst, 16)
                chars.update(chr(first + i) for i in range(int(hi, 16) - int(lo, 16) + 1))
    return chars


def t_pdf_runtime():
    print("PDF runtime: pins and revision")
    names = set(A.PDF_PINS)
    check(names == set(A.PDF_FILES), "every pinned download has a trimming rule, and vice versa")
    check(all(u.startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", s) for u, s in A.PDF_PINS.values()),
          "pins: every URL is https and every checksum is 64 hex")
    check(re.fullmatch(r"[0-9a-f]{12}", A.PDF_REV) is not None, "PDF_REV is 12 hex")
    check("playwright" not in A.PDF_RENDER.lower() and "playwright" not in repr(A.PDF_PINS).lower(),
          "no Playwright in the renderer or the pins (the renderer speaks DevTools over a pipe itself)")
    src = PROGRAM.read_text()
    old = next(iter(A.PDF_PINS.values()))[1]
    v1 = load_variant(src.replace(old, ("0" if old[0] != "0" else "1") + old[1:], 1), "variant_pin")
    v2 = load_variant(src.replace("import base64, fcntl", "import base64,  fcntl", 1), "variant_render")
    v3 = load_variant(src.replace('<dir prefix="relative">fonts</dir>', '<dir prefix="relative">fonts2</dir>', 1), "variant_conf")
    check(v1.PDF_REV != A.PDF_REV, "PDF_REV changes when a pinned checksum changes")
    check(v2.PDF_REV == A.PDF_REV, "PDF_REV does not change when only PDF_RENDER changes (no new download)")
    check(v3.PDF_REV != A.PDF_REV, "PDF_REV changes when the fontconfig template changes")
    check(load_variant(src, "variant_same").PDF_REV == A.PDF_REV, "PDF_REV is stable for the same program")
    code_rule = re.search(r"^code \{[^}]*\}", A.PDF_CSS, re.MULTILINE)
    check(code_rule is not None and "font-variant-ligatures: none" in code_rule.group(0)
          and "padding: .1em 0;" in code_rule.group(0),
          "PDF code: no ligatures, and no side padding (copied as a space)")
    for name, member, dest in (
            ("chrome", CH + "chrome-headless-shell", "chrome/chrome-headless-shell"),
            ("chrome", CH + "locales/en-US.pak", "chrome/locales/en-US.pak"),
            ("chrome", CH + "locales/de.pak", None), ("chrome", CH + "libvulkan.so.1", None),
            ("chrome", CH + "libvk_swiftshader.so", None), ("chrome", CH + "vk_swiftshader_icd.json", None),
            ("mermaid", "package/dist/chunks/mermaid.esm.min/x.mjs.map", None),
            ("mermaid", "package/dist/mermaid.js", None),
            ("mermaid", "package/dist/mermaid.esm.min.mjs", "lib/mermaid/dist/mermaid.esm.min.mjs"),
            ("layout-elk", "package/dist/mermaid-layout-elk.esm.min.mjs",
             "lib/@mermaid-js/layout-elk/dist/mermaid-layout-elk.esm.min.mjs"),
            ("highlightjs", "package/highlight.min.js", "lib/@highlightjs/cdn-assets/highlight.min.js"),
            ("highlightjs", "package/styles/github.min.css", "lib/@highlightjs/cdn-assets/styles/github.min.css")):
        check(A._pdf_target(member, A.PDF_FILES[name]) == dest, f"rules: {member} -> {dest}")
    for want in (A.PDF_MERMAID, A.PDF_MERMAID_ELK, A.PDF_HLJS + "/highlight.min.js",
                 A.PDF_HLJS + "/styles/github.min.css"):
        rel = "lib/" + want[len(A.PDF_LIB) + 1:]
        check(rel in {"lib/mermaid/dist/mermaid.esm.min.mjs",
                      "lib/@mermaid-js/layout-elk/dist/mermaid-layout-elk.esm.min.mjs",
                      "lib/@highlightjs/cdn-assets/highlight.min.js",
                      "lib/@highlightjs/cdn-assets/styles/github.min.css"}, f"the print page's {rel} is a file the rules install")

    print("PDF runtime: renderer launch")
    r = A.PDF_RENDER
    check("--no-sandbox" not in r and "no-sandbox" not in r, "PDF_RENDER never passes --no-sandbox")
    for flag in ("--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=http://127.0.0.1:9",
                 "--proxy-bypass-list=<-loopback>", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"):
        check(f'"{flag}"' in r, f"PDF_RENDER blocks the network with {flag}")
    check('"/proc/self/oom_score_adj"' in r and r.index("oom_score_adj") < r.index("posix_spawn"),
          "PDF_RENDER raises its own oom_score_adj before it starts Chromium")
    check(r.index('call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})')
          < r.index('call("Target.createTarget"'),
          "PDF_RENDER turns on browser-level auto-attach (popups included) before it creates the page")
    check('os.environ["PDF_CHROME"]' in r and 'os.environ["PDF_LIB_DIR"]' in r and "/opt/" not in r,
          "PDF_RENDER takes the headless shell and the libraries from the environment")

    print("PDF runtime: installer")
    files = fixture_downloads()
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td).resolve()
        base = td / "x" / "artifacts" / "pdf-runtime"
        net = FakeNet(files, base)
        (base).mkdir(parents=True)
        (base / "aaaaaaaaaaaa").mkdir()
        (base / "aaaaaaaaaaaa" / "REV").write_text("aaaaaaaaaaaa\n")
        (base / ".tmp-bbbbbbbbbbbb-1").mkdir()
        (base / ".tmp-other-format").mkdir()
        (base / "keep-me").mkdir()
        msgs = []
        with patched(A, pdf_download=net, PDF_PINS=fixture_pins(files)):
            final = A.install_pdf_runtime(base, say=msgs.append)
            check(final == base / A.PDF_REV and (final / "REV").read_text() == A.PDF_REV + "\n",
                  "install: DIR/<PDF_REV>/REV is written and holds PDF_REV")
            check(tree(final) - {"REV", "fonts.conf"} == FIXTURE_KEPT,
                  "install: only the listed files are kept (extras, other locales, Vulkan, maps, other builds dropped)")
            if tree(final) - {"REV", "fonts.conf"} != FIXTURE_KEPT:
                print("      extra:", sorted(tree(final) - {"REV", "fonts.conf"} - FIXTURE_KEPT),
                      "missing:", sorted(FIXTURE_KEPT - tree(final)))
            check(os.access(final / "chrome" / "chrome-headless-shell", os.X_OK), "install: the headless shell is executable")
            conf = (final / "fonts.conf").read_text()
            check('<dir prefix="relative">fonts</dir>' in conf and '<cachedir prefix="relative">fontcache</cachedir>' in conf
                  and str(final) not in conf and "RUNTIME" not in conf,
                  "install: fonts.conf paths are relative to the file, so the runtime can move")
            check(net.calls == list(A.PDF_PINS) and not net.early,
                  "install: every pin downloaded once, and the final directory only appears when complete")
            check(not any(d.exists() for d in net.dests) and not any(p.name.startswith(".tmp-") for p in base.iterdir()),
                  "install: the download directory and the staging directory are gone")
            check(not (base / "aaaaaaaaaaaa").exists() and not (base / ".tmp-bbbbbbbbbbbb-1").exists()
                  and not (base / ".tmp-other-format").exists(),
                  "install: other revisions and stale staging directories are removed")
            check((base / "keep-me").is_dir(), "install: unrelated entries in the directory are left alone")
            net.calls.clear()
            msgs.clear()
            again = A.install_pdf_runtime(base, say=msgs.append)
            check(again == final and not net.calls and any("already installed" in m for m in msgs),
                  "install is idempotent: a second run downloads nothing and says already installed")

        # Two installers into one directory take turns: the second waits, then finds it installed.
        base5 = td / "race" / "artifacts" / "pdf-runtime"
        first_in, release = threading.Event(), threading.Event()
        inner = FakeNet(files, base5)

        def slow(url, dest):
            if not first_in.is_set():
                first_in.set()
                release.wait(20)
            inner(url, dest)
        said = {"a": [], "b": []}
        results, errors = {}, []

        def run(who):
            try:
                results[who] = A.install_pdf_runtime(base5, say=said[who].append)
            except BaseException as e:  # reported below
                errors.append(repr(e))
        with patched(A, pdf_download=slow, PDF_PINS=fixture_pins(files)):
            ta = threading.Thread(target=run, args=("a",))
            ta.start()
            first_in.wait(10)
            tb = threading.Thread(target=run, args=("b",))
            tb.start()
            time.sleep(0.5)
            waiting = tb.is_alive()
            release.set()
            ta.join(30)
            tb.join(30)
        check(not errors and results.get("a") == results.get("b") == base5 / A.PDF_REV,
              f"install: two concurrent installers both succeed ({errors})")
        check(waiting and any("waiting" in m for m in said["b"]) and any("already installed" in m for m in said["b"]),
              "install: the second installer waits for the first, says so, then finds it already installed")
        check(inner.calls == list(A.PDF_PINS) and ents(base5) == [A.PDF_REV],
              "install: concurrent installers download once and leave one clean runtime")

        # A download that fails part way leaves nothing behind.
        base2 = td / "fail" / "artifacts" / "pdf-runtime"
        net = FakeNet(files, base2, fail_on="dejavu")
        with patched(A, pdf_download=net, PDF_PINS=fixture_pins(files)):
            try:
                A.install_pdf_runtime(base2, say=lambda m: None)
                raised = None
            except A.PdfInstallError as e:
                raised = str(e)
        check(raised is not None and "dejavu" in raised and ents(base2) == [],
              f"install: a failed download aborts and leaves no runtime ({raised})")

        # An atomic install: if the final rename fails nothing is left either.
        base3 = td / "rename" / "artifacts" / "pdf-runtime"
        with patched(A, pdf_download=FakeNet(files), PDF_PINS=fixture_pins(files)), \
                patched(A.os, rename=lambda *a: (_ for _ in ()).throw(OSError("boom"))):
            try:
                A.install_pdf_runtime(base3, say=lambda m: None)
                raised = None
            except OSError as e:
                raised = e
        check(raised is not None and ents(base3) == [], "install: a failure at the last step leaves no partial runtime")

        # Checksum: a download that differs from its pin is refused before anything is unpacked.
        base4 = td / "sum" / "artifacts" / "pdf-runtime"
        tampered = dict(files, inter=zip_bytes({"extras/ttf/Inter-Regular.ttf": b"EVIL"}))
        extracted = []
        real_extract = A._pdf_extract
        with patched(A, pdf_download=FakeNet(tampered), PDF_PINS=fixture_pins(files),
                     _pdf_extract=lambda name, *a: extracted.append(name) or real_extract(name, *a)):
            try:
                A.install_pdf_runtime(base4, say=lambda m: None)
                raised = None
            except A.PdfInstallError as e:
                raised = str(e)
        check(raised is not None and "checksum mismatch" in raised and "inter" in raised,
              f"install: a checksum mismatch aborts ({raised and raised[:60]})")
        check("inter" not in extracted and ents(base4) == [],
              "install: the mismatching download is never unpacked, and no runtime is left behind")

        # Path traversal and links, in members the rules would keep and in ones they would drop.
        def refuse(label, name, blob):
            b = td / ("trav-" + label) / "artifacts" / "pdf-runtime"
            served = dict(files, **{name: blob})
            with patched(A, pdf_download=FakeNet(served), PDF_PINS=fixture_pins(served)):
                try:
                    A.install_pdf_runtime(b, say=lambda m: None)
                    got = None
                except A.PdfInstallError as e:
                    got = str(e)
            escaped = [p for p in (td / ("trav-" + label)).rglob("evil*")]
            check(got is not None and b.is_dir() and ents(b) == [] and not escaped,
                  f"install: {label} is refused, nothing written or left behind ({got and got[:50]})")
        refuse("zip member ../ (a dropped file)", "inter", zip_bytes({"extras/ttf/Inter-Regular.ttf": b"i", "../evil.txt": b"x"}))
        refuse("zip member that a rule would keep, via ..", "chrome",
               zip_bytes({CH + "chrome-headless-shell": b"x", CH + "../../evil": b"x"}))
        refuse("zip absolute path", "jetbrains-mono", zip_bytes({"/tmp/evil.ttf": b"x"}))
        refuse("zip symlink", "dejavu", zip_bytes({"dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf": (b"/etc/passwd", 0o120777)}))
        link = tarfile.TarInfo("package/dist/evil-link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        refuse("tar symlink", "mermaid", tgz_bytes({"package/dist/mermaid.esm.min.mjs": b"m"}, extra=[link]))
        refuse("tar member ../", "highlightjs", tgz_bytes({"package/highlight.min.js": b"h", "package/../../evil.js": b"x"}))
        refuse("tar absolute path", "layout-elk", tgz_bytes({"/tmp/evil.mjs": b"x"}))

    print("PDF runtime: command line")
    stub = []
    with patched(A, install_pdf_runtime=lambda d, say=print: stub.append(d) or pathlib.Path(d) / A.PDF_REV):
        rc, out, err = run_main("--install-pdf-runtime", "--pdf-runtime", "/nonexistent/artifacts/pdf-runtime")
    check(rc == 0 and stub == [pathlib.Path("/nonexistent/artifacts/pdf-runtime")],
          "main: --install-pdf-runtime is a real option and installs into --pdf-runtime")
    def boom(d, say=print):
        raise A.PdfInstallError("nope")
    with patched(A, install_pdf_runtime=boom):
        rc, out, err = run_main("--install-pdf-runtime")
    check(rc == 1 and "nope" in err, "main: a failed install exits 1 and says why")
    for argv in (["--build-pdf-image"], ["--pdf-image", "artifacts-pdf:local"], ["--pdf-base", "some/image"],
                 ["--build-pdf-image", "--pdf-base", "mcr.microsoft.com/playwright:v1"]):
        calls = []
        with patched(A.subprocess, run=lambda *a, **k: calls.append(a), Popen=lambda *a, **k: calls.append(a)):
            rc, out, err = run_main(*argv)
        flag = next(a for a in argv if a.startswith("--"))
        check(rc == 2 and "--install-pdf-runtime" in err and "was removed" in err and not calls,
              f"main: removed {' '.join(argv[:1])} exits 2, names --install-pdf-runtime, runs nothing")
    rc, out, err = run_main("--pdf-rev")
    check(rc == 0 and out == A.PDF_REV + "\n", "main: --pdf-rev prints PDF_REV")
    default = A.default_pdf_runtime()
    rc, out, err = run_main("--print-apparmor-profile")
    check(rc == 0 and out == A.pdf_apparmor_profile(default) and "  userns," in out and "flags=(unconfined)" in out
          and f"profile {A.PDF_APPARMOR_NAME} {default}/*/chrome/chrome-headless-shell " in out and "/**/" not in out,
          "main: --print-apparmor-profile names the default runtime directory exactly (no /**/ pattern)")
    rc, out, err = run_main("--print-apparmor-profile", "--pdf-runtime", "/srv/viewer/artifacts/pdf-runtime")
    check(rc == 0 and "profile artifacts-chrome-headless-shell /srv/viewer/artifacts/pdf-runtime/*/chrome/chrome-headless-shell flags=(unconfined) {" in out
          and out.count("/srv/viewer") == 1, "main: --print-apparmor-profile honours --pdf-runtime")
    old = os.getcwd()
    try:
        os.chdir("/srv")
        rc, out, err = run_main("--print-apparmor-profile", "--pdf-runtime", "rel/../rt")
    finally:
        os.chdir(old)
    check(rc == 0 and " /srv/rt/*/chrome/chrome-headless-shell " in out, "main: a relative --pdf-runtime is made absolute in the profile")
    for bad in ('/tmp/a b', '/tmp/a*b', '/tmp/a?b', '/tmp/a[b', '/tmp/a]b', '/tmp/a{b', '/tmp/a}b', '/tmp/a^b', '/tmp/a"b',
                '/tmp/a\nb', '/tmp/a\tb', '/tmp/a,b', '/tmp/a\\b'):
        rc, out, err = run_main("--print-apparmor-profile", "--pdf-runtime", bad)
        check(rc == 2 and out == "" and "AppArmor" in err, f"main: --print-apparmor-profile refuses {bad!r} with exit 2")
    host = pathlib.Path("/etc/apparmor.d") / A.PDF_APPARMOR_NAME
    if host.is_file() and str(default) in host.read_text():
        check(host.read_text() == A.pdf_apparmor_profile(default),
              "the profile installed on this host is byte-identical to what the program prints")
    else:
        skip("no installed profile for the default runtime directory on this host")
    with tempfile.TemporaryDirectory() as td:
        fake = pathlib.Path(td) / "profile"
        with patched(A, PDF_APPARMOR_FILE=fake):
            state = " ".join(A.pdf_apparmor_state(td + "/artifacts/pdf-runtime"))
            fake.write_text("stale")
            stale = " ".join(A.pdf_apparmor_state(td + "/artifacts/pdf-runtime"))
            fake.write_text(A.pdf_apparmor_profile(td + "/artifacts/pdf-runtime"))
            good = " ".join(A.pdf_apparmor_state(td + "/artifacts/pdf-runtime"))
            odd = " ".join(A.pdf_apparmor_state(td + "/a b"))
        restricted = pathlib.Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").is_file()
        if restricted:
            check("NOT installed" in state and "installed but DIFFERENT" in stale and "installed and current" in good
                  and "unknown" not in good and "not readable without root" in good and "warning" in odd,
                  "apparmor state: reports the world-readable profile file (missing, stale, current), never 'unknown'")
        else:
            skip("apparmor state: this kernel has no userns restriction")
    rc, out, err = run_main("--help")
    check(rc == 0 and "docker" not in out.lower() and "--install-pdf-runtime" in out and "--build-pdf-image" not in out,
          "--help names no docker and lists --install-pdf-runtime")
    check("docker" not in (A.__doc__ or "").lower(), "the module docstring names no docker")

    print("PDF runtime: render_pdf error mapping")
    with tempfile.TemporaryDirectory() as td:
        rt = pathlib.Path(td).resolve()
        launched = []
        with patched(A.subprocess, Popen=lambda *a, **k: launched.append(a) or None):
            pdf, why = A.render_pdf("<p>x</p>", rt)
        check(pdf is None and "not installed" in why and "--install-pdf-runtime" in why and not launched,
              "render_pdf: a missing runtime says to run --install-pdf-runtime and starts nothing")
        (rt / A.PDF_REV).mkdir()
        (rt / A.PDF_REV / "REV").write_text("0000000000bad\n")
        pdf, why = A.render_pdf("<p>x</p>", rt)
        check(pdf is None and "--install-pdf-runtime" in why, "render_pdf: a REV of another revision does not count")
        (rt / A.PDF_REV / "REV").write_text(A.PDF_REV + "\n")

        # A stand-in renderer runs as a real process (the real one needs the runtime):
        # it behaves like PDF_RENDER on the pipes, so render_pdf's I/O is exercised.
        stub_ok = """
import os, sys, time
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(b"%PDF-1.7 " + str(len(data)).encode() + b"\\n%%EOF\\n")
sys.stdout.flush()
quiet = os.open(os.devnull, os.O_WRONLY)
os.dup2(quiet, 1)
os.dup2(quiet, 2)
time.sleep(1.5)   # Chromium shutting down
"""
        pidfile = pathlib.Path(td) / "helper.pid"
        stub_hang = f"""
import subprocess, sys, time
sys.stdin.buffer.read()
child = subprocess.Popen(["sleep", "60"])   # a Chromium helper in the same process group
open({str(pidfile)!r}, "w").write(str(child.pid))
time.sleep(60)
"""
        seen = {}
        real_popen = subprocess.Popen

        def spy(argv, **kw):
            seen.update(argv=argv, kw=kw, home_existed=os.path.isdir(kw["cwd"]))
            return real_popen(argv, **kw)

        def render(script, page="<p>page</p>", timeout=90, **kw):
            with patched(A, PDF_RENDER=script, PDF_TIMEOUT=timeout), patched(A.subprocess, Popen=spy), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                t = time.monotonic()
                out = A.render_pdf(page, kw.pop("runtime", rt), **kw)
                return out, time.monotonic() - t, err.getvalue()

        def slots_free():
            got = [A.PDF_SLOTS.acquire(blocking=False) for _ in range(2)]
            for g in got:
                if g:
                    A.PDF_SLOTS.release()
            return all(got)

        (pdf, why), took, _ = render(stub_ok)
        env = seen["kw"]["env"]
        check(pdf == b"%PDF-1.7 11\n%%EOF\n" and why is None, "render_pdf: the page goes in on stdin and the PDF comes back")
        check(took < 1.0, f"render_pdf: answers once the PDF is written, not when the renderer exits ({took:.2f} s, exit after 1.5 s)")
        check(seen["argv"][1:3] == ["-I", "-c"] and seen["kw"]["start_new_session"] is True,
              "render_pdf: python -I -c PDF_RENDER in its own session")
        check(set(env) == {"PATH", "HOME", "TMPDIR", "LANG", "FONTCONFIG_FILE", "PDF_CHROME", "PDF_LIB_DIR"}
              and env["HOME"] == seen["kw"]["cwd"] and env["PDF_CHROME"] == str(rt / A.PDF_REV / "chrome" / "chrome-headless-shell")
              and env["PDF_LIB_DIR"] == str(rt / A.PDF_REV / "lib") and env["FONTCONFIG_FILE"] == str(rt / A.PDF_REV / "fonts.conf"),
              "render_pdf: minimal environment pointing at this revision, HOME and cwd one fresh directory")
        check(seen["home_existed"] and os.path.exists(env["HOME"]) and not slots_free(),
              "render_pdf: while the renderer shuts down, its slot and temp HOME are still held")
        time.sleep(2.5)
        check(not os.path.exists(env["HOME"]) and slots_free(),
              "render_pdf: once the renderer has exited, its temp HOME is removed and its slot is free")
        big = "<p>" + "x" * (5 << 20) + "</p>"
        (pdf, why), _, _ = render(stub_ok, page=big)
        check(pdf == b"%PDF-1.7 " + str(len(big)).encode() + b"\n%%EOF\n",
              "render_pdf: a 5 MB page goes through the pipes whole (no deadlock)")
        (pdf, why), _, _ = render("import sys; sys.stdin.read(); sys.stderr.write('FATAL: No usable sandbox! Update your kernel'); sys.exit(1)")
        check(pdf is None and "sandbox is not usable" in why and "--print-apparmor-profile" in why,
              "render_pdf: 'No usable sandbox' points at the AppArmor profile")
        (pdf, why), _, _ = render("import sys; sys.stdin.read(); print('not a pdf')")
        check(pdf is None and why == "rendering failed (details in the service log)", "render_pdf: output that is not a PDF fails")
        (pdf, why), _, _ = render("import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'%PDF-1.7 cut short'); sys.exit(1)")
        check(pdf is None and why == "rendering failed (details in the service log)",
              "render_pdf: a PDF cut short (no %%EOF) fails")
        (pdf, why), _, logged = render("import sys; sys.stdin.read(); sys.stderr.write('boom'); sys.exit(1)")
        check(pdf is None and why == "rendering failed (details in the service log)" and "boom" in logged,
              "render_pdf: a failing renderer gives the plain failure and logs its stderr")
        (pdf, why), took, _ = render(stub_hang, timeout=1)
        time.sleep(0.5)
        helper = int(pidfile.read_text()) if pidfile.exists() else None
        try:
            alive = helper is not None and os.kill(helper, 0) is None   # signal 0: does it still exist?
        except ProcessLookupError:
            alive = False
        if alive:
            os.kill(helper, signal.SIGKILL)  # do not leave it behind for the next run
        check(pdf is None and why == "rendering took longer than 1 s" and took < 8,
              f"render_pdf: a render past the time limit fails ({took:.1f} s)")
        check(helper is not None and not alive, "render_pdf: a timeout kills the whole process group, helpers included")
        check(slots_free(), "render_pdf: every render slot is released again")

        # The renderer volunteers as the OOM killer's first victim, and Chromium inherits that.
        fake_chrome = pathlib.Path(td) / "fake-chrome"
        score = pathlib.Path(td) / "score"
        fake_chrome.write_text(f"#!/bin/sh\ncat /proc/self/oom_score_adj > {score}\n")
        fake_chrome.chmod(0o755)
        subprocess.run([sys.executable, "-I", "-c", A.PDF_RENDER], input=b"<p>x</p>", cwd=td, timeout=30,
                       capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": td, "TMPDIR": td,
                                                 "PDF_CHROME": str(fake_chrome), "PDF_LIB_DIR": td})
        check(score.exists() and score.read_text().strip() == "1000",
              f"PDF_RENDER: the browser starts with oom_score_adj 1000 ({score.read_text().strip() if score.exists() else 'not run'})")

        # wait=True (the command line): the shutdown and the cleanup are done before it returns.
        (pdf, why), took, _ = render(stub_ok, wait=True)
        check(pdf is not None and took >= 1.4 and not os.path.exists(seen["kw"]["cwd"]) and slots_free(),
              f"render_pdf(wait=True): returns after the renderer exited, temp HOME removed, slot free ({took:.1f} s)")

        # A relative runtime directory still gives the renderer absolute paths.
        old = os.getcwd()
        try:
            os.chdir(rt)
            (pdf, why), _, _ = render(stub_ok, runtime=pathlib.Path("."), wait=True)
            env = seen["kw"]["env"]
        finally:
            os.chdir(old)
        check(pdf is not None and all(os.path.isabs(env[k]) for k in ("FONTCONFIG_FILE", "PDF_CHROME", "PDF_LIB_DIR"))
              and env["PDF_CHROME"] == str(rt / A.PDF_REV / "chrome" / "chrome-headless-shell"),
              "render_pdf: a relative runtime directory is resolved to absolute paths for the renderer")

        # The slot must come back whatever goes wrong after it was taken.
        def boom(*a, **k):
            raise OSError("no space left on device")
        for _ in range(2):  # twice: a leak of one slot per failure would exhaust both
            with patched(A.tempfile, mkdtemp=boom), contextlib.redirect_stderr(io.StringIO()):
                pdf, why = A.render_pdf("<p>x</p>", rt)
            check(pdf is None and "temporary directory" in why, f"render_pdf: mkdtemp failing is reported ({why})")
        check(slots_free(), "render_pdf: after mkdtemp failed twice, both render slots are free")
        homes = set(glob_homes())
        helper_pid = pathlib.Path(td) / "own.pid"
        stub_pid = f"import os, time\nopen({str(helper_pid)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"

        def exchange_fails(*a, **k):
            for _ in range(100):  # let the renderer start and write its pid
                if helper_pid.exists() and helper_pid.read_text():
                    break
                time.sleep(0.05)
            raise OSError("broken pipe, or any other surprise")
        for _ in range(2):
            helper_pid.unlink(missing_ok=True)
            with patched(A, PDF_RENDER=stub_pid, _exchange=exchange_fails), contextlib.redirect_stderr(io.StringIO()):
                pdf, why = A.render_pdf("<p>x</p>", rt)
            time.sleep(0.3)
            try:
                alive = os.kill(int(helper_pid.read_text()), 0) is None
            except (ProcessLookupError, ValueError, OSError):
                alive = False
            check(pdf is None and why == "rendering failed (details in the service log)" and not alive,
                  f"render_pdf: an unexpected error from _exchange fails the render and kills the renderer ({why})")
        check(slots_free() and set(glob_homes()) == homes,
              "render_pdf: after _exchange failed twice, both slots are free and no temp HOME is left")

    print("PDF runtime: the real runtime")
    rt = real_runtime()
    if rt is None:
        skip(NO_RUNTIME)
        return
    marker = "Qwertzuiop Vjxkz"
    page = A.render_page("t", f"<h1>{marker}</h1><p>plain body</p>", pdf=True)
    t0 = time.monotonic()
    pdf, why = A.render_pdf(page, rt)
    check(pdf is not None and pdf.startswith(b"%PDF") and why is None, f"render: a page prints to a PDF ({why})")
    check(pdf is not None and set(marker.replace(" ", "")) <= pdf_unicode(pdf),
          "render: the page's text is in the PDF (its fonts map back to it)")
    print(f"      render took {time.monotonic() - t0:.2f} s")
    homes_before = set(glob_homes())
    r = subprocess.run([sys.executable, str(PROGRAM), "--check-pdf-runtime", "--pdf-runtime", str(rt)],
                       capture_output=True, text=True, timeout=120)
    check(set(glob_homes()) <= homes_before, f"--check-pdf-runtime leaves no temp directory behind ({set(glob_homes()) - homes_before})")
    check(r.returncode == 0 and r.stdout.strip() == "ok", f"--check-pdf-runtime exits 0 and prints ok ({r.stdout.strip()} {r.stderr.strip()[-80:]})")
    empty = tempfile.mkdtemp()
    r = subprocess.run([sys.executable, str(PROGRAM), "--check-pdf-runtime", "--pdf-runtime", empty],
                       capture_output=True, text=True, timeout=120)
    shutil.rmtree(empty)
    check(r.returncode == 1 and "--install-pdf-runtime" in r.stderr, "--check-pdf-runtime exits 1 for a missing runtime")

    # The check fails when the fonts are missing (it used to say ok with a runtime that printed text in no font).
    with tempfile.TemporaryDirectory() as td:
        broken = pathlib.Path(td) / A.PDF_REV
        broken.mkdir()
        shutil.copy(rt / A.PDF_REV / "REV", broken / "REV")
        (broken / "fonts.conf").write_text(A.PDF_FONTS_CONF)  # what the installer writes
        for n in ("chrome", "lib"):
            (broken / n).symlink_to(rt / A.PDF_REV / n)
        (broken / "fonts").mkdir()  # no fonts in it
        with patched(A, PDF_RENDER=NO_SANDBOX(A.PDF_RENDER)):
            ok, why = A.check_pdf_runtime(td)
        check(not ok and "fonts" in why, f"check_pdf_runtime: fails when the runtime's fonts are missing ({why})")
        # A copy of the runtime at another path still finds its fonts: fonts.conf is path-independent.
        # (The sandbox is off here only because the AppArmor profile admits one path.)
        shutil.copytree(rt / A.PDF_REV / "fonts", broken / "fonts", dirs_exist_ok=True)
        with patched(A, PDF_RENDER=NO_SANDBOX(A.PDF_RENDER)):
            ok, why = A.check_pdf_runtime(td)
            pdf, _ = A.render_pdf(A.render_page("t", "<p>moved</p>", pdf=True), td, wait=True)
        check(ok and pdf and re.search(rb"/BaseFont\s*/[A-Z]{6}\+Inter-Regular", pdf),
              f"a copy of the runtime at another path embeds Inter ({why})")

    # Network: a page that tries <img>, <link>, fetch(), a WebSocket, a window.open() popup and WebRTC
    # (STUN and TURN servers on a local UDP port) against live local listeners. The page carries no
    # CSP of its own, so this tests the browser switches and the request interception, not the
    # policy of the real print page.
    def network_run(render_script=None):
        hits, udp_hits = [], []
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(16)
        lsock.settimeout(0.2)
        usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        usock.bind(("127.0.0.1", 0))
        usock.settimeout(0.2)
        port, uport = lsock.getsockname()[1], usock.getsockname()[1]
        stop = threading.Event()

        def accept():
            while not stop.is_set():
                try:
                    c, _ = lsock.accept()
                except OSError:
                    continue
                hits.append(1)
                c.close()

        def datagrams():
            while not stop.is_set():
                try:
                    usock.recvfrom(4096)
                except OSError:
                    continue
                udp_hits.append(1)
        threads = [threading.Thread(target=f, daemon=True) for f in (accept, datagrams)]
        for t in threads:
            t.start()
        ctl = socket.create_connection(("127.0.0.1", port))
        ctl.close()
        usock_c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        usock_c.sendto(b"x", ("127.0.0.1", uport))
        usock_c.close()
        time.sleep(0.5)
        saw = (len(hits), len(udp_hits))
        del hits[:], udp_hits[:]
        url = f"127.0.0.1:{port}"
        net_page = f"""<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="http://{url}/c.css">
<body><p>{marker}</p><img src="http://{url}/i.png"><script src="http://{url}/s.js"></script>
<script>
try {{ fetch("http://{url}/f").catch(() => {{}}); }} catch (e) {{}}
try {{ const w = new WebSocket("ws://{url}/w"); w.onerror = () => {{}}; }} catch (e) {{}}
try {{ window.open("http://{url}/popup"); }} catch (e) {{}}
try {{
  const pc = new RTCPeerConnection({{iceServers: [{{urls: "stun:127.0.0.1:{uport}"}},
      {{urls: "turn:127.0.0.1:{uport}", username: "u", credential: "p"}}]}});
  pc.createDataChannel("d");
  pc.createOffer().then(o => pc.setLocalDescription(o)).catch(() => {{}});
}} catch (e) {{}}
setTimeout(() => {{ document.documentElement.dataset.ready = "1"; }}, 2500);
</script></body>"""
        with patched(A, PDF_RENDER=render_script or A.PDF_RENDER):
            pdf, why = A.render_pdf(net_page, rt, wait=True)
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(2)
        lsock.close()
        usock.close()
        return saw, len(hits), len(udp_hits), pdf, why

    saw, tcp, udp, pdf, why = network_run()
    check(saw == (1, 1), "network test: the local listeners do see a direct TCP connection and a UDP datagram")
    check(tcp == 0 and udp == 0,
          f"network: <img>, <link>, <script>, fetch(), a WebSocket, a window.open popup and WebRTC cause "
          f"{tcp} TCP connections and {udp} UDP datagrams to local listeners (want 0 and 0)")
    check(pdf is not None and pdf.startswith(b"%PDF") and set("Qwertzuiop") <= pdf_unicode(pdf),
          f"network: the PDF still renders ({why})")

    # Controls: take protections away in a copy of the renderer and the page's attempts do get out,
    # so the zero above is the protections' doing. Interception on the page only (the old design)
    # lets the popup through; without the switches WebRTC UDP is not stopped by interception.
    switches = ('"--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=http://127.0.0.1:9", '
                '"--proxy-bypass-list=<-loopback>",')
    webrtc = '"--force-webrtc-ip-handling-policy=disable_non_proxied_udp",'
    child_fetch = 'send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, child)'
    main_fetch = '    s = attached[target]\n'
    check(switches in A.PDF_RENDER and webrtc in A.PDF_RENDER and child_fetch in A.PDF_RENDER and main_fetch in A.PDF_RENDER,
          "controls: the renderer source has the pieces the controls take away")
    no_switch = A.PDF_RENDER.replace(webrtc, "")
    _, tcp, udp, pdf, why = network_run(no_switch)
    check(tcp == 0 and udp > 0, f"control: without the WebRTC switch, interception does not stop UDP ({tcp} TCP, {udp} UDP)")
    page_only = (A.PDF_RENDER.replace(switches, "").replace(webrtc, "").replace(child_fetch, "pass")
                 .replace(main_fetch, main_fetch + '    call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, s)\n'))
    _, tcp, udp, pdf, why = network_run(page_only)
    check(tcp > 0, f"control: interception on the page only, no switches: the popup reaches the listener ({tcp} TCP, {udp} UDP)")


def main():
    print(f"program under test: {PROGRAM}")
    if A.markdown is None:
        print("python3-markdown is required for these tests", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        vault = build_vault(pathlib.Path(tmp).resolve())
        lib = A.Library([vault])
        t_resolve(lib)
        t_render(lib)
        t_mermaid(lib)
        t_http(lib)
        t_perf(pathlib.Path(tmp).resolve())
        t_pdf_runtime()
    print()
    if FAILS:
        print(f"{len(FAILS)} check(s) failed:")
        for f in FAILS:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
