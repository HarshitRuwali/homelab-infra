"""Behavioural tests for the vault viewer (artifacts.py).

Run from vault-viewer/:  python3 tests/test_viewer.py
Exit 0 = all pass. Needs python3-markdown; no pytest. ARTIFACTS_PY=/path/to/copy.py
points the tests at another copy of the program (used to mutation-test the checks).
The end-to-end PDF check runs only when the docker image artifacts-pdf:local exists.
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
import shutil
import time
import subprocess
import sys
import tempfile
import threading
import urllib.parse

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


def docker_usable():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "version"], capture_output=True, check=False).returncode == 0


def image_present(name):
    return shutil.which("docker") is not None and \
        subprocess.run(["docker", "image", "inspect", name], capture_output=True, check=False).returncode == 0


def t_http(lib):
    print("HTTP guards")
    A.Handler.log_message = lambda self, fmt, *args: None
    srv = A.ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
    port = srv.server_address[1]
    A.Handler.hosts = {f"127.0.0.1:{port}"}
    A.Handler.library = lib
    A.Handler.gate = A.TailnetGate(None)
    A.Handler.pdf_image = "artifacts-pdf:does-not-exist"
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
        if not shutil.which("docker"):
            check(st == 503 and b"cannot run docker" in body, "/pdf/<note> without docker -> 503 'cannot run docker'")
        elif not docker_usable():
            skip("/pdf/<note> with a missing image: the docker daemon is not reachable")
        else:
            check(st == 503 and b"--build-pdf-image" in body,
                  "/pdf/<note> with a missing image -> 503 naming --build-pdf-image")

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
        if not image_present("artifacts-pdf:local"):
            skip("artifacts-pdf:local is not built (python3 artifacts.py --build-pdf-image)")
        else:
            A.Handler.pdf_image = "artifacts-pdf:local"
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


def t_pdf_build():
    print("PDF image build")
    cmd = A.pdf_build_command("img:t")
    check(cmd == ["docker", "build", "--pull", "-t", "img:t", "-"],
          "build command: docker build --pull, the Dockerfile on stdin")
    D = A.PDF_DOCKERFILE
    check(re.fullmatch(r"[0-9a-f]{64}", A.PDF_CHROME_SHA256) is not None
          and f"ADD --checksum=sha256:{A.PDF_CHROME_SHA256} {A.PDF_CHROME_URL} " in D
          and A.PDF_CHROME_VERSION in A.PDF_CHROME_URL and A.PDF_CHROME_URL.endswith("/chrome-headless-shell-linux64.zip"),
          "Dockerfile takes Chromium's headless shell from Chrome for Testing, pinned by version and checksum")
    check("locales -type f ! -name en-US.pak -delete" in D, "Dockerfile keeps only the en-US locale pack")
    check("playwright" not in D.lower() and "playwright" not in A.PDF_RENDER.lower(),
          "no Playwright in the image or the renderer (the renderer speaks DevTools over a pipe itself)")
    final = D[D.rindex("\nFROM "):]
    check(final.startswith("\nFROM debian:bookworm-slim\n") and "pip" not in final and "unzip" not in final,
          "final stage: Debian 12 slim, no pip and no unzip (builder stages only)")
    check("/opt/chrome-headless-shell/chrome-headless-shell" in A.PDF_RENDER
          and "COPY --from=fetch /opt/chrome-headless-shell /opt/chrome-headless-shell" in D,
          "the renderer launches the headless shell where the Dockerfile puts it")
    check("find /lib-out -name '*.map' -delete" in D and "COPY --from=lib /lib-out /opt/vault-pdf/lib" in D,
          "Dockerfile copies only the pruned print libraries, without source maps")
    for want in (A.PDF_MERMAID, A.PDF_MERMAID_ELK, A.PDF_HLJS + "/highlight.min.js",
                 A.PDF_HLJS + "/styles/github.min.css"):
        rel = want[len(A.PDF_LIB) + 1:]
        check(rel in D, f"Dockerfile keeps {rel}, which the print page loads")
    check(re.fullmatch(r"[0-9a-f]{64}", A.PDF_INTER_SHA256) is not None
          and f"ADD --checksum=sha256:{A.PDF_INTER_SHA256} {A.PDF_INTER_URL} " in A.PDF_DOCKERFILE
          and "fonts-inter" not in A.PDF_DOCKERFILE,
          "Dockerfile takes Inter as TrueType from the checksummed release, not Debian's CFF fonts-inter")
    check(re.fullmatch(r"[0-9a-f]{12}", A.PDF_REV) is not None
          and A.PDF_DOCKERFILE.endswith(f"\nLABEL {A.PDF_REV_LABEL}={A.PDF_REV}\n"),
          "Dockerfile labels the image with PDF_REV")
    base = A.PDF_DOCKERFILE[:A.PDF_DOCKERFILE.rindex("LABEL ")]
    check(A.PDF_REV == A.hashlib.sha256((base + A.PDF_RENDER).encode()).hexdigest()[:12]
          and A.PDF_REV != A.hashlib.sha256((base + A.PDF_RENDER + " ").encode()).hexdigest()[:12],
          "PDF_REV is a hash of the Dockerfile and the render script, so either change gives a new revision")
    code_rule = re.search(r"^code \{[^}]*\}", A.PDF_CSS, re.MULTILINE)
    check(code_rule is not None and "font-variant-ligatures: none" in code_rule.group(0)
          and "padding: .1em 0;" in code_rule.group(0),
          "PDF code: no ligatures, and no side padding (copied as a space)")

    calls = []
    real_run, real_argv = A.subprocess.run, sys.argv
    A.subprocess.run = lambda cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0)
    try:
        def run_main(*argv):
            sys.argv = ["artifacts.py", *argv]
            del calls[:]
            try:
                A.main()
            except SystemExit as e:
                return e.code
        rc = run_main("--build-pdf-image")
        check(rc == 0 and len(calls) == 1 and calls[0][0] == A.pdf_build_command(A.PDF_IMAGE)
              and calls[0][1].get("input") == A.PDF_DOCKERFILE.encode(),
              "main: --build-pdf-image runs the build command with the Dockerfile on stdin")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = run_main("--build-pdf-image", "--pdf-base", "mcr.microsoft.com/playwright:v1.56.0-noble")
        check(rc == 2 and not calls, f"main: removed --pdf-base exits 2 and runs no docker (rc={rc})")
        check("--pdf-base was removed" in err.getvalue() and "--build-pdf-image" in err.getvalue(),
              "main: removed --pdf-base says what replaced it")
        err = io.StringIO()
        os.environ["ARTIFACTS_PDF_BASE"] = "mcr.microsoft.com/playwright:v1.56.0-noble"
        try:
            with contextlib.redirect_stderr(err):
                rc = run_main("--build-pdf-image")
        finally:
            del os.environ["ARTIFACTS_PDF_BASE"]
        check(rc == 0 and len(calls) == 1 and "ignoring ARTIFACTS_PDF_BASE" in err.getvalue(),
              "main: a leftover ARTIFACTS_PDF_BASE is ignored with a warning, the build still runs")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run_main("--pdf-rev")
        check(rc == 0 and out.getvalue() == A.PDF_REV + "\n" and not calls,
              "main: --pdf-rev prints PDF_REV and runs no docker")

        # A failed render against an image of another revision says so.
        def fake(label, render_rc=1):
            def run(cmd, **kw):
                if cmd[:3] == ["docker", "image", "inspect"]:
                    return subprocess.CompletedProcess(cmd, 0, label + "\n", "")
                return subprocess.CompletedProcess(cmd, render_rc, b"", b"boom")
            return run
        with contextlib.redirect_stderr(io.StringIO()):
            A.subprocess.run = fake("0123456789ab")
            _, why = A.render_pdf("<p>x</p>", "img:t")
            check(why is not None and "does not match this program" in why and "0123456789ab" in why
                  and "--build-pdf-image" in why, "render_pdf: an image of another revision is named as the cause")
            A.subprocess.run = fake("<no value>")
            _, why = A.render_pdf("<p>x</p>", "img:t")
            check(why is not None and "image revision none" in why, "render_pdf: an unlabelled image is reported as revision none")
            A.subprocess.run = fake(A.PDF_REV)
            _, why = A.render_pdf("<p>x</p>", "img:t")
            check(why == "rendering failed (details in the service log)",
                  "render_pdf: a matching image that fails gives the plain failure")
    finally:
        A.subprocess.run, sys.argv = real_run, real_argv


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
        t_pdf_build()
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
