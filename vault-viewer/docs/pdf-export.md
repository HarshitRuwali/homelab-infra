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
container that runs headless Chromium (Playwright). The PDF comes back on
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
`artifacts.py` (`PDF_DOCKERFILE`): the Playwright Python image, the matching
`playwright` pip package, Mermaid 11 with the ELK layout, highlight.js, and the
two fonts. It is large; expect several GB on disk after the build.

```bash
# Build, or rebuild to pick up a newer Mermaid
python3 ~/.local/bin/artifacts --build-pdf-image

# Remove it; the button then explains the rebuild
docker image rm artifacts-pdf:local
```

`--build-pdf-image` runs `docker build --pull`, needs network once, and exits
without starting the server.

**Keep the versions in step.** The `playwright` pip version in
`PDF_DOCKERFILE` must equal the tag of the Playwright base image. Change both
together: Playwright launches the browser build that matches its own version,
so a mismatch breaks rendering.

If the image is missing, the button shows:

```text
PDF export: the PDF image artifacts-pdf:local is missing: run `artifacts --build-pdf-image`
```

Render errors are written to the service log; see
[Operations](operations.md#status-and-logs).

## Timings

Once the image is built, a render takes a few seconds, most of it starting the
container and Chromium; long notes with many Mermaid diagrams add little.
