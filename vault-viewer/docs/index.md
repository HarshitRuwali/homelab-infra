# Vault Viewer

A read-only web viewer for an [Obsidian](https://obsidian.md) vault, served on
a host's Tailscale address. One Python program (`artifacts.py`), standard
library only, run as a systemd user service.

The vault is written by agents, so everything in it is treated as untrusted
input. See [Security](security.md) for the model.

## What it does

- **Renders the whole vault.** Notes with `[[wikilinks]]`, `![[embeds]]`,
  callouts, frontmatter and backlinks; Mermaid diagrams; self-contained HTML
  artifacts; SVG and raster images; text sources such as `.d2`, `.json` and
  `.py`.
- **Navigates it.** A file tree with a "recent" list and search on the left,
  the selected file on the right. The page reloads within seconds when a file
  changes on disk. Any diagram opens full screen with pan and zoom.
- **Exports PDF.** Notes and text files download as a print-styled A4 PDF,
  rendered in a locked-down container. See [PDF export](pdf-export.md).
- **Stays read-only.** There is no route that writes, renames or deletes.

Keyboard shortcuts in the viewer:

| Key | Action |
|---|---|
| `/` | Focus search |
| `[` | Show or hide the sidebar |
| `r` | Reload the current file |
| `p` | Download the current note as PDF |
| `Alt` + `Left` | Back |
| `Esc` | Clear search, close the drawer or the full-screen diagram |

## Prerequisites

| Needed for | What |
|---|---|
| Everything | `/usr/bin/python3` (the unit runs it by that path) and `systemd --user` |
| Notes rendered as HTML | `python3-markdown` (Debian and Ubuntu package name). Optional: without it, notes render as plain text and the service logs `python3-markdown is missing` |
| `--tailnet-user` | The `tailscale` CLI, logged in on the host |
| PDF export | `docker`, usable by the installing user |

The installer installs no packages. By default it writes only under your home
directory: the program goes to `ARTIFACTS_BIN` (default
`$HOME/.local/bin/artifacts`), so pointing that variable elsewhere writes
there instead, and `--build-pdf-image` creates a docker image in your docker
daemon.

## Getting started

Find the host's Tailscale address, then install:

```bash
tailscale ip -4
cd vault-viewer
./install.sh --bind 100.64.0.10 \
  --allow-host myhost --allow-host myhost.example-tailnet.ts.net \
  --tailnet-user you@github --title myhost ~/obsidian
```

Open `http://100.64.0.10:7777` from one of your own tailnet devices.

To see what the installer would do first, add `--dry-run`: it runs the
preflight checks (so it can fail), prints the unit file and the actions, and
changes nothing. It does not print the next-steps text.

### Installer options

```text
install.sh --bind ADDR [--port N] [--allow-host NAME]... [--tailnet-user LOGIN]
           [--title NAME] [--build-pdf-image] [--dry-run] ROOT...
install.sh -h | --help
```

| Option | Meaning |
|---|---|
| `--bind ADDR` | Required. The one IPv4 address the server listens on, normally the host's Tailscale address. It must be a unicast dotted-quad address: `0.0.0.0` (all interfaces), multicast addresses, octets above 255, leading zeros such as `010.1.1.1`, and hostnames are rejected with exit status 2. |
| `--port N` | Listen port, 1 to 65535. Defaults to 7777. |
| `--allow-host NAME` | Extra `Host` header name to accept, such as the machine name or its tailnet name. Repeatable. The bind address is always accepted. |
| `--tailnet-user LOGIN` | Admit only this login's untagged tailnet devices. Without it, the server does no login check. See [Security](security.md#the-tailnet-gate). |
| `--title NAME` | Name shown in the sidebar. Defaults to the vault folder names. |
| `--build-pdf-image` | Also build the PDF container image. Needs docker, takes a few minutes and downloads about 1 GB. See [PDF export](pdf-export.md). |
| `--dry-run` | Print the unit and the actions, change nothing. |
| `ROOT...` | One or more vault folders to serve. Each must be an existing directory. |

No option value, ROOT or `ARTIFACTS_BIN` may contain a control character
(newline, carriage return, tab and so on); the installer exits with status 2.

### What the installer does

1. With `--build-pdf-image`, builds the PDF image first. If the build fails,
   the installer exits with status 1 before it has changed anything else.
2. Copies `artifacts.py` to `${ARTIFACTS_BIN:-$HOME/.local/bin/artifacts}`.
3. Renders `systemd/artifacts.service` into
   `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/artifacts.service`. If a
   unit is already there and differs, it is first copied to
   `artifacts.service.bak` next to it.
4. Runs `systemctl --user daemon-reload`, `enable` and `restart`.
5. Verifies the service: the unit must be active (`systemctl --user is-active`)
   and, within 10 s, the server must answer `http://ADDR:PORT/` with `200`. A
   `403` also counts, but only when you passed `--tailnet-user`: the installing
   host may be a tagged tailnet device, which the
   [gate](security.md#the-tailnet-gate) refuses by design. Any other code, no
   answer, an inactive unit, or a `403` without `--tailnet-user` (a `Host`
   header mismatch) is a failure: the installer prints the last status lines,
   runs `systemctl --user disable --now artifacts.service` so the unit does not
   crash-loop, and exits with status 1.
6. Prints the next steps.

Each vault folder is a root. File ids are `<root folder name>/<path>`, so
serving `~/obsidian/personal` gives ids that start with `personal/`.

## Where next

- [Security](security.md): the threat model and what each control stops.
- [PDF export](pdf-export.md): contents, container, limits, rebuilding.
- [Operations](operations.md): upgrade, logs, uninstall, troubleshooting.
