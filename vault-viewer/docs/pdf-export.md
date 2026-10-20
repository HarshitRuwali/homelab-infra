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

**Keep the versions in step.** The `playwright` pip package must match the
Playwright base image, because Playwright launches the browser build that
matches its own version. You only change the tag of the `PDF_BASE` default in
`artifacts.py`: the pip version is derived from the base image tag at build
time (the `BASE` and `PLAYWRIGHT` build arguments of `PDF_DOCKERFILE`), so the
two cannot drift.

### Building on an existing Playwright image

The default base is large. A host that already has some Playwright image
locally, such as the Node flavour (`mcr.microsoft.com/playwright:v1.56.0-noble`),
can build on it instead and add only the fonts, the libraries and the pip
package. That saves the download of a second base image.

```bash
python3 ~/.local/bin/artifacts --build-pdf-image \
    --pdf-base mcr.microsoft.com/playwright:v1.56.0-noble
```

The installer takes the same option (`install.sh ... --build-pdf-image
--pdf-base IMAGE`), and `ARTIFACTS_PDF_BASE` sets it for the program.

- Either flavour works. The Node image has no pip, so the build installs
  `python3-pip` (already present, so a no-op, on the Python image).
- The Playwright version comes from the image tag, which must contain
  `vX.Y.Z` followed by `-` or the end of the tag (`v1.56.0-noble`, `v1.56.0`).
  A tag such as `latest` or `v1.56-noble`, or a digest alone, is refused by
  the program with exit status 2 and nothing is built (the installer then
  reports that the image build failed and installs nothing).
- With a custom base, docker is not asked to `--pull`: the point is to reuse
  the local image, and a re-pull could download several GB. If the image is
  not local, docker still fetches it once, as any build does.
- It does not always save disk during the build. With docker's containerd
  image store, BuildKit can unpack the base image again for the build, which
  needs free space about the size of the base image. If the build stops with
  `no space left on device`, free that much space first.

If the image is missing, the button shows:

```text
PDF export: the PDF image artifacts-pdf:local is missing: run `artifacts --build-pdf-image`
```

Render errors are written to the service log; see
[Operations](operations.md#status-and-logs).

## Timings

Once the image is built, a render takes a few seconds, most of it starting the
container and Chromium; long notes with many Mermaid diagrams add little.
