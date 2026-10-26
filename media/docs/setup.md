# Setup

This is the canonical procedure for installing the stack on a host. Run every
command on that host. The installer is a dry run unless you pass `--apply`.

## Prerequisites

| Needed | Check |
|---|---|
| Docker Engine with the Compose plugin | `docker compose version` |
| The media share mounted at `MEDIA_ROOT` (default `/mnt/storage`), with a `Torrent` directory in it | `findmnt /mnt/storage` and `ls /mnt/storage/Torrent` |
| `/dev/net/tun`, which gluetun needs for WireGuard | `ls -l /dev/net/tun` |
| root, or a user with `sudo` | The installer runs its privileged steps through `sudo` unless it is already root |
| `tailscale`, logged in (optional) | `tailscale status`. Without it, or with `--no-tailscale-serve`, the qBittorrent WebUI stays on `127.0.0.1` only |

The media share must be owned by `MEDIA_UID:MEDIA_GID` (default
`100000:100000`), so that downloads get the same owner as the rest of it.

The installer checks all of this before it changes anything and stops with a
message if a requirement is missing. It also stops if the WebUI port
(`QBT_WEBUI_PORT`, default `8081`) is already taken by something other than
this stack.

## 1. Review the settings

Settings live in `.env.example`, which the installer copies to
`MEDIA_DIR/.env` on the first run and never overwrites afterwards.

| Key | Default | Meaning |
|---|---|---|
| `TZ` | `Asia/Kolkata` | Time zone for the containers |
| `MEDIA_DIR` | `/opt/media` | Where the stack lives on the host: `compose.yaml`, `.env`, config, secrets. Install-time only, see below |
| `MEDIA_ROOT` | `/mnt/storage` | The media share on the host |
| `MEDIA_UID`, `MEDIA_GID` | `100000` | Owner of the files on the share |
| `QBT_WEBUI_PORT` | `8081` | qBittorrent WebUI port, on `127.0.0.1` and the tailnet only |

To change a value before the first install, edit `.env.example`. After the
first install, edit `MEDIA_DIR/.env` and run the installer again.

`MEDIA_DIR` is the exception. It is chosen at install time, from the
environment or `.env.example`:

```bash
MEDIA_DIR=/srv/media ./install.sh --apply
```

Do not edit the `MEDIA_DIR=` line in the installed `.env` afterwards. The
installer reads `MEDIA_DIR` from the environment or `.env.example`, while
`docker compose` reads it from the installed `.env`, so the two would point at
different directories. The installer stops with a message if the line in an
existing `.env` differs from the `MEDIA_DIR` in use. To move the stack, install
it again with the new `MEDIA_DIR` and move the state by hand.

## 2. Create the secret files

The VPN and the tunnel each need a secret that is never part of this
repository. [Secrets](secrets.md) explains what goes in each file and how to
write it without it reaching your shell history. In short, you need:

- `/opt/media/secrets/plex-tunnel.env`, for `plex-tunnel`
- `/opt/media/secrets/wireguard_private_key`, for `gluetun`

You can install first and add a secret later. The installer starts only the
services whose secret is in place: Plex always, `plex-tunnel` once its file
exists, `gluetun` and `qbittorrent` once the key exists.

## 3. Preview, then install

```bash
cd media
./install.sh                    # dry run: every action is printed, nothing changes
./install.sh --apply            # create the directories, copy the files, pull and start
```

`./install.sh --help` lists the options. With `--apply` the installer:

1. runs the preflight checks above;
2. validates `compose.yaml` against a scratch directory holding empty stand-in secrets, before anything is installed, so a broken file never replaces the installed copy;
3. creates `MEDIA_DIR`, `MEDIA_DIR/secrets` (mode 0700, root) and the `config/plex`, `config/qbittorrent` and `transcode` directories (owned by `MEDIA_UID:MEDIA_GID`);
4. copies `compose.yaml` to `MEDIA_DIR` and creates `MEDIA_DIR/.env` if it is not there yet;
5. seeds `MEDIA_DIR/config/qbittorrent/qBittorrent/qBittorrent.conf` if it does not exist (see below);
6. pulls the images and starts the services whose secrets are in place;
7. publishes the qBittorrent WebUI on the tailnet with `tailscale serve`, if the VPN key exists and `tailscale` is installed. With `--no-tailscale-serve` it skips that and, if `tailscale` is installed, removes an existing serve for the WebUI port (`tailscale serve --tcp 8081 off`);
8. verifies the result.

### The qBittorrent configuration file

gluetun's port-forward hook sets qBittorrent's listen port by calling its API
on `127.0.0.1` without a login, which needs `WebUI\LocalHostAuth=false`. On a
fresh install the installer therefore creates
`MEDIA_DIR/config/qbittorrent/qBittorrent/qBittorrent.conf` (owner
`MEDIA_UID:MEDIA_GID`, mode 0640) with exactly:

```ini
[Preferences]
WebUI\Port=8081
WebUI\LocalHostAuth=false
```

`WebUI\Port` is the value of `QBT_WEBUI_PORT`. The installer never modifies an
existing `qBittorrent.conf`. A file migrated from another host is adapted by
hand, see [Migration](migration.md#qbittorrent). In a dry run the step is
printed like the others. The reasoning is in
[Security](security.md#webui-authentication-and-localhostauth).

Re-run `./install.sh --apply` after any change to `compose.yaml` or after adding
a secret. It is idempotent.

## 4. Verify

The installer's last step checks these, and fails with a message when one does
not hold:

- Plex answers on `http://127.0.0.1:32400/identity`.
- When the VPN key exists: gluetun becomes healthy, its public IPv4 address differs from the host's (the installer stops if it cannot determine the host's address), and the qBittorrent WebUI answers `200` on `127.0.0.1:8081`. Plex and the WebUI are polled for up to about two minutes, since both can take a while to start.

To check by hand:

```bash
docker compose -p media ps                      # four services running, gluetun healthy
docker exec gluetun wget -qO- https://ipv4.icanhazip.com   # the VPN exit address
curl -4s https://ipv4.icanhazip.com                        # the host's address; must differ from the line above
```

Open the qBittorrent WebUI at `http://127.0.0.1:8081` on the host, or over the
tailnet at the host's tailnet name on port 8081.

If you are moving an existing Plex library and torrents in, do that before the
first start. See [Migration](migration.md).
