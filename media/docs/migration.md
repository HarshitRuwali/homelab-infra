# Migration from LXC containers

This is the procedure used on 2026-10-22 to move this host's Plex and
qBittorrent from two Proxmox LXC containers into the stack. It is written to be
reusable. The goals are no library rescan, no loss of watch history, no torrent
recheck, and one live `cloudflared` connector at all times.

Do the work on the new host (`MEDIA_DIR` below, default `/opt/media`). Run
`./install.sh --apply` first so the directories exist. It starts Plex, so
stop it again (`docker compose -p media stop plex`) before you copy state in.
`plex-tunnel`, `gluetun` and `qbittorrent` do not start until their secrets
exist (see [Setup](setup.md)), so create those last.

## Plex

1. **Stop Plex in the LXC**, so the database is not written during the copy:
   `systemctl stop plexmediaserver`.
2. **Archive the server directory**, leaving out the transcoder's scratch directory:

    ```bash
    tar -C /var/lib/plexmediaserver/Library \
        --exclude='Application Support/Plex Media Server/Cache/Transcode' \
        -czf /tmp/plex.tar.gz 'Application Support/Plex Media Server'
    ```

    Copy the archive to the new host by whatever channel you trust.
3. **Unpack it into the stack's config**, so the path inside the container is
   `/config/Library/Application Support/Plex Media Server`:

    ```bash
    sudo install -d /opt/media/config/plex/Library
    sudo tar -C /opt/media/config/plex/Library -xzf /tmp/plex.tar.gz
    ```

4. **Fix the owner** to the share's owner (`MEDIA_UID:MEDIA_GID`):
   `sudo chown -R 100000:100000 /opt/media/config/plex`.
5. **Keep the library paths identical.** The stack mounts the share at
   `/data/share` inside the container. If the old Plex saw the same media
   under `/data/share`, nothing needs to be rescanned: the library root paths,
   the watch history and the server's `machineIdentifier` all carry over, so
   clients see the same server.
6. **Move the tunnel token** into `MEDIA_DIR/secrets/plex-tunnel.env` as
   `TUNNEL_TOKEN=...` (see [Secrets](secrets.md)).
7. **Start the stack** with `./install.sh --apply`, and check that Plex answers
   and the libraries are intact.
   The copied `Preferences.xml` may have Remote Access enabled
   (`PublishServerOnPlexOnlineKey="1"`), which makes Plex try UPnP or NAT-PMP
   port mapping on the router. Remote access here is meant to go only through
   the tunnel's custom server URL (Settings, Network, Custom server access
   URLs). In Settings, Remote Access, confirm that no public port is mapped, and
   keep UPnP and NAT-PMP disabled on the router (OPNsense for the cloud-services
   VM). Do not turn Remote Access off for this: whether that hides the custom
   URL from clients is unverified. See [Security](security.md#plex-remote-access).
8. **Stop the old cloudflared connector** in the LXC (stop whichever
   service or container runs it), so only one connector serves the tunnel. Check the tunnel in the
   Cloudflare dashboard shows the new connector only.

## qBittorrent

qBittorrent 5 in the linuxserver image keeps its configuration and its data
together in `/config/qBittorrent/`, because `XDG_CONFIG_HOME` and
`XDG_DATA_HOME` are both `/config`. On the old LXC they were two directories, so
merge both into the one:

| From (old LXC, in the qBittorrent user's home) | What | Skip |
|---|---|---|
| `.local/share/qBittorrent/` | `BT_backup` (the resume data for every torrent) | logs |
| `.config/qBittorrent/` | `qBittorrent.conf`, `categories.json`, `watched_folders.json`, `rss` | `ipc-socket`, `lockfile` |

1. Stop qBittorrent in the LXC.
2. Copy both directories into `MEDIA_DIR/config/qbittorrent/qBittorrent/`,
   leaving out the items in the right-hand column, then
   `sudo chown -R 100000:100000 /opt/media/config/qbittorrent`.
3. Edit `MEDIA_DIR/config/qbittorrent/qBittorrent/qBittorrent.conf`
   (with qBittorrent stopped, or the file is overwritten on exit):

    | Key | Value | Why |
    |---|---|---|
    | `WebUI\Port` | `8081` | Matches `QBT_WEBUI_PORT`; 8000 is gluetun's control server and 8080 is Nextcloud |
    | `WebUI\LocalHostAuth` | `false` | gluetun's port-forward hook calls the API without a login. The installer sets this only in a file it creates itself, never in one you copied in. See [Security](security.md#webui-authentication-and-localhostauth) |
    | `Session\Interface`, `Session\InterfaceName` | cleared | gluetun sets the interface to the tunnel when it forwards the port |

4. **Keep the save paths identical.** The stack mounts `MEDIA_ROOT/Torrent` at
   `/data/share/Torrent`. If the old torrents saved to `/data/share/Torrent`,
   they resume without a recheck.
5. Create `MEDIA_DIR/secrets/wireguard_private_key`, run `./install.sh --apply`,
   and check [the VPN and the forwarded port](operations.md#check-the-vpn).

## Retire the LXC containers

1. On the Proxmox host, set each container to not start at boot (`onboot 0`),
   and leave it stopped, not deleted, for two weeks as a way back.
2. **Remove the host from the Ansible inventory before stopping it for good.**
   The host-down alert rule is rendered from the inventory by
   `ansible/roles/grafana_alerting/templates/rules-availability.yaml.j2`, so a
   stopped host that is still in the inventory pages as down. After removing it,
   re-run `ansible/playbooks/central-alerting.yml` to regenerate the rule.
3. After two weeks with no need to go back, delete the containers.
