# Operations

Commands below run on the host. `docker compose -p media` addresses the stack
from any directory; add `sudo` if your user cannot reach the Docker socket.
State and the installed compose file live in `MEDIA_DIR` (default `/opt/media`).

## Update the images

Every image in `compose.yaml` follows `:latest`. The fleet's nightly container
updater (the Ansible role `docker_updates`, `fleet-docker-update.timer` on
cloud-services) pulls them and runs `docker compose up -d` on the project, which
recreates every service whose image changed. Nothing here needs a commit for a
new release.

To update at once instead of waiting for the timer:

```bash
docker compose -p media pull
docker compose -p media up -d
```

Then check [the VPN](#check-the-vpn) and the logs. `./install.sh --apply` pulls
and recreates the same way, and is still the way to apply a change to
`compose.yaml` itself. It does not touch `MEDIA_DIR/.env` or the secrets.

The updater starts every service of the project, so both [secret
files](secrets.md) must stay in place on the host: without the WireGuard key
gluetun cannot start. To take a secret away for a while, first add `media` to
`docker_update_skip_projects` in `ansible/inventory/host_vars/cloud-services.yml`
and re-run `playbooks/docker-updates.yml`.

A new Plex or qBittorrent can migrate its own database on the first start, so
[rolling back](#roll-back) to an older image may need the copy of the
state directories described there.

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

## Resource limits, logs and health

Every service has a memory cap and rotated logs, set in `compose.yaml`. The host
has no swap and shares its RAM with other stacks, so the caps keep one runaway
container from taking everything. The defaults come from 7-day peaks with
headroom (see the table in [Setup](setup.md#1-review-the-settings)); raise one in
`MEDIA_DIR/.env` and run `./install.sh --apply` if a service grows past it. A
container that exceeds its cap is killed and restarted by
`restart: unless-stopped`; `docker inspect -f '{{.State.OOMKilled}}' <name>`
tells you if that was the reason.

Logs use the `json-file` driver capped at 3 files of 10 MB per container.

`qbittorrent` and `plex-tunnel` have healthchecks, as `plex` and `gluetun` do:
qBittorrent's login page answers on its WebUI port, and `cloudflared tunnel
ready` reports a live connection to Cloudflare. `docker compose -p media ps`
shows the state. Docker does not restart an unhealthy container; the state is
there for you and for monitoring. The tunnel's metrics endpoint listens on
`127.0.0.1` only (`PLEX_TUNNEL_METRICS_PORT`).

The tunnel runs on the host network and binds that port explicitly, so cloudflared
cannot fall back to another one. If something else on the VM already listens on
it (default `20241`), `plex-tunnel` fails to start and restart-loops, and remote
Plex is down. Check with `ss -ltn | grep 20241`. To change it, set
`PLEX_TUNNEL_METRICS_PORT` to a free port in `.env` and run
`docker compose -p media up -d plex-tunnel`.

### Plex: transcoding and nightly maintenance

The VM has no `/dev/dri` (no GPU passed through), so Plex has no hardware
transcoding. Any transcode runs in software on the VM's vCPUs, and `compose.yaml`
adds no `devices:` for it. Plex's own nightly maintenance (Butler: library scan,
media analysis, preview thumbnails) runs from 02:00 to 05:00 in `TZ`
(Asia/Kolkata) and uses about one core for roughly an hour. That load is
expected. It is not a transcode, and it is not a fault.

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

A bad release reaches the host with the next nightly run. To go back, set that
service's image in `compose.yaml` to the last good version tag (the one
`docker image ls` shows for the previous image, or the registry's tag list),
add `media` to `docker_update_skip_projects` on cloud-services so the next
nightly run leaves it alone, and run `./install.sh --apply`. Return to `:latest`
and drop the skip once a fixed release is out. Configuration is not changed by
an image update, but a newer Plex or qBittorrent can migrate its own database on
the first start, so going back may also need a copy of the state directories
listed below.

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
