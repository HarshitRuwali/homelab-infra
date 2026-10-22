# vault-viewer

A read-only web viewer for an Obsidian vault, served on a host's Tailscale
address. Notes with wikilinks, embeds, callouts and backlinks, Mermaid
diagrams, HTML artifacts and images, with search, live reload and PDF export.
One stdlib Python program run as a systemd user service. The vault is written
by agents, so the server treats everything in it as untrusted.

**Documentation:** <https://harshitruwali.github.io/homelab-infra/vault-viewer/>

## Quick start

Needs `python3` and `systemd --user`. Optional: `python3-markdown` (notes
render as plain text without it), the `tailscale` CLI for `--tailnet-user`, and
docker for PDF export.

```bash
tailscale ip -4                             # the address to bind
cd vault-viewer
./install.sh --bind 100.64.0.10 \
  --allow-host myhost --allow-host myhost.example-tailnet.ts.net \
  --tailnet-user you@github --title myhost ~/obsidian
```

Then open `http://100.64.0.10:7777` from one of your own tailnet devices. Add
`--dry-run` to print the unit and the actions without changing anything, and
`--build-pdf-image` to build the PDF container image as well. The
[Getting started](docs/index.md#getting-started) page lists every option and
what the installer does.

## Layout

```text
artifacts.py                The server: one file, standard library only
install.sh                  Installs the program and a systemd user unit
systemd/artifacts.service   Unit template the installer renders
tests/                      Viewer, installer and docs-consistency tests
docs/                       MkDocs source for the published site
mkdocs.yml                  Site configuration
```

## Documentation

| Page | Covers |
|---|---|
| [Getting started](docs/index.md) | Features, prerequisites, installer options |
| [Security](docs/security.md) | Threat model and each control |
| [PDF export](docs/pdf-export.md) | Contents, container, limits, rebuilding the image |
| [Operations](docs/operations.md) | Upgrade, logs, uninstall, troubleshooting |

Preview the site locally:

```bash
python3 -m venv .venv
.venv/bin/pip install -r docs/requirements.txt
.venv/bin/mkdocs serve                      # live preview on :8000
```

## Tests

Run from this directory:

```bash
python3 tests/test_viewer.py                # server behaviour and security checks
./tests/test_install.sh                     # installer against a scratch home
./tests/docs-consistency-test.sh            # docs against the code
```

`ARTIFACTS_PY=/path/to/copy.py python3 tests/test_viewer.py` runs the viewer
tests against another copy of the program. The end-to-end PDF check runs only
when the `artifacts-pdf:local` image exists and is skipped otherwise.

The docs-consistency test fails when a documented flag is not in a parser, a
named path is missing, a link or anchor does not resolve, a page is missing from
the nav, or the documented PDF image name differs from the one in `artifacts.py`.
