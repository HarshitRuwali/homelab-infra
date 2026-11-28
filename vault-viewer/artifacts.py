#!/usr/bin/env python3
"""artifacts: a read-only web view of an Obsidian vault, claude.ai-artifact style.

Renders the whole vault: notes (Markdown with [[wikilinks]], ![[embeds]],
callouts, frontmatter, backlinks, Mermaid diagrams), HTML artifacts, SVG
diagrams and images, plus text sources such as .d2 and zip archives (a
download, not a preview). A file tree with a
"recent" list and search on the left, the selected file on the right,
reloading when it changes on disk. Any diagram opens full screen with pan and
zoom.

It is meant to serve a vault on a host's Tailscale address (for example
http://myhost:7777). Everything in the vault is written by agents, so it is
treated as untrusted:
  * the server binds one address (default 127.0.0.1) and refuses any Host
    header but that address or an --allow-host name (DNS rebinding);
  * with --tailnet-user, only that user's own untagged tailnet devices get in,
    checked with `tailscale whois`; loopback always does;
  * notes and artifacts are served with a CSP `sandbox` (opaque origin) and
    `connect-src 'none'`, and shown in a sandboxed iframe: their scripts run,
    but they can't call this server's API, make network requests or submit
    forms. Links inside a note ask the viewer to navigate (postMessage), and
    the viewer only opens files it listed itself;
  * only files under the vault roots are served, hidden paths never are.

Notes and text files also download as PDF (/pdf/<id>, the viewer's PDF
button): a light, print-styled page rendered by headless Chromium (its
headless shell, installed once with --install-pdf-runtime into a per-user
directory) with Chromium's own sandbox on and the network blocked by browser
switches and request interception. Mermaid, highlight.js and the fonts come
from that runtime, and images are inlined, so nothing is fetched while
printing. On Ubuntu 23.10+ the sandbox needs an AppArmor profile
(--print-apparmor-profile).

Obsidian canvases (.canvas, JSON Canvas) render as a pannable, zoomable board. Obsidian
bases (.base) are evaluated by the Obsidian CLI (--obsidian-cli) and shown as tables.

Needs python3-markdown (Debian/Ubuntu package) for notes; the tailscale CLI
for --tailnet-user; network access once for --install-pdf-runtime; the Obsidian
CLI for bases (python3-yaml names a base's views, optional).
Usage: artifacts.py [--bind ADDR] [--port 7777] [--allow-host NAME] [--tailnet-user LOGIN]
                    [--pdf-runtime DIR] [--install-pdf-runtime] [--check-pdf-runtime]
                    [--print-apparmor-profile] [--pdf-rev]
                    [--obsidian-cli CMD] [--obsidian-vault DIR] [ROOT ...]
"""
import argparse
import base64
import collections
import email.utils
import fcntl
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import markdown
    from markdown.extensions.toc import slugify
except ImportError:  # notes then render as plain text
    markdown = None

    def slugify(value, separator):
        return re.sub(r"[-\s]+", separator, re.sub(r"[^\w\s-]", "", value).strip().lower())

try:  # optional: names the views of a .base file; without it only the default view is queried
    import yaml
except ImportError:
    yaml = None

DEFAULT_ROOTS = [Path.home() / "obsidian"]
KINDS = {
    ".md": "note",
    ".html": "html", ".htm": "html",
    ".svg": "svg",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".d2": "text", ".txt": "text", ".json": "text", ".yaml": "text", ".yml": "text", ".mmd": "text",
    ".csv": "text", ".toml": "text", ".py": "text", ".sh": "text", ".go": "text", ".ts": "text", ".js": "text",
    ".canvas": "canvas", ".base": "base",
    ".zip": "archive",
    ".css": "asset", ".woff2": "asset",
}
CDNS = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com"
HLJS = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0"
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
MERMAID_ELK = "https://cdn.jsdelivr.net/npm/@mermaid-js/layout-elk@0/dist/mermaid-layout-elk.esm.min.mjs"
# The same libraries inside the PDF runtime, served to the page by PDF_RENDER.
PDF_LIB = "/lib"
PDF_HLJS = PDF_LIB + "/@highlightjs/cdn-assets"
PDF_MERMAID = PDF_LIB + "/mermaid/dist/mermaid.esm.min.mjs"
PDF_MERMAID_ELK = PDF_LIB + "/@mermaid-js/layout-elk/dist/mermaid-layout-elk.esm.min.mjs"
PDF_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
           "img-src data:; font-src 'self' data:; connect-src 'none'; form-action 'none'; base-uri 'none'")


def csp(hosts):
    me = " ".join(f"http://{h}" for h in sorted(hosts))
    return "; ".join([
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox",
        "default-src 'none'",
        f"script-src 'unsafe-inline' 'unsafe-eval' {me} {CDNS}",
        f"style-src 'unsafe-inline' {me} https://fonts.googleapis.com {CDNS}",
        f"font-src {me} https://fonts.gstatic.com data: {CDNS}",
        f"img-src {me} data: blob:",
        "media-src data: blob:",
        "connect-src 'none'",
        "frame-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
    ])


def raw_url(ident):
    return "/raw/" + "/".join(urllib.parse.quote(p) for p in ident.split("/"))


FILE_CHUNK = 256 << 10  # bytes per write when streaming a vault file
PDF_INLINE_BUDGET = 40 << 20  # bytes of images one PDF page may inline


def data_url(path):
    """A vault image inlined, for PDFs (the renderer has no way back to this server)."""
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{ctype};base64,{base64.b64encode(path.read_bytes()).decode()}"


def inliner(lib, items_by_id):
    """tid -> data: URL for a PDF page; each image read once, the total capped,
    and "" for anything that isn't an image, has gone, or can't be read."""
    cache, used = {}, [0]

    def inline(tid):
        if tid not in cache:
            p = lib.resolve(tid) if items_by_id.get(tid, {}).get("kind") in ("image", "svg") else None
            try:
                url = data_url(p) if p and used[0] + p.stat().st_size <= PDF_INLINE_BUDGET else ""
            except OSError:
                url = ""
            used[0] += len(url)
            cache[tid] = url
        return cache[tid]
    return inline


# ------------------------------------------------------------------ the vault

class Library:
    SCAN_TTL = 2.0  # seconds a vault scan is reused
    TEXT_CACHE_CHARS = 48 << 20  # bound on cached search text (text plus its lowercase copy)

    def __init__(self, roots, ttl=SCAN_TTL):
        self.roots = [r.expanduser().resolve() for r in roots]
        self.ttl = ttl
        self._links = {}  # path -> (mtime, [targets])
        self.lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._snap, self._snap_at = None, 0.0
        self._texts = collections.OrderedDict()  # id -> (mtime, size, text, lowered), for search
        self._texts_chars = 0
        self._texts_lock = threading.Lock()

    def search_text(self, it):
        """(text, lowered) of a note/text/html item for search, cached by (id, mtime, size);
        None if it cannot be read. Oldest entries are dropped past TEXT_CACHE_CHARS."""
        key = (it["mtime"], it["size"])
        with self._texts_lock:
            hit = self._texts.get(it["id"])
            if hit and hit[:2] == key:
                self._texts.move_to_end(it["id"])
                return hit[2], hit[3]
        p = self.resolve(it["id"])
        try:
            text = p.read_text(errors="replace")
        except (OSError, AttributeError):
            return None
        if it["kind"] == "html":
            text = re.sub(r"<[^>]+>", " ", text)
        elif it["kind"] == "canvas":
            text = canvas_search_text(text)
        low = text.lower()
        cost = len(text) + len(low)
        if cost <= self.TEXT_CACHE_CHARS // 4:
            with self._texts_lock:
                old = self._texts.pop(it["id"], None)
                if old:
                    self._texts_chars -= len(old[2]) + len(old[3])
                self._texts[it["id"]] = (*key, text, low)
                self._texts_chars += cost
                while self._texts_chars > self.TEXT_CACHE_CHARS:
                    _, (_, _, t, lo) = self._texts.popitem(last=False)
                    self._texts_chars -= len(t) + len(lo)
        return text, low

    def scan(self):
        """The vault's items, sorted by id. Cached for SCAN_TTL seconds across every
        caller (tabs, search, note renders); the list is shared, so do not mutate it."""
        return self.snapshot()[0]

    def snapshot(self):
        """(items, JSON text of the items, strong ETag) from one cached scan.
        Edits, new files and deletions show up after at most the TTL."""
        with self._scan_lock:
            now = time.monotonic()
            if self._snap is None or now - self._snap_at >= self.ttl:
                items = self._walk()
                body = json.dumps(items, separators=(",", ":"))
                etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:24] + '"'
                self._snap, self._snap_at = (items, body, etag), now
            return self._snap

    def _walk(self):
        items = []
        for root in self.roots:
            if not root.is_dir():
                continue
            prefix = root.name + "/"
            stack = [(str(root), "")]
            while stack:
                path, rel = stack.pop()
                try:
                    it = os.scandir(path)
                except OSError:
                    continue
                with it:
                    for e in it:
                        name = e.name
                        if name.startswith("."):
                            continue
                        try:
                            if e.is_dir():  # follows symlinks, like os.walk's dirnames
                                if not e.is_symlink():
                                    stack.append((e.path, rel + name + "/"))
                                continue
                            kind = KINDS.get(os.path.splitext(name)[1].lower())
                            if kind in (None, "asset"):
                                continue
                            st = e.stat()
                        except OSError:
                            continue
                        items.append({"id": prefix + rel + name, "name": name, "kind": kind,
                                      "mtime": st.st_mtime, "size": st.st_size})
        items.sort(key=lambda i: i["id"].lower())
        return items

    def resolve(self, ident):
        """Vault-relative id -> real path, or None if outside the roots, hidden or of an unknown type."""
        root_name, _, rel = ident.partition("/")
        for root in self.roots:
            if root.name != root_name or not rel:
                continue
            if any(part.startswith(".") or part == ".." for part in Path(rel).parts):
                return None
            p = (root / rel).resolve()
            if root not in p.parents or not p.is_file() or p.suffix.lower() not in KINDS:
                return None
            return p
        return None

    # Obsidian-style link resolution: by file name anywhere in the vault
    # ("note" means note.md), or by a path suffix ("folder/note").
    @staticmethod
    def index(items):
        by_name = {}
        for it in items:
            name = it["name"].lower()
            by_name.setdefault(name, []).append(it["id"])
            if name.endswith(".md"):
                by_name.setdefault(name[:-3], []).append(it["id"])
        return by_name

    @staticmethod
    def link(target, by_name, items_by_id):
        t = target.strip().lower()
        if not t:
            return None
        cands = by_name.get(t.rsplit("/", 1)[-1], [])
        if "/" in t:
            want = t if "." in t.rsplit("/", 1)[-1] else t + ".md"
            cands = [c for c in cands if c.lower().endswith("/" + want)]
        cands = [c for c in cands if c in items_by_id]
        return min(cands, key=len) if cands else None

    def outgoing(self, path):
        """Wikilink targets in a note, cached by mtime (for backlinks)."""
        try:
            m = path.stat().st_mtime
        except OSError:
            return []
        with self.lock:
            hit = self._links.get(path)
            if hit and hit[0] == m:
                return hit[1]
        found = []
        map_outside_code(path.read_text(errors="replace"),
                         lambda s: found.extend(re.findall(r"\[\[([^\]\n]+)\]\]", s)) or s)
        targets = [t.split("|")[0].split("#")[0] for t in found]
        with self.lock:
            self._links[path] = (m, targets)
        return targets


FENCE = re.compile(r"^(```|~~~)")


def map_outside_code(text, fn):
    """Apply fn to the parts of Markdown text outside fenced blocks and `code` spans."""
    out, buf, in_fence = [], [], False

    def flush():
        if buf:
            parts = re.split(r"(`+[^`\n]*`+)", "".join(buf))
            out.append("".join(p if i % 2 else fn(p) for i, p in enumerate(parts)))
            buf.clear()
    for line in text.splitlines(keepends=True):
        if FENCE.match(line.lstrip()):
            if not in_fence:
                flush()
            in_fence = not in_fence
            out.append(line)
        elif in_fence:
            out.append(line)
        else:
            buf.append(line)
    flush()
    return "".join(out)


def split_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            nl = text.find("\n", end + 4)
            return text[4:end], text[(nl + 1) if nl != -1 else len(text):]
    return None, text


def parse_frontmatter(fm):
    """Enough YAML for Obsidian properties: scalars, [a, b] lists and - item lists."""
    props, key = [], None
    for line in fm.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([\w.-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            elif val == "":
                val = []
            else:
                val = val.strip("'\"")
            props.append([key, val])
        elif key and re.match(r"^\s+-\s+", line) and props and isinstance(props[-1][1], list):
            props[-1][1].append(re.sub(r"^\s+-\s+", "", line).strip().strip("'\""))
    return props


# ------------------------------------------------------------------ rendering

NOTE_CSS = """
:root { color-scheme: dark; --bg:#1e1e22; --text:#dcddde; --dim:#9a9aa5; --line:#34343b; --accent:#a88bfa;
  --code:#26262c; --unres:#7d6fb0; }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body { margin:0; background:var(--bg); color:var(--text);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 36px 40px 80px; overflow-wrap: break-word; }
h1,h2,h3,h4,h5,h6 { line-height:1.3; margin: 1.6em 0 .5em; font-weight:650; }
h1 { font-size: 2em; margin-top: .2em; } h2 { font-size: 1.5em; border-bottom:1px solid var(--line); padding-bottom:.2em; }
h3 { font-size: 1.22em; } h4 { font-size: 1.05em; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
a.internal.unresolved { color: var(--unres); opacity:.75; cursor: default; text-decoration: none; }
a.external::after { content: " \\2197"; font-size: .8em; }
code { font: .88em/1.5 "JetBrains Mono", ui-monospace, Menlo, monospace; background: var(--code);
  padding: .12em .35em; border-radius: 4px; overflow-wrap: anywhere; }
pre { background: var(--code); border-radius: 8px; padding: 14px 16px; overflow:auto; }
pre code { background: none; padding: 0; font-size: .85em; overflow-wrap: normal; }
pre code.hljs { background: none; padding: 0; }
blockquote { margin: 1em 0; padding: .2em 1em; border-left: 3px solid var(--line); color: var(--dim); }
table { border-collapse: collapse; margin: 1em 0; display:block; overflow:auto; }
th, td { border: 1px solid var(--line); padding: 6px 10px; text-align:left; vertical-align: top; }
th { background: #26262c; }
img { max-width: 100%; height: auto; }
img.embed { display:block; margin: 1em auto; border-radius: 6px; cursor: zoom-in; }
hr { border: 0; border-top: 1px solid var(--line); margin: 2em 0; }
ul, ol { padding-left: 1.5em; } li { margin: .15em 0; }
.props { border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin: 0 0 1.5em; font-size:.85em; }
.props div { display:flex; gap:12px; padding:2px 0; } .props .k { color:var(--dim); min-width:90px; }
.pill { display:inline-block; background:#2e2a40; color:#c9b8ff; border-radius: 10px; padding: 0 8px; margin: 0 4px 2px 0; }
.callout { border-radius: 8px; padding: 10px 14px; margin: 1em 0; background: #25252d; border-left: 3px solid var(--accent); }
.callout-title { font-weight: 650; margin-bottom: .2em; }
.callout.warning, .callout.caution, .callout.attention { border-color:#e0a458; } .callout.danger, .callout.error, .callout.bug { border-color:#e06c75; }
.callout.tip, .callout.success, .callout.check, .callout.done { border-color:#7ec699; } .callout.info, .callout.todo { border-color:#61afef; }
.embed-note { border-left: 3px solid var(--accent); padding: 4px 12px; margin: 1em 0; background:#23232a; border-radius: 0 6px 6px 0; }
.backlinks { margin-top: 3em; padding-top: 1em; border-top: 1px solid var(--line); font-size: .9em; }
.backlinks h4 { color: var(--dim); margin: 0 0 .4em; font-weight: 600; text-transform: uppercase; letter-spacing: .5px; font-size:.8em; }
.path { color: var(--dim); font-size: .8em; margin-bottom: .8em; }
.embed-canvas::before { content: "\\25A6  "; color: var(--dim); }
.base-view h2, .base-view h3, .base-view h4 { margin-top: 1.2em; }
.base-view .count { color: var(--dim); font-size: .82em; margin: -.4em 0 1em; }
.base-embed { border-left: 3px solid var(--accent); padding: 2px 0 2px 12px; margin: 1em 0; }
.base-embed .base-name { color: var(--dim); font-size: .85em; }
.base-error { border-radius: 8px; padding: 10px 14px; margin: 1em 0; background: #25252d; border-left: 3px solid #e06c75; }
pre.mermaid { background: #23232a; text-align:center; padding: 16px; overflow: auto; color: transparent; min-height: 60px; max-height: 85vh; }
pre.mermaid svg { max-width: none; }
pre.mermaid[data-processed] { color: inherit; }
pre.mermaid svg { cursor: zoom-in; height: auto; }
.diagram-hint { color: var(--dim); font-size: .75em; text-align: right; margin: -0.6em 0 1em; }
/* Copy button on code blocks: in a wrapper, so it stays in the corner while the
   code scrolls sideways. Shown on hover or focus; always on touch screens. */
.codewrap { position: relative; margin: 1em 0; }
.codewrap > pre { margin: 0; }
.copy { position: absolute; top: 6px; right: 6px; font: 600 .72em/1 system-ui, sans-serif; color: var(--dim);
  background: #2a2a31; border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; cursor: pointer;
  opacity: 0; transition: opacity .15s; }
.codewrap:hover .copy, .copy:focus-visible, .copy.done { opacity: 1; }
.copy:hover { color: var(--text); }
.copy.done { color: #7ec699; border-color: #7ec699; }
@media (hover: none) { .copy { opacity: 1; } }
@media print { .copy { display: none; } }
/* Phones: tighter margins, smaller headings, and wide blocks (code, tables,
   diagrams) scroll sideways inside themselves instead of widening the page. */
@media (max-width: 640px) {
  main { padding: 18px 16px 60px; }
  h1 { font-size: 1.6em; } h2 { font-size: 1.3em; } h3 { font-size: 1.12em; }
  pre { padding: 12px; border-radius: 6px; }
  pre.mermaid { padding: 10px; }
  ul, ol { padding-left: 1.25em; }
  th, td { padding: 5px 8px; }
  table { font-size: .92em; }
  .props { padding: 6px 10px; }
  .props div { gap: 8px; } .props .k { min-width: 64px; }
  .props div > span:last-child { min-width: 0; overflow-wrap: anywhere; }
  .callout { padding: 8px 12px; }
}
"""

# A PDF is printed and shared, so it's light, and nothing in it may scroll:
# code wraps, tables and diagrams fit the page.
PDF_CSS = """
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin:0; background:#fff; color:#1f2328;
  font: 10.5pt/1.55 "Inter", "Noto Sans", "DejaVu Sans", sans-serif; }
main { overflow-wrap: break-word; }
h1,h2,h3,h4,h5,h6 { line-height:1.3; margin: 1.4em 0 .5em; font-weight:650; break-after: avoid; }
h1 { font-size: 1.9em; margin-top: 0; } h2 { font-size: 1.45em; border-bottom:1px solid #d8dee4; padding-bottom:.2em; }
h3 { font-size: 1.2em; } h4 { font-size: 1.05em; }
a { color: #5b3fd1; text-decoration: none; }
a.internal { color: inherit; }
/* Code shows the characters as typed (no "->" arrow ligature). Inline code
   draws its side padding as a shadow: real padding leaves a gap that PDF
   readers copy as a space ("NEW.conf ." instead of "NEW.conf."). */
code { font: .9em/1.5 "JetBrains Mono", "DejaVu Sans Mono", monospace; background: #f3f4f6;
  font-variant-ligatures: none; padding: .1em 0; border-radius: 4px; overflow-wrap: anywhere;
  box-shadow: .25em 0 0 #f3f4f6, -.25em 0 0 #f3f4f6; }
pre { background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px 12px;
  white-space: pre-wrap; overflow-wrap: anywhere; }
pre code, pre code.hljs { background: none; padding: 0; box-shadow: none; overflow: visible; font-size: .85em; }
blockquote { margin: 1em 0; padding: .2em 1em; border-left: 3px solid #d0d7de; color: #57606a; }
table { border-collapse: collapse; margin: 1em 0; font-size: .92em; }
th, td { border: 1px solid #d0d7de; padding: 5px 9px; text-align:left; vertical-align: top; }
th { background: #f6f8fa; }
tr, img, .callout, .embed-note, pre.mermaid { break-inside: avoid; }
img { max-width: 100%; height: auto; }
img.embed { display:block; margin: 1em auto; }
img[src=""] { display: none; }  /* not inlined (see inliner): no broken-image icon */
hr { border: 0; border-top: 1px solid #d8dee4; margin: 1.6em 0; }
ul, ol { padding-left: 1.5em; } li { margin: .15em 0; }
.callout { border-radius: 6px; padding: 8px 12px; margin: 1em 0; background: #f6f3ff; border-left: 3px solid #8b6cf0; }
.callout-title { font-weight: 650; margin-bottom: .2em; }
.callout.warning, .callout.caution, .callout.attention { border-color:#d4892a; background:#fdf6ec; }
.callout.danger, .callout.error, .callout.bug { border-color:#cf4b55; background:#fdf0f1; }
.callout.tip, .callout.success, .callout.check, .callout.done { border-color:#3f9d63; background:#eff8f2; }
.callout.info, .callout.todo { border-color:#3b82c4; background:#eef5fc; }
.embed-note { border-left: 3px solid #8b6cf0; padding: 4px 12px; margin: 1em 0; background:#f6f8fa; }
pre.mermaid { background: none; border: 0; padding: 0; text-align: center; white-space: normal; }
pre.mermaid svg { max-width: 100%; height: auto; }
"""

NOTE_JS = """
document.addEventListener('click', (e) => {
  const a = e.target.closest('[data-id]');
  if (!a) return;
  e.preventDefault();
  parent.postMessage({type: 'vault-open', id: a.dataset.id, anchor: a.dataset.anchor || ''}, '*');
});
let t = null;
addEventListener('scroll', () => { clearTimeout(t); t = setTimeout(() =>
  parent.postMessage({type: 'vault-scroll', y: Math.round(scrollY)}, '*'), 150); });
addEventListener('load', () => {
  const y = new URLSearchParams(location.search).get('y');
  if (y && !location.hash) scrollTo(0, +y);
});
// Copy buttons on code blocks. The page is plain HTTP in a sandboxed frame, so
// the async clipboard API is usually missing here: fall back to execCommand,
// and if the frame may not copy either, ask the viewer page (which shares the
// click's user activation) to copy it.
function execCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}
let copyPending = null;
function copied(btn, ok) {
  clearTimeout(btn._t);
  if (ok) { btn.textContent = 'Copied'; btn.classList.add('done'); }
  else {  // leave the code selected, so the keyboard shortcut works
    const r = document.createRange(); r.selectNodeContents(btn._code);
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r);
    btn.textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? 'Press \\u2318C' : 'Press Ctrl+C';
  }
  btn._t = setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('done'); }, 1800);
}
addEventListener('message', (e) => {
  if (e.source === parent && e.data && e.data.type === 'vault-copied' && copyPending) {
    copied(copyPending, e.data.ok === true); copyPending = null;
  }
});
function addCopyButtons() {
  document.querySelectorAll('pre > code').forEach((code) => {
    const pre = code.parentElement;
    if (pre.parentElement.classList.contains('codewrap')) return;
    const wrap = document.createElement('div'); wrap.className = 'codewrap';
    pre.before(wrap); wrap.appendChild(pre);
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'copy'; btn.textContent = 'Copy';
    btn.setAttribute('aria-label', 'Copy code'); btn._code = code;
    btn.addEventListener('click', async () => {
      const text = code.textContent.replace(/\\n$/, '');
      if (navigator.clipboard && window.isSecureContext) {
        try { await navigator.clipboard.writeText(text); return copied(btn, true); } catch (e) { /* fall through */ }
      }
      if (execCopy(text)) return copied(btn, true);
      if (parent !== window) { copyPending = btn; parent.postMessage({type: 'vault-copy', text}, '*'); }
      else copied(btn, false);
    });
    wrap.appendChild(btn);
  });
}
"""

# Mermaid 11 with the ELK layout engine (much better subgraph and edge routing
# than the default dagre); falls back to dagre if ELK can't load. In the viewer
# each diagram opens full screen on click. For a PDF it loads the copy baked
# into the PDF runtime, uses the light theme, fits diagrams to the page width and
# flags the page ready once every diagram is drawn.
def mermaid_head(pdf=False):
    src, elk = (PDF_MERMAID, PDF_MERMAID_ELK) if pdf else (MERMAID, MERMAID_ELK)
    fit = "true" if pdf else "false"
    after = "" if pdf else """
  const svg = pre.querySelector("svg");
  if (!svg) continue;
  const hint = document.createElement("div");
  hint.className = "diagram-hint";
  hint.textContent = (matchMedia("(hover: none)").matches ? "tap" : "click") + " to open full screen";
  pre.after(hint);
  svg.addEventListener("click", () => {
    const c = svg.cloneNode(true);
    const vb = svg.viewBox.baseVal;
    c.setAttribute("width", vb.width); c.setAttribute("height", vb.height);
    c.removeAttribute("style");
    c.setAttribute("style", "background:#1e1e22");
    parent.postMessage({type: "vault-zoom", svg: new XMLSerializer().serializeToString(c),
                         title: "diagram " + (i + 1) + " in " + document.title}, "*");
  });"""
    return f"""<script type="module">
import mermaid from "{src}";
// Stop Mermaid's own render-on-load right away: otherwise it races the ELK
// import below and draws the first diagrams with the default theme.
mermaid.initialize({{ startOnLoad: false }});
let layout = new URLSearchParams(location.search).get("layout") === "dagre" ? "dagre" : "elk";
if (layout === "elk") {{
  try {{ mermaid.registerLayoutLoaders((await import("{elk}")).default); }}
  catch (e) {{ layout = "dagre"; console.warn("mermaid ELK layout unavailable, using dagre", e); }}
}}
mermaid.initialize({{
  startOnLoad: false, theme: "{'default' if pdf else 'dark'}", securityLevel: "strict", layout,
  fontFamily: "-apple-system, BlinkMacSystemFont, Inter, Segoe UI, sans-serif",
  // Viewer: natural size, never shrunk to the column: wide diagrams scroll
  // sideways (and open full screen on click) instead of turning into
  // unreadable text. PDF: shrunk to the page, which can't scroll.
  flowchart: {{ useMaxWidth: {fit}, htmlLabels: true, padding: 12 }},
  sequence: {{ useMaxWidth: {fit}, wrap: true }},
  state: {{ useMaxWidth: {fit} }}, class: {{ useMaxWidth: {fit} }}, er: {{ useMaxWidth: {fit} }},
  gantt: {{ useMaxWidth: {fit} }}, journey: {{ useMaxWidth: {fit} }}, mindmap: {{ useMaxWidth: {fit} }},
  elk: {{ mergeEdges: false, nodePlacementStrategy: "BRANDES_KOEPF" }},
}});
const blocks = [...document.querySelectorAll("pre.mermaid")];
for (const [i, pre] of blocks.entries()) {{
  try {{ await mermaid.run({{ nodes: [pre] }}); }}
  catch (e) {{
    pre.removeAttribute("data-processed");
    pre.style.color = "#e06c75"; pre.style.textAlign = "left";
    pre.textContent = "Mermaid could not render this diagram:\\n" + (e.message || e) + "\\n\\n" + pre.dataset.src;
    continue;
  }}{after}
}}{chr(10) + 'document.documentElement.dataset.ready = "1";' if pdf else ""}
</script>"""


def render_page(title, body, extra_head="", pdf=False):
    if pdf:  # the page PDF_RENDER prints; ready at once unless the Mermaid script (extra_head) has diagrams to draw
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{PDF_CSP}"><title>{html.escape(title)}</title>
<link rel="stylesheet" href="{PDF_HLJS}/styles/github.min.css">
<style>{PDF_CSS}</style>{extra_head}</head>
<body><main>{body}</main>
<script src="{PDF_HLJS}/highlight.min.js"></script>
<script>
document.querySelectorAll('pre code:not(.language-mermaid)').forEach((b) => {{ try {{ hljs.highlightElement(b); }} catch (e) {{}} }});
{"" if extra_head else 'document.documentElement.dataset.ready = "1";'}
</script></body></html>"""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<link rel="stylesheet" href="{HLJS}/styles/github-dark.min.css">
<style>{NOTE_CSS}</style>{extra_head}</head>
<body><main>{body}</main>
<script src="{HLJS}/highlight.min.js"></script>
<script>{NOTE_JS}
document.querySelectorAll('pre code:not(.language-mermaid)').forEach((b) => {{ try {{ hljs.highlightElement(b); }} catch (e) {{}} }});
addCopyButtons();
</script></body></html>"""


def mermaid_blocks(out):
    """<pre><code class="language-mermaid"> -> <pre class="mermaid"> (source kept for error reports)."""
    def repl(m):
        src = m.group(1)
        return f'<pre class="mermaid" data-src="{html.escape(html.unescape(src), quote=True)}">{src}</pre>'
    return re.sub(r'<pre><code class="language-mermaid">(.*?)</code></pre>', repl, out, flags=re.S)


def render_markdown(lib, body, ident, items_by_id, by_name, pdf=False, inline=None, embed_bases=True, budget=None):
    """Markdown text to (HTML, extra <head> markup): wikilinks, embeds, callouts, relative
    links and Mermaid. The one pipeline for notes and for the text of canvas nodes, so both
    are treated alike. embed_bases=False shows ![[x.base]] as a link instead of evaluating it.
    budget (a BaseBudget) is shared by everything one page evaluates; a new one if None."""
    note_dir = ident.rsplit("/", 1)[0]
    budget = budget or BaseBudget()
    base_embeds = []  # (id, view or None), swapped in after Markdown has run
    # A random per-render token marks where a base goes: nothing a note says can forge it.
    token = "zz" + secrets.token_hex(8) + "n"

    def wikilink(m):
        bang, inner = m.group(1), m.group(2)
        target, _, alias = inner.partition("|")
        target, _, anchor = target.partition("#")
        tid = lib.link(target, by_name, items_by_id) if target else ident
        label = html.escape(alias or (target + (" > " + anchor if anchor else "")) or anchor)
        if not tid:
            return f'<a class="internal unresolved" title="not in the vault">{label}</a>'
        kind = items_by_id[tid]["kind"]
        attrs = f'href="#{html.escape(tid)}" data-id="{html.escape(tid)}"'
        if anchor:
            attrs += f' data-anchor="{html.escape(slugify(anchor, "-"))}"'
        if pdf:
            attrs = ""
        if bang and kind in ("svg", "image"):
            width = f' style="width:{int(alias)}px"' if alias.isdigit() else ""
            if pdf:
                return f'<img class="embed" src="{inline(tid)}" alt="{html.escape(target)}"{width}>'
            return (f'<img class="embed" src="{raw_url(tid)}?v={int(items_by_id[tid]["mtime"])}" alt="{html.escape(target)}" '
                    f'title="click to open full screen" data-id="{html.escape(tid)}"{width}>')
        if bang and kind == "note":
            return f'<div class="embed-note">Embedded: <a class="internal" {attrs}>{html.escape(target)}</a></div>'
        if bang and kind == "canvas":
            return f'<div class="embed-note embed-canvas">Canvas: <a class="internal" {attrs}>{html.escape(target)}</a></div>'
        if bang and kind == "base":
            if pdf or not embed_bases:
                return f'<div class="embed-note">Base: <a class="internal" {attrs}>{html.escape(target)}</a></div>'
            if len(base_embeds) >= BASE_MAX_EMBEDS:
                budget.truncated = True
                return (f'<div class="embed-note">Base: <a class="internal" {attrs}>{html.escape(target)}</a> '
                        f'(not evaluated: at most {BASE_MAX_EMBEDS} base embeds per page)</div>')
            base_embeds.append((tid, anchor.strip() or None))
            return f"{token}{len(base_embeds) - 1}zz"
        return f'<a class="internal" {attrs}>{label}</a>'

    body = map_outside_code(body, lambda s: re.sub(r"(!?)\[\[([^\]\n]+)\]\]", wikilink, s))
    if markdown:
        out = markdown.markdown(body, extensions=["tables", "fenced_code", "sane_lists", "toc"])
    else:
        out = f"<pre>{html.escape(body)}</pre>"

    # Callouts: > [!type] Title
    def callout(m):
        kind, title, rest = m.group(1).lower(), m.group(3).strip() or m.group(1).capitalize(), m.group(4)
        return f'<div class="callout {html.escape(kind)}"><div class="callout-title">{title}</div><p>{rest}</div>'
    out = re.sub(r"<blockquote>\s*<p>\[!(\w+)\]([+-]?)[ \t]*([^\n<]*)\n?(.*?)</blockquote>", callout, out, flags=re.S)

    # Relative Markdown links and images: resolve against the note's folder.
    def rel(m):
        attr, url = m.group(1), html.unescape(m.group(2))
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            if attr == "href":
                return f'{attr}="{html.escape(url)}" target="_blank" rel="noopener noreferrer" class="external"'
            return m.group(0)
        if url.startswith("#") or url.startswith("/"):
            return m.group(0)
        path_part, _, anchor = url.partition("#")
        tid = os.path.normpath(f"{note_dir}/{urllib.parse.unquote(path_part)}")
        if tid not in items_by_id:
            return m.group(0)
        if attr == "src":
            return f'src="{inline(tid) if pdf else raw_url(tid)}"'
        if pdf:
            return 'class="internal"'
        extra = f' data-anchor="{html.escape(anchor)}"' if anchor else ""
        return f'href="#{html.escape(tid)}" data-id="{html.escape(tid)}"{extra}'
    out = re.sub(r'\b(href|src)="([^"]*)"', rel, out)

    # A base embed is evaluated by the Obsidian CLI and swapped in last, so none of the
    # passes above touch its markup (a base's own links are built with this viewer's format).
    def known(m):
        return int(m.group(1)) < len(base_embeds)

    def base_embed(m):
        if not known(m):
            return m.group(0)
        tid, view = base_embeds[int(m.group(1))]
        return base_html(lib, tid, items_by_id, view, embed=True, budget=budget)

    def in_heading(m):  # a heading holds a link, not a table, and its id must stay readable
        def card(t):
            if not known(t):
                return t.group(0)
            tid = base_embeds[int(t.group(1))][0]
            return f'<a class="internal" {vault_ref(tid)}>{html.escape(items_by_id[tid]["name"])}</a>'
        tag = re.sub(re.escape(token) + r"\d+zz", "base", m.group(2))
        return f"<h{m.group(1)}{tag}>" + re.sub(re.escape(token) + r"(\d+)zz", card, m.group(3)) + f"</h{m.group(1)}>"
    if base_embeds:
        out = re.sub(r"<h([1-6])\b([^>]*)>(.*?)</h\1>", in_heading, out, flags=re.S)
        out = re.sub(r"(?:<p>\s*)?" + re.escape(token) + r"(\d+)zz(?:\s*</p>)?", base_embed, out)

    head = ""
    if 'class="language-mermaid"' in out:
        out = mermaid_blocks(out)
        head = mermaid_head(pdf)
    return out, head


def render_note(lib, path, ident, pdf=False):
    """A note as a viewer page, or with pdf=True as the page a PDF prints: light,
    images inlined, vault links as plain text, no properties or backlinks."""
    items = lib.scan()
    items_by_id = {i["id"]: i for i in items}
    by_name = lib.index(items)
    text = path.read_text(errors="replace")
    fm, body = split_frontmatter(text)
    inline = inliner(lib, items_by_id) if pdf else None
    out, head = render_markdown(lib, body, ident, items_by_id, by_name, pdf, inline)
    if pdf:
        return render_page(path.stem, out, head, pdf=True)

    props = ""
    if fm:
        rows = []
        for k, v in parse_frontmatter(fm):
            val = " ".join(f'<span class="pill">{html.escape(x)}</span>' for x in v) if isinstance(v, list) else html.escape(v)
            rows.append(f'<div><span class="k">{html.escape(k)}</span><span>{val}</span></div>')
        props = f'<div class="props">{"".join(rows)}</div>' if rows else ""

    # Backlinks: notes whose [[links]] resolve to this file.
    back = []
    for it in items:
        if it["kind"] != "note" or it["id"] == ident:
            continue
        p = lib.resolve(it["id"])
        if p and any(lib.link(t, by_name, items_by_id) == ident for t in lib.outgoing(p)):
            back.append(it)
    bl = ""
    if back:
        links = "".join(f'<li><a class="internal" href="#{html.escape(b["id"])}" data-id="{html.escape(b["id"])}">'
                        f'{html.escape(b["name"][:-3])}</a></li>' for b in back)
        bl = f'<div class="backlinks"><h4>Linked from</h4><ul>{links}</ul></div>'

    crumbs = html.escape(" / ".join(ident.split("/")[1:-1]))
    return render_page(path.stem, f'<div class="path">{crumbs}</div>{props}{out}{bl}', head)


def render_text(path, ident, pdf=False):
    ext = path.suffix[1:].lower()
    src = path.read_text(errors="replace")
    crumbs = "" if pdf else f'<div class="path">{html.escape(" / ".join(ident.split("/")[1:-1]))}</div>'
    if ext == "mmd":  # a standalone Mermaid file renders as the diagram
        body = (f'{crumbs}<h1>{html.escape(path.name)}</h1>'
                + mermaid_blocks(f'<pre><code class="language-mermaid">{html.escape(src)}</code></pre>'))
        return render_page(path.name, body, mermaid_head(pdf), pdf)
    lang = {"py": "python", "sh": "bash", "ts": "typescript", "js": "javascript", "yml": "yaml"}.get(ext, ext)
    body = (f'{crumbs}<h1>{html.escape(path.name)}</h1>'
            f'<pre><code class="language-{html.escape(lang)}">{html.escape(src)}</code></pre>')
    return render_page(path.name, body, pdf=pdf)


# ------------------------------------------------------------------ Obsidian bases

BASE_TIMEOUT = 15  # seconds one CLI call may take
BASE_TTL = 5.0  # seconds a query result is reused
BASE_CACHE_MAX = 64  # results kept
BASE_MAX_ROWS = 2000  # rows shown per view
BASE_MAX_OUTPUT = 8 << 20  # bytes of CLI output accepted (the call is killed past this)
BASE_MAX_VIEWS = 20  # views queried per base
BASE_MAX_EMBEDS = 20  # base embeds evaluated per page
BASE_PAGE_BUDGET = 30.0  # seconds of CLI time one page may spend in all
BASE_SLOT_WAIT = 10.0  # seconds to wait for a free CLI slot
BASE_SLOTS = threading.BoundedSemaphore(2)  # CLI calls at once, like PDF_SLOTS
NO_CLI = "Bases need the Obsidian CLI: not configured (see --obsidian-cli)."


class BaseBudget:
    """What one page may spend on bases: CLI time in total and a count of embeds."""

    def __init__(self, seconds=None):
        self.deadline = time.monotonic() + (BASE_PAGE_BUDGET if seconds is None else seconds)
        self.truncated = False

    def left(self):
        return self.deadline - time.monotonic()


def _read_bounded(proc, timeout, cap):
    """Read a process's stdout (at most cap bytes) and stderr (first 64 KiB kept) until both
    close or time runs out. Returns (stdout, stderr, problem) with problem None, "timeout"
    or "overflow"."""
    sel = selectors.DefaultSelector()
    bufs = {"out": bytearray(), "err": bytearray()}
    for name, f in (("out", proc.stdout), ("err", proc.stderr)):
        os.set_blocking(f.fileno(), False)
        sel.register(f, selectors.EVENT_READ, name)
    end = time.monotonic() + timeout
    try:
        while sel.get_map():
            left = end - time.monotonic()
            if left <= 0:
                return bytes(bufs["out"]), bytes(bufs["err"]), "timeout"
            for key, _ in sel.select(left):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                buf = bufs[key.data]
                if key.data == "out":
                    buf += chunk
                    if len(buf) > cap:
                        return bytes(buf), bytes(bufs["err"]), "overflow"
                elif len(buf) < 65536:
                    buf += chunk
        try:
            proc.wait(timeout=max(0.1, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            return bytes(bufs["out"]), bytes(bufs["err"]), "timeout"
        return bytes(bufs["out"]), bytes(bufs["err"]), None
    finally:
        sel.close()


class BaseEngine:
    """Evaluates .base files through the Obsidian CLI. The command is only ever run as
    `CMD base:query path=<vault-relative .base file> [view=<view>] format=json`, as an
    argument list (no shell, CMD one program path), for a file this viewer resolved inside
    the Obsidian vault. Its output is untrusted: callers escape it."""

    def __init__(self, cli=None, vault=None):
        self.argv = [cli] if cli and cli.strip() and cli.strip() != "none" else None
        self.vault = Path(vault).expanduser().resolve() if vault else None
        self._cache = collections.OrderedDict()  # (id, view, mtime) -> (stamp, rows, error)
        self._inflight = {}  # key -> Event, for a call already running
        self._lock = threading.Lock()

    def relpath(self, path):
        """The path of a .base file as the CLI names it (relative to the Obsidian vault),
        or None if it is not a .base file inside that vault."""
        if self.vault is None or path.suffix.lower() != ".base":
            return None
        try:
            rel = path.resolve().relative_to(self.vault)
        except ValueError:
            return None
        if not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
            return None
        out = rel.as_posix()
        return None if any(ord(c) < 32 for c in out) else out

    def unavailable(self, path):
        """Why this file cannot be evaluated (a message), or None."""
        if not self.argv:
            return NO_CLI
        if self.vault is None:
            return "Bases need the Obsidian CLI: no Obsidian vault is set (see --obsidian-vault)."
        if self.relpath(path) is None:
            return "This base is outside the Obsidian vault the CLI has open (--obsidian-vault), so it cannot be evaluated."
        return None

    def _fresh(self, key):
        hit = self._cache.get(key)
        return hit if hit and time.monotonic() - hit[0] < BASE_TTL else None

    def query(self, path, ident, view, mtime, budget=None):
        """(rows, None) or (None, message). Results are cached briefly, and identical calls
        that overlap share one CLI run."""
        why = self.unavailable(path)
        if why:
            return None, why
        budget = budget or BaseBudget()
        key = (ident, view, mtime)
        with self._lock:
            hit = self._fresh(key)
            if hit:
                return hit[1], hit[2]
            running = self._inflight.get(key)
            if running is None:
                running = self._inflight[key] = threading.Event()
                leader = True
            else:
                leader = False
        if not leader:
            running.wait(max(0.0, min(budget.left(), BASE_TIMEOUT + BASE_SLOT_WAIT)))
            with self._lock:
                hit = self._fresh(key)
            if hit:
                return hit[1], hit[2]
            return None, "The Obsidian CLI did not answer in time."
        try:
            rows, err = self._run(self.relpath(path), view, budget)
            with self._lock:
                self._cache[key] = (time.monotonic(), rows, err)
                self._cache.move_to_end(key)
                while len(self._cache) > BASE_CACHE_MAX:
                    self._cache.popitem(last=False)
            return rows, err
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            running.set()

    def _run(self, rel, view, budget):
        argv = [*self.argv, "base:query", f"path={rel}"]
        if view is not None:
            if any(ord(c) < 32 for c in view):
                return None, "This view name cannot be queried."
            argv.append(f"view={view}")
        argv.append("format=json")
        if budget.left() <= 0:
            return None, "Skipped: time limit for bases on this page."
        if not BASE_SLOTS.acquire(timeout=min(BASE_SLOT_WAIT, max(0.0, budget.left()))):
            return None, "The Obsidian CLI is busy, try again in a moment."
        try:
            try:
                # Its own session, so a timeout or overflow can kill the whole process group
                # (a wrapper's children too), not just the wrapper.
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
            except OSError as e:
                return None, f"The Obsidian CLI could not be run: {e}"
            try:
                out, err, problem = _read_bounded(proc, min(BASE_TIMEOUT, max(0.5, budget.left())), BASE_MAX_OUTPUT)
            finally:
                _kill_group(proc)  # whatever is still running; harmless when nothing is
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                for f in (proc.stdout, proc.stderr):
                    try:
                        f.close()
                    except OSError:
                        pass
        finally:
            BASE_SLOTS.release()
        if problem == "timeout":
            return None, f"The Obsidian CLI did not answer within {BASE_TIMEOUT} s."
        if problem == "overflow":
            return None, f"The Obsidian CLI returned more than {BASE_MAX_OUTPUT >> 20} MiB."
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip().splitlines()
            return None, (msg[0][:300] if msg else f"The Obsidian CLI failed (exit {proc.returncode}).")
        try:
            rows = json.loads(out.decode(errors="replace"))
        except (ValueError, RecursionError):
            return None, "The Obsidian CLI did not return JSON."
        if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
            return None, "The Obsidian CLI returned JSON that is not a list of rows."
        return rows, None


OBSIDIAN = BaseEngine()  # replaced by main() from --obsidian-cli and --obsidian-vault


def base_views(src):
    """[(name, type)] of a .base file's views, in file order; [] when PyYAML is missing,
    the YAML does not parse or it names no views."""
    if yaml is None:
        return []
    try:
        data = yaml.safe_load(src)
    except Exception:  # noqa: BLE001 (yaml raises several types; a bad file just has no views)
        return []
    views = data.get("views") if isinstance(data, dict) else None
    out = []
    for v in views if isinstance(views, list) else []:
        if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip():
            out.append((v["name"], str(v.get("type") or "table")))
    return out


def base_cell(v, depth=0):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if depth > 6:
        return "\u2026"
    if isinstance(v, list):
        return ", ".join(base_cell(x, depth + 1) for x in v)
    if isinstance(v, dict):
        try:
            return json.dumps(v, ensure_ascii=False)
        except (ValueError, RecursionError):
            return "\u2026"
    return str(v)


def vault_ref(tid):
    """href and data-id for a viewer id, the format the viewer page opens."""
    t = html.escape(tid)
    return f'href="#{t}" data-id="{t}"'


def base_table(lib, rows, items_by_id):
    """Rows (dicts) as an HTML table; every value is escaped. `path` is shown as a link to the
    note in the viewer, on the `file name` column when the view has one, else in its own."""
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    name_col = "file name" if "file name" in cols else None
    shown = [c for c in cols if not (c == "path" and name_col)]

    vault = str(OBSIDIAN.vault) if OBSIDIAN.vault is not None else None

    def link_for(row):
        """The viewer id of a row's note. The path is relative to the Obsidian vault, which may
        be inside a viewer root, equal to it, or contain it, so go through the real path."""
        p = row.get("path")
        if vault is None or not isinstance(p, str) or not p:
            return None
        full = os.path.normpath(os.path.join(vault, p))
        if full != vault and not full.startswith(vault.rstrip(os.sep) + os.sep):
            return None
        for root in lib.roots:
            r = str(root).rstrip(os.sep)
            if full.startswith(r + os.sep):
                tid = root.name + "/" + full[len(r) + 1:]
                return tid if tid in items_by_id else None
        return None

    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in shown)
    body = []
    for r in rows[:BASE_MAX_ROWS]:
        tid = link_for(r)
        cells = []
        for c in shown:
            if c == name_col or (c == "path" and not name_col):
                p = r.get("path")
                if c == name_col:
                    text = base_cell(r.get(c))
                elif isinstance(p, str):
                    text = os.path.basename(p)
                    text = text[:-3] if text.lower().endswith(".md") else text
                else:
                    text = base_cell(p)
                if tid:
                    cells.append(f'<td><a class="internal" {vault_ref(tid)}>{html.escape(text)}</a></td>')
                else:
                    cells.append(f"<td>{html.escape(text)}</td>")
            else:
                cells.append(f"<td>{html.escape(base_cell(r.get(c)))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    n = len(rows)
    count = f'<p class="count">{n} row{"s" if n != 1 else ""}' + (f", showing the first {BASE_MAX_ROWS}" if n > BASE_MAX_ROWS else "") + "</p>"
    if not rows:
        return count
    return f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>{count}'


def base_html(lib, tid, items_by_id, only_view=None, embed=False, budget=None):
    """The evaluated views of a base (all, or only_view) as an HTML fragment, or the reason
    it cannot be shown with the base's YAML. Never raises."""
    path = lib.resolve(tid)
    if path is None or path.suffix.lower() != ".base":
        return '<div class="base-error">This base is not in the vault.</div>'
    try:
        src = path.read_text(errors="replace")
    except OSError:
        return '<div class="base-error">This base cannot be read.</div>'
    h = "h4" if embed else "h2"

    def source_block():
        return f'<p class="base-name">The base file:</p><pre><code class="language-yaml">{html.escape(src)}</code></pre>'

    budget = budget or BaseBudget()
    views = base_views(src)
    if only_view is not None and yaml is None:
        views = [(only_view, "table")]  # the views cannot be listed without PyYAML: the CLI checks the name
    elif only_view is not None:
        views = [v for v in views if v[0] == only_view]
        if not views:
            return (f'<div class="base-error">No view named "{html.escape(only_view[:100])}" in this base.</div>'
                    + source_block())
    mtime = items_by_id.get(tid, {}).get("mtime", 0)
    why = OBSIDIAN.unavailable(path)
    if why:
        out = f'<div class="base-error">{html.escape(why)}</div>' + source_block()
    else:
        parts, failed = [], False
        if len(views) > BASE_MAX_VIEWS:
            parts.append(f'<div class="base-error">This base has {len(views)} views: showing the first {BASE_MAX_VIEWS}.</div>')
            views = views[:BASE_MAX_VIEWS]
        for name, vtype in views or [(None, "table")]:
            rows, err = OBSIDIAN.query(path, tid, name, mtime, budget)
            title = ""
            if name is not None:
                title = html.escape(name) + (f" ({html.escape(vtype)})" if vtype != "table" else "")
                title = f"<{h}>{title}</{h}>"
            if err:
                failed = True
                parts.append(f'<div class="base-view">{title}<div class="base-error">{html.escape(err)}</div></div>')
            else:
                parts.append(f'<div class="base-view">{title}{base_table(lib, rows, items_by_id)}</div>')
        out = "".join(parts) + (source_block() if failed else "")
    if embed:
        out = (f'<div class="base-embed"><div class="base-name">Base: <a class="internal" {vault_ref(tid)}>'
               f'{html.escape(path.stem)}</a></div>{out}</div>')
    return out


def render_base(lib, path, ident):
    items_by_id = {i["id"]: i for i in lib.scan()}
    crumbs = html.escape(" / ".join(ident.split("/")[1:-1]))
    body = (f'<div class="path">{crumbs}</div><h1>{html.escape(path.stem)}</h1>'
            + base_html(lib, ident, items_by_id, budget=BaseBudget()))
    return render_page(path.stem, body)


# ------------------------------------------------------------------ JSON Canvas

CANVAS_COLORS = {"1": "#fb464c", "2": "#e9973f", "3": "#e0de71", "4": "#44cf6e", "5": "#53dfdd", "6": "#a882ff"}
CANVAS_MAX_NODES, CANVAS_MAX_EDGES = 2000, 5000
CANVAS_MAX_BYTES = 8 << 20
SIDE_VEC = {"top": (0, -1), "right": (1, 0), "bottom": (0, 1), "left": (-1, 0)}
EXCERPT_CHARS = 600


def canvas_color(v):
    """A preset number or a #hex color as a CSS color, else None (nothing else reaches a style)."""
    if isinstance(v, str):
        if v in CANVAS_COLORS:
            return CANVAS_COLORS[v]
        if re.fullmatch(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", v):
            return v
    return None


def _num(v, default, lo=-10_000_000, hi=10_000_000):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return default
    return max(lo, min(hi, v))


def parse_canvas(text):
    """(nodes, edges) as clean dicts from JSON Canvas text; raises ValueError (JSONDecodeError
    included) if it is not a JSON object. Missing or odd fields get defaults, unusable nodes
    and edges are dropped."""
    try:
        data = json.loads(text)
    except RecursionError:
        raise ValueError("nested too deeply") from None
    if not isinstance(data, dict):
        raise ValueError("the top level is not a JSON object")
    nodes, ids = [], set()
    for n in (data.get("nodes") if isinstance(data.get("nodes"), list) else [])[:CANVAS_MAX_NODES]:
        if not isinstance(n, dict) or not isinstance(n.get("id"), str):
            continue
        nodes.append({**n, "x": _num(n.get("x"), 0), "y": _num(n.get("y"), 0),
                      "width": _num(n.get("width"), 250, 1), "height": _num(n.get("height"), 60, 1)})
        ids.add(n["id"])
    edges = []
    for e in (data.get("edges") if isinstance(data.get("edges"), list) else [])[:CANVAS_MAX_EDGES]:
        if isinstance(e, dict) and e.get("fromNode") in ids and e.get("toNode") in ids:
            edges.append(e)
    return nodes, edges


def canvas_search_text(text):
    """What search matches in a canvas: text nodes, group labels, edge labels, file names."""
    try:
        nodes, edges = parse_canvas(text)
    except ValueError:
        return ""
    parts = []
    for n in nodes:
        for k in ("text", "label", "file", "url"):
            if isinstance(n.get(k), str):
                parts.append(n[k])
    parts += [e["label"] for e in edges if isinstance(e.get("label"), str)]
    return "\n".join(parts)


def _auto_sides(a, b):
    dx = (b["x"] + b["width"] / 2) - (a["x"] + a["width"] / 2)
    dy = (b["y"] + b["height"] / 2) - (a["y"] + a["height"] / 2)
    if abs(dx) >= abs(dy):
        return ("right", "left") if dx >= 0 else ("left", "right")
    return ("bottom", "top") if dy >= 0 else ("top", "bottom")


def _anchor(n, side):
    return {"top": (n["x"] + n["width"] / 2, n["y"]), "bottom": (n["x"] + n["width"] / 2, n["y"] + n["height"]),
            "left": (n["x"], n["y"] + n["height"] / 2), "right": (n["x"] + n["width"], n["y"] + n["height"] / 2)}[side]


def _arrow(p, side):
    """An arrowhead with its tip at p, on the `side` of the node it points into."""
    dx, dy = SIDE_VEC[side]
    px, py = -dy, dx
    pts = [p, (p[0] + dx * 13 + px * 6, p[1] + dy * 13 + py * 6), (p[0] + dx * 13 - px * 6, p[1] + dy * 13 - py * 6)]
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def canvas_edge(e, by_id, ox, oy):
    """(svg markup, label markup) for one edge, in board coordinates."""
    a, b = by_id[e["fromNode"]], by_id[e["toNode"]]
    auto_a, auto_b = _auto_sides(a, b)
    sa = e.get("fromSide") if e.get("fromSide") in SIDE_VEC else auto_a
    sb = e.get("toSide") if e.get("toSide") in SIDE_VEC else auto_b
    (x1, y1), (x2, y2) = _anchor(a, sa), _anchor(b, sb)
    x1, y1, x2, y2 = x1 + ox, y1 + oy, x2 + ox, y2 + oy
    k = max(40.0, min(300.0, 0.4 * ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5))
    c1 = (x1 + SIDE_VEC[sa][0] * k, y1 + SIDE_VEC[sa][1] * k)
    c2 = (x2 + SIDE_VEC[sb][0] * k, y2 + SIDE_VEC[sb][1] * k)
    d = f"M{x1:.1f},{y1:.1f} C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {x2:.1f},{y2:.1f}"
    col = canvas_color(e.get("color"))
    style = f' style="--ec:{col}"' if col else ""
    svg = f'<g class="edge"{style}><path d="{d}"/>'
    if e.get("fromEnd") == "arrow":
        svg += f'<polygon points="{_arrow((x1, y1), sa)}"/>'
    if e.get("toEnd", "arrow") == "arrow":
        svg += f'<polygon points="{_arrow((x2, y2), sb)}"/>'
    svg += "</g>"
    label = ""
    if isinstance(e.get("label"), str) and e["label"].strip():
        mx = (x1 + 3 * c1[0] + 3 * c2[0] + x2) / 8
        my = (y1 + 3 * c1[1] + 3 * c2[1] + y2) / 8
        label = f'<div class="elabel" style="left:{mx:.1f}px;top:{my:.1f}px">{html.escape(e["label"][:200])}</div>'
    return svg, label


CANVAS_CSS = """
html, body { height: 100%; overflow: hidden; overscroll-behavior: none; }
main { max-width: none; padding: 0; height: 100%; }
#cv { position: fixed; inset: 0; overflow: hidden; touch-action: none; cursor: grab; background: var(--bg);
  background-image: radial-gradient(var(--line) 1px, transparent 1px); background-size: 24px 24px; }
#cv.drag { cursor: grabbing; }
#board { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
#edges { position: absolute; left: 0; top: 0; overflow: visible; pointer-events: none; }
.edge path { fill: none; stroke: var(--ec, var(--dim)); stroke-width: 2; }
.edge polygon { fill: var(--ec, var(--dim)); stroke: none; }
.cn { position: absolute; overflow: hidden; border: 2px solid var(--c, var(--line)); border-radius: 8px;
  background: color-mix(in srgb, var(--c, transparent) 10%, var(--code)); }
.cn-group { background: color-mix(in srgb, var(--c, var(--dim)) 8%, transparent); border-style: dashed; overflow: visible; }
.cn-group > .cn-label { position: absolute; left: 0; bottom: 100%; margin-bottom: 4px; color: var(--dim); font-size: 14px;
  white-space: nowrap; }
.cn-body { height: 100%; overflow: auto; padding: 8px 12px; font-size: 15px; line-height: 1.5; overflow-wrap: anywhere; user-select: text; }
.cn-body > :first-child { margin-top: 0; } .cn-body > :last-child { margin-bottom: 0; }
.cn-body h1, .cn-body h2, .cn-body h3 { margin: .6em 0 .3em; font-size: 1.2em; border: 0; padding: 0; }
.cn-body img { max-width: 100%; }
.cn-body pre { padding: 8px 10px; }
.cn-title { font-weight: 650; margin-bottom: 4px; }
.cn-dim { color: var(--dim); font-size: .85em; }
.cn-image { padding: 0; display: grid; place-items: center; }
.cn-image img { width: 100%; height: 100%; object-fit: contain; margin: 0; cursor: zoom-in; }
.elabel { position: absolute; transform: translate(-50%, -50%); background: var(--bg); border: 1px solid var(--line);
  border-radius: 6px; padding: 1px 8px; font-size: 13px; white-space: nowrap; pointer-events: none; }
.cvtools { position: fixed; right: 12px; bottom: 12px; z-index: 5; display: flex; gap: 6px; }
.cvtools button { background: var(--code); color: var(--text); border: 1px solid var(--line); border-radius: 8px;
  min-width: 40px; height: 40px; font: 600 16px/1 system-ui, sans-serif; cursor: pointer; }
.cvtools button:hover { background: var(--line); }
.cv-empty { position: fixed; inset: 0; display: grid; place-items: center; color: var(--dim); }
"""

CANVAS_JS = """
(() => {
  const vp = document.getElementById('cv'), board = document.getElementById('board');
  if (!vp || !board) return;
  const W = +board.dataset.w, H = +board.dataset.h;
  let k = 1, tx = 0, ty = 0;
  const apply = () => { board.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + k + ')'; };
  const lim = (v) => Math.max(0.02, Math.min(4, v));
  function fit() {
    const r = vp.getBoundingClientRect();
    k = lim(Math.min(r.width / W, r.height / H, 1));
    tx = (r.width - W * k) / 2; ty = (r.height - H * k) / 2; apply();
  }
  function zoomAt(f, cx, cy) {
    const nk = lim(k * f);
    tx = cx - (cx - tx) * nk / k; ty = cy - (cy - ty) * nk / k; k = nk; apply();
  }
  const mid = () => { const r = vp.getBoundingClientRect(); return [r.width / 2, r.height / 2]; };
  document.getElementById('zin').onclick = () => zoomAt(1.25, ...mid());
  document.getElementById('zout').onclick = () => zoomAt(0.8, ...mid());
  document.getElementById('zfit').onclick = fit;
  vp.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = vp.getBoundingClientRect();
    const f = e.ctrlKey ? Math.exp(-e.deltaY * 0.01) : (e.deltaY < 0 ? 1.12 : 1 / 1.12);
    zoomAt(f, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  const pts = new Map();
  let moved = 0, last = null;
  const span = () => { const [a, b] = [...pts.values()]; return [Math.hypot(a.x - b.x, a.y - b.y), (a.x + b.x) / 2, (a.y + b.y) / 2]; };
  vp.addEventListener('pointerdown', (e) => {
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    moved = 0; vp.classList.add('drag');
    if (pts.size === 2) last = span();
  });
  addEventListener('pointermove', (e) => {
    const p = pts.get(e.pointerId);
    if (!p) return;
    const r = vp.getBoundingClientRect();
    if (pts.size === 1) {
      moved += Math.abs(e.clientX - p.x) + Math.abs(e.clientY - p.y);
      tx += e.clientX - p.x; ty += e.clientY - p.y; apply();
      p.x = e.clientX; p.y = e.clientY;
    } else {
      p.x = e.clientX; p.y = e.clientY;
      const [d, cx, cy] = span();
      if (last && last[0] > 0) { tx += cx - last[1]; ty += cy - last[2]; zoomAt(d / last[0], cx - r.left, cy - r.top); }
      last = [d, cx, cy]; moved += 10;
    }
  });
  const up = (e) => { pts.delete(e.pointerId); if (!pts.size) vp.classList.remove('drag'); last = pts.size === 2 ? span() : null; if (!pts.size) setTimeout(() => { moved = 0; }, 60); };
  addEventListener('pointerup', up); addEventListener('pointercancel', up);
  // A drag that ends on a link must not follow it.
  document.addEventListener('click', (e) => { if (moved > 6) { e.preventDefault(); e.stopPropagation(); moved = 0; } }, true);
  addEventListener('keydown', (e) => {
    if (e.key === '+' || e.key === '=') zoomAt(1.25, ...mid());
    else if (e.key === '-') zoomAt(0.8, ...mid());
    else if (e.key === '0') fit();
  });
  addEventListener('resize', fit);
  fit();
})();
"""


def canvas_excerpt(text):
    """The start of a note for a canvas card: front matter dropped, raw HTML removed
    (a cut-off tag must not swallow the page), cut to a few hundred characters."""
    body = split_frontmatter(text)[1].strip()[:EXCERPT_CHARS * 2]
    body = re.sub(r"<[^>]*>?", "", body, flags=re.S).strip()
    return body[:EXCERPT_CHARS] + ("…" if len(body) > EXCERPT_CHARS else "")


def canvas_vault_dir(lib, path, root_name):
    """The Obsidian vault folder a canvas's file paths are relative to: --obsidian-vault when
    the canvas is inside it, else the nearest folder above the canvas, inside its viewer
    root, that holds a .obsidian folder, else the viewer root itself."""
    here = path.resolve()
    if OBSIDIAN.vault is not None and OBSIDIAN.vault in here.parents:
        return OBSIDIAN.vault
    root = next((r for r in lib.roots if r.name == root_name and r in here.parents), None)
    if root is None:
        return None
    for d in here.parents:
        if (d / ".obsidian").is_dir():
            return d
        if d == root:
            break
    return root


def canvas_file_id(lib, vault_dir, rel):
    """The viewer id of a canvas file node's vault-relative path, or None if it is not
    under any viewer root."""
    if vault_dir is None:
        return None
    base = str(vault_dir).rstrip(os.sep)
    full = os.path.normpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep):
        return None
    for r in lib.roots:
        rs = str(r).rstrip(os.sep)
        if full.startswith(rs + os.sep):
            return r.name + "/" + full[len(rs) + 1:]
    return None


def render_canvas(lib, path, ident):
    """A .canvas file as a viewer page: a pannable, zoomable board. Bad content gives an
    error page with a link to the raw JSON, not an exception."""
    raw = raw_url(ident)

    def error(msg):
        return render_page(path.stem, f'<div style="padding:24px"><h1>{html.escape(path.stem)}</h1>'
                           f'<div class="callout danger"><div class="callout-title">This canvas could not be read</div>'
                           f'<p>{html.escape(msg)}</p></div>'
                           f'<p><a href="{raw}" target="_blank" rel="noopener noreferrer">Open the raw JSON</a></p></div>')
    try:
        if path.stat().st_size > CANVAS_MAX_BYTES:
            return error("The file is too large to render.")
        nodes, edges = parse_canvas(path.read_text(errors="replace"))
    except ValueError as e:
        return error(f"Not valid JSON Canvas: {e}")
    except OSError as e:
        return error(f"Cannot read the file: {e.strerror or e}")
    if not nodes:
        return render_page(path.stem, '<div class="cv-empty">This canvas is empty.</div>', f"<style>{CANVAS_CSS}</style>")

    items = lib.scan()
    items_by_id = {i["id"]: i for i in items}
    by_name = lib.index(items)
    root = ident.split("/", 1)[0]
    vault_dir = canvas_vault_dir(lib, path, root)
    budget = BaseBudget()
    heads = []

    def markdown_html(text, embed_bases=True):
        out, head = render_markdown(lib, text, ident, items_by_id, by_name, embed_bases=embed_bases, budget=budget)
        if head and head not in heads:
            heads.append(head)
        return out

    def file_body(n):
        f = n.get("file")
        sub = n.get("subpath") if isinstance(n.get("subpath"), str) and n["subpath"].startswith("#") else ""
        if not isinstance(f, str) or not f:
            return '<div class="cn-body cn-dim">file node without a file</div>'
        tid = canvas_file_id(lib, vault_dir, f)
        it = items_by_id.get(tid) if tid else None
        if it is None:
            return (f'<div class="cn-body"><div class="cn-title">{html.escape(os.path.basename(f))}</div>'
                    f'<div class="cn-dim">not in the vault: {html.escape(f[:200])}</div></div>')
        t = html.escape(tid)
        if it["kind"] in ("image", "svg"):
            return (f'<div class="cn-body cn-image"><img src="{raw_url(tid)}?v={int(it["mtime"])}" '
                    f'alt="{html.escape(it["name"])}" title="click to open" data-id="{t}"></div>')
        anchor = f' data-anchor="{html.escape(slugify(sub[1:], "-"))}"' if sub else ""
        title = html.escape(it["name"][:-3] if it["kind"] == "note" else it["name"]) + (html.escape(" > " + sub[1:]) if sub else "")
        excerpt = ""
        if it["kind"] == "note":
            p = lib.resolve(tid)
            try:
                excerpt = markdown_html(canvas_excerpt(p.read_text(errors="replace")), embed_bases=False) if p else ""
            except OSError:
                excerpt = ""
        return (f'<div class="cn-body"><div class="cn-title"><a class="internal" href="#{t}" data-id="{t}"{anchor}>{title}</a></div>'
                f'{excerpt}</div>')

    def link_body(n):
        u = n.get("url")
        if not isinstance(u, str):
            return '<div class="cn-body cn-dim">link node without a URL</div>'
        shown = html.escape(u[:500])
        if re.fullmatch(r"https?://[^\s<>\"']+", u, re.I):
            return (f'<div class="cn-body"><a class="external" href="{html.escape(u, quote=True)}" target="_blank" '
                    f'rel="noopener noreferrer">{shown}</a></div>')
        return f'<div class="cn-body">{shown}</div>'

    minx = min(n["x"] for n in nodes)
    miny = min(n["y"] for n in nodes)
    pad = 60
    ox, oy = pad - minx, pad - miny
    bw = max(n["x"] + n["width"] for n in nodes) - minx + 2 * pad
    bh = max(n["y"] + n["height"] for n in nodes) - miny + 2 * pad
    by_id = {n["id"]: n for n in nodes}

    def box(n, cls, inner):
        col = canvas_color(n.get("color"))
        style = f"left:{n['x'] + ox:.1f}px;top:{n['y'] + oy:.1f}px;width:{n['width']:.1f}px;height:{n['height']:.1f}px"
        if col:
            style += f";--c:{col}"
        return f'<div class="cn {cls}" style="{style}">{inner}</div>'

    groups, others = [], []
    for n in nodes:
        t = n.get("type")
        if t == "group":
            label = n.get("label")
            lab = f'<div class="cn-label">{html.escape(label[:200])}</div>' if isinstance(label, str) and label else ""
            groups.append(box(n, "cn-group", lab))
        elif t == "text":
            txt = n.get("text") if isinstance(n.get("text"), str) else ""
            others.append(box(n, "cn-text", f'<div class="cn-body">{markdown_html(txt)}</div>'))
        elif t == "file":
            others.append(box(n, "cn-file", file_body(n)))
        elif t == "link":
            others.append(box(n, "cn-link", link_body(n)))
        else:
            name = html.escape(str(t)[:40]) if t is not None else "node"
            others.append(box(n, "cn-unknown", f'<div class="cn-body cn-dim">{name}</div>'))
    svgs, labels = [], []
    for e in edges:
        s, lab = canvas_edge(e, by_id, ox, oy)
        svgs.append(s)
        labels.append(lab)
    board = (f'<div id="board" data-w="{bw:.0f}" data-h="{bh:.0f}" style="width:{bw:.0f}px;height:{bh:.0f}px">'
             f'{"".join(groups)}<svg id="edges" width="{bw:.0f}" height="{bh:.0f}">{"".join(svgs)}</svg>'
             f'{"".join(others)}{"".join(labels)}</div>')
    tools = ('<div class="cvtools"><button id="zout" type="button" title="Zoom out (-)" aria-label="Zoom out">−</button>'
             '<button id="zfit" type="button" title="Fit to view (0)" aria-label="Fit to view">Fit</button>'
             '<button id="zin" type="button" title="Zoom in (+)" aria-label="Zoom in">+</button></div>')
    body = f'<div id="cv">{board}</div>{tools}<script>{CANVAS_JS}</script>'
    return render_page(path.stem, body, f"<style>{CANVAS_CSS}</style>" + "".join(heads))


def search(lib, q, limit=40):
    q = q.strip().lower()
    if len(q) < 2:
        return []
    out = []
    for it in lib.scan():
        if it["kind"] not in ("note", "text", "html", "canvas"):
            continue
        got = lib.search_text(it)
        if got is None:
            continue
        text, low = got
        i = low.find(q)
        if i == -1:
            continue
        snip = re.sub(r"\s+", " ", text[max(0, i - 60):i + len(q) + 80]).strip()
        out.append({"id": it["id"], "name": it["name"], "kind": it["kind"], "snippet": snip})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ PDF export

# Chromium's headless shell (driven over the DevTools protocol by PDF_RENDER,
# standard library only) plus the page's libraries and fonts, so printing needs
# no network. `artifacts --install-pdf-runtime` downloads them once into a
# per-user directory (the "PDF runtime"); PDF_RENDER then runs on the host with
# Chromium's own sandbox on, and no network (see the launch switches).
# Kept small on purpose: only the headless shell (no full Chromium, Firefox or
# WebKit, no Playwright), only its en-US locale pack, no software Vulkan, only
# the library files the print page loads (no source maps or other builds) and
# only the font faces the print CSS uses.
# Every download is pinned by URL and sha256, and checked BEFORE it is
# unpacked. The headless shell comes from Chrome for Testing: change its version
# (in the URL) and checksum together. Inter is static TrueType from the upstream
# release: Debian's fonts-inter ships CFF outlines (.otf), which Chromium can only
# embed in a PDF as Type 3 glyph drawings, so PDF readers then select and copy
# that text badly or not at all.
PDF_PINS = {
    "chrome": (
        "https://storage.googleapis.com/chrome-for-testing-public/153.0.8010.12/linux64/"
        "chrome-headless-shell-linux64.zip",
        "a9da028861a0cf789ff25c2fed45f5f1aaf969ed9247835b6a7821a4f7af9d1d"),
    "inter": (
        "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip",
        "9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e"),
    "jetbrains-mono": (
        "https://github.com/JetBrains/JetBrainsMono/releases/download/v2.242/JetBrainsMono-2.242.zip",
        "4e315b4ef176ce7ffc971b14997bdc8f646e3d1e5b913d1ecba3a3b10b4a1a9f"),
    "dejavu": (
        "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip",
        "7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a"),
    "noto-emoji": (
        "https://github.com/googlefonts/noto-emoji/raw/v2.042/fonts/NotoColorEmoji.ttf",
        "c2f19f6a404baa7da7a710b018c2892d7b51386983ddca146811f76aea0b6861"),
    "mermaid": (
        "https://registry.npmjs.org/mermaid/-/mermaid-11.17.2.tgz",
        "6ad2f42c3fc26bbf9e45cbb6d11898972573ea52b33a5f4ff51952899f950ffd"),
    "layout-elk": (
        "https://registry.npmjs.org/@mermaid-js/layout-elk/-/layout-elk-0.2.3.tgz",
        "68b5d3e1baa6d48ed2b67c715c2c69e459fe852224d9cd9edd906149a17278ab"),
    "highlightjs": (
        "https://registry.npmjs.org/@highlightjs/cdn-assets/-/cdn-assets-11.9.0.tgz",
        "62b15dbb4430b9692eb864e99393f4578646da1b25d437fc9e10a79786330dd0"),
}
# What is kept from each download: (regex over the archive member's path, where
# it goes in the runtime), first match wins, a member that matches nothing is
# dropped. A download that is not an archive (.ttf) is one member with an empty
# name. The runtime's layout lives here, and so does the trimming.
_FACES = "Regular|Italic|SemiBold|SemiBoldItalic|Bold|BoldItalic"
PDF_FILES = {
    "chrome": [(r"chrome-headless-shell-linux64/(?!locales/(?!en-US\.pak$)|libvk_swiftshader\.so$|libvulkan\.so\.1$"
                r"|vk_swiftshader_icd\.json$)(.+)", r"chrome/\1")],
    "inter": [(rf"extras/ttf/(Inter-(?:{_FACES})\.ttf)", r"fonts/inter/\1")],
    "jetbrains-mono": [(rf"fonts/ttf/(JetBrainsMono-(?:{_FACES})\.ttf)", r"fonts/jetbrains-mono/\1")],
    "dejavu": [(r"[^/]+/ttf/(DejaVu(?:Sans|Sans-Bold|SansMono|SansMono-Bold|Serif|Serif-Bold)\.ttf)",
                r"fonts/dejavu/\1")],
    "noto-emoji": [(r"", "fonts/noto/NotoColorEmoji.ttf")],
    "mermaid": [(r"package/dist/(mermaid\.esm\.min\.mjs)", r"lib/mermaid/dist/\1"),
                (r"package/dist/(chunks/mermaid\.esm\.min/(?!.*\.map$).+)", r"lib/mermaid/dist/\1")],
    "layout-elk": [(r"package/dist/(mermaid-layout-elk\.esm\.min\.mjs)", r"lib/@mermaid-js/layout-elk/dist/\1"),
                   (r"package/dist/(chunks/mermaid-layout-elk\.esm\.min/(?!.*\.map$).+)",
                    r"lib/@mermaid-js/layout-elk/dist/\1")],
    "highlightjs": [(r"package/(highlight\.min\.js|styles/github\.min\.css)", r"lib/@highlightjs/cdn-assets/\1")],
}
# Fontconfig for the render: the host's own rules (/etc/fonts/conf.d, which
# give the same text as the fonts a Debian container would) plus only the
# runtime's fonts. PDF_RENDER runs with a throwaway HOME, so a user's own fonts
# never leak into a PDF. The two paths are relative to this file's own
# directory, so a runtime copied to another path still finds its fonts.
# The host's rules are part of the render: a host that remaps families
# (docs/pdf-export.md, "Fonts") can change the output.
PDF_FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/conf.d</include>
  <dir prefix="relative">fonts</dir>
  <cachedir prefix="relative">fontcache</cachedir>
</fontconfig>
"""
# Ubuntu 23.10+ keeps Chromium's sandbox from using user namespaces
# (kernel.apparmor_restrict_unprivileged_userns=1) unless the binary has an
# AppArmor profile that grants `userns`. The viewer never falls back to
# --no-sandbox: install.sh loads this profile instead (`--print-apparmor-profile`).
# The profile attaches to the exact runtime directory it is printed for (the
# `*` is the revision), never to a pattern other users could create elsewhere.
PDF_APPARMOR_NAME = "artifacts-chrome-headless-shell"
PDF_APPARMOR_FILE = Path("/etc/apparmor.d") / PDF_APPARMOR_NAME
_APPARMOR_UNSAFE = re.compile(r'[*?\[\]{}^"\\,\s\x00-\x1f]')


def abs_runtime(runtime_dir):
    """The one place a runtime directory becomes an absolute path (~ expanded, cwd applied,
    . and .. folded), so a relative --pdf-runtime means the same thing for the renderer."""
    return Path(os.path.abspath(os.path.expanduser(str(runtime_dir))))


def pdf_apparmor_profile(runtime_dir):
    """The AppArmor profile text for the runtime in runtime_dir. Raises ValueError for a path
    with characters that mean something in an AppArmor path pattern (or whitespace and quotes)."""
    base = str(abs_runtime(runtime_dir))
    if _APPARMOR_UNSAFE.search(base):
        raise ValueError(f"the runtime directory {base!r} contains characters that are special in an "
                         "AppArmor path pattern, or whitespace or quotes: choose another --pdf-runtime")
    return f"""\
# Written by the vault-viewer installer: lets the viewer's PDF renderer
# (Chromium's headless shell) use Chromium's own sandbox, which needs
# unprivileged user namespaces. Only this user's runtime folder matches.
abi <abi/4.0>,
include <tunables/global>

profile {PDF_APPARMOR_NAME} {base}/*/chrome/chrome-headless-shell flags=(unconfined) {{
  userns,

  include if exists <local/{PDF_APPARMOR_NAME}>
}}
"""


PDF_REMOVED = ("PDFs no longer use docker; install the runtime with `--install-pdf-runtime`")


# Runs as a host subprocess (`python3 -I -c`, see render_pdf): the page arrives
# on stdin, the PDF leaves on stdout. It drives Chromium's headless shell over
# the DevTools protocol on a pipe (fds 3 and 4, NUL-separated JSON), standard
# library only. The page is served at a made-up origin, with only the runtime's
# lib/ files beside it; every other request is refused by the Fetch
# interception, which is enabled on every target Chromium creates (browser-level
# auto-attach, so popups too). The launch switches below keep it from reaching the
# network even where interception does not apply (no name resolution, no proxy
# but a dead local port, no WebRTC UDP). The sandbox stays ON. Print parameters follow what Playwright
# used for this before, so the output is unchanged.
PDF_RENDER = r"""
import base64, fcntl, json, os, select, signal, sys, tempfile, time, urllib.parse
from pathlib import Path

CHROME = os.environ["PDF_CHROME"]
try:  # the renderer shares the service's memory cgroup: make it, and Chromium after it, the OOM killer's first pick
    with open("/proc/self/oom_score_adj", "w") as f:
        f.write("1000")
except OSError:
    pass
page_html = sys.stdin.buffer.read()
lib = Path(os.environ["PDF_LIB_DIR"]).resolve()
types = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css"}
flags = [
    "--headless", "--remote-debugging-pipe", "--no-startup-window",
    "--user-data-dir=" + tempfile.mkdtemp(prefix="chrome-"),
    "--disable-dev-shm-usage", "--enable-unsafe-swiftshader", "--force-color-profile=srgb",
    "--hide-scrollbars", "--mute-audio", "--export-tagged-pdf",
    "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
    "--disable-background-networking", "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
    "--disable-breakpad", "--disable-component-update", "--disable-default-apps", "--disable-extensions",
    "--disable-field-trial-config", "--disable-hang-monitor", "--disable-sync", "--metrics-recording-only",
    "--no-default-browser-check", "--no-first-run", "--no-service-autorun", "--password-store=basic",
    "--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]

def high(fd):  # move off 0-9, so the dup2 onto 3 and 4 below never maps an fd onto itself
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.close(fd)
    return moved

cmd_r, cmd_w = map(high, os.pipe())
out_r, out_w = map(high, os.pipe())
null = high(os.open(os.devnull, os.O_RDWR))
pid = os.posix_spawn(CHROME, [CHROME, *flags], os.environ, file_actions=[
    (os.POSIX_SPAWN_DUP2, null, 0), (os.POSIX_SPAWN_DUP2, null, 1), (os.POSIX_SPAWN_DUP2, null, 2),
    (os.POSIX_SPAWN_DUP2, cmd_r, 3), (os.POSIX_SPAWN_DUP2, out_w, 4)])
for fd in (cmd_r, out_w, null):
    os.close(fd)

buf, scanned, last_id, seen, attached = bytearray(), 0, 0, set(), {}

def read_msg(deadline):
    global scanned
    while True:
        end = buf.find(b"\0", scanned)
        if end >= 0:
            msg = json.loads(buf[:end])
            del buf[:end + 1]
            scanned = 0
            return msg
        scanned = len(buf)
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([out_r], [], [], left)[0]:
            raise TimeoutError
        chunk = os.read(out_r, 1 << 20)
        if not chunk:
            raise RuntimeError("Chromium exited")
        buf.extend(chunk)

def send(method, params=None, session=None):
    global last_id
    last_id += 1
    msg = {"id": last_id, "method": method, "params": params or {}}
    if session:
        msg["sessionId"] = session
    view = memoryview(json.dumps(msg).encode() + b"\0")
    while view:
        view = view[os.write(cmd_w, view):]
    return last_id

def serve(p, session):
    url = urllib.parse.urlsplit(p["request"]["url"])
    body = None
    if url.netloc == "print.invalid":
        if url.path == "/":
            body, ctype = page_html, "text/html; charset=utf-8"
        elif url.path.startswith("/lib/"):
            f = (lib / urllib.parse.unquote(url.path[5:])).resolve()
            if lib in f.parents and f.is_file():
                body, ctype = f.read_bytes(), types.get(f.suffix, "application/octet-stream")
    if body is None:
        send("Fetch.failRequest", {"requestId": p["requestId"], "errorReason": "Failed"}, session)
    else:
        send("Fetch.fulfillRequest", {"requestId": p["requestId"], "responseCode": 200,
             "responseHeaders": [{"name": "Content-Type", "value": ctype}],
             "body": base64.b64encode(body).decode()}, session)

def handle(m):
    method, p = m.get("method"), m.get("params", {})
    if method == "Fetch.requestPaused":
        serve(p, m.get("sessionId"))
    elif method == "Target.attachedToTarget":  # every new target (frames, workers, popups): same request filter
        child = p["sessionId"]
        attached[p["targetInfo"]["targetId"]] = child
        send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, child)
        send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, child)
        send("Runtime.runIfWaitingForDebugger", None, child)
    elif method:
        seen.add((method, m.get("sessionId")))

def call(method, params=None, session=None, timeout=30):
    i, deadline = send(method, params, session), time.monotonic() + timeout
    while True:
        m = read_msg(deadline)
        if m.get("id") == i:
            if "error" in m:
                raise RuntimeError(f"{method}: {m['error'].get('message')}")
            return m.get("result", {})
        handle(m)

def wait_event(method, session, timeout):
    deadline = time.monotonic() + timeout
    while (method, session) not in seen:
        handle(read_msg(deadline))

def pump(seconds):
    deadline = time.monotonic() + seconds
    try:
        while True:
            handle(read_msg(deadline))
    except TimeoutError:
        pass

mm = 3.78 / 96  # inches per mm: 3.78 CSS px per mm, 96 per inch (the rounding the old output used)
footer = ('<div style="font: 7.5pt Inter, sans-serif; color: #8a8f98; width: 100%; padding: 0 14mm; '
          'display: flex; justify-content: space-between"><span class="title"></span>'
          '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
try:
    # Browser-level auto-attach: every target Chromium ever creates, a window.open() popup
    # included, is paused at start and given the request filter (handle) before it runs.
    call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
    target = call("Target.createTarget", {"url": "about:blank"})["targetId"]
    deadline = time.monotonic() + 30
    while target not in attached:
        handle(read_msg(deadline))
    s = attached[target]
    call("Page.enable", None, s)
    call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 720, "deviceScaleFactor": 1,
         "mobile": False, "screenWidth": 1280, "screenHeight": 720,
         "screenOrientation": {"angle": 0, "type": "landscapePrimary"}}, s)
    started = time.monotonic()
    nav = call("Page.navigate", {"url": "http://print.invalid/"}, s)
    if nav.get("errorText"):
        raise RuntimeError("navigation failed: " + nav["errorText"])
    wait_event("Page.loadEventFired", s, 30 - (time.monotonic() - started))
    ready_by = time.monotonic() + 20
    while not call("Runtime.evaluate", {"expression": "document.documentElement.dataset.ready === '1'",
                   "returnByValue": True}, s)["result"].get("value"):
        if time.monotonic() > ready_by:
            print("page not ready after 20 s, printing it as it is", file=sys.stderr)
            break
        pump(0.02)
    pdf = call("Page.printToPDF", {
        "landscape": False, "displayHeaderFooter": True, "headerTemplate": "<span></span>",
        "footerTemplate": footer, "printBackground": True, "scale": 1, "paperWidth": 8.27, "paperHeight": 11.7,
        "marginTop": 16 * mm, "marginBottom": 18 * mm, "marginLeft": 14 * mm, "marginRight": 14 * mm,
        "pageRanges": "", "preferCSSPageSize": False, "generateTaggedPDF": False,
        "generateDocumentOutline": False}, s, timeout=60)["data"]
    sys.stdout.buffer.write(base64.b64decode(pdf))
    sys.stdout.flush()
    # The PDF is complete: point stdout and stderr at /dev/null so the server sees
    # end of file and answers now, while Chromium shuts down below.
    quiet = os.open(os.devnull, os.O_WRONLY)
    os.dup2(quiet, 1)
    os.dup2(quiet, 2)
    try:
        call("Browser.close", timeout=5)
    except (TimeoutError, RuntimeError):
        pass
finally:
    for _ in range(50):
        if os.waitpid(pid, os.WNOHANG)[0]:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
"""

# The runtime lives in DIR/<PDF_REV>/: the revision covers what is installed
# (the pins, the trimming rules and the fontconfig template), so a program only
# ever uses a runtime built for it, and an upgrade installs beside the old one.
# PDF_RENDER is not part of it: the script runs from the program, so changing it
# needs no new runtime. `artifacts --pdf-rev` prints the revision.
PDF_REV = hashlib.sha256(json.dumps([PDF_PINS, PDF_FILES, PDF_FONTS_CONF],
                                    sort_keys=True).encode()).hexdigest()[:12]


def default_pdf_runtime():
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(data) / "artifacts" / "pdf-runtime"


class PdfInstallError(Exception):
    pass


def pdf_download(url, dest):
    """Fetch url into the file dest (the one place the installer touches the network)."""
    req = urllib.request.Request(url, headers={"User-Agent": "artifacts-pdf-runtime"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def _pdf_target(name, rules):
    """Where an archive member goes in the runtime, or None to drop it."""
    for pattern, dest in rules:
        m = re.fullmatch(pattern, name)
        if m:
            return m.expand(dest)
    return None


def _pdf_safe(name):
    """Archive members with absolute paths or .. never leave the staging directory."""
    return bool(name) and not name.startswith("/") and "\\" not in name and ".." not in name.split("/")


def _pdf_members(kind, path):
    """Yield (name, mode, open-for-reading) for every regular file of an archive, and refuse
    links, devices and unsafe names anywhere in it, kept or not."""
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not _pdf_safe(info.filename.rstrip("/")):
                    raise PdfInstallError(f"unsafe path in the archive: {info.filename!r}")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise PdfInstallError(f"link in the archive: {info.filename!r}")
                if not info.is_dir():
                    yield info.filename, (mode & 0o777) or 0o644, lambda i=info: z.open(i)
    else:
        with tarfile.open(path) as t:
            for m in t:
                if not _pdf_safe(m.name.rstrip("/")):
                    raise PdfInstallError(f"unsafe path in the archive: {m.name!r}")
                if m.isdir():
                    continue
                if not m.isreg():
                    raise PdfInstallError(f"link or special file in the archive: {m.name!r}")
                yield m.name, m.mode & 0o777, lambda m=m: t.extractfile(m)


def _pdf_extract(name, path, dest_root):
    """Unpack the kept files of one verified download under dest_root; returns how many."""
    rules = PDF_FILES[name]
    url = PDF_PINS[name][0]
    kind = "zip" if url.endswith(".zip") else "tar" if url.endswith(".tgz") else "file"
    members = [("", 0o644, lambda: open(path, "rb"))] if kind == "file" else _pdf_members(kind, path)
    kept = 0
    for member, mode, opener in members:
        rel = _pdf_target(member, rules)
        if rel is None:
            continue
        dest = dest_root / rel
        if dest_root.resolve() not in dest.resolve().parents:
            raise PdfInstallError(f"{name}: {member!r} would land outside the runtime")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with opener() as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        os.chmod(dest, (mode | 0o600) & 0o755)
        kept += 1
    if not kept:
        raise PdfInstallError(f"{name}: nothing in the download matches the expected layout")
    return kept


def pdf_runtime_ready(runtime_dir):
    try:
        return (abs_runtime(runtime_dir) / PDF_REV / "REV").read_text().strip() == PDF_REV
    except OSError:
        return False


def install_pdf_runtime(runtime_dir, say=print):
    """Download, verify and unpack the PDF runtime into runtime_dir/<PDF_REV>, atomically.
    Raises PdfInstallError (leaving no partial runtime) on any failure. Installers of one
    directory take turns (a flock on DIR/.lock), so they cannot corrupt each other."""
    base = abs_runtime(runtime_dir)
    final = base / PDF_REV
    if pdf_runtime_ready(base):
        say(f"already installed: {final}")
        return final
    base.mkdir(parents=True, exist_ok=True)
    with open(base / ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say(f"another installer is using {base}: waiting for it")
            fcntl.flock(lock, fcntl.LOCK_EX)
        if pdf_runtime_ready(base):  # the installer we waited for did the work
            say(f"already installed: {final}")
            return final
        return _install_locked(base, final, say)


def _install_locked(base, final, say):
    stage = base / f".tmp-{PDF_REV}-{os.getpid()}-{threading.get_ident()}"
    downloads = Path(tempfile.mkdtemp(prefix="artifacts-pdf-dl-"))
    try:
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
        for name, (url, sha) in PDF_PINS.items():
            say(f"downloading {name}: {url}")
            archive = downloads / name
            try:
                pdf_download(url, archive)
            except OSError as e:  # urllib's errors are OSErrors
                raise PdfInstallError(f"{name}: download failed: {e}") from e
            got = hashlib.sha256(archive.read_bytes()).hexdigest()
            if got != sha:
                raise PdfInstallError(f"{name}: checksum mismatch (expected {sha}, got {got}); nothing installed")
            try:
                _pdf_extract(name, archive, stage)
            except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
                raise PdfInstallError(f"{name}: cannot unpack: {e}") from e
        chrome = stage / "chrome" / "chrome-headless-shell"
        if not chrome.is_file():
            raise PdfInstallError("the headless shell is missing from its download")
        chrome.chmod(0o755)
        (stage / "fonts.conf").write_text(PDF_FONTS_CONF)
        (stage / "REV").write_text(PDF_REV + "\n")  # last: its presence means "complete"
        if final.exists():  # an interrupted older install without REV
            shutil.rmtree(final)
        os.rename(stage, final)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(downloads, ignore_errors=True)
    # With the lock held no other installer is running, so every .tmp-* is stale.
    for other in base.iterdir():  # other revisions and leftovers of interrupted installs, nothing else
        if other != final and re.fullmatch(r"[0-9a-f]{12}|\.tmp-.*", other.name):
            shutil.rmtree(other, ignore_errors=True)
    return final


def pdf_runtime_size(path):
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file() and not f.is_symlink())


def pdf_apparmor_state(runtime_dir):
    """Lines saying whether Chromium's sandbox can work on this host. The kernel's list of
    loaded profiles needs root, so this reports what a user can read: whether the installed
    profile file is there and is the one this program prints for this runtime."""
    try:
        restricted = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip() == "1"
    except OSError:
        return ["AppArmor user namespace restriction: off (not present)"]
    try:
        want = pdf_apparmor_profile(runtime_dir)
    except ValueError as e:
        return [f"AppArmor user namespace restriction: {'ON' if restricted else 'off'}", f"warning: {e}"]
    try:
        have = PDF_APPARMOR_FILE.read_text()
        profile = "installed and current" if have == want else "installed but DIFFERENT from `artifacts --print-apparmor-profile`"
    except OSError:
        profile = "NOT installed"
    lines = [f"AppArmor user namespace restriction: {'ON' if restricted else 'off'}; "
             f"profile file {PDF_APPARMOR_FILE}: {profile} (whether it is loaded is not readable without root)"]
    if restricted and profile != "installed and current":
        lines.append("Chromium's sandbox will fail without it: see `artifacts --print-apparmor-profile`")
    return lines


PDF_SLOTS = threading.BoundedSemaphore(2)
PDF_TIMEOUT = 90


def render_pdf(page_html, runtime_dir, wait=False):
    """Print a page to PDF with the installed runtime. Returns (pdf bytes, None) or (None, reason).
    The server answers as soon as the PDF is complete and lets the renderer's shutdown finish in
    the background; wait=True (the command line) finishes it, and its cleanup, before returning."""
    runtime_dir = abs_runtime(runtime_dir)
    rt = runtime_dir / PDF_REV
    if not pdf_runtime_ready(runtime_dir):
        return None, "the PDF runtime is not installed: run `artifacts --install-pdf-runtime`"
    # cwd, HOME and TMPDIR are one fresh directory (Chromium's profile goes there too), so
    # nothing of the user's reaches the render and nothing is left behind.
    try:
        home = tempfile.mkdtemp(prefix="artifacts-pdf-")
    except OSError as e:
        return None, f"cannot create a temporary directory for the render: {e}"
    if not PDF_SLOTS.acquire(timeout=60):
        shutil.rmtree(home, ignore_errors=True)
        return None, "busy rendering other PDFs, try again"
    # From here on _finish owns the slot and the directory: whatever happens, it runs once.
    env = {"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8",
           "FONTCONFIG_FILE": str(rt / "fonts.conf"), "PDF_CHROME": str(rt / "chrome" / "chrome-headless-shell"),
           "PDF_LIB_DIR": str(rt / "lib")}
    proc, handed = None, False  # handed: _finish has been given the slot and the directory
    try:
        proc = subprocess.Popen([sys.executable, "-I", "-c", PDF_RENDER], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=home, env=env,
                                start_new_session=True)
        # The slot and the temp directory are given back once the renderer has exited,
        # which a successful render leaves to a background thread (_finish).
        stdout, stderr = _exchange(proc, page_html.encode(), PDF_TIMEOUT)
        ok = stdout.startswith(b"%PDF") and stdout.rstrip().endswith(b"%%EOF")
        handed = True
        if ok and not wait:
            # The renderer closed its output after the last byte of the PDF: answer now,
            # and let Chromium's shutdown (about 150 ms) finish in the background.
            try:
                threading.Thread(target=_finish, args=(proc, home, PDF_SHUTDOWN), daemon=True).start()
            except RuntimeError:  # no thread to be had: do it here
                _finish(proc, home, PDF_SHUTDOWN)
        else:
            _finish(proc, home, PDF_SHUTDOWN)
    except subprocess.TimeoutExpired:
        if not handed:
            _kill_group(proc)
            _finish(proc, home, 0)
        return None, f"rendering took longer than {PDF_TIMEOUT} s"
    except BaseException as e:
        if not handed:
            if proc:
                _kill_group(proc)
            _finish(proc, home, 0)
        if not isinstance(e, Exception):
            raise
        if proc is None and isinstance(e, OSError):
            return None, f"cannot run the PDF renderer: {e}"
        sys.stderr.write(f"artifacts: pdf: {type(e).__name__}: {e}\n")
        return None, "rendering failed (details in the service log)"
    err = stderr.decode(errors="replace").strip()
    if err:
        sys.stderr.write(f"artifacts: pdf: {err[-2000:]}\n")
    if not ok:
        if "No usable sandbox" in err:
            return None, ("Chromium's sandbox is not usable on this host: install the AppArmor profile "
                          "(see `artifacts --print-apparmor-profile`)")
        return None, "rendering failed (details in the service log)"
    return stdout, None


PDF_SHUTDOWN = 10  # seconds the renderer gets to exit after its output closed


def _exchange(proc, data, timeout):
    """Feed stdin and read stdout and stderr to end of file. Unlike communicate(),
    this does not wait for the process to exit. Raises TimeoutExpired."""
    out, err = bytearray(), bytearray()
    view = memoryview(data)
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as sel:
        os.set_blocking(proc.stdin.fileno(), False)
        sel.register(proc.stdin, selectors.EVENT_WRITE)
        sel.register(proc.stdout, selectors.EVENT_READ, out)
        sel.register(proc.stderr, selectors.EVENT_READ, err)
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            for key, _ in sel.select(left):
                if key.fileobj is proc.stdin:
                    try:
                        view = view[os.write(key.fd, view[:1 << 16]):]
                    except BrokenPipeError:
                        view = view[:0]
                    if not view:
                        sel.unregister(proc.stdin)
                        proc.stdin.close()
                    continue
                chunk = os.read(key.fd, 1 << 16)
                if chunk:
                    key.data.extend(chunk)
                else:
                    sel.unregister(key.fileobj)
                    key.fileobj.close()
    return bytes(out), bytes(err)


def _finish(proc, home, grace):
    """Reap the renderer (killing its process group if it outlives grace seconds),
    then free its temp directory and its render slot."""
    try:
        if proc:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
            _kill_group(proc)  # whatever Chromium helpers are left; harmless when none are
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for f in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if f and not f.closed:
                        f.close()
                except OSError:
                    pass
    finally:
        shutil.rmtree(home, ignore_errors=True)
        PDF_SLOTS.release()


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the render was started as a session leader
    except (ProcessLookupError, PermissionError):
        pass


PDF_CHECK_TEXT = "PDF runtime check"


def check_pdf_runtime(runtime_dir):
    """Render a tiny page and check the PDF embeds the runtime's Inter (so the fonts were
    found, not silently replaced). Returns (True, "ok") or (False, reason)."""
    pdf, why = render_pdf(render_page(PDF_CHECK_TEXT, f"<p>{PDF_CHECK_TEXT}</p>", pdf=True), runtime_dir, wait=True)
    if not pdf:
        return False, why
    if not re.search(rb"/BaseFont\s*/[A-Z]{6}\+Inter-", pdf):
        return False, ("the PDF does not embed the runtime's fonts (fontconfig did not find "
                       f"{abs_runtime(runtime_dir) / PDF_REV / 'fonts'})")
    return True, "ok"


# ------------------------------------------------------------------ the server

class TailnetGate:
    """Admit loopback, and tailnet peers that belong to one user and carry no
    tags (tagged devices are servers, not the user's own machines)."""

    def __init__(self, login):
        self.login = login.lower() if login else None
        self.cache = {}
        self.lock = threading.Lock()

    def allowed(self, ip):
        if ipaddress.ip_address(ip).is_loopback or not self.login:
            return True
        with self.lock:
            hit = self.cache.get(ip)
            if hit and hit[1] > time.time():
                return hit[0]
        ok = False
        try:
            out = subprocess.run(["tailscale", "whois", "--json", ip], capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                d = json.loads(out.stdout)
                ok = (not d.get("Node", {}).get("Tags")) and \
                    d.get("UserProfile", {}).get("LoginName", "").lower() == self.login
        except (OSError, ValueError, subprocess.TimeoutExpired):
            ok = False
        with self.lock:
            self.cache[ip] = (ok, time.time() + 60)
        return ok


def attachment(fname):
    """Content-Disposition for a download: ASCII fallback plus the UTF-8 name."""
    plain = re.sub(r'[^\w .-]', "_", fname, flags=re.A)
    return {"Content-Disposition": f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(fname)}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "artifacts"
    library: Library = None
    hosts = {"127.0.0.1:7777", "localhost:7777"}
    gate = TailnetGate(None)
    title = None
    pdf_runtime = default_pdf_runtime()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), self.client_address[0], fmt % args))

    def log_request(self, code="-", size="-"):
        # The page polls /api/tree and revalidates assets: successful API calls and
        # 304s are noise. Errors, pages, PDFs and downloads are still logged.
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        if status == 304 or (self.path.startswith("/api/") and 200 <= status < 300):
            return
        super().log_request(code, size)

    def _headers(self, status, ctype, length, cache, extra):
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _send(self, status, body, ctype, extra=None, cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self._headers(status, ctype, len(body), cache, extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_modified(self, etag, extra=None, cache="no-store"):
        self._headers(HTTPStatus.NOT_MODIFIED, None, None, cache, {"ETag": etag, **(extra or {})})

    def _matches(self, etag, mtime):
        """Does the request's validator (If-None-Match, else If-Modified-Since
        unless mtime is None) match?"""
        inm = self.headers.get("If-None-Match")
        if inm is not None:
            want = {t.strip().removeprefix("W/") for t in inm.split(",")}
            return "*" in want or etag in want
        ims = self.headers.get("If-Modified-Since")
        if ims and mtime is not None:
            try:
                return mtime <= email.utils.parsedate_to_datetime(ims).timestamp()
            except (TypeError, ValueError):
                return False
        return False

    def _send_tree(self):
        lib = self.library
        items, body, items_etag = lib.snapshot()
        head = json.dumps({"roots": [r.name for r in lib.roots], "title": self.title})
        # The validator covers the whole response: items plus roots and title.
        etag = '"' + hashlib.sha256((head + items_etag).encode()).hexdigest()[:24] + '"'
        if self._matches(etag, None):  # the tree has no Last-Modified: If-None-Match only
            return self._not_modified(etag, cache="private, no-cache")
        return self._send(HTTPStatus.OK, head[:-1] + ',"items":' + body + "}", "application/json",
                          {"ETag": etag}, cache="private, no-cache")

    def _send_file(self, path, ctype, extra):
        """A vault file with validators, revalidated on every use, streamed in chunks."""
        try:
            f = open(path, "rb")
        except OSError:
            return self._send(HTTPStatus.NOT_FOUND, "not found\n", "text/plain")
        with f:
            st = os.fstat(f.fileno())
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            cache = "private, no-cache"
            if self._matches(etag, int(st.st_mtime)):
                return self._not_modified(etag, cache=cache)
            self._headers(HTTPStatus.OK, ctype, st.st_size, cache, {
                **(extra or {}), "ETag": etag, "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True)})
            if self.command == "HEAD":
                return
            left = st.st_size
            while left > 0:
                chunk = f.read(min(FILE_CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.gate.allowed(self.client_address[0]):
            return self._send(HTTPStatus.FORBIDDEN, "forbidden\n", "text/plain")
        if (self.headers.get("Host") or "").lower() not in self.hosts:
            return self._send(HTTPStatus.FORBIDDEN, "forbidden host\n", "text/plain")
        url = urllib.parse.urlsplit(self.path)
        sandboxed = {"Content-Security-Policy": csp(self.hosts)}
        if url.path == "/":
            return self._send(HTTPStatus.OK, PAGE, "text/html; charset=utf-8", {
                "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                           "img-src 'self' data: blob:; frame-src 'self'; connect-src 'self'; base-uri 'none'; "
                                           "form-action 'none'; frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
            })
        if url.path.startswith("/api/"):
            # Only for the viewer page itself: refuse cross-origin readers.
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
                return self._send(HTTPStatus.FORBIDDEN, "[]", "application/json")
            if url.path == "/api/tree":
                return self._send_tree()
            if url.path == "/api/search":
                q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
                return self._send(HTTPStatus.OK, json.dumps(search(self.library, q)), "application/json")
            return self._send(HTTPStatus.NOT_FOUND, "{}", "application/json")
        if url.path.startswith("/pdf/"):
            # Starts a container: only for the viewer page or a direct request, never another site.
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                return self._send(HTTPStatus.FORBIDDEN, "forbidden\n", "text/plain")
            ident = urllib.parse.unquote(url.path[len("/pdf/"):])
            p = self.library.resolve(ident)
            kind = KINDS[p.suffix.lower()] if p else None
            if kind not in ("note", "text"):
                return self._send(HTTPStatus.NOT_FOUND, "only notes and text files export as PDF\n", "text/plain")
            if self.command == "HEAD":
                return self._send(HTTPStatus.METHOD_NOT_ALLOWED, "", "text/plain", {"Allow": "GET"})
            try:
                page = render_note(self.library, p, ident, pdf=True) if kind == "note" else render_text(p, ident, pdf=True)
                pdf, why = render_pdf(page, self.pdf_runtime)
            except Exception as e:  # noqa: BLE001 (any failure must still answer the button)
                sys.stderr.write(f"artifacts: pdf: {ident}: {e!r}\n")
                pdf, why = None, "rendering failed (details in the service log)"
            if pdf is None:
                return self._send(HTTPStatus.SERVICE_UNAVAILABLE, f"PDF export: {why}\n", "text/plain")
            return self._send(HTTPStatus.OK, pdf, "application/pdf", attachment(p.stem + ".pdf"))
        for prefix in ("/raw/", "/view/"):
            if url.path.startswith(prefix):
                ident = urllib.parse.unquote(url.path[len(prefix):])
                p = self.library.resolve(ident)
                if p is None:
                    return self._send(HTTPStatus.NOT_FOUND, "not found\n", "text/plain")
                kind = KINDS[p.suffix.lower()]
                if prefix == "/view/" and kind == "note":
                    try:
                        page = render_note(self.library, p, ident)
                    except Exception as e:  # noqa: BLE001 (a bad note must give a page, not a dropped connection)
                        sys.stderr.write(f"artifacts: view: {ident}: {e!r}\n")
                        page = render_page(p.stem, f'<div class="base-error">This note could not be rendered '
                                           f'(details in the service log). <a href="{raw_url(ident)}" target="_blank" '
                                           f'rel="noopener noreferrer">Open the raw file</a></div>')
                    return self._send(HTTPStatus.OK, page, "text/html; charset=utf-8", sandboxed)
                if prefix == "/view/" and kind == "text":
                    return self._send(HTTPStatus.OK, render_text(p, ident), "text/html; charset=utf-8", sandboxed)
                if prefix == "/view/" and kind in ("canvas", "base"):
                    try:
                        page = render_canvas(self.library, p, ident) if kind == "canvas" else render_base(self.library, p, ident)
                    except Exception as e:  # noqa: BLE001 (a bad file must give a page, not a dropped connection)
                        sys.stderr.write(f"artifacts: view: {ident}: {e!r}\n")
                        page = render_page(p.stem, f'<div class="base-error">This file could not be rendered '
                                           f'(details in the service log). <a href="{raw_url(ident)}" target="_blank" '
                                           f'rel="noopener noreferrer">Open the raw file</a></div>')
                    return self._send(HTTPStatus.OK, page, "text/html; charset=utf-8", sandboxed)
                ctype = mimetypes.guess_type(p.name)[0] or "text/plain"
                if kind in ("note", "text", "base"):
                    ctype = "text/plain"
                elif kind == "canvas":
                    ctype = "application/json"
                if ctype.startswith("text/") or ctype in ("image/svg+xml", "application/json", "application/javascript"):
                    ctype += "; charset=utf-8"
                if kind == "archive":
                    return self._send_file(p, "application/zip", {**sandboxed, **attachment(p.name)})
                return self._send_file(p, ctype, sandboxed)
        return self._send(HTTPStatus.NOT_FOUND, "not found\n", "text/plain")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vault</title>
<style>
  :root { --bg:#1a1a1e; --panel:#222227; --line:#33333a; --text:#e6e6ea; --dim:#9a9aa5; --accent:#a88bfa; --hover:#2b2b31; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--text);
    font:14px/1.45 -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; }
  body { overflow:hidden; overscroll-behavior:none; }
  #app { display:grid; grid-template-columns: 310px 1fr; height:100%; height:100dvh; }
  #app.collapsed { grid-template-columns: 0 1fr; }
  aside { background:var(--panel); border-right:1px solid var(--line); display:flex; flex-direction:column; min-width:0; overflow:hidden; }
  aside header { padding:14px 14px 10px; }
  aside h1 { font-size:15px; margin:0 0 10px; font-weight:600; letter-spacing:.2px; cursor:pointer; }
  aside h1 span { color:var(--accent); }
  #q { width:100%; padding:8px 10px; border-radius:8px; border:1px solid var(--line); background:var(--bg); color:var(--text); outline:none; }
  #q:focus { border-color:var(--accent); }
  #list { overflow:auto; padding:2px 8px 24px; flex:1; }
  .sec { color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.6px; margin:12px 8px 4px; }
  .row { display:flex; gap:6px; align-items:center; padding:4px 8px; border-radius:6px; cursor:pointer; white-space:nowrap; }
  @media (hover: hover) { .row:hover { background:var(--hover); } }
  .row.active { background:#332b45; }
  .row .ic { width:14px; flex:none; color:var(--dim); font-size:11px; text-align:center; }
  .row .nm { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; }
  .row .tag { font-size:9.5px; padding:0 4px; border-radius:3px; border:1px solid var(--line); color:var(--dim); flex:none; }
  .row .when { color:var(--dim); font-size:11px; flex:none; }
  .row.fresh .nm::after { content:""; display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--accent); margin-left:6px; vertical-align:middle; }
  .row.folder .nm { color:#c8c8d0; }
  .hit .snip { color:var(--dim); font-size:12px; white-space:normal; padding:0 8px 6px 28px; margin-top:-2px; cursor:pointer; }
  .hit .snip mark { background:#4b3a7a; color:var(--text); border-radius:2px; }
  main { display:flex; flex-direction:column; min-width:0; }
  .bar { display:flex; align-items:center; gap:10px; padding:10px 14px; border-bottom:1px solid var(--line); min-height:49px; }
  .bar .title { font-weight:600; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bar .meta { color:var(--dim); font-size:12px; white-space:nowrap; }
  button, a.btn { background:transparent; color:var(--text); border:1px solid var(--line); border-radius:7px; padding:5px 10px; font:inherit; font-size:12.5px; cursor:pointer; text-decoration:none; }
  @media (hover: hover) { button:hover, a.btn:hover { background:var(--hover); } }
  #stage { flex:1; position:relative; overflow:hidden; background:#1e1e22; }
  iframe { border:0; width:100%; height:100%; display:block; background:#1e1e22; }
  iframe.light { background:#fff; }
  #imgwrap { position:absolute; inset:0; overflow:hidden; cursor:grab; background:#1e1e22; touch-action:none; }
  #imgwrap.drag { cursor:grabbing; }
  #imgwrap img { position:absolute; left:0; top:0; transform-origin:0 0; user-select:none; -webkit-user-drag:none; }
  #closezoom { position:absolute; top:12px; right:14px; z-index:3; background:#2b2b31; }
  .empty { position:absolute; inset:0; display:grid; place-items:center; color:var(--dim); text-align:center; padding:40px; }
  .hidden { display:none !important; }
  #obsbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:7px 14px; border-bottom:1px solid var(--line); background:#28233a; font-size:13px; }
  #obsbar span { flex:1; min-width:220px; }
  #obs .app { display:none; }
  aside footer { border-top:1px solid var(--line); padding:8px 14px; display:flex; flex-wrap:wrap; gap:6px; align-items:center; color:var(--dim); font-size:12px; }
  aside footer span { flex:1; }
  aside footer button { font-size:12px; padding:3px 8px; }
  #scrim { display:none; }
  /* Phones: the sidebar becomes a drawer over the page (opened with the menu
     button, closed by picking a file or tapping outside it). */
  @media (max-width: 760px) {
    #app, #app.collapsed { grid-template-columns: 1fr; }
    aside { position:fixed; z-index:20; top:0; bottom:0; left:0; width:min(86vw, 340px);
      transform:translateX(-102%); transition:transform .2s ease; }
    #app.drawer aside { transform:none; box-shadow:0 0 40px rgba(0,0,0,.55); }
    #app.drawer #scrim { display:block; position:fixed; inset:0; z-index:19; background:rgba(0,0,0,.45); }
    #q { font-size:16px; padding:10px 12px; }  /* iOS zooms into inputs under 16px */
    .row { padding-top:9px; padding-bottom:9px; font-size:15px; }
    .sec { margin-top:16px; }
    .bar { gap:6px; padding:6px 8px; min-height:52px; }
    .bar .meta, #reload { display:none; }
    button, a.btn { padding:8px 11px; font-size:14px; min-width:38px; }
    #obs .long { display:none; } #obs .app { display:inline; }
  }
  @media (hover: none) { #zin, #zout { display:none; } }  /* pinch instead */
</style></head>
<body>
<div id="app">
  <aside>
    <header>
      <h1 id="home" title="Home"><span>&#9670;</span> <b id="vname">Vault</b></h1>
      <input id="q" placeholder="Search names and text (/)" autocomplete="off" spellcheck="false">
    </header>
    <div id="list"></div>
    <footer><span>Open links in Obsidian</span>
      <button id="obsmode" title="On this device, open vault links in the Obsidian app instead of this page">Off</button>
      <button id="obsvault" title="The vault's name in Obsidian on this device">Vault&hellip;</button></footer>
  </aside>
  <main>
    <div class="bar">
      <button id="toggle" title="Toggle sidebar ([)">&#9776;</button>
      <button id="back" title="Back (alt+left)">&#8592;</button>
      <div class="title" id="title">Nothing open</div>
      <div class="meta" id="meta"></div>
      <span id="zoomctl" class="hidden">
        <button id="zout" title="Zoom out (-)">&minus;</button>
        <button id="zfit" title="Fit (0)">Fit</button>
        <button id="zin" title="Zoom in (+)">+</button>
      </span>
      <button id="reload" title="Reload (r)">Reload</button>
      <button id="pdf" class="hidden" title="Download as PDF (p)">PDF</button>
      <a class="btn hidden" id="obs" title="Open in the Obsidian app (o)"><span class="long">Obsidian</span><span class="app">App</span> &#8599;</a>
      <a class="btn" id="open" target="_blank" rel="noopener" title="Open the raw file in a new tab">Raw &#8599;</a>
    </div>
    <div id="obsbar" class="hidden" role="status"></div>
    <div id="stage">
      <div class="empty" id="empty"><div id="emptymsg">Loading the file list&hellip;</div></div>
      <div class="empty hidden" id="dl"><div><div id="dlname"></div><p>Archives are not previewed.</p><a class="btn" id="dllink" download>Download</a></div></div>
      <iframe id="frame" class="hidden" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>
      <div id="imgwrap" class="hidden"><button id="closezoom" class="hidden" title="Back to the note (Esc)">&#10005; Close</button><img id="img" alt=""></div>
    </div>
  </main>
  <div id="scrim"></div>
</div>
<script>
const $ = (id) => document.getElementById(id);
const ICON = { note: "≣", html: "◈", svg: "◇", image: "▣", text: "≡", archive: "▤", canvas: "▦", base: "▥" };
let items = [], byId = new Map(), current = null, seen = new Set(), firstLoad = true, overlay = false;
let open = new Set(JSON.parse(localStorage.getItem("vault-open") || "[]"));
let scrolls = {}, zoom = { s: 1, x: 0, y: 0 }, hits = null, searchTimer = null, retryTimer = null;
let frameDoc = null;  // the file version the frame holds; an image or archive only hides the frame
const phone = matchMedia("(max-width: 760px)");
// Desktop: the sidebar collapses. Phone: it's a drawer over the page.
const toggleSidebar = (force) => $("app").classList.toggle(phone.matches ? "drawer" : "collapsed", force);

function ago(t) {
  const d = Date.now() / 1000 - t;
  if (d < 60) return "now";
  if (d < 3600) return Math.floor(d / 60) + "m";
  if (d < 86400) return Math.floor(d / 3600) + "h";
  return Math.floor(d / 86400) + "d";
}
const enc = (id) => id.split("/").map(encodeURIComponent).join("/");
const label = (it) => it.kind === "note" ? it.name.replace(/\.md$/, "") : it.name;
const saveOpen = () => localStorage.setItem("vault-open", JSON.stringify([...open]));

function row(it, depth, extra) {
  const el = document.createElement("div");
  el.className = "row" + (current && current.id === it.id ? " active" : "") + (!seen.has(it.id) && !firstLoad ? " fresh" : "");
  el.style.paddingLeft = (8 + depth * 14) + "px";
  el.innerHTML = '<span class="ic"></span><span class="nm"></span>' + (extra ? '<span class="when"></span>' : (it.kind !== "note" ? '<span class="tag"></span>' : ""));
  el.querySelector(".ic").textContent = ICON[it.kind] || "•";
  el.querySelector(".nm").textContent = label(it);
  if (extra) el.querySelector(".when").textContent = extra;
  else if (it.kind !== "note") el.querySelector(".tag").textContent = it.name.split(".").pop();
  el.title = it.id;
  el.onclick = () => select(it);
  return el;
}

function buildTree() {
  const root = { dirs: new Map(), files: [] };
  for (const it of items) {
    const parts = it.id.split("/").slice(1, -1);
    let node = root, path = it.id.split("/")[0];
    for (const p of parts) {
      path += "/" + p;
      if (!node.dirs.has(p)) node.dirs.set(p, { dirs: new Map(), files: [], path });
      node = node.dirs.get(p);
    }
    node.files.push(it);
  }
  return root;
}

function renderTree(node, depth, list) {
  const dirs = [...node.dirs.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  for (const [name, d] of dirs) {
    const isOpen = open.has(d.path);
    const el = document.createElement("div");
    el.className = "row folder";
    el.style.paddingLeft = (8 + depth * 14) + "px";
    el.innerHTML = '<span class="ic"></span><span class="nm"></span>';
    el.querySelector(".ic").textContent = isOpen ? "▾" : "▸";
    el.querySelector(".nm").textContent = name;
    el.onclick = () => { isOpen ? open.delete(d.path) : open.add(d.path); saveOpen(); render(); };
    list.appendChild(el);
    if (isOpen) renderTree(d, depth + 1, list);
  }
  const files = [...node.files].sort((a, b) => (a.kind === "note" ? 0 : 1) - (b.kind === "note" ? 0 : 1) || label(a).localeCompare(label(b)));
  for (const it of files) list.appendChild(row(it, depth, null));
}

function render() {
  const list = $("list"), q = $("q").value.trim().toLowerCase();
  const keep = list.scrollTop;
  list.innerHTML = "";
  const sec = (t) => { const h = document.createElement("div"); h.className = "sec"; h.textContent = t; list.appendChild(h); };
  if (q) {
    const names = items.filter((i) => i.id.toLowerCase().includes(q));
    sec(`Names (${names.length})`);
    names.slice(0, 50).forEach((it) => list.appendChild(row(it, 0, ago(it.mtime))));
    if (hits) {
      sec(`In text (${hits.length})`);
      for (const h of hits) {
        const it = byId.get(h.id); if (!it) continue;
        const wrap = document.createElement("div"); wrap.className = "hit";
        wrap.appendChild(row(it, 0, null));
        const s = document.createElement("div"); s.className = "snip";
        const i = h.snippet.toLowerCase().indexOf(q);
        s.append(h.snippet.slice(0, i));
        const m = document.createElement("mark"); m.textContent = h.snippet.slice(i, i + q.length); s.append(m);
        s.append(h.snippet.slice(i + q.length));
        s.onclick = () => select(it);
        wrap.appendChild(s); list.appendChild(wrap);
      }
    }
  } else {
    sec("Recent");
    [...items].sort((a, b) => b.mtime - a.mtime).slice(0, 6).forEach((it) => list.appendChild(row(it, 0, ago(it.mtime))));
    sec("Files");
    renderTree(buildTree(), 0, list);
  }
  list.scrollTop = keep;
}

function fit() {
  const img = $("img"), wrap = $("imgwrap");
  const w = +img.dataset.w || img.naturalWidth || 1000, h = +img.dataset.h || img.naturalHeight || 800;
  const s = Math.min(wrap.clientWidth / w, wrap.clientHeight / h, 3) * 0.96;
  zoom = { s, x: (wrap.clientWidth - w * s) / 2, y: (wrap.clientHeight - h * s) / 2 };
  applyZoom();
}
function applyZoom() { $("img").style.transform = `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`; }
function zoomAt(f, cx, cy) {
  const r = $("imgwrap").getBoundingClientRect();
  cx = cx ?? r.width / 2; cy = cy ?? r.height / 2;
  const s = Math.min(Math.max(zoom.s * f, 0.05), 20);
  zoom.x = cx - (cx - zoom.x) * (s / zoom.s); zoom.y = cy - (cy - zoom.y) * (s / zoom.s); zoom.s = s;
  applyZoom();
}
function showImage(url, w, h, keep) {
  const img = $("img");
  img.dataset.w = w || ""; img.dataset.h = h || "";
  img.style.width = w ? w + "px" : ""; img.style.height = h ? h + "px" : "";
  img.onload = () => { if (!keep) fit(); };
  img.src = url;
}

// A diagram from inside a note, full screen over it; Esc, Close or the
// browser's back (a history entry of its own) go back to the note without
// reloading it.
function openOverlay(svgText, title) {
  const vb = /viewBox\s*=\s*["']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(svgText.slice(0, 4000));
  const st = history.state || {};
  if (!st.overlay) history.pushState({ depth: (st.depth || 0) + 1, overlay: true }, "");
  overlay = true;
  $("frame").classList.add("hidden");
  $("imgwrap").classList.remove("hidden");
  $("zoomctl").classList.remove("hidden");
  $("closezoom").classList.remove("hidden");
  $("meta").textContent = title || "";
  showImage("data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgText), vb && vb[1], vb && vb[2], false);
}
function closeOverlay(fromHistory) {
  if (!overlay) return;
  if (!fromHistory && history.state && history.state.overlay) return history.back();  // popstate closes it
  overlay = false;
  $("closezoom").classList.add("hidden");
  $("imgwrap").classList.add("hidden");
  $("zoomctl").classList.add("hidden");
  $("frame").classList.remove("hidden");
  if (current) $("meta").textContent = metaFor(current);
}
const metaFor = (it) => it.id.split("/").slice(1, -1).join(" / ") + " · " + new Date(it.mtime * 1000).toLocaleString();

function show(it, opts = {}) {
  overlay = false;
  $("closezoom").classList.add("hidden");
  $("empty").classList.add("hidden");
  $("title").textContent = label(it);
  $("meta").textContent = metaFor(it);
  $("open").href = "/raw/" + enc(it.id);
  updateObs();
  $("pdf").classList.toggle("hidden", it.kind !== "note" && it.kind !== "text");
  const isImg = it.kind === "svg" || it.kind === "image", isArc = it.kind === "archive";
  $("dl").classList.toggle("hidden", !isArc);
  if (isArc) {
    $("dlname").textContent = it.name;
    $("dllink").href = "/raw/" + enc(it.id) + "?v=" + it.mtime;
  }
  $("frame").classList.toggle("hidden", isImg || isArc);
  $("imgwrap").classList.toggle("hidden", !isImg);
  $("zoomctl").classList.toggle("hidden", !isImg);
  const v = "?v=" + it.mtime;
  if (isImg) {
    const url = "/raw/" + enc(it.id) + v;
    if (it.kind === "svg") {
      // Many SVGs (D2's included) carry only a viewBox: size the image from it.
      fetch(url, { cache: "no-store" }).then((r) => r.text()).then((t) => {
        const vb = /viewBox\s*=\s*["']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(t.slice(0, 4000));
        showImage(url, vb && vb[1], vb && vb[2], opts.keep);
      });
    } else showImage(url, null, null, opts.keep);
  } else {
    const f = $("frame");
    f.classList.toggle("light", it.kind === "html");
    const base = (["note", "text", "canvas", "base"].includes(it.kind) ? "/view/" : "/raw/") + enc(it.id) + v;
    // Back or forward to the file the frame still holds (an image opened from
    // a note only hid the frame): reloading it would lose its scroll position.
    if (opts.restore && frameDoc === base && !opts.anchor) return;
    frameDoc = base;
    const y = opts.keep && scrolls[it.id] ? "&y=" + scrolls[it.id] : "";
    // replace(), not src =: a frame navigation would add a browser history
    // entry of its own, and back would step through the frame instead of files.
    f.contentWindow.location.replace(base + y + (opts.anchor ? "#" + encodeURIComponent(opts.anchor) : ""));
  }
}

// Each file opened is a browser history entry (state.depth counts entries
// back to the first page), so back and forward, the phone's back gesture
// included, move between files. how: "push" (default), "replace", or
// "history" when the browser already moved there.
function select(it, anchor, how) {
  current = it; seen.add(it.id);
  const p = it.id.split("/").slice(0, -1);
  let acc = p[0];
  for (const part of p.slice(1)) { acc += "/" + part; open.add(acc); }
  saveOpen();
  const hash = "#" + encodeURIComponent(it.id), st = history.state || {};
  if (how !== "history" && (location.hash !== hash || st.overlay)) {
    if (how === "replace" || st.overlay) history.replaceState({ depth: st.depth || 0 }, "", hash);
    else history.pushState({ depth: (st.depth || 0) + 1 }, "", hash);
  }
  if (phone.matches) toggleSidebar(false);
  $("obsbar").classList.add("hidden");
  show(it, { anchor, restore: how === "history" }); render();
}

// The Obsidian app. A web page cannot tell whether an app is installed, so
// each device remembers it: when the Obsidian button has opened the app, the
// page offers to open vault links there from then on. A link then opens the
// app, and this page, already showing the file, stays behind it as the
// fallback. Nothing here reaches the server; the setting and the vault name
// live in this browser's storage.
const OBS_KINDS = new Set(["note", "image", "svg", "canvas", "base"]);  // files Obsidian shows itself
const OBS_FRESH = 600;  // s: a newer file may not have synced to the device yet
const store = (s) => ({
  get(k) { try { return s.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { v === null ? s.removeItem(k) : s.setItem(k, v); } catch (e) { /* private mode */ } },
});
const local = store(localStorage), session = store(sessionStorage);
const obsMode = () => local.get("vault-obsidian");  // "on", "off", or null: not asked yet
function obsNames() {
  try { const n = JSON.parse(local.get("vault-obsidian-names") || "{}"); return n && typeof n === "object" ? n : {}; }
  catch (e) { return {}; }
}
function vaultOf(it) {
  const root = it.id.split("/")[0], name = obsNames()[root];
  return typeof name === "string" && name ? name : root;
}
const obsUrl = (it) => "obsidian://open?vault=" + encodeURIComponent(vaultOf(it)) +
  "&file=" + encodeURIComponent(it.id.split("/").slice(1).join("/"));

function updateObs() {
  const ok = current && OBS_KINDS.has(current.kind);
  $("obs").classList.toggle("hidden", !ok);
  if (ok) $("obs").href = obsUrl(current);
  $("obsmode").textContent = obsMode() === "on" ? "On" : "Off";
}
function setObsMode(mode) { local.set("vault-obsidian", mode); updateObs(); }

// A message above the file, with buttons; any button also closes it.
function notice(msg, actions = []) {
  const bar = $("obsbar");
  bar.replaceChildren();
  const s = document.createElement("span");
  s.textContent = msg;
  bar.append(s);
  for (const [text, fn] of [...actions, ["Dismiss", null]]) {
    const b = document.createElement("button");
    b.textContent = text;
    b.onclick = () => { bar.classList.add("hidden"); if (fn) fn(); };
    bar.append(b);
  }
  bar.classList.remove("hidden");
}

// True when the page loses focus or is hidden within MS: the app took over.
function launched(ms) {
  return new Promise((resolve) => {
    const vis = () => { if (document.visibilityState === "hidden") finish(true); };
    const yes = () => finish(true);
    const t = setTimeout(() => finish(false), ms);
    function finish(v) {
      clearTimeout(t);
      removeEventListener("blur", yes); removeEventListener("pagehide", yes);
      document.removeEventListener("visibilitychange", vis);
      resolve(v);
    }
    addEventListener("blur", yes); addEventListener("pagehide", yes);
    document.addEventListener("visibilitychange", vis);
  });
}

function didNotOpen(it, actions) {
  notice(`Obsidian did not open, so the file is shown here. Is the app installed on this device, with a vault named "${vaultOf(it)}"?`,
    [...actions, ["Vault name…", askVault]]);
}

// The link itself opens the app; this only watches whether it did.
$("obs").onclick = () => {
  const it = current;
  launched(4000).then((ok) => {
    if (!ok) didNotOpen(it, []);
    else if (obsMode() === null)
      notice("Obsidian opened. Open vault links in Obsidian on this device from now on?",
        [["Yes", () => setObsMode("on")], ["No", () => setObsMode("off")]]);
  });
};

// Called once, for the file named in the URL the page was opened with.
function autoObsidian(it) {
  if (obsMode() !== "on" || !OBS_KINDS.has(it.kind)) return;
  if (session.get("vault-obsidian-tried") === it.id) return;  // reloading the same link stays here
  session.set("vault-obsidian-tried", it.id);
  const age = Date.now() / 1000 - it.mtime;
  if (age < OBS_FRESH) {
    return notice(`Changed ${Math.max(1, Math.round(age / 60))} min ago, so it may not have reached Obsidian on this device yet. Showing it here.`,
      [["Open in Obsidian", () => $("obs").click()]]);
  }
  location.href = obsUrl(it);
  launched(4000).then((ok) => { if (!ok) didNotOpen(it, [["Stop trying on this device", () => setObsMode("off")]]); });
}

function askVault() {
  const root = (current || items[0] || { id: "" }).id.split("/")[0];
  if (!root) return;
  const v = prompt(`The vault's name in Obsidian on this device (the files under "${root}"):`, vaultOf({ id: root + "/" }));
  if (v === null) return;
  const names = obsNames();
  if (v.trim() && v.trim() !== root) names[root] = v.trim(); else delete names[root];
  local.set("vault-obsidian-names", JSON.stringify(names));
  updateObs();
}

function goHome(how) {
  const readme = items.find((i) => i.id.split("/").length === 2 && /^readme\.md$/i.test(i.name));
  if (readme) select(readme, "", how);
}

// Rendered on the server (a few seconds); the button says so meanwhile.
async function downloadPdf() {
  const it = current, b = $("pdf");
  if (!it || b.disabled || b.classList.contains("hidden")) return;
  b.disabled = true; b.textContent = "PDF\u2026";
  try {
    const r = await fetch("/pdf/" + enc(it.id), { cache: "no-store" });
    if (!r.ok) throw new Error((await r.text()).trim() || r.statusText);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = label(it) + ".pdf";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  } catch (e) { alert(e.message); }
  finally { b.disabled = false; b.textContent = "PDF"; }
}

// Until the first tree arrives the stage says it is loading; a failed first
// load says why and retries every 2 s instead of waiting for the next poll.
async function refresh() {
  try {
    const r = await fetch("/api/tree", { cache: "no-cache" });  // revalidates: 304 when nothing changed
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    items = data.items; byId = new Map(items.map((i) => [i.id, i]));
    $("vname").textContent = data.title || data.roots.join(", ");
    document.title = (data.title || data.roots.join(", ")) + " vault";
    if (firstLoad) {
      items.forEach((i) => seen.add(i.id));
      history.replaceState({ depth: (history.state && history.state.depth) || 0 }, "");
      const it = byId.get(decodeURIComponent(location.hash.slice(1)));
      firstLoad = false;
      $("emptymsg").textContent = "Pick a note or an artifact on the left.";
      if (it) { select(it, "", "replace"); autoObsidian(it); } else goHome("replace");
    } else if (current && !overlay) {
      const now = byId.get(current.id);
      if (now && now.mtime !== current.mtime) { current = now; show(now, { keep: true }); }
    }
    render();
  } catch (e) {
    // Later polls stay quiet (the server may be restarting); the first load says so.
    if (firstLoad) {
      $("emptymsg").textContent = "Could not load the file list (" + (e.message || e) + "). Retrying\u2026";
      clearTimeout(retryTimer); retryTimer = setTimeout(refresh, 2000);
    }
  }
}

window.addEventListener("message", (e) => {
  if (e.source !== $("frame").contentWindow || !e.data || typeof e.data !== "object") return;
  const d = e.data;
  if (d.type === "vault-open" && typeof d.id === "string") {
    const it = byId.get(d.id);
    if (it) select(it, typeof d.anchor === "string" ? d.anchor : "");
  } else if (d.type === "vault-scroll" && current && Number.isFinite(d.y)) {
    scrolls[current.id] = d.y;
  } else if (d.type === "vault-copy" && typeof d.text === "string" && d.text.length < 5e6) {
    // A code block's copy button, when the sandboxed frame may not copy itself.
    // Only text reaches the clipboard, and only during the click that sent it.
    const reply = (ok) => e.source.postMessage({ type: "vault-copied", ok }, "*");
    const viaTextarea = () => {
      const ta = document.createElement("textarea");
      ta.value = d.text; ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
      document.body.appendChild(ta); ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      ta.remove();
      return ok;
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(d.text).then(() => reply(true), () => reply(viaTextarea()));
    else reply(viaTextarea());
  } else if (d.type === "vault-zoom" && typeof d.svg === "string" && d.svg.length < 20e6 && /^\s*<svg[\s>]/.test(d.svg)) {
    // Shown as an <img> data URL: an SVG in an image can't run scripts.
    openOverlay(d.svg, typeof d.title === "string" ? d.title : "");
  }
});
window.addEventListener("popstate", () => {
  if (overlay && !(history.state && history.state.overlay)) closeOverlay(true);
  const it = byId.get(decodeURIComponent(location.hash.slice(1)));
  if (it && (!current || it.id !== current.id)) select(it, "", "history");
});
$("q").oninput = () => {
  hits = null; render();
  clearTimeout(searchTimer);
  const q = $("q").value.trim();
  if (q.length >= 2) searchTimer = setTimeout(async () => {
    const r = await fetch("/api/search?q=" + encodeURIComponent(q), { cache: "no-store" });
    if ($("q").value.trim() === q) { hits = await r.json(); render(); }
  }, 250);
};
$("home").onclick = () => goHome();
$("back").onclick = () => { if (overlay) return closeOverlay(); if (history.state && history.state.depth > 0) history.back(); };
$("closezoom").onclick = () => closeOverlay();
$("toggle").onclick = () => toggleSidebar();
$("scrim").onclick = () => toggleSidebar(false);
$("reload").onclick = () => current && show(current, { keep: true });
$("pdf").onclick = downloadPdf;
$("obsmode").onclick = () => setObsMode(obsMode() === "on" ? "off" : "on");
$("obsvault").onclick = askVault;
$("zin").onclick = () => zoomAt(1.25); $("zout").onclick = () => zoomAt(0.8); $("zfit").onclick = fit;
const wrap = $("imgwrap");
wrap.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = wrap.getBoundingClientRect();
  if (e.ctrlKey || e.metaKey) zoomAt(Math.exp(-e.deltaY * 0.01), e.clientX - r.left, e.clientY - r.top);
  else { zoom.x -= e.deltaX; zoom.y -= e.deltaY; applyZoom(); }
}, { passive: false });
// Pointer events cover mouse and touch: one pointer pans, two pinch-zoom
// around their midpoint, a double tap fits (dblclick for the mouse).
const pts = new Map();
let pinch = null, lastTap = 0;
wrap.addEventListener("pointerdown", (e) => {
  if (e.target.closest("button")) return;
  wrap.setPointerCapture(e.pointerId);
  pts.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY });
  pinch = null;
  wrap.classList.add("drag");
});
wrap.addEventListener("pointermove", (e) => {
  const p = pts.get(e.pointerId);
  if (!p) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  p.x = e.clientX; p.y = e.clientY;
  if (pts.size === 1) { zoom.x += dx; zoom.y += dy; applyZoom(); return; }
  const [a, b] = [...pts.values()], r = wrap.getBoundingClientRect();
  const d = Math.hypot(a.x - b.x, a.y - b.y), mx = (a.x + b.x) / 2 - r.left, my = (a.y + b.y) / 2 - r.top;
  if (pinch && pinch.d > 0) { zoom.x += mx - pinch.mx; zoom.y += my - pinch.my; zoomAt(d / pinch.d, mx, my); }
  pinch = { d, mx, my };
});
const lift = (e) => {
  const p = pts.get(e.pointerId);
  if (!p) return;
  pts.delete(e.pointerId); pinch = null;
  if (!pts.size) wrap.classList.remove("drag");
  if (e.type === "pointerup" && e.pointerType !== "mouse" && !pts.size && Math.hypot(p.x - p.x0, p.y - p.y0) < 10) {
    if (e.timeStamp - lastTap < 300) { fit(); lastTap = 0; } else lastTap = e.timeStamp;
  }
};
wrap.addEventListener("pointerup", lift);
wrap.addEventListener("pointercancel", lift);
wrap.addEventListener("dblclick", fit);
window.addEventListener("resize", () => !$("imgwrap").classList.contains("hidden") && fit());
document.addEventListener("keydown", (e) => {
  if (e.target === $("q")) { if (e.key === "Escape") { $("q").value = ""; hits = null; render(); $("q").blur(); } return; }
  if (e.key === "Escape") { if ($("app").classList.contains("drawer")) toggleSidebar(false); else closeOverlay(); }
  else if (e.key === "/") { e.preventDefault(); if (phone.matches) toggleSidebar(true); $("q").focus(); }
  else if (e.key === "[") toggleSidebar();
  else if (e.key === "r") current && show(current, { keep: true });
  else if (e.key === "p" && !e.ctrlKey && !e.metaKey && !e.altKey) downloadPdf();
  else if (e.key === "o" && !e.ctrlKey && !e.metaKey && !e.altKey) { if (!$("obs").classList.contains("hidden")) $("obs").click(); }
  else if (e.altKey && e.key === "ArrowLeft") $("back").click();
  else if (!$("imgwrap").classList.contains("hidden")) {
    if (e.key === "+" || e.key === "=") zoomAt(1.25);
    else if (e.key === "-") zoomAt(0.8);
    else if (e.key === "0") fit();
  }
});
updateObs();
refresh();
// Poll every 10 s, not while the tab is hidden; catch up as soon as it is shown again.
setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 10000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refresh(); });
</script>
</body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bind", default=os.environ.get("ARTIFACTS_BIND", "127.0.0.1"),
                    help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ARTIFACTS_PORT", 7777)))
    ap.add_argument("--allow-host", action="append", default=[],
                    help="extra Host header name to accept, e.g. myhost (repeatable)")
    ap.add_argument("--tailnet-user", default=os.environ.get("ARTIFACTS_TAILNET_USER"),
                    help="only admit this user's untagged tailnet devices (tailscale login name)")
    ap.add_argument("--title", help="name shown in the sidebar (default: the vault folder names)")
    ap.add_argument("--pdf-runtime", metavar="DIR", type=Path, default=default_pdf_runtime(),
                    help="where the PDF runtime is installed (default %(default)s; the AppArmor profile "
                         "printed by --print-apparmor-profile names this directory)")
    ap.add_argument("--install-pdf-runtime", action="store_true",
                    help="download (once) and install the PDF runtime into --pdf-runtime, then exit")
    ap.add_argument("--check-pdf-runtime", action="store_true",
                    help="render a tiny page with the installed runtime, print ok or the reason, then exit")
    ap.add_argument("--print-apparmor-profile", action="store_true",
                    help="print the AppArmor profile Chromium's sandbox needs on Ubuntu 23.10+ for the --pdf-runtime "
                         "directory, then exit")
    ap.add_argument("--obsidian-cli", metavar="CMD", default=os.environ.get("ARTIFACTS_OBSIDIAN_CLI"),
                    help="the Obsidian CLI command that evaluates .base files (run as `CMD base:query path=... "
                         "[view=...] format=json`), one program path, never run through a shell; default: off, "
                         "bases then show their YAML only; `none` also disables it (env ARTIFACTS_OBSIDIAN_CLI)")
    ap.add_argument("--obsidian-vault", metavar="DIR", type=Path, default=os.environ.get("ARTIFACTS_OBSIDIAN_VAULT"),
                    help="the Obsidian vault folder the CLI's app has open; only .base files inside it are "
                         "evaluated (default: the first ROOT; env ARTIFACTS_OBSIDIAN_VAULT)")
    ap.add_argument("--pdf-rev", action="store_true",
                    help="print the revision of the PDF runtime this program uses, then exit")
    # Removed: the PDF image (docker) and, before it, the Playwright base image. Kept hidden so
    # an old command line says what changed instead of "unrecognized arguments".
    ap.add_argument("--build-pdf-image", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pdf-image", metavar="IMAGE", nargs="?", const="", help=argparse.SUPPRESS)
    ap.add_argument("--pdf-base", metavar="IMAGE", help=argparse.SUPPRESS)
    ap.add_argument("roots", nargs="*", type=Path, help="vault folders to serve (default: ~/obsidian)")
    args = ap.parse_args()
    args.pdf_runtime = abs_runtime(args.pdf_runtime)
    for flag, given in (("--build-pdf-image", args.build_pdf_image), ("--pdf-image", args.pdf_image is not None),
                        ("--pdf-base", args.pdf_base is not None)):
        if given:
            print(f"artifacts: `{flag}` was removed: {PDF_REMOVED}", file=sys.stderr)
            sys.exit(2)
    if args.pdf_rev:
        print(PDF_REV)
        sys.exit(0)
    if args.print_apparmor_profile:
        try:
            print(pdf_apparmor_profile(args.pdf_runtime), end="")
        except ValueError as e:
            print(f"artifacts: {e}", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)
    if args.install_pdf_runtime:
        try:
            final = install_pdf_runtime(args.pdf_runtime)
        except (PdfInstallError, OSError) as e:
            print(f"artifacts: installing the PDF runtime failed: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"installed {final} ({pdf_runtime_size(final) / (1 << 20):.0f} MiB)")
        print("\n".join(pdf_apparmor_state(args.pdf_runtime)))
        sys.exit(0)
    if args.check_pdf_runtime:
        ok, why = check_pdf_runtime(args.pdf_runtime)
        print(why, file=sys.stdout if ok else sys.stderr)
        sys.exit(0 if ok else 1)
    names = {args.bind, *args.allow_host}
    if ipaddress.ip_address(args.bind).is_loopback:
        names |= {"127.0.0.1", "localhost"}
    Handler.hosts = {f"{n.lower()}:{args.port}" for n in names}
    Handler.gate = TailnetGate(args.tailnet_user)
    Handler.title = args.title
    Handler.pdf_runtime = args.pdf_runtime
    Handler.library = Library(args.roots or DEFAULT_ROOTS)
    global OBSIDIAN
    OBSIDIAN = BaseEngine(args.obsidian_cli, args.obsidian_vault or Handler.library.roots[0])
    if markdown is None:
        print("artifacts: python3-markdown is missing, notes render as plain text", file=sys.stderr)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"artifacts: http://{args.bind}:{args.port}  roots: {', '.join(map(str, Handler.library.roots))}"
          f"  hosts: {', '.join(sorted(Handler.hosts))}"
          f"  tailnet user: {args.tailnet_user or 'any'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
