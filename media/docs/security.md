# Security

The stack publishes as little as possible, and the one component that touches
the public internet (the torrent client) cannot reach it except through a VPN.

## Exposure model

| Component | Listens on | Reachable from | Why |
|---|---|---|---|
| Plex | Host network, `:32400` | The host's LAN | Plex's own sign-in protects it. Local clients find it on the LAN |
| Plex, remote | Nothing: `plex-tunnel` dials out to Cloudflare | The internet, through the tunnel | No router port is forwarded. The tunnel's ingress is `http://localhost:32400`, and Plex authentication still applies |
| qBittorrent WebUI | `127.0.0.1:8081`, published by gluetun | The host, and the tailnet through `tailscale serve` | Never the LAN, never the internet |
| BitTorrent port | The VPN's forwarded port, inside the tunnel | The internet, through ProtonVPN | The only inbound path for torrent peers |

`compose.yaml` publishes exactly one port, and it is bound to `127.0.0.1`.
`tests/compose-test.sh` fails if any service publishes a port on another
address.

### The WebUI on the tailnet

`install.sh` runs:

```bash
tailscale serve --bg --tcp 8081 tcp://127.0.0.1:8081
```

This makes the WebUI reachable from the tailnet on TCP 8081 without binding it
to any LAN address. It is a plain TCP forward, so the WebUI login still applies.
Pass `--no-tailscale-serve` to `install.sh` to skip it; if `tailscale` is
installed, the installer then also removes an existing serve for that port
(`tailscale serve --tcp 8081 off`).

### Why port 8081

Two ports are taken on this host. `8000` is gluetun's own control server inside
the shared network namespace, so qBittorrent cannot also listen on it. `8080`
is Nextcloud on the host. The port is set by `QBT_WEBUI_PORT`; the installer
refuses to continue if something other than this stack already holds it.

## Kill switch by construction

There is no separate kill-switch to configure or to fail.

- `qbittorrent` uses `network_mode: service:gluetun`. It has no network
  interface of its own, only gluetun's.
- gluetun's firewall drops egress that is not going through the VPN. If the
  tunnel drops, torrent traffic stops rather than falling back to the host's
  address.
- When the port-forward hook goes down, gluetun resets qBittorrent's listen
  port to `0` and its interface to `lo`.
- gluetun only connects to P2P servers that support port forwarding
  (`PORT_FORWARD_ONLY=on`).

[Operations](operations.md) shows how to confirm that the exit address differs
from the host's.

## WebUI authentication and `LocalHostAuth`

On a fresh install, `install.sh` seeds
`MEDIA_DIR/config/qbittorrent/qBittorrent/qBittorrent.conf` with
`WebUI\LocalHostAuth=false` (and `WebUI\Port`), which lets clients on
`127.0.0.1` call qBittorrent's API without logging in. gluetun's port-forward
hook needs this: it sets the listen port and interface by calling the API from
inside the shared namespace, without credentials. The installer only creates
the file when it does not exist and never modifies an existing one. A migrated
file is adapted by hand, see [Migration](migration.md#qbittorrent).

What "localhost" means here:

- Requests through the published `127.0.0.1` port, or through
  `tailscale serve`, arrive from the Docker bridge, not from `127.0.0.1`
  inside the namespace. They are not exempt and still need the WebUI login.
- gluetun's hook is not the only client on `127.0.0.1` inside the namespace.
  qBittorrent itself runs there, so its add-by-URL feature and its RSS
  downloader, any search plugin, and any external program it is configured to
  run all reach the API without a login.

The residual risk is a request forged from inside qBittorrent, for example a
malicious RSS feed or torrent URL that makes it fetch a URL on `127.0.0.1`.
That is a server-side request forgery limited to `GET` requests, and
qBittorrent's state-changing API endpoints require `POST`. Two settings keep it
that way:

- Leave search plugins off, which is the default. Do not install any.
- Do not set an autorun program (Options, Downloads, Run external program),
  since it would run inside the namespace with the same unauthenticated access.

Do not add another container to `network_mode: service:gluetun`, and do not
publish the port on anything but `127.0.0.1`, without revisiting this.

## Plex Remote Access

Plex runs on the host network. If Remote Access is enabled in Plex (in
`Preferences.xml` it shows as `PublishServerOnPlexOnlineKey="1"`), Plex tries to
map a public port on the router with UPnP or NAT-PMP. Remote access for this
stack is meant to go only through the tunnel, as a custom server access URL
(Settings, Network, Custom server access URLs) that points at the tunnel's
hostname. Check that nothing else is exposed:

1. In Plex, Settings, Remote Access, confirm that no public port is mapped (it
   should report that the server is not mapped).
2. Keep UPnP and NAT-PMP disabled on the router. For the cloud-services VM that
   is OPNsense.

Do not turn Remote Access off to get this result: whether that would hide the
custom server URL from clients has not been verified.

## Secrets

The tunnel token and the WireGuard key are root-only files outside the
repository. See [Secrets](secrets.md). The key is passed to gluetun as a
compose secret and a `_SECRETFILE` variable, so it is not an environment
variable in `docker inspect` output. `.env` holds no secrets.

## File ownership

qBittorrent and Plex run as `MEDIA_UID:MEDIA_GID` (default `100000:100000`),
the owner of the media share, so files written by the stack have the same owner
as the rest of the share and nothing runs as root against it. Plex mounts the
share read-only.
