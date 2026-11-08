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
button): a light, print-styled page rendered by headless Chromium in a
container (--pdf-image, built once with --build-pdf-image) that runs with no
network, a read-only root and no capabilities. Mermaid, highlight.js and the
fonts are baked into that image, and images are inlined, so nothing is fetched
while printing.

Needs python3-markdown (Debian/Ubuntu package) for notes; the tailscale CLI
for --tailnet-user; docker for PDFs.
Usage: artifacts.py [--bind ADDR] [--port 7777] [--allow-host NAME] [--tailnet-user LOGIN]
                    [--pdf-image IMAGE] [--build-pdf-image] [--pdf-rev] [ROOT ...]
"""
import argparse
import base64
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
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

DEFAULT_ROOTS = [Path.home() / "obsidian"]
KINDS = {
    ".md": "note",
    ".html": "html", ".htm": "html",
    ".svg": "svg",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".d2": "text", ".txt": "text", ".json": "text", ".yaml": "text", ".yml": "text", ".mmd": "text",
    ".csv": "text", ".toml": "text", ".py": "text", ".sh": "text", ".go": "text", ".ts": "text", ".js": "text",
    ".zip": "archive",
    ".css": "asset", ".woff2": "asset",
}
CDNS = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com"
HLJS = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0"
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
MERMAID_ELK = "https://cdn.jsdelivr.net/npm/@mermaid-js/layout-elk@0/dist/mermaid-layout-elk.esm.min.mjs"
# The same libraries inside the PDF image, served to the page by PDF_RENDER.
PDF_LIB = "/lib"
PDF_HLJS = PDF_LIB + "/@highlightjs/cdn-assets"
PDF_MERMAID = PDF_LIB + "/mermaid/dist/mermaid.esm.min.mjs"
PDF_MERMAID_ELK = PDF_LIB + "/@mermaid-js/layout-elk/dist/mermaid-layout-elk.esm.min.mjs"
PDF_IMAGE = "artifacts-pdf:local"
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
    def __init__(self, roots):
        self.roots = [r.expanduser().resolve() for r in roots]
        self._links = {}  # path -> (mtime, [targets])
        self.lock = threading.Lock()

    def scan(self):
        items = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in filenames:
                    p = Path(dirpath) / name
                    kind = KINDS.get(p.suffix.lower())
                    if name.startswith(".") or kind in (None, "asset"):
                        continue
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    rel = p.relative_to(root).as_posix()
                    items.append({"id": f"{root.name}/{rel}", "name": p.name, "kind": kind,
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
# into the PDF image, uses the light theme, fits diagrams to the page width and
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


def render_note(lib, path, ident, pdf=False):
    """A note as a viewer page, or with pdf=True as the page a PDF prints: light,
    images inlined, vault links as plain text, no properties or backlinks."""
    items = lib.scan()
    items_by_id = {i["id"]: i for i in items}
    by_name = lib.index(items)
    text = path.read_text(errors="replace")
    fm, body = split_frontmatter(text)
    note_dir = ident.rsplit("/", 1)[0]
    inline = inliner(lib, items_by_id) if pdf else None

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

    head = ""
    if 'class="language-mermaid"' in out:
        out = mermaid_blocks(out)
        head = mermaid_head(pdf)
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


def search(lib, q, limit=40):
    q = q.strip().lower()
    if len(q) < 2:
        return []
    out = []
    for it in lib.scan():
        if it["kind"] not in ("note", "text", "html"):
            continue
        p = lib.resolve(it["id"])
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if it["kind"] == "html":
            text = re.sub(r"<[^>]+>", " ", text)
        i = text.lower().find(q)
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
# no network. Built with `artifacts --build-pdf-image`. Kept small on purpose:
# Debian 12 slim (whose libgbm1 does not pull in LLVM, unlike Ubuntu 24.04's),
# only the headless shell (no full Chromium, Firefox or WebKit, no Playwright),
# only the shared libraries it links, and only the library files the print page
# loads (no source maps or other builds).
# The headless shell comes from Chrome for Testing, pinned by version and
# checksum: change PDF_CHROME_VERSION and PDF_CHROME_SHA256 together.
PDF_CHROME_VERSION = "153.0.8010.12"
PDF_CHROME_URL = ("https://storage.googleapis.com/chrome-for-testing-public/"
                  f"{PDF_CHROME_VERSION}/linux64/chrome-headless-shell-linux64.zip")
PDF_CHROME_SHA256 = "a9da028861a0cf789ff25c2fed45f5f1aaf969ed9247835b6a7821a4f7af9d1d"
# Inter as static TrueType, from the upstream release. Debian's fonts-inter
# ships CFF outlines (.otf), which Chromium can only embed in a PDF as Type 3
# glyph drawings: PDF readers then select and copy that text badly or not at all.
PDF_INTER_URL = "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip"
PDF_INTER_SHA256 = "9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e"
PDF_DOCKERFILE = f"""
FROM node:22-slim AS lib
RUN npm install --prefix /build --no-audit --no-fund \\
      mermaid@11 @mermaid-js/layout-elk@0 @highlightjs/cdn-assets@11.9.0 \\
    && cd /build/node_modules && mkdir /lib-out \\
    && cp --parents mermaid/dist/mermaid.esm.min.mjs @mermaid-js/layout-elk/dist/mermaid-layout-elk.esm.min.mjs \\
         @highlightjs/cdn-assets/highlight.min.js @highlightjs/cdn-assets/styles/github.min.css /lib-out \\
    && cp -r --parents mermaid/dist/chunks/mermaid.esm.min \\
         @mermaid-js/layout-elk/dist/chunks/mermaid-layout-elk.esm.min /lib-out \\
    && find /lib-out -name '*.map' -delete
FROM debian:bookworm-slim AS fetch
ADD --checksum=sha256:{PDF_INTER_SHA256} {PDF_INTER_URL} /inter.zip
ADD --checksum=sha256:{PDF_CHROME_SHA256} {PDF_CHROME_URL} /chrome.zip
RUN apt-get update && apt-get install -y --no-install-recommends unzip \\
    && unzip -j /inter.zip 'extras/ttf/Inter-*.ttf' -d /inter \\
    && unzip -q /chrome.zip -d /opt && mv /opt/chrome-headless-shell-linux64 /opt/chrome-headless-shell
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 \\
      libnss3 libnspr4 libgbm1 libdrm2 libasound2 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \\
      libdbus-1-3 libxkbcommon0 libx11-6 libxcb1 libxext6 libxcomposite1 libxdamage1 libxfixes3 \\
      libxi6 libxrandr2 fontconfig-config fonts-dejavu-core fonts-jetbrains-mono fonts-noto-color-emoji \\
    && rm -rf /var/lib/apt/lists/* \\
    && useradd --create-home pwuser
COPY --from=fetch /inter /usr/share/fonts/truetype/inter
COPY --from=fetch /opt/chrome-headless-shell /opt/chrome-headless-shell
COPY --from=lib /lib-out /opt/vault-pdf/lib
"""


PDF_BASE_REMOVED = ("--pdf-base was removed: the PDF image now builds on debian:bookworm-slim with only "
                    "Chromium's headless shell, so there is no Playwright base image to pick")


def pdf_build_command(image):
    """The `docker build` argv for the PDF image (Dockerfile on stdin)."""
    return ["docker", "build", "--pull", "-t", image, "-"]


# Runs inside the container: the page arrives on stdin, the PDF leaves on
# stdout. It drives Chromium's headless shell over the DevTools protocol on a
# pipe (fds 3 and 4, NUL-separated JSON), standard library only. The page is
# served at a made-up origin, with only PDF_LIB files beside it; every other
# request is refused (and the container has no network). Launch switches and
# print parameters follow what Playwright used for this before, so the output
# is unchanged.
PDF_RENDER = r"""
import base64, fcntl, json, os, select, signal, sys, tempfile, time, urllib.parse
from pathlib import Path

CHROME = "/opt/chrome-headless-shell/chrome-headless-shell"
page_html = sys.stdin.buffer.read()
lib = Path("/opt/vault-pdf/lib").resolve()
types = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css"}
flags = [
    "--headless", "--no-sandbox", "--remote-debugging-pipe", "--no-startup-window",
    "--user-data-dir=" + tempfile.mkdtemp(prefix="chrome-"),
    "--disable-dev-shm-usage", "--enable-unsafe-swiftshader", "--force-color-profile=srgb",
    "--hide-scrollbars", "--mute-audio", "--export-tagged-pdf",
    "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
    "--disable-background-networking", "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
    "--disable-breakpad", "--disable-component-update", "--disable-default-apps", "--disable-extensions",
    "--disable-field-trial-config", "--disable-hang-monitor", "--disable-sync", "--metrics-recording-only",
    "--no-default-browser-check", "--no-first-run", "--no-service-autorun", "--password-store=basic",
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

buf, scanned, last_id, seen = bytearray(), 0, 0, set()

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
    elif method == "Target.attachedToTarget":  # frames and workers: same request filter
        child = p["sessionId"]
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
    target = call("Target.createTarget", {"url": "about:blank"})["targetId"]
    s = call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, s)
    call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, s)
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
        pump(0.1)
    pdf = call("Page.printToPDF", {
        "landscape": False, "displayHeaderFooter": True, "headerTemplate": "<span></span>",
        "footerTemplate": footer, "printBackground": True, "scale": 1, "paperWidth": 8.27, "paperHeight": 11.7,
        "marginTop": 16 * mm, "marginBottom": 18 * mm, "marginLeft": 14 * mm, "marginRight": 14 * mm,
        "pageRanges": "", "preferCSSPageSize": False, "generateTaggedPDF": False,
        "generateDocumentOutline": False}, s, timeout=60)["data"]
    sys.stdout.buffer.write(base64.b64decode(pdf))
    sys.stdout.flush()
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

# The render script and the image depend on each other's layout, so the image
# carries the revision of the pair it was built from, and a program only trusts
# an image with its own revision. `artifacts --pdf-rev` prints it.
PDF_REV_LABEL = "vault-viewer.pdf-rev"
PDF_REV = hashlib.sha256((PDF_DOCKERFILE + PDF_RENDER).encode()).hexdigest()[:12]
PDF_DOCKERFILE += f"LABEL {PDF_REV_LABEL}={PDF_REV}\n"


def pdf_image_rev(image):
    """The PDF_REV label of a local image: "" if it has none, None if docker can't say."""
    try:
        out = subprocess.run(["docker", "image", "inspect", "-f", f'{{{{ index .Config.Labels "{PDF_REV_LABEL}" }}}}',
                              image], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    rev = out.stdout.strip()
    return "" if rev == "<no value>" else rev


PDF_SLOTS = threading.BoundedSemaphore(2)


def render_pdf(page_html, image):
    """Print a page to PDF in a throwaway container. Returns (pdf bytes, None) or (None, reason)."""
    if not PDF_SLOTS.acquire(timeout=60):
        return None, "busy rendering other PDFs, try again"
    name = f"artifacts-pdf-{uuid.uuid4().hex[:12]}"
    try:
        out = subprocess.run(
            ["docker", "run", "--rm", "-i", "--name", name, "--pull", "never", "--network", "none",
             "--read-only", "--tmpfs", "/tmp:rw,size=512m", "--env", "HOME=/tmp", "--user", "pwuser",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "1g", "--pids-limit", "512",
             image, "python3", "-c", PDF_RENDER],
            input=page_html.encode(), capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        return None, "rendering took longer than 90 s"
    except OSError as e:
        return None, f"cannot run docker: {e}"
    finally:
        PDF_SLOTS.release()
    err = out.stderr.decode(errors="replace").strip()
    if err:
        sys.stderr.write(f"artifacts: pdf: {err[-2000:]}\n")
    if out.returncode != 0 or not out.stdout.startswith(b"%PDF"):
        if "No such image" in err:
            return None, f"the PDF image {image} is missing: run `artifacts --build-pdf-image`"
        rev = pdf_image_rev(image)
        if rev is not None and rev != PDF_REV:
            return None, (f"the PDF image {image} does not match this program (image revision {rev or 'none'}, "
                          f"program {PDF_REV}): run `artifacts --build-pdf-image`")
        return None, "rendering failed (details in the service log)"
    return out.stdout, None


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
    pdf_image = PDF_IMAGE

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), self.client_address[0], fmt % args))

    def _send(self, status, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

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
                lib = self.library
                return self._send(HTTPStatus.OK, json.dumps({"roots": [r.name for r in lib.roots], "title": self.title, "items": lib.scan()}),
                                  "application/json")
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
                pdf, why = render_pdf(page, self.pdf_image)
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
                    return self._send(HTTPStatus.OK, render_note(self.library, p, ident), "text/html; charset=utf-8", sandboxed)
                if prefix == "/view/" and kind == "text":
                    return self._send(HTTPStatus.OK, render_text(p, ident), "text/html; charset=utf-8", sandboxed)
                ctype = mimetypes.guess_type(p.name)[0] or "text/plain"
                if kind in ("note", "text"):
                    ctype = "text/plain"
                if ctype.startswith("text/") or ctype in ("image/svg+xml", "application/json", "application/javascript"):
                    ctype += "; charset=utf-8"
                if kind == "archive":
                    return self._send(HTTPStatus.OK, p.read_bytes(), "application/zip", {**sandboxed, **attachment(p.name)})
                return self._send(HTTPStatus.OK, p.read_bytes(), ctype, sandboxed)
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
      <a class="btn" id="open" target="_blank" rel="noopener" title="Open the raw file in a new tab">Raw &#8599;</a>
    </div>
    <div id="stage">
      <div class="empty" id="empty"><div>Pick a note or an artifact on the left.</div></div>
      <div class="empty hidden" id="dl"><div><div id="dlname"></div><p>Archives are not previewed.</p><a class="btn" id="dllink" download>Download</a></div></div>
      <iframe id="frame" class="hidden" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>
      <div id="imgwrap" class="hidden"><button id="closezoom" class="hidden" title="Back to the note (Esc)">&#10005; Close</button><img id="img" alt=""></div>
    </div>
  </main>
  <div id="scrim"></div>
</div>
<script>
const $ = (id) => document.getElementById(id);
const ICON = { note: "≣", html: "◈", svg: "◇", image: "▣", text: "≡", archive: "▤" };
let items = [], byId = new Map(), current = null, seen = new Set(), firstLoad = true, overlay = false;
let open = new Set(JSON.parse(localStorage.getItem("vault-open") || "[]"));
let scrolls = {}, zoom = { s: 1, x: 0, y: 0 }, hits = null, searchTimer = null;
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
    const base = (it.kind === "note" || it.kind === "text" ? "/view/" : "/raw/") + enc(it.id) + v;
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
  show(it, { anchor }); render();
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

async function refresh() {
  try {
    const r = await fetch("/api/tree", { cache: "no-store" });
    const data = await r.json();
    items = data.items; byId = new Map(items.map((i) => [i.id, i]));
    $("vname").textContent = data.title || data.roots.join(", ");
    document.title = (data.title || data.roots.join(", ")) + " vault";
    if (firstLoad) {
      items.forEach((i) => seen.add(i.id));
      history.replaceState({ depth: (history.state && history.state.depth) || 0 }, "");
      const it = byId.get(decodeURIComponent(location.hash.slice(1)));
      firstLoad = false;
      if (it) select(it, "", "replace"); else goHome("replace");
    } else if (current && !overlay) {
      const now = byId.get(current.id);
      if (now && now.mtime !== current.mtime) { current = now; show(now, { keep: true }); }
    }
    render();
  } catch (e) { /* server restarting; next tick */ }
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
  else if (e.altKey && e.key === "ArrowLeft") $("back").click();
  else if (!$("imgwrap").classList.contains("hidden")) {
    if (e.key === "+" || e.key === "=") zoomAt(1.25);
    else if (e.key === "-") zoomAt(0.8);
    else if (e.key === "0") fit();
  }
});
refresh();
setInterval(refresh, 2000);
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
    ap.add_argument("--pdf-image", default=os.environ.get("ARTIFACTS_PDF_IMAGE", PDF_IMAGE),
                    help=f"docker image that renders PDFs (default {PDF_IMAGE})")
    ap.add_argument("--build-pdf-image", action="store_true",
                    help="build --pdf-image with docker, then exit (needs network once)")
    ap.add_argument("--pdf-rev", action="store_true",
                    help=f"print the revision the PDF image must carry (its {PDF_REV_LABEL} label), then exit")
    # Removed: the PDF image no longer builds on a Playwright image. Kept hidden so
    # an old command line says what changed instead of "unrecognized arguments".
    ap.add_argument("--pdf-base", metavar="IMAGE", help=argparse.SUPPRESS)
    ap.add_argument("roots", nargs="*", type=Path, help="vault folders to serve (default: ~/obsidian)")
    args = ap.parse_args()
    if args.pdf_rev:
        print(PDF_REV)
        sys.exit(0)
    if args.pdf_base is not None:
        print(f"artifacts: {PDF_BASE_REMOVED}; run `artifacts --build-pdf-image` without it", file=sys.stderr)
        sys.exit(2)
    if args.build_pdf_image:
        if os.environ.get("ARTIFACTS_PDF_BASE"):
            print(f"artifacts: ignoring ARTIFACTS_PDF_BASE: {PDF_BASE_REMOVED}", file=sys.stderr)
        sys.exit(subprocess.run(pdf_build_command(args.pdf_image), input=PDF_DOCKERFILE.encode()).returncode)
    names = {args.bind, *args.allow_host}
    if ipaddress.ip_address(args.bind).is_loopback:
        names |= {"127.0.0.1", "localhost"}
    Handler.hosts = {f"{n.lower()}:{args.port}" for n in names}
    Handler.gate = TailnetGate(args.tailnet_user)
    Handler.title = args.title
    Handler.pdf_image = args.pdf_image
    Handler.library = Library(args.roots or DEFAULT_ROOTS)
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
