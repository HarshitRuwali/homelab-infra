# Operations

## Install

See [Getting started](index.md#getting-started) for the command and options.
Run the installer as the user that should own the service, from the
`vault-viewer/` directory of a checkout.

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
