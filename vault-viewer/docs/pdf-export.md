# PDF export

Notes and text files have a **PDF** button in the viewer's top bar (key `p`).
It downloads a light, print-styled A4 PDF named after the file, ready to share.
HTML artifacts, SVGs and images have no PDF button.

## Prerequisites

- The PDF runtime, installed once per user: Chromium's headless shell, the page
  libraries and the fonts, about 180 MB to download and about 235 MB on disk.
  Pass `--install-pdf-runtime` to the installer, or install it later as
  described under [The runtime](#the-runtime). No docker and no system packages
  are needed.
- On Ubuntu 23.10 and later, an AppArmor profile that lets Chromium's sandbox
  run. The installer writes it with `sudo`; see
  [The AppArmor profile](#the-apparmor-profile).

## What the PDF contains

- The body: headings, tables, callouts and code. Code is syntax-highlighted and
  long lines wrap instead of being clipped.
- Embedded images and Mermaid diagrams. Diagrams use a light theme and shrink
  to the page width but stay vector, so they zoom cleanly.
- External links, which stay clickable.
- Code as selectable text, so it copies from any PDF reader. There is no copy
  button: PDF readers do not run page scripts.
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

`GET /pdf/<id>` renders the note as a print page and pipes it to a short-lived
subprocess on the host. The subprocess (`PDF_RENDER` in `artifacts.py`, plain
Python with the standard library) starts Chromium's headless shell from the
runtime with `--remote-debugging-pipe` and speaks the DevTools protocol to it
over that pipe. The PDF comes back on stdout and the process tree is gone
afterwards.

Each render gets:

| Control | Effect |
|---|---|
| Chromium's own sandbox, on | The renderer processes run in user and PID namespaces with a seccomp filter. `--no-sandbox` is never used |
| A fresh temporary directory as working directory, `HOME`, `TMPDIR` and Chromium profile | Nothing of the user's reaches the render; the directory is removed afterwards |
| A minimal environment | Only `PATH`, `HOME`, `TMPDIR`, `LANG`, `FONTCONFIG_FILE`, and the paths of the browser and libraries |
| `--host-resolver-rules=MAP * ~NOTFOUND` | No name resolves |
| `--proxy-server=http://127.0.0.1:9`, `--proxy-bypass-list=<-loopback>` | Every HTTP(S) and WebSocket connection, loopback included, goes to a dead local port |
| `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` | No WebRTC UDP (STUN, TURN, peer connections). Fetch interception cannot see UDP, so only this switch stops it |
| Fetch interception, on every target | Every request other than the page and the runtime's libraries is refused. Browser-level auto-attach pauses each new target (the page, frames, workers and `window.open` popups) until it has the filter, so a popup is filtered like the page |
| `connect-src 'none'` in the page policy | No `fetch`, XHR or WebSocket from the page. It binds the page only, not a popup |
| `oom_score_adj` 1000 | The renderer and Chromium share the service's memory cgroup, and this makes them the OOM killer's first choice over the server |

Mermaid, highlight.js and the Inter and JetBrains Mono fonts come from the
runtime, and images are inlined as `data:` URLs, so nothing is fetched while
printing. Inside Chromium the page is served from a made-up origin. The
[Security](security.md#pdf-rendering) page says what these controls stop and
what they do not.

Notes are untrusted. Raw HTML in a note does run inside that Chromium, and is
contained by the sandbox, the blocked network, the page policy and the request
filter. The layers overlap: the test suite checks that an `<img>`, `<link>`,
`<script>`, `fetch()`, a WebSocket, a `window.open` popup and a WebRTC peer
connection with STUN and TURN servers all cause no connection to a local TCP
listener or UDP port, and that with the request filter on the page only and the
network switches removed the popup does reach the listener, and that without the
WebRTC switch UDP gets out.

## Limits

| Limit | Value |
|---|---|
| Concurrent renders | 2. A third waits up to 60 s for a slot, then the request fails with "busy rendering other PDFs, try again" |
| Render time | 90 s, after which the renderer and its whole process group are killed |
| Memory and processes | The unit caps the server and its renders together (`MemoryMax=1536M`, `TasksMax=1024`) |
| Inlined images | 40 MB per PDF |
| Supported files | Notes and text files only; anything else answers `404` |
| `HEAD` requests | `405`, so a link preview never starts a render |
| Cross-site requests | `403` when `Sec-Fetch-Site` is present and is not `same-origin` or `none`, because every request starts a render. A request without the header is accepted |

The usual viewer checks (tailnet gate and `Host` header) apply before any of
this. A failed render answers the button with a short message and a `503`
instead of dropping the connection.

## The runtime

The runtime lives in a per-user directory, by default
`${XDG_DATA_HOME:-~/.local/share}/artifacts/pdf-runtime` (change it with
`--pdf-runtime DIR`). The program only uses a runtime built for it, in a
subdirectory named after its revision:

```text
pdf-runtime/<PDF_REV>/chrome/     Chromium's headless shell, trimmed
pdf-runtime/<PDF_REV>/lib/        Mermaid, ELK layout, highlight.js
pdf-runtime/<PDF_REV>/fonts/      Inter, JetBrains Mono, DejaVu, Noto Color Emoji
pdf-runtime/<PDF_REV>/fonts.conf  fontconfig template, paths relative to the file
pdf-runtime/<PDF_REV>/REV         written last; its presence means "complete"
pdf-runtime/.lock                 held while an installer works
```

`PDF_REV` is the first 12 hex digits of a SHA-256 over the pinned downloads,
the trimming rules and the font configuration template, so it changes only
when one of those does. It does not cover the render script: that script runs
from the program, so changing it never forces a new download.
`python3 artifacts.py --pdf-rev` prints it.

### What it contains

Every download is pinned to a version and a SHA-256 in `PDF_PINS`, and checked
before anything is extracted; a mismatch stops the install.

| Part | Pin |
|---|---|
| Browser | Chromium headless shell 153.0.8010.12, from Chrome for Testing. Only `en-US.pak` is kept of the locale packs, and the software Vulkan files are dropped |
| Libraries | Mermaid 11.17.2, `@mermaid-js/layout-elk` 0.2.3 and `@highlightjs/cdn-assets` 11.9.0, cut down to the files the print page loads (the minified ESM builds and their chunks, `highlight.min.js` and the GitHub style), without source maps |
| Fonts | Inter 4.1 and JetBrains Mono 2.242 (regular, italic, semibold and bold, as static TrueType), DejaVu 2.37 as the fallback, and Noto Color Emoji 2.042 |

There is no full Chromium, Firefox or WebKit and no driver: the render script
speaks the DevTools protocol itself. Scripts outside the bundled fonts (CJK,
for example) are not covered and print as empty boxes.

### Fonts

`fonts.conf` lists the runtime's `fonts` directory and its cache by paths
relative to the file (`prefix="relative"`), so a runtime copied to another path
(as `deploy.sh` does) still finds its fonts. It also includes the host's
`/etc/fonts/conf.d`, so the host's font configuration (alias and substitution
rules) is part of the render, and a host that remaps families could change the
output. Output of one note was compared byte for byte, apart from the
creation date, modification date and file ID, between Ubuntu 26.04 and the
Debian 12 image that the docker renderer used, and was identical. The render
runs with a throwaway `HOME`, so a user's own fonts are not read, and the file
itself lists no font directory but the runtime's; host rules that add another
would change the output. `--check-pdf-runtime` fails unless the PDF it
renders embeds Inter, so a runtime whose fonts were not found does not pass.

Inter and JetBrains Mono are the static TrueType releases, not the CFF `.otf`
builds that distributions package. Chromium can only embed CFF outlines in a
PDF as Type 3 glyph drawings, and PDF readers select and copy Type 3 text badly
or not at all.

Sizes, measured on 2026-11-18: about 180 MB to download once, about 235 MB
installed (headless shell 209 MB, libraries 9 MB, fonts 17 MB).

### Install, check and update

```bash
# Install (or update): downloads once, verifies, installs atomically
python3 ~/.local/bin/artifacts --install-pdf-runtime

# Render a tiny page with it: prints ok, or the reason it failed
python3 ~/.local/bin/artifacts --check-pdf-runtime
```

The installer script does the first step for you with `--install-pdf-runtime`.
The install builds in a temporary directory beside the final one, writes `REV`
last and renames it into place, so an interrupted install leaves no partial
runtime. Running it again when the revision is already installed prints
`already installed` and changes nothing. Installers of one directory take turns
through a lock on `.lock` in it: a second one prints that it is waiting, then
finds the runtime installed. On success it removes every other revision and
every temporary directory (with the lock held none belongs to a live
installer), which is the cleanup of old versions. It prints the installed size
and the AppArmor state: whether the restriction is on and whether the profile
file in `/etc/apparmor.d` exists and matches this program's profile for this
runtime directory. Whether the kernel has loaded it needs root to read, so it
is not reported.

`--pdf-runtime` may be relative; it is resolved to an absolute path once, from
the current directory.

**Updating.** A program update needs a new runtime only when it changes a pin,
the trimming rules or the font configuration template; most updates do not.
Run `--install-pdf-runtime` after updating: when the revision is unchanged it
prints `already installed`, and when it changed the new directory is built and
the old one is removed once the new one is complete.
[`deploy.sh`](operations.md#deploy-to-other-hosts) does both for other hosts.
To move to a newer Chromium or library, change the matching entry in `PDF_PINS`
(URL and SHA-256), which changes `PDF_REV`, then install again. The DevTools
calls the render script makes (`Target`, `Fetch`, `Page.navigate`,
`Runtime.evaluate`, `Page.printToPDF`) are stable parts of the protocol.

**Removing it.** Delete the directory. The button then says the runtime is not
installed.

```bash
rm -rf ~/.local/share/artifacts/pdf-runtime
```

If the runtime is missing, the button shows:

```text
PDF export: the PDF runtime is not installed: run `artifacts --install-pdf-runtime`
```

### The AppArmor profile

Chromium's sandbox needs unprivileged user namespaces. Ubuntu 23.10 and later
set `kernel.apparmor_restrict_unprivileged_userns=1`, which blocks them for any
program without an AppArmor profile that grants `userns`. Without the profile
Chromium stops with "No usable sandbox!". The viewer never falls back to
`--no-sandbox`; it reports the failure instead:

```text
PDF export: Chromium's sandbox is not usable on this host: install the AppArmor profile (see `artifacts --print-apparmor-profile`)
```

The profile is printed by `artifacts --print-apparmor-profile`. It is named
`artifacts-chrome-headless-shell`, applies to
`<runtime directory>/*/chrome/chrome-headless-shell` (the exact, absolute runtime
directory, with the revision as the only wildcard, so a binary copied to
another place does not get `userns`), is `unconfined` apart from granting
`userns`, and includes an optional local override file.
`--print-apparmor-profile` takes `--pdf-runtime DIR` and prints the profile for
that directory; it refuses, with status 2, a directory containing a space, a
quote, a backslash, a comma, a control character or any of `*?[]{}^`, which
mean something in an AppArmor path pattern. The installer writes it to `/etc/apparmor.d/artifacts-chrome-headless-shell` with
`sudo` and loads it; pass `--no-apparmor` to skip that. To do it by hand:

```bash
python3 ~/.local/bin/artifacts --print-apparmor-profile | sudo tee /etc/apparmor.d/artifacts-chrome-headless-shell >/dev/null
sudo apparmor_parser -r /etc/apparmor.d/artifacts-chrome-headless-shell
```

For a runtime outside the default directory, add `--pdf-runtime DIR` to the first
command.

The profile matches by path, so it covers only the runtime directory it was
printed for. After moving the runtime or changing `--pdf-runtime`, print and load
it again (add `--pdf-runtime DIR` to the first command when you use a custom
directory).

### Migrating from the docker image

Earlier versions rendered inside a throwaway docker container from the image
`artifacts-pdf:local` (581 MB), and before that from a Playwright image of about
4 GB. Both are gone. The options `--build-pdf-image`, `--pdf-image` and
`--pdf-base` now exit with status 2 and name `--install-pdf-runtime`, in the
program and in the installer, and `deploy.sh` does the same for `--image` and
`--pdf build`. The old `ARTIFACTS_PDF_BASE` and `ARTIFACTS_PDF_IMAGE` variables have no
effect. Install the runtime, then remove the old image if it is still there:

```bash
python3 ~/.local/bin/artifacts --install-pdf-runtime
docker image rm artifacts-pdf:local     # optional cleanup, only if it exists
docker builder prune                    # optional: the build cache the image left
```

Render errors are written to the service log; see
[Operations](operations.md#status-and-logs).

## Timings

Measured on 2026-11-18, a render of a 3-page note takes about 0.55 s, against
about 1.35 s in the docker container it replaced; the difference is the
container start. Long notes with many Mermaid diagrams add little.
