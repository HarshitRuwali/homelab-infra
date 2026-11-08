# PDF export

Notes and text files have a **PDF** button in the viewer's top bar (key `p`).
It downloads a light, print-styled A4 PDF named after the file, ready to share.
HTML artifacts, SVGs and images have no PDF button.

## Prerequisites

- `docker`, usable by the user the service runs as.
- The PDF image, built once. Pass `--build-pdf-image` to the installer, or build
  it later as described under [The image](#the-image).

## What the PDF contains

- The body: headings, tables, callouts and code. Code is syntax-highlighted and
  long lines wrap instead of being clipped.
- Embedded images and Mermaid diagrams. Diagrams use a light theme and shrink
  to the page width but stay vector, so they zoom cleanly.
- External links, which stay clickable.
- A footer on every page: the title on the left, `page / total` on the right.
- Selectable text that copies as written. Every font is embedded as TrueType,
  and code shows its characters as typed (no `->` arrow ligature).

## What it leaves out

- The frontmatter properties box, the "Linked from" backlinks and the folder
  breadcrumb. These are vault internals.
- `[[wikilinks]]` as links: they become plain text, because they point into the
  vault and mean nothing to a reader outside it.
- Images that cannot be inlined. They are dropped silently, with no
  broken-image icon.

Only images inside the vault are inlined. Each image is read once, and a
symlink that points out of the vault is dropped, not followed.

## How it is rendered

`GET /pdf/<id>` renders the note as a print page and pipes it into a throwaway
container that runs Chromium's headless shell (driven by Playwright). The PDF comes back on
stdout and the container is removed.

The container runs with:

| Flag | Effect |
|---|---|
| `--network none` | No network at all |
| `--read-only`, `--tmpfs /tmp:rw,size=512m` | Read-only root; 512 MB of scratch space |
| `--user pwuser`, `--cap-drop ALL`, `--security-opt no-new-privileges` | No root, no capabilities, no privilege gain |
| `--memory 1g`, `--pids-limit 512` | Bounded memory and process count |
| `--pull never` | Never fetches an image on demand |

Mermaid, highlight.js and the Inter and JetBrains Mono fonts are baked into the
image, and images are inlined as `data:` URLs, so nothing is fetched while
printing. Inside Chromium the page is served from a made-up origin, every
request other than the page and its bundled libraries is refused, and the page
carries `connect-src 'none'`.

Notes are untrusted. Raw HTML in a note does run inside that Chromium, and is
contained by the missing network, the page policy and the request filter.

## Limits

| Limit | Value |
|---|---|
| Concurrent renders | 2. A third waits up to 60 s for a slot, then the request fails with "busy rendering other PDFs, try again" |
| Render time | 90 s, after which the container is removed |
| Inlined images | 40 MB per PDF |
| Supported files | Notes and text files only; anything else answers `404` |
| `HEAD` requests | `405`, so a link preview never starts a render |
| Cross-site requests | `403` when `Sec-Fetch-Site` is present and is not `same-origin` or `none`, because every request starts a container. A request without the header is accepted |

The usual viewer checks (tailnet gate and `Host` header) apply before any of
this. A failed render answers the button with a short message and a `503`
instead of dropping the connection.

## The image

The image is `artifacts-pdf:local`. It is built from the Dockerfile embedded in
`artifacts.py` (`PDF_DOCKERFILE`) and holds only what printing needs, so it
stays at a few hundred MB on disk:

- **Base:** `debian:bookworm-slim`. Debian 12's `libgbm1` does not pull in
  LLVM and the Mesa drivers, which Ubuntu 24.04's does (about 180 MB more).
- **Browser:** Chromium's headless shell only, installed with
  `playwright install --only-shell chromium`. There is no full Chromium,
  Firefox or WebKit, and no `--with-deps`: the image installs only the shared
  libraries the headless shell links against, and no X server or extra font
  families.
- **Driver:** the `playwright` pip package, pinned by `PDF_PLAYWRIGHT`. It is
  installed with the browser in a builder stage and copied in, so pip itself
  is not in the image.
- **Libraries:** Mermaid 11 with the ELK layout and highlight.js, cut down to
  the files the print page loads (the minified ESM builds and their chunks,
  `highlight.min.js` and the GitHub style), without source maps.
- **Fonts:** Inter and JetBrains Mono, DejaVu as the fallback, and Noto
  Color Emoji. Scripts outside those fonts (CJK, for example) are not covered
  and print as empty boxes.

Inter comes from its upstream release as static TrueType (`PDF_INTER_URL`,
checked against `PDF_INTER_SHA256`; the build fails on a mismatch), not from
Debian's `fonts-inter`. That package ships CFF outlines (`.otf`), which
Chromium can only embed in a PDF as Type 3 glyph drawings, and PDF readers
select and copy Type 3 text badly or not at all. An image built before this
change still has those fonts: rebuild it.

```bash
# Build, or rebuild to pick up a newer Mermaid
python3 ~/.local/bin/artifacts --build-pdf-image

# Remove it; the button then explains the rebuild
docker image rm artifacts-pdf:local
```

`--build-pdf-image` runs `docker build --pull`, needs network once, and exits
without starting the server.

**Keep the versions in step.** Playwright launches the browser build that
matches its own version, so the pip package and the headless shell are
installed together in one build step from `PDF_PLAYWRIGHT`. Change only that
constant to move to a newer Playwright.

### Upgrading from the Playwright-based image

Earlier versions built the image on Playwright's own base image, which carries
every browser and came to about 4 GB on disk. The `--pdf-base` option and
`ARTIFACTS_PDF_BASE` chose that base and are gone: `--pdf-base` now exits with
status 2 and says so, both in the program and in the installer, and a leftover
`ARTIFACTS_PDF_BASE` is ignored with a warning. To move over, rebuild the image
and remove the build cache the old one left behind:

```bash
python3 ~/.local/bin/artifacts --build-pdf-image
docker image prune
docker builder prune
```

If the image is missing, the button shows:

```text
PDF export: the PDF image artifacts-pdf:local is missing: run `artifacts --build-pdf-image`
```

Render errors are written to the service log; see
[Operations](operations.md#status-and-logs).

## Timings

Once the image is built, a render takes a few seconds, most of it starting the
container and Chromium; long notes with many Mermaid diagrams add little.
