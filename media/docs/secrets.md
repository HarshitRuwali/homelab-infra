# Secrets

The stack needs two secret files. They live on the host, outside this
repository, in `MEDIA_DIR/secrets` (default `/opt/media/secrets`). The
directory is mode `0700` and each file is mode `0600`, owned by `root:root`.
`install.sh` creates the directory with those permissions and never overwrites
or reads the contents of anything in it.

**Never commit these files, and never paste their contents into a chat, a
ticket, a log or a command line.**

| File | Used by | Holds |
|---|---|---|
| `plex-tunnel.env` | `plex-tunnel`, as an `env_file` | One line: `TUNNEL_TOKEN=<token>` |
| `wireguard_private_key` | `gluetun`, as a compose secret | The WireGuard private key, and nothing else |

## plex-tunnel.env

`plex-tunnel` runs `cloudflared` against an existing remote-managed Cloudflare
tunnel. The file holds the tunnel's token on one line:

```text
TUNNEL_TOKEN=<token of the Cloudflare tunnel>
```

The tunnel's ingress, set in the Cloudflare dashboard, must be
`http://localhost:32400`. The container uses the host network, so that address
is the host's Plex.

Only one `cloudflared` connector should serve the tunnel at a time. If the
token came from an older connector, stop that one when you start this one. See
[Migration](migration.md).

## wireguard_private_key

The file holds only the `PrivateKey` value from a ProtonVPN WireGuard
configuration, with no `PrivateKey =` prefix and no other lines.

To get it:

1. In the Proton account dashboard, go to Downloads, then WireGuard configuration.
2. Create a configuration with the **NAT-PMP (Port Forwarding)** option enabled.
3. Copy the `PrivateKey` value from the configuration.

Any server will do. gluetun picks a P2P server that supports port forwarding
itself, because the stack sets `PORT_FORWARD_ONLY=on`.

## Writing a secret file without exposing it

Typing a secret into `echo` or `printf` puts it in your shell history and in the
process list. Write the file from standard input instead, so the value is never
part of a command line and is not echoed to the terminal:

```bash
sudo install -d -m 0700 /opt/media/secrets
sudo sh -c 'umask 077; cat > /opt/media/secrets/wireguard_private_key'
```

Paste the key, then press Ctrl-D to end the input. The
`umask 077` makes the file `0600`. Do the same for the tunnel file, pasting the
single `TUNNEL_TOKEN=...` line:

```bash
sudo sh -c 'umask 077; cat > /opt/media/secrets/plex-tunnel.env'
```

Check ownership and mode without reading the contents:

```bash
sudo ls -l /opt/media/secrets
```

Both files should show `-rw-------` and `root root`. Then run
`./install.sh --apply` again so the services that were waiting for a secret
start. See [Setup](setup.md).

## Rotating a secret

Write the new value over the file the same way, then restart the service that
uses it: `docker compose -p media restart plex-tunnel` for the tunnel token, or
`docker compose -p media restart gluetun qbittorrent` for the WireGuard key
(qBittorrent shares gluetun's network namespace, so it restarts with it). Revoke the old tunnel token or WireGuard configuration in its
provider's dashboard afterwards.
