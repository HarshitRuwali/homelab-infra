# Obsidian CLI and Bases

The viewer draws `.base` files (Obsidian Bases) by asking the Obsidian app to
evaluate them. Obsidian's command line interface only talks to a running
desktop app, so on a server with no screen the app runs in a container on a
virtual display. The same container gives agents and scripts on the host an
`obsidian` command for the rest of the CLI: search, backlinks, links,
properties, tags, tasks and more.

Canvas files (`.canvas`) need none of this: the viewer draws them itself.

## Prerequisites

- Docker, and a user in the `docker` group. The `obsidian` command runs
  `docker exec`, so anything that runs it (the viewer included) needs the
  same access.
- About 1 GB of disk for the image and up to 1 GB of memory for the container
  (about 300 MB in use with a vault of about 900 notes).
- Obsidian 1.12 or later; the image pins a release (see
  [Updating Obsidian](#updating-obsidian)).

## Set up

```bash
cd vault-viewer/obsidian
./setup.sh ~/obsidian/personal
```

`setup.sh` builds the image `obsidian-cli:<version>`, starts the container
`obsidian` with the vault open, installs the `obsidian` command into
`~/.local/bin`, waits until the app answers, and prints the next step. Options:
`--name NAME`, `--bin-dir DIR`, `--memory SIZE`, `--dry-run`, and `--remove` to
take the container and the command away again (the image and the settings
volumes stay). `./setup.sh --help` lists them.

Then tell the viewer to use it, by re-running its installer with your usual
options plus the two Obsidian ones:

```bash
cd vault-viewer
./install.sh --bind 100.64.0.10 --tailnet-user you@github \
  --obsidian-cli ~/.local/bin/obsidian --obsidian-vault ~/obsidian/personal \
  ~/obsidian/personal
```

`--obsidian-vault` is the folder the app has open; it defaults to the first
vault folder. Without `--obsidian-cli`, Bases pages show the Base's source and
say that Bases need the CLI.

## The obsidian command

```bash
obsidian search query="vault viewer" limit=5
obsidian backlinks path="agent-notes/ai-box/local-model-stack.md" counts
obsidian base:query path="Projects.base" view="Open" format=json
obsidian help
```

- Arguments are the app's own `key=value` form. Paths are relative to the
  vault. `obsidian help` lists every command, and Obsidian's documentation is
  at <https://obsidian.md/help/cli>.
- The command exits 1 when the app answers `Error: ...` (the app's own CLI
  exits 0 even then), and non-zero with a message when the container is not
  running.
- The app's output is on stdout; warnings and errors go to stderr.
- `OBSIDIAN_CONTAINER` names the container when it is not `obsidian`.
- `OBSIDIAN_TIMEOUT` is how long a command may run, in seconds (default 60).
  The limit runs inside the container, so a hung command stops there too; the
  command then exits 124.
- The app watches the vault, so files written on the host show up in search
  and Bases within a second or two.

## How it works

| Piece | What it is |
|---|---|
| `obsidian/Dockerfile` | Debian slim, Xvfb, and the official Obsidian Linux build pinned by version and SHA-256 |
| `obsidian/entrypoint.sh` | starts Xvfb, writes the app's global settings (this vault open, CLI on, updates off), then runs the app |
| `obsidian/obsidian` | the host command: `docker exec` into the container and run `obsidian-cli` there |
| `obsidian/setup.sh` | builds, runs and installs the above |

The app keeps its settings in two Docker volumes, never in the vault:
`obsidian-home` (the app's own settings) and `obsidian-vault-config`, mounted
over `<vault>/.obsidian` inside the container. The vault on the host only gets
an empty `.obsidian` folder, the mount point. So the server's app settings do
not travel with the vault to other devices, and a phone's or laptop's settings
do not change the server's app.

## Security

- **No network.** The container runs with `--network none`; the app needs none
  (updates come from rebuilding the image).
- **Contained.** It runs as the vault owner's user id with no capabilities,
  `no-new-privileges`, a read-only root filesystem, and memory and process
  limits. Chromium's own sandbox is off inside the container (it needs user
  namespaces the container does not grant), so the container is the sandbox.
- **Writes to the vault.** The vault is mounted read-write, because CLI
  commands such as `property:set` or `base:create` change notes. Nothing in the
  viewer runs those: it only runs `base:query`.
- **The command is code execution in the app.** `obsidian eval` and the `dev:`
  commands run JavaScript inside the app. That stays inside the container, but
  anything allowed to run `obsidian` can do it, and anything allowed to run
  `docker` is root-equivalent on the host. Give neither to anything you would
  not give a shell. The viewer's use of it is described in
  [Security](security.md).

## Updating Obsidian

Change `OBSIDIAN_VERSION` and `OBSIDIAN_SHA256` in `obsidian/Dockerfile` to a
newer release from <https://github.com/obsidianmd/obsidian-releases/releases>
(use the `obsidian-<version>.tar.gz` asset; its SHA-256 digest is on the
release page), then re-run `./setup.sh` with the same vault. The settings
volumes carry over.

## Troubleshooting

| Symptom | Check |
|---|---|
| `obsidian: the obsidian container is not running` | `docker ps -a --filter name=obsidian`, then `docker logs obsidian` |
| `Command line interface is not enabled` | the entrypoint writes `"cli": true` on every start; restart the container |
| Bases page says the CLI failed | run the same `obsidian base:query path=...` by hand and read its error |
| A new note is missing from results | the app may still be indexing after a start; wait for the container to report `healthy` |
