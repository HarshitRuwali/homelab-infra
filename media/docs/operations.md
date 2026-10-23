# Operations

Commands below run on the host. `docker compose -p media` addresses the stack
from any directory; add `sudo` if your user cannot reach the Docker socket.
State and the installed compose file live in `MEDIA_DIR` (default `/opt/media`).

## Update an image

Every image is pinned to a specific tag in `compose.yaml`, never `latest`, so an
update is a reviewed change:

1. Edit the tag of the service in `compose.yaml` (for example the `plex` image).
2. Commit it, then on the host run `./install.sh` to preview and
   `./install.sh --apply` to apply. The installer copies the new
   `compose.yaml` to `MEDIA_DIR`, validates it, pulls the new image and
   recreates the services whose definition changed.
3. Check [the VPN](#check-the-vpn) and the logs.

`./install.sh --apply` does not touch `MEDIA_DIR/.env` or the secrets.

The fleet's nightly container updater does not update this stack. The Ansible
role `docker_updates` runs `docker compose up -d` on every project without a
service list, which would try to start gluetun without its secret, and every
image here is pinned, so `ansible/inventory/host_vars/cloud-services.yml` lists
`media` in `docker_update_skip_projects`. Updates happen only by bumping the pin
and re-running `./install.sh --apply`, as above.

## Check the VPN

The address gluetun reaches the internet from must differ from the host's:

```bash
docker exec gluetun wget -qO- https://ipv4.icanhazip.com     # through the VPN
curl -4s https://ipv4.icanhazip.com                          # the host's own address
```

If the two match, or the first prints nothing, torrent traffic is not leaving
through the tunnel. Because qBittorrent has no network except gluetun's, the
failure mode is "no connectivity", not a leak. Look at the gluetun logs.

## Find the forwarded port

gluetun asks ProtonVPN for a forwarded port over NAT-PMP and logs it. Its
port-forward hook then sets that port as qBittorrent's listen port and binds
qBittorrent to the tunnel interface through qBittorrent's API on `127.0.0.1`.

```bash
docker logs gluetun 2>&1 | grep -i 'port forward'
```

In the qBittorrent WebUI, Options, Connection, the listening port should show
the same number. If the line is missing, the key was created without NAT-PMP
(Port Forwarding) enabled, or gluetun has not reached a P2P server yet.

## Logs and status

```bash
docker compose -p media ps
docker compose -p media logs -f gluetun
docker compose -p media logs --tail 100 qbittorrent
docker compose -p media logs --tail 100 plex plex-tunnel
```

## Restart

```bash
docker compose -p media restart plex
docker compose -p media restart plex-tunnel
docker compose -p media restart gluetun qbittorrent   # always together
```

qBittorrent shares gluetun's network namespace, so restart them together. The
`restart: unless-stopped` policy brings the whole stack back after a reboot.

## Roll back

Set the tag in `compose.yaml` back to the previous value (`git revert` of the
update works) and run `./install.sh --apply` again. Images that are not pulled
yet are fetched, and the service is recreated on the old tag. Configuration is
not changed by an image update, but a newer Plex or qBittorrent can migrate its
own database on the first start, so take a copy of the state directories
listed below before a major update if you may need to go back.

## Where state lives

| Path under `MEDIA_DIR` | Contents |
|---|---|
| `config/plex` | Plex's `/config`: the library database, metadata, watch history and server identity, under `Library/Application Support/Plex Media Server` |
| `config/qbittorrent/qBittorrent` | qBittorrent's configuration and data together: `qBittorrent.conf`, `categories.json`, resume data in `BT_backup` |
| `transcode` | Plex's transcode scratch space |
| `secrets` | The two [secret files](secrets.md) |
| `compose.yaml`, `.env` | The installed copy of this stack and its settings |

Downloads and media live on the share at `MEDIA_ROOT`, not under `MEDIA_DIR`.
Back up `config/plex` and `config/qbittorrent` to keep the library and the
torrent list.
