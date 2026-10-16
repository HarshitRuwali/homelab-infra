"""Behavioural tests for the vault viewer (artifacts.py).

Run from vault-viewer/:  python3 tests/test_viewer.py
Exit 0 = all pass. Needs python3-markdown; no pytest. ARTIFACTS_PY=/path/to/copy.py
points the tests at another copy of the program (used to mutation-test the checks).
The end-to-end PDF check runs only when the docker image artifacts-pdf:local exists.
"""
import base64
import http.client
import importlib.machinery
import importlib.util
import os
import pathlib
import re
import shutil
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
    (vault / UNICODE_NAME).write_text(MERMAID_NOTE)
    (vault / "pic.png").write_bytes(PNG)
    (vault / "sub" / "pic2.png").write_bytes(PNG)
    (vault / "code.py").write_text("print('hi')\n")
    (vault / "notes.xyz").write_text("unknown type\n")
    (vault / ".hidden" / "secret.md").write_text("# secret\n")
    (outside / "secret.png").write_bytes(b"\x89PNG-outside-secret")
    (outside / "secret.md").write_text("# outside\n")
    (vault / "leak.png").symlink_to(outside / "secret.png")
    (vault / "leak.md").symlink_to(outside / "secret.md")
    return vault


def t_resolve(lib):
    print("Library.resolve")
    check(lib.resolve("vault/Note.md") == lib.roots[0] / "Note.md", "normal note resolves to its path")
    check(lib.resolve("vault/sub/pic2.png") is not None, "file in a subfolder resolves")
    check(lib.resolve("vault/../outside/secret.md") is None, ".. traversal -> None")
    check(lib.resolve("vault/sub/../../outside/secret.md") is None, "nested .. traversal -> None")
    check(lib.resolve("vault/.hidden/secret.md") is None, "hidden folder -> None")
    check(lib.resolve("vault/notes.xyz") is None, "unknown extension -> None")
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
    finally:
        srv.shutdown()
        srv.server_close()


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
