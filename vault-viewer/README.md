# vault-viewer

A read-only web viewer for an Obsidian vault, served on a host's Tailscale
address. Notes with wikilinks, embeds, callouts and backlinks, Mermaid
diagrams, HTML artifacts and images, with search, live reload, PDF export and
an **Obsidian** button that opens the file in the app.
One stdlib Python program run as a systemd user service. The vault is written
by agents, so the server treats everything in it as untrusted.

**Documentation:** <https://harshitruwali.github.io/homelab-infra/vault-viewer/>

## Quick start

Needs `python3` and `systemd --user`. Optional: `python3-markdown` (notes
render as plain text without it) and the `tailscale` CLI for `--tailnet-user`.
PDF export needs no docker: `--install-pdf-runtime` installs its own headless
Chromium.

```bash
tailscale ip -4                             # the address to bind
cd vault-viewer
./install.sh --bind 100.64.0.10 \
  --allow-host myhost --allow-host myhost.example-tailnet.ts.net \
  --tailnet-user you@github --title myhost ~/obsidian
```

Then open `http://100.64.0.10:7777` from one of your own tailnet devices. Add
`--dry-run` to print the unit and the actions without changing anything, and
`--install-pdf-runtime` to install the PDF runtime as well. The
[Getting started](docs/index.md#getting-started) page lists every option and
what the installer does.

To install or update other machines from this checkout, over SSH:

```bash
./deploy.sh --dry-run box1 box2       # probe and print the plan
./deploy.sh box1 box2                 # update the program and, when needed, the PDF runtime
```

See [Deploy to other hosts](docs/operations.md#deploy-to-other-hosts).

## Layout

```text
artifacts.py                The server: one file, standard library only
install.sh                  Installs the program and a systemd user unit
deploy.sh                   Installs or updates it on other hosts over SSH
systemd/artifacts.service   Unit template the installer renders
tests/                      Viewer, installer, deploy and docs-consistency tests
docs/                       MkDocs source for the published site
mkdocs.yml                  Site configuration
```

## Documentation

| Page | Covers |
|---|---|
| [Getting started](docs/index.md) | Features, prerequisites, installer options |
| [Security](docs/security.md) | Threat model and each control |
| [PDF export](docs/pdf-export.md) | Contents, the runtime, the sandbox, limits, updating |
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
./tests/e2e.sh                              # the page in headless Chromium (needs the PDF runtime)
```

`./tests/e2e.sh --bench URL NOTE` times cold loads of a running viewer instead:
see [Benchmark a page load](docs/operations.md#benchmark-a-page-load).

`ARTIFACTS_PY=/path/to/copy.py python3 tests/test_viewer.py` runs the viewer
tests against another copy of the program. The end-to-end PDF check runs only
when the PDF runtime for this program is installed and is skipped otherwise.

The docs-consistency test fails when a documented flag is not in a parser, a
named path is missing, a link or anchor does not resolve, a page is missing from
the nav, the documented runtime directory or AppArmor profile name differs from the one in
`artifacts.py`, or a `--pdf` mode shown with `deploy.sh` is not accepted by it.
