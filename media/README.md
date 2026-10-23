# media

Plex and qBittorrent as one Docker Compose stack on a single host. Plex is
reachable remotely through a Cloudflare tunnel, with no router port open.
qBittorrent shares a network namespace with gluetun, a ProtonVPN WireGuard
client with NAT-PMP port forwarding, so every torrent packet leaves through the
VPN or does not leave at all. The qBittorrent WebUI is published on
`127.0.0.1` and, optionally, the tailnet.

**Documentation:** <https://harshitruwali.github.io/homelab-infra/media/>

## Quick start

Run on the host that will run the stack, as root or as a user with sudo.

Needs the Docker Compose plugin, the media share mounted at `MEDIA_ROOT`
(default `/mnt/storage`, with a `Torrent` directory in it), `/dev/net/tun`, and
optionally the `tailscale` CLI. The two secret files are described in
[Secrets](docs/secrets.md); the stack starts without them, and each service
that needs one waits for it.

```bash
cd media
./install.sh                                # dry run: prints every action, changes nothing
./install.sh --apply                        # copies the stack to /opt/media and starts it
```

The installer creates `/opt/media/.env` from `.env.example` on the first run
and never overwrites it afterwards. On a fresh install it also seeds
qBittorrent's configuration with the setting the VPN port-forward hook needs.
Add `--no-tailscale-serve` to keep the qBittorrent WebUI off the tailnet; if
`tailscale` is installed, that also removes an existing `tailscale serve` for
the WebUI port. [Setup](docs/setup.md) has the full
procedure, including how to create the secret files and how to verify the
result.

## Layout

```text
compose.yaml    The stack: plex, plex-tunnel, gluetun, qbittorrent
.env.example    Per-host settings, copied to MEDIA_DIR/.env on the first run
install.sh      Dry-run-first installer
tests/          Compose, docs-consistency and installer tests
docs/           MkDocs source for the published site
mkdocs.yml      Site configuration
```

## Documentation

| Page | Covers |
|---|---|
| [Overview](docs/index.md) | What runs where, and what is exposed |
| [Setup](docs/setup.md) | Prerequisites, installing, verifying |
| [Secrets](docs/secrets.md) | The two secret files and how to write them safely |
| [Operations](docs/operations.md) | Updating images, checking the VPN, logs, rollback, state |
| [Security](docs/security.md) | Exposure model and the reasons for each choice |
| [Migration](docs/migration.md) | Moving an existing Plex and qBittorrent from LXC containers |

Preview the site locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r docs/requirements.txt
.venv/bin/mkdocs serve                      # live preview on :8000
```

## Tests

Run from this directory:

```bash
./tests/run.sh                              # everything below
./tests/compose-test.sh                     # rendered compose file: ports, network, secret
./tests/docs-consistency-test.sh            # docs against the code
./tests/install-test.sh                     # install.sh against fake docker and tailscale
```

The compose and docs tests need only `docker` with the Compose plugin. The
install test fakes every external command and needs no Docker at all. None of
them starts a container or touches `/opt/media`.
