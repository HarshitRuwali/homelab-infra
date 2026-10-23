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
`tailscale whois` and, for PDFs, `docker`. PDFs need no internet at all.

## Navigation by message

A link inside a note cannot navigate the viewer directly, because the content
has an opaque origin. It posts a message to the viewer instead. The viewer
accepts messages only from its own iframe, and only opens a file whose id it
listed itself in `/api/tree`. A note cannot make the viewer open an arbitrary
URL or a file outside the vault. A diagram's full-screen view receives the SVG
as an image, which cannot run scripts.

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

## What it does not do

- **No TLS and no passwords.** The connection is plain HTTP over the tailnet,
  which WireGuard already encrypts. Access control is the tailnet plus the
  gate above.
- **No per-folder permissions.** Anyone admitted can read everything under the
  roots. Serve a folder that contains only what you want visible.
- **No secrets in the vault.** The viewer displays what it finds. Keep tokens,
  keys and `.env` contents out of any served root.
