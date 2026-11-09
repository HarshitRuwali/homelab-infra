# Operations

## Install

See [Getting started](index.md#getting-started) for the command and options.
Run the installer as the user that should own the service, from the
`vault-viewer/` directory of a checkout.
To install or update other machines over SSH, see
[Deploy to other hosts](#deploy-to-other-hosts).

```bash
./install.sh --dry-run --bind 100.64.0.10 ~/obsidian    # preflight and preview, changes nothing
./install.sh --bind 100.64.0.10 ~/obsidian
```

By default the installer writes under your home directory only (setting
`ARTIFACTS_BIN` moves the program, and `--build-pdf-image` adds a docker image):

| What | Where |
|---|---|
| Program | `${ARTIFACTS_BIN:-$HOME/.local/bin/artifacts}` |
| Unit | `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/artifacts.service` |
| Previous unit | `artifacts.service.bak`, next to the unit, only when a differing unit was already there |

## Reinstall and upgrade

The installer is idempotent and is the supported way to change the service. To upgrade after
`git pull`, or to change an option such as the bind address, a hostname, the
title or a root, run it again with the full set of options you want. It
overwrites the program and the unit, reloads systemd and restarts the service.
Options are not merged with the previous run. When the new unit differs from
the one on disk, the old one is kept as `artifacts.service.bak` (one backup,
replaced by the next differing run). Re-running with identical options makes no
backup.

If the post-install check fails, the installer disables and stops the unit and
exits with status 1; see
[What the installer does](index.md#what-the-installer-does) for the rule.

## Deploy to other hosts

`deploy.sh` installs or updates the viewer on other machines over SSH, from
this checkout. It runs on your machine; each host needs `python3`, a systemd
user session and, for PDF export, docker usable by the SSH user.

```bash
./deploy.sh --dry-run box1 box2                  # probe each host and print the plan
./deploy.sh box1 box2                            # update both
./deploy.sh -i ~/.ssh/infra-worker user@100.64.0.20
./deploy.sh newbox -- --bind 100.64.0.30 --tailnet-user you@github ~/obsidian
```

A `HOST` is any ssh destination: an alias from `~/.ssh/config`, a name or
`user@host`. For each one, in order:

1. **Probe.** Reads the installed unit (program path, bind address, port,
   PDF image), and checks `python3`, docker and free disk space.
2. **PDF image.** The image must match the program: the program's
   `--pdf-rev` names the revision, and every image carries it as the
   `vault-viewer.pdf-rev` label. When the host's image already matches, it is
   left alone. Otherwise it is replaced before the program changes, and the old
   one keeps an `-prev` tag until the check passes. If this step fails, the host
   is left as it was.
3. **Program.** On a host that runs `artifacts.service`, the program is
   replaced (the old one is kept as `<program>.prev`), the unit is left as it
   is, and the service restarts. An identical program is left alone. A host
   without the unit is installed with `install.sh` and the `INSTALL_OPTIONS`
   given after `--`; given those options, an installed host is reconfigured
   with them too.
4. **Check.** The unit is active, the port answers (HTTP 200, or 403 when the
   [tailnet gate](security.md#the-tailnet-gate) refuses the host itself), and
   a test page prints to PDF. If an update fails the check, the previous
   program and image are put back and the service restarts.

| Option | Meaning |
|---|---|
| `--image MODE` | `auto` (default): copy this machine's `artifacts-pdf:local` when it matches the program, otherwise build on the host. `ship`: always copy it (`docker save`, compressed, into `docker load`), which suits a host with little disk or a slow link. `build`: always build on the host. `skip`: leave the image alone and skip the PDF part of the check. |
| `-i FILE` | ssh identity file. |
| `--dry-run` | Probe each host and print the plan; change nothing. |

To copy images, build one here first with `python3 artifacts.py
--build-pdf-image`. Hosts are handled one after another; the exit status is 0
when every host succeeded and 1 otherwise, with a summary at the end. The SSH
connection is reused for all steps of a host, and scripts reach the host
base64-encoded, so the remote login shell only runs `bash`.

## Survive logout and reboot

A systemd user service runs only while its user has a session, unless lingering
is enabled. The installer's next-steps text suggests it:

```bash
loginctl enable-linger "$USER"
```

## Status and logs

```bash
systemctl --user status artifacts.service
journalctl --user -u artifacts.service -n 50
journalctl --user -u artifacts.service -f
```

The service prints its address, roots, accepted `Host` names and tailnet user
on start, then one line per request. PDF render errors appear here too.

## Uninstall

```bash
systemctl --user disable --now artifacts.service
rm ~/.config/systemd/user/artifacts.service ~/.local/bin/artifacts
systemctl --user daemon-reload
docker image rm artifacts-pdf:local     # only if you built the PDF image
```

Adjust the two paths if you set `ARTIFACTS_BIN` or `XDG_CONFIG_HOME`. The vault
itself is never touched.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Links or bookmarks stop working | The bind address no longer matches the host's Tailscale address. Compare the unit's `--bind` with `tailscale ip -4` and re-run the installer with the current one. |
| `403 forbidden host` | You browsed by a name that is not accepted. Add it with `--allow-host NAME` and reinstall. The port is part of the check. |
| `403 forbidden` | The [tailnet gate](security.md#the-tailnet-gate) refused the device: it is tagged, belongs to another login, or `tailscale whois` failed. Check with `tailscale whois --json <client ip>`. |
| The page does not load at all | The server is not running (`systemctl --user status artifacts.service`), the tailnet ACL does not allow the port from your device, or the port is taken by another program. |
| No diagrams or code colours | The viewing device cannot reach cdnjs, jsdelivr or unpkg. See [Browser dependencies](security.md#browser-dependencies). |
| Notes show as plain text | `python3-markdown` is missing on the host. Install the distribution package, then restart the service. |
| PDF button reports the image is missing | Build it: `python3 ~/.local/bin/artifacts --build-pdf-image`. See [PDF export](pdf-export.md#the-image). |
| PDF button reports the image does not match this program | The program was updated without its image. Rebuild it as above, or run [`deploy.sh`](#deploy-to-other-hosts), which updates both. |
| PDF button reports a failed render | Read the service log; see [PDF export](pdf-export.md#limits) for the limits. |
| Service stops when you log out or after a reboot | Enable lingering; see [Survive logout and reboot](#survive-logout-and-reboot). |

## Running by hand

For a quick look without installing anything, run the program directly.
The program has no dry-run mode (`--dry-run` is an installer flag). Its
`ROOT` arguments are optional and default to `~/obsidian`. It binds
`127.0.0.1` by default:

```bash
python3 artifacts.py --port 7777 ~/obsidian
```

Its own options, from its argument parser (`python3 artifacts.py --help`):

| Option | Meaning |
|---|---|
| `--bind ADDR` | Address to listen on. Defaults to `127.0.0.1` (or `ARTIFACTS_BIND`). The program itself accepts any IP address; only the installer applies the stricter [bind rules](index.md#installer-options). |
| `--port N` | Listen port. Defaults to 7777 (or `ARTIFACTS_PORT`). |
| `--allow-host NAME` | Extra `Host` header name to accept. Repeatable. |
| `--tailnet-user LOGIN` | Admit only this login's untagged tailnet devices (or `ARTIFACTS_TAILNET_USER`). |
| `--title NAME` | Name shown in the sidebar. |
| `--pdf-image IMAGE` | PDF image name (or `ARTIFACTS_PDF_IMAGE`). |
| `--build-pdf-image` | Build the PDF image with docker, then exit. |
| `--pdf-rev` | Print the revision the PDF image must carry (its `vault-viewer.pdf-rev` label), then exit. |
