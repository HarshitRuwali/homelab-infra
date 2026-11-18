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
`ARTIFACTS_BIN` moves the program, and `--install-pdf-runtime` adds the PDF runtime under `~/.local/share`):

| What | Where |
|---|---|
| Program | `${ARTIFACTS_BIN:-$HOME/.local/bin/artifacts}` |
| Unit | `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/artifacts.service` |
| Previous unit | `artifacts.service.bak`, next to the unit, only when a differing unit was already there |
| PDF runtime, with `--install-pdf-runtime` | `${XDG_DATA_HOME:-$HOME/.local/share}/artifacts/pdf-runtime/<PDF_REV>/`, see [PDF export](pdf-export.md#the-runtime) |
| AppArmor profile, only when the kernel restricts user namespaces | `/etc/apparmor.d/artifacts-chrome-headless-shell`, written with `sudo` unless you pass `--no-apparmor` |

## Reinstall and upgrade

The installer is idempotent and is the supported way to change the service. To upgrade after
`git pull`, or to change an option such as the bind address, a hostname, the
title or a root, run it again with the full set of options you want. It
overwrites the program and the unit, reloads systemd and restarts the service.
Options are not merged with the previous run. When the new unit differs from
the one on disk, the old one is kept as `artifacts.service.bak` (one backup,
replaced by the next differing run). Re-running with identical options makes no
backup. The PDF runtime is installed only when you pass
`--install-pdf-runtime`; a runtime for the current program is left alone, and
an older revision is replaced.

If the post-install check fails, the installer disables and stops the unit and
exits with status 1; see
[What the installer does](index.md#what-the-installer-does) for the rule.

## Deploy to other hosts

`deploy.sh` installs or updates the viewer on other machines over SSH, from
this checkout. It runs on your machine; each host needs `python3`, a systemd
user session, about 250 MB of free disk for the PDF runtime and, when the host
restricts unprivileged user namespaces, passwordless `sudo` to install the
AppArmor profile (without it the commands are printed).

```bash
./deploy.sh --dry-run box1 box2                  # probe each host and print the plan
./deploy.sh box1 box2                            # update both
./deploy.sh -i ~/.ssh/infra-worker user@100.64.0.20
./deploy.sh newbox -- --bind 100.64.0.30 --tailnet-user you@github ~/obsidian
```

A `HOST` is any ssh destination: an alias from `~/.ssh/config`, a name or
`user@host`. For each one, in order:

1. **Probe.** Reads the installed unit (program path, bind address, port),
   the PDF runtime revision, whether the kernel restricts user namespaces, and
   checks `python3` and free disk space.
2. **PDF runtime.** The runtime must match the program: the program's
   `--pdf-rev` names the revision, and the runtime sits in a directory of that
   name. When the host already has it, it is left alone. Otherwise it is copied
   or downloaded before the program changes, and the old revisions are kept
   until the check passes. If this step fails, the host is left as it was.
   When the host restricts user namespaces, the AppArmor profile is installed
   with `sudo -n`; if that needs a password, the commands are printed and the
   PDF check is skipped.
3. **Program.** On a host that runs `artifacts.service`, the program is
   replaced (the old one is kept as `<program>.prev`), the unit is left as it
   is, and the service restarts. An identical program is left alone. A host
   without the unit is installed with `install.sh` and the `INSTALL_OPTIONS`
   given after `--`; given those options, an installed host is reconfigured
   with them too.
4. **Check.** The unit is active, the port answers (HTTP 200, or 403 when the
   [tailnet gate](security.md#the-tailnet-gate) refuses the host itself), and
   a test page prints to PDF. If an update fails the check, the previous
   program and runtime are put back and the service restarts.

| Option | Meaning |
|---|---|
| `--pdf MODE` | `auto` (default): copy this machine's runtime when it matches the program, otherwise download it on the host. `ship`: always copy it (a tar stream over ssh), which suits a host with no internet access; the host checks the copy against a file list sent with it and writes `REV` only after that passes, so a cut transfer never counts as installed. `fetch`: always download it on the host (`--install-pdf-runtime`). `skip`: leave the runtime alone and skip the PDF part of the check. |
| `-i FILE` | ssh identity file. |
| `--dry-run` | Probe each host and print the plan; change nothing. |

To copy the runtime, install it here first with `python3 artifacts.py
--install-pdf-runtime`. The older `--image` and `--pdf build` are gone and exit
with status 2 naming `--pdf`. Hosts are handled one after another; the exit status is 0
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
on start, then one line per request, except successful `/api/*` calls and
`304 Not Modified` replies (the page polls the file list, so logging those
would drown everything else). Errors, page, file, PDF and download requests
are logged, and PDF render errors appear here too.

## Uninstall

```bash
systemctl --user disable --now artifacts.service
rm ~/.config/systemd/user/artifacts.service ~/.local/bin/artifacts
systemctl --user daemon-reload
rm -rf ~/.local/share/artifacts/pdf-runtime                         # only if you installed the PDF runtime
sudo apparmor_parser -R /etc/apparmor.d/artifacts-chrome-headless-shell   # only if the installer wrote the profile: unload it
sudo rm /etc/apparmor.d/artifacts-chrome-headless-shell                   # then remove it
```

Adjust the paths if you set `ARTIFACTS_BIN`, `XDG_CONFIG_HOME` or `XDG_DATA_HOME`.
The vault itself is never touched.

If you used an earlier version that built a docker image, `docker image rm
artifacts-pdf:local` removes it; see
[Migrating from the docker image](pdf-export.md#migrating-from-the-docker-image).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Links or bookmarks stop working | The bind address no longer matches the host's Tailscale address. Compare the unit's `--bind` with `tailscale ip -4` and re-run the installer with the current one. |
| `403 forbidden host` | You browsed by a name that is not accepted. Add it with `--allow-host NAME` and reinstall. The port is part of the check. |
| `403 forbidden` | The [tailnet gate](security.md#the-tailnet-gate) refused the device: it is tagged, belongs to another login, or `tailscale whois` failed. Check with `tailscale whois --json <client ip>`. |
| The stage says `Could not load the file list (HTTP 403)` | The page loaded but the file list was refused, usually the [tailnet gate](security.md#the-tailnet-gate) after a failed `tailscale whois` (a refusal is cached for 60 s). The page retries every 2 s on its own. |
| The window stays empty: no sidebar, no title bar, not even the dark background | The browser did not draw the tab. The page has no step that can hide everything: its sidebar and title bar are plain HTML drawn before any script runs. If the service log shows `GET /` and `GET /view/...` answered with 200 for that device, the server did its part; time the same note with the [benchmark](#benchmark-a-page-load) to confirm, then try another browser or turn the browser's hardware acceleration off and on. |
| The page does not load at all | The server is not running (`systemctl --user status artifacts.service`), the tailnet ACL does not allow the port from your device, or the port is taken by another program. |
| No diagrams or code colours | The viewing device cannot reach cdnjs, jsdelivr or unpkg. See [Browser dependencies](security.md#browser-dependencies). |
| Notes show as plain text | `python3-markdown` is missing on the host. Install the distribution package, then restart the service. |
| PDF button reports the PDF runtime is not installed | Install it: `python3 ~/.local/bin/artifacts --install-pdf-runtime`. This also happens after a program update that changed a pin, the trimming or the fonts, because the runtime directory is named after the revision of those; [`deploy.sh`](#deploy-to-other-hosts) installs both. See [PDF export](pdf-export.md#the-runtime). |
| PDF button reports Chromium's sandbox is not usable | The kernel restricts unprivileged user namespaces and the AppArmor profile is missing. Install it with the two commands [below](#install-the-apparmor-profile). See [The AppArmor profile](pdf-export.md#the-apparmor-profile). |
| Not sure which of the above it is | `python3 ~/.local/bin/artifacts --check-pdf-runtime` renders a tiny page and prints `ok` or the reason. |
| PDF button reports a failed render | Read the service log; see [PDF export](pdf-export.md#limits) for the limits. |
| Service stops when you log out or after a reboot | Enable lingering; see [Survive logout and reboot](#survive-logout-and-reboot). |

### Install the AppArmor profile

```bash
python3 ~/.local/bin/artifacts --print-apparmor-profile | sudo tee /etc/apparmor.d/artifacts-chrome-headless-shell >/dev/null
sudo apparmor_parser -r /etc/apparmor.d/artifacts-chrome-headless-shell
```

For a runtime outside the default directory, add `--pdf-runtime DIR` to the first
command: the profile names that directory and no other.

## Benchmark a page load

`tests/e2e.sh` loads the page in the PDF runtime's headless shell over the
DevTools pipe, with no docker and no Playwright, so it needs the runtime
(`python3 artifacts.py --install-pdf-runtime`) and, on Ubuntu 23.10 and later,
the AppArmor profile. With no arguments it runs the page checks against a scratch vault.
With `--bench` it times cold loads (a fresh browser context each run, so an
empty cache) of one note on a running viewer, with the note path in the URL
hash written both with plain and with `%2F`-encoded slashes, and prints one
JSON line per run: first contentful paint, file list shown, note title shown and
note text shown, in milliseconds from navigation.

The tailnet gate refuses the host's own address, so on the viewer's host time a
loopback copy over the same vault (a second process; the service is untouched):

```bash
python3 ~/.local/bin/artifacts --port 7778 ~/obsidian &
./tests/e2e.sh --bench http://127.0.0.1:7778 obsidian/some/note.md 5
kill %1
```

`NOTE` is the vault id shown after `#` in the viewer's URL. Server-side timings
alone come from curl, for example
`curl -s -o /dev/null -w '%{time_total}\n' http://127.0.0.1:7778/api/tree`.

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
| `--pdf-runtime DIR` | Where the PDF runtime is installed (resolved to an absolute path). Defaults to `${XDG_DATA_HOME:-~/.local/share}/artifacts/pdf-runtime`. |
| `--install-pdf-runtime` | Download and install the PDF runtime, then exit. |
| `--check-pdf-runtime` | Render a tiny page with the runtime, print `ok` or the reason, then exit. |
| `--print-apparmor-profile` | Print the AppArmor profile for Chromium's sandbox for the `--pdf-runtime` directory, then exit (status 2 for a directory with characters special in AppArmor patterns). |
| `--pdf-rev` | Print the revision of the PDF runtime this program uses, then exit. |
