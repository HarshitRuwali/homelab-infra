# Security

The vault is written by agents. A note can carry arbitrary HTML and script, a
link to anywhere, or a symlink to a file you did not mean to publish. The
viewer is built so that opening such a note on your own device does not hand
the note anything useful.

## Threat model

| Threat | Control |
|---|---|
| Another machine on the LAN or another network reaches the port | [One bind address](#bind-address) |
| A web page in your browser talks to the server by DNS rebinding | [Host header check](#host-header-check) |
| Another user, or a server, on the same tailnet reads the vault | [Tailnet gate](#the-tailnet-gate) |
| A note's script reads the viewer's API or phones home | [Sandbox and CSP](#sandboxed-content) |
| A note's script drives the viewer | [Navigation by message](#navigation-by-message) |
| A request reaches a file outside the vault or a hidden file | [File access](#file-access) |
| Another web site makes your browser start PDF renders | [Cross-site requests](pdf-export.md#limits) |
| A note's HTML attacks the host through the PDF renderer | [PDF rendering](#pdf-rendering) |
| A base or its query result abuses the Obsidian CLI | [The Obsidian CLI](#the-obsidian-cli) |

## Bind address

The server listens on one address, `--bind`. Use the host's Tailscale address
(`tailscale ip -4`), so the service is not on the LAN and not on loopback. The
default when running `artifacts.py` by hand is `127.0.0.1`. The value must be
an IP address, not a name. The installer is stricter: it takes only a unicast
IPv4 address and rejects `0.0.0.0` (which would listen on every interface),
multicast addresses, out-of-range or leading-zero octets and hostnames.

## Host header check

Every request must carry a `Host` header of the form `name:port`, where the
name is the bind address or one of the `--allow-host` names and the port is
`--port`. Anything else gets `403 forbidden host`. This blocks DNS rebinding:
a hostile page cannot make your browser send requests to the server under a
name it controls. A loopback bind also accepts `127.0.0.1` and `localhost`.

If you browse by machine name, pass that name as `--allow-host`.

## The tailnet gate

With `--tailnet-user LOGIN`, each non-loopback client address is looked up with
`tailscale whois`. It is admitted only when the device carries **no tags** and
belongs to `LOGIN`. Tagged devices are servers rather than your own machines,
so they are refused, as is every other user on the tailnet. A refused client
gets `403 forbidden`.

Details worth knowing:

- Loopback is always admitted.
- The answer is cached per client address for 60 seconds.
- The gate fails closed: if the `tailscale` CLI is missing, errors or times out,
  the client is refused.
- Without `--tailnet-user` there is no login check, and the bind address plus
  your tailnet ACLs are the only access control. Prefer setting it.
- When you install from a tagged device with `--tailnet-user`, a `403` from the
  installer's own check is expected, not a failure. Without `--tailnet-user`, a
  `403` there is a failure (a `Host` header mismatch).

## Sandboxed content

Notes, HTML artifacts, SVGs and text files are served from `/view/` and
`/raw/` with a `Content-Security-Policy` that includes `sandbox`, and the
viewer shows them in a sandboxed `<iframe>`. The sandbox gives the content an
opaque origin, so it cannot read the viewer page, its cookies or its API.

| Directive | Effect |
|---|---|
| `sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox` | Scripts run (diagrams need them); no forms, no top navigation, no same-origin access. Links open in a new window that is not sandboxed. |
| `connect-src 'none'` | No `fetch`, XHR, WebSocket or beacon from the content. |
| `frame-src 'none'` | The content cannot embed frames. |
| `form-action 'none'`, `base-uri 'none'` | No form posts, no `<base>` rewriting. |
| `default-src 'none'` | Anything not listed is blocked. |
| `script-src`, `style-src`, `font-src` | Inline code, the viewer's own origin and the CDNs below. |
| `img-src` | The viewer's own origin, `data:` and `blob:`. |

The viewer page itself is served with `frame-ancestors 'none'` and
`X-Frame-Options: DENY`, so another site cannot frame it. The `/api/` routes
refuse a request whose `Sec-Fetch-Site` header is present and is not
`same-origin`. A request without that header passes, because the header is
sent by browsers and `curl` or another non-browser client does not send it, so
this check is a defence against other web sites in your browser, not an access
control. The `/pdf/` route accepts an absent header, `same-origin` and `none`
(a typed or bookmarked address); see [PDF export](pdf-export.md#limits).

### Browser dependencies

The viewer loads highlight.js and Mermaid (with the ELK layout) from cdnjs,
jsdelivr and unpkg, and fonts from Google Fonts. The **viewing device** needs
internet access for diagrams and code highlighting; without it, text and tables
still render. The server itself makes no outbound calls apart from
`tailscale whois` and, for PDFs, the local renderer. PDFs need no internet at all.

## The Obsidian CLI

Bases (`.base`) are evaluated by running the command given with `--obsidian-cli`
(for example a wrapper around a headless Obsidian). The viewer runs it only in
this shape, as an argument list with no shell:

```text
CMD base:query path=<vault-relative .base file> [view=<view name>] format=json
```

- The file is one the viewer resolved itself: a `.base` file inside the vault
  folders, hidden paths and symlinks that leave the vault refused (the same
  checks as every other file), and also inside `--obsidian-vault`. A base
  outside that folder is never passed to the command.
- `path=` and `view=` are each one argument, and the command is one program
  path, never split into words. A view name is passed only if it is a view
  named in the base's own YAML, so a `![[X.base#Name]]` embed cannot send an
  arbitrary string. Without PyYAML the views cannot be listed, and the name
  from the embed is passed as it is, so the CLI judges it. Names with control
  characters are refused.
- The command is never found on its own: with no `--obsidian-cli` (or
  `ARTIFACTS_OBSIDIAN_CLI`) nothing is run.
- No other subcommand or option is ever built from vault content, and the
  command inherits the service's environment but gets no input on stdin.
- A call runs in its own process group and is killed, children included, after
  15 s or once it has written more than 8 MiB (stdout is read with that cap, not
  collected first). At most 2 calls run at once, a request waits at most 10 s
  for a free slot, identical calls that overlap share one run, and results are
  cached for 5 s. One page queries at most 20 views per base and 20 base embeds
  and spends at most 30 s in all on the CLI; the rest is shown as skipped.
- **The output is untrusted.** The command's JSON, and its error text, come from
  an app that evaluates vault content. Every value is HTML-escaped before it
  reaches a page, a `path` becomes a link only if it names a file the viewer
  itself lists, and output that is not a JSON list of objects is shown as an
  error. A ` ```base ` code block in a note is never sent to the command.

The viewer trusts the command itself: point `--obsidian-cli` only at a program
you control. `none` turns the feature off, and then bases show their YAML only.

Canvases (`.canvas`) need no tool. They are rendered by the viewer, served with
the same sandbox and CSP as notes, and their text nodes use the notes'
Markdown pipeline, so they carry the same properties. A link node is a link only
for an `http` or `https` URL, with `rel="noopener noreferrer"`; colors must be a
preset number or a `#hex` value before they reach a style.

## PDF rendering

A PDF is made by printing the note in Chromium's headless shell, and a note can
carry raw HTML and script that runs there. The renderer is a subprocess of the
service on the same host, not a container. Its controls:

- **Chromium's own sandbox is on.** The renderer processes run in user and PID
  namespaces with a seccomp filter. `--no-sandbox` is never used. On Ubuntu
  23.10 and later this needs an AppArmor profile for the browser binary, which
  grants only `userns` and attaches to the exact runtime directory it was printed
  for, so a binary copied elsewhere does not get it; without it the render fails with a clear message
  instead of running unsandboxed. See
  [The AppArmor profile](pdf-export.md#the-apparmor-profile).
- **The network is blocked in layers, and each layer stops different things.**
  Fetch interception refuses every request except the page and the runtime's
  bundled libraries. It is enabled on every target Chromium creates (browser-level
  auto-attach pauses a new target, a `window.open` popup included, until it has
  the filter), but it only sees HTTP-level requests, not UDP. The launch flags
  resolve no name (`--host-resolver-rules=MAP * ~NOTFOUND`) and send all
  HTTP(S) and WebSocket traffic, loopback included, to a dead local proxy port,
  which also covers a target the filter missed; `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`
  is the only layer that stops WebRTC UDP. The page's policy carries
  `connect-src 'none'`, which binds the page itself, not a popup.
- **A temporary `HOME` per render.** The working directory, `HOME`, `TMPDIR`
  and Chromium's profile are one fresh directory, removed afterwards, and the
  environment holds only a few variables. Nothing of the service user's
  configuration reaches the render.
- **Bounded resources.** At most 2 renders run at a time, each is killed with
  its whole process group after 90 s, and the unit caps the service and its
  renders together (`MemoryMax=1536M`, `TasksMax=1024`). Each renderer sets its
  `oom_score_adj` to 1000 (Chromium inherits it), so within that shared limit
  the OOM killer picks the render before the server.
- **Pinned content.** The browser, libraries and fonts are pinned by SHA-256
  and checked before extraction, and archive members with absolute paths, `..`
  or links are refused.

What changed against the docker container this replaced: the renderer used to
run with no network namespace, a read-only root, no capabilities, no new
privileges and per-render memory and process limits, in a filesystem view that
held only the image. Now the browser process runs as the service user in the
host's filesystem, with no separate filesystem view, and the limits are shared
by the unit rather than per render. What keeps a hostile note contained is
Chromium's sandbox around the code that handles the note, plus the blocked
network. A sandbox escape would land as the service user, which can read the
vault and that user's files. The viewer needs no docker group membership any
more, which removes a route to root on the host.

## Navigation by message

A link inside a note cannot navigate the viewer directly, because the content
has an opaque origin. It posts a message to the viewer instead. The viewer
accepts messages only from its own iframe, and only opens a file whose id it
listed itself in `/api/tree`. A note cannot make the viewer open an arbitrary
URL or a file outside the vault. A diagram's full-screen view receives the SVG
as an image, which cannot run scripts.

A code block's **Copy** button copies inside the frame when the browser allows
it. The viewer is plain HTTP and the frame is sandboxed, so the clipboard API is
usually unavailable there; `execCommand("copy")` is tried next, and if the frame
may not copy either, it asks the viewer with a `vault-copy` message. The viewer
then puts that text, and only text of at most 5 MB, on the clipboard, during the
same click (the browser's user-activation rule), and replies whether it worked.
A note can therefore place text on the clipboard when you click in it, which it
could already do inside its own frame. If nothing works, the button selects the
code and asks for the copy shortcut.

## Opening the Obsidian app

The viewer page opens `obsidian://open` links in two cases only: when you
press its **Obsidian** button, and, on a device where you switched on
**Open links in Obsidian**, once for the file named in the URL the page was
opened with. That file must be one the page listed itself in `/api/tree`, so a
crafted link cannot name a path outside the vault or one that does not exist.
Messages from a note's frame only select files in the viewer; they never open
the app. The link carries the vault name and the file's path inside the vault,
nothing else. The setting and the vault name are kept in the browser's
`localStorage` and never reach the server.

## File access

- Only files under a vault root are served. Ids are resolved after following
  symlinks, so a symlink that points out of the vault returns `404`.
- Hidden files and folders (any path part starting with `.`) are never listed
  and never served, and `..` is rejected.
- Only a fixed list of file types is served: Markdown, HTML, SVG, common image
  formats, and plain-text sources such as `.txt`, `.json`, `.yaml`, `.py`, `.sh`
  and `.d2`, plus `.zip` archives. An archive is sent as
  `application/zip` with `Content-Disposition: attachment` and is never
  unpacked or rendered by the viewer.
- The server answers `GET` and `HEAD` only. Nothing writes to the vault.
- Responses stay `private`: the file list and vault files are sent with
  `Cache-Control: private, no-cache` and an `ETag` (files also `Last-Modified`),
  so a browser may store them but must revalidate before reuse and a shared
  cache must not. The Host, tailnet and `Sec-Fetch-Site` checks run before any
  `304` is considered. Notes, search and PDFs stay `no-store`.

## What it does not do

- **No TLS and no passwords.** The connection is plain HTTP over the tailnet,
  which WireGuard already encrypts. Access control is the tailnet plus the
  gate above.
- **No per-folder permissions.** Anyone admitted can read everything under the
  roots. Serve a folder that contains only what you want visible.
- **No secrets in the vault.** The viewer displays what it finds. Keep tokens,
  keys and `.env` contents out of any served root.
