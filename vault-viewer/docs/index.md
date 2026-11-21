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
  `.py`; `.zip` archives, which download instead of previewing.
- **Navigates it.** A file tree with a "recent" list and search on the left,
  the selected file on the right. The page picks up changes on disk within
  about 12 seconds (it checks every 10 s, skips the check while its tab is
  hidden and checks at once when the tab is shown again; the server reuses one
  vault scan for 2 s). Any diagram opens full screen with pan and zoom. Every code
  block has a **Copy** button (shown on hover, always on touch screens).
- **Exports PDF.** Notes and text files download as a print-styled A4 PDF,
  rendered by a sandboxed headless Chromium on the host, with no network. See
  [PDF export](pdf-export.md).
- **Hands off to the Obsidian app.** Notes and images have an **Obsidian**
  button that opens them in the app on the device you are using. A device
  can also open every vault link in the app; see
  [Opening files in Obsidian](#opening-files-in-obsidian).
- **Stays read-only.** There is no route that writes, renames or deletes.

Keyboard shortcuts in the viewer:

| Key | Action |
|---|---|
| `/` | Focus search |
| `[` | Show or hide the sidebar |
| `r` | Reload the current file |
| `p` | Download the current note as PDF |
| `o` | Open the current note or image in the Obsidian app |
| `Alt` + `Left` | Back |
| `Esc` | Clear search, close the drawer or the full-screen diagram |

## Opening files in Obsidian

The **Obsidian** button (**App** on a phone) opens the current note or image
with an `obsidian://open` link. HTML artifacts, text files and archives have no
button: Obsidian does not show them, so they stay in the viewer. The device
needs the Obsidian app with a copy of the vault, for example synced through
Nextcloud.

A web page cannot tell whether an app is installed, so each device remembers
it in the browser:

1. Press **Obsidian** once. If the app opens, the viewer asks whether to open
   vault links in Obsidian on this device from now on. If nothing opens within
   4 seconds, it says so.
2. With the setting on, a link to the viewer, such as one an agent posts in
   chat, opens the file in Obsidian. The viewer page shows the same file
   behind the app, so if the app does not open, the file is already in front
   of you and the viewer says why. Reloading the same link stays in the
   viewer, and moving between files inside the viewer never leaves it.
3. A file changed in the last 10 minutes opens in the viewer, with a note: it
   may not have synced to this device yet, and Obsidian cannot open a file it
   does not have. Its **Open in Obsidian** button tries anyway.

The setting is at the bottom of the sidebar: **Open links in Obsidian**
switches it on or off, and **Vault** sets the vault's name in Obsidian on this
device. The name defaults to the vault folder's name (`personal` for
`~/obsidian/personal`); change it when the app knows the vault by another name.
Both are stored in this browser only and never sent to the server.

## Prerequisites

| Needed for | What |
|---|---|
| Everything | `/usr/bin/python3` (the unit runs it by that path) and `systemd --user` |
| Notes rendered as HTML | `python3-markdown` (Debian and Ubuntu package name). Optional: without it, notes render as plain text and the service logs `python3-markdown is missing` |
| `--tailnet-user` | The `tailscale` CLI, logged in on the host |
| PDF export | The PDF runtime (installed with `--install-pdf-runtime`; no docker), and on Ubuntu 23.10 and later an AppArmor profile, which the installer writes with `sudo` |

The installer installs no packages. By default it writes only under your home
directory: the program goes to `ARTIFACTS_BIN` (default
`$HOME/.local/bin/artifacts`), so pointing that variable elsewhere writes
there instead. `--install-pdf-runtime` adds the PDF runtime under
`${XDG_DATA_HOME:-$HOME/.local/share}/artifacts/pdf-runtime`, and the AppArmor
profile goes to `/etc/apparmor.d` with `sudo` when the kernel needs it.

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
           [--title NAME] [--install-pdf-runtime] [--no-apparmor] [--dry-run] ROOT...
install.sh -h | --help
```

| Option | Meaning |
|---|---|
| `--bind ADDR` | Required. The one IPv4 address the server listens on, normally the host's Tailscale address. It must be a unicast dotted-quad address: `0.0.0.0` (all interfaces), multicast addresses, octets above 255, leading zeros such as `010.1.1.1`, and hostnames are rejected with exit status 2. |
| `--port N` | Listen port, 1 to 65535. Defaults to 7777. |
| `--allow-host NAME` | Extra `Host` header name to accept, such as the machine name or its tailnet name. Repeatable. The bind address is always accepted. |
| `--tailnet-user LOGIN` | Admit only this login's untagged tailnet devices. Without it, the server does no login check. See [Security](security.md#the-tailnet-gate). |
| `--title NAME` | Name shown in the sidebar. Defaults to the vault folder names. |
| `--install-pdf-runtime` | Also download and install the PDF runtime (Chromium's headless shell, libraries and fonts). Downloads about 180 MB once. See [PDF export](pdf-export.md#the-runtime). |
| `--no-apparmor` | Do not install the AppArmor profile that Chromium's sandbox needs on Ubuntu 23.10 and later. The PDF button then reports the sandbox as unusable until you install it. See [PDF export](pdf-export.md#the-apparmor-profile). |
| `--dry-run` | Print the unit and the actions, change nothing. |
| `ROOT...` | One or more vault folders to serve. Each must be an existing directory. |

No option value, ROOT or `ARTIFACTS_BIN` may contain a control character
(newline, carriage return, tab and so on); the installer exits with status 2.

### What the installer does

1. With `--install-pdf-runtime`, installs the PDF runtime first. If that
   fails, the installer exits with status 1 before it has changed anything else.
2. When the kernel restricts unprivileged user namespaces and the profile is
   missing, writes the AppArmor profile to
   `/etc/apparmor.d/artifacts-chrome-headless-shell` with `sudo` and loads it
   (skipped with `--no-apparmor`). A failure here is a warning, not an error:
   the viewer works and only the PDF button needs the profile.
3. Copies `artifacts.py` to `${ARTIFACTS_BIN:-$HOME/.local/bin/artifacts}`.
4. Renders `systemd/artifacts.service` into
   `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/artifacts.service`. If a
   unit is already there and differs, it is first copied to
   `artifacts.service.bak` next to it.
5. Runs `systemctl --user daemon-reload`, `enable` and `restart`.
6. Verifies the service: the unit must be active (`systemctl --user is-active`)
   and, within 10 s, the server must answer `http://ADDR:PORT/` with `200`. A
   `403` also counts, but only when you passed `--tailnet-user`: the installing
   host may be a tagged tailnet device, which the
   [gate](security.md#the-tailnet-gate) refuses by design. Any other code, no
   answer, an inactive unit, or a `403` without `--tailnet-user` (a `Host`
   header mismatch) is a failure: the installer prints the last status lines,
   runs `systemctl --user disable --now artifacts.service` so the unit does not
   crash-loop, and exits with status 1.
7. Prints the next steps, including the state of PDF export.

Each vault folder is a root. File ids are `<root folder name>/<path>`, so
serving `~/obsidian/personal` gives ids that start with `personal/`.

## Where next

- [Security](security.md): the threat model and what each control stops.
- [PDF export](pdf-export.md): contents, the runtime, the sandbox, limits, updating.
- [Operations](operations.md): upgrade, logs, uninstall, troubleshooting.
