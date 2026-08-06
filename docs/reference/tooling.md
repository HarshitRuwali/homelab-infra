# Building the docs

These pages are a MkDocs site. Building it needs one tool, and publishing it
is one workflow.

## Prerequisite

[uv](https://docs.astral.sh/uv/) is the only thing you need. It resolves
MkDocs from `pyproject.toml` and pins it in `uv.lock`.

=== "macOS"

    ```bash
    brew install uv
    ```

=== "Linux"

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

## Locally

```bash
uv run --group docs mkdocs serve           # preview on http://127.0.0.1:8000
uv run --group docs mkdocs build           # render into site/
uv run --group docs mkdocs build --strict  # what the pipeline runs
```

`uv run` creates and updates `.venv` on demand, so a fresh clone needs no venv,
no activation and no `pip install`.

!!! tip "Use `--strict` before pushing"
    It promotes broken internal links and pages missing from the nav into
    build failures instead of warnings you scroll past. The publish workflow
    uses it, so a non-strict build passing locally is not a guarantee.

### Dependencies

A single [PEP 735](https://peps.python.org/pep-0735/) group in
`pyproject.toml`:

```toml
[dependency-groups]
docs = [
    "mkdocs==1.6.1",
    "mkdocs-material==9.5.49",
    "pymdown-extensions==10.14",
]
```

!!! info "`uv.lock` is committed, `.venv/` is not"
    The lock file pins exact versions so every machine renders identically.
    The environment it builds is disposable: delete `.venv` any time and the
    next `uv run` rebuilds it.

!!! warning "MkDocs is pinned to 1.x deliberately"
    MkDocs 2.0 removes the plugin system and rewrites theming with no
    migration path, which breaks mkdocs-material outright. Do not loosen
    `mkdocs==1.6.1` without rebuilding and checking the nav.

## Publishing

`.github/workflows/docs.yml` publishes to GitHub Pages. It is the **only**
workflow in this repository.

```mermaid
flowchart LR
    P["push to master<br/>touching docs/"] --> V["verify<br/>strict build + checks"]
    V --> D["deploy<br/>GitHub Pages"]
```

The `verify` job, in order:

1. **`mkdocs build --strict`** with `--frozen`, so the site is built from
   exactly the pinned versions and a stale `uv.lock` fails rather than
   silently resolving something newer.
2. **Cross-page heading anchors.** `--strict` validates page-to-page links but
   **not** the `#anchor` part, so a link to a renamed heading builds clean and
   404s in the browser.
3. **No site-specific data**, scanned across the rendered site as well as the
   sources.
4. **No em dashes.** House style.

### The leak check

This repository is public, so the published site is public. The docs are the
easiest place for a real address to end up, because examples get pasted from a
working terminal.

| Rejected | Allowed |
|---|---|
| the live monitoring domain | `example.com` |
| `10.10.x.x`, this fleet's guest LAN | `10.0.x.x` in `hosts.example.yml` |
| the Tailscale CGNAT range | `100.64.0.11` and `.12`, the documented examples |

The ranges are narrow on purpose so the placeholder-based examples keep
working. The scan covers `site/` as well as `docs/`, so an address that
arrives via a snippet or an include is still caught before it is served.

### What it cannot do

```yaml
permissions:
  contents: read     # checkout
  pages: write       # upload the artifact
  id-token: write    # claim the deployment
```

No `contents: write`, no repository secrets, no `ansible-playbook`, no SSH.
The workflow cannot push to the repository and cannot reach any host in the
fleet.

### Triggers

Only `push` to `master` touching `docs/`, `mkdocs.yml`, `pyproject.toml`,
`uv.lock` or the workflow itself. Editing a playbook or a dashboard does not
republish. `workflow_dispatch` republishes by hand from the Actions tab
without an empty commit.

Concurrency is `cancel-in-progress: false` on purpose: a half-finished Pages
deploy leaves the published site broken, so an in-flight deploy completes.

### Enabling Pages

The workflow enables Pages itself, via `actions/configure-pages@v5` with
`enablement: true`. A fresh clone of this repo publishes without anyone
visiting Settings first.

!!! bug "Why that step exists"
    The very first run failed exactly here. Every check in `verify` passed,
    then `deploy-pages` failed with a message that does not say what is wrong.
    `GET /repos/<owner>/<repo>/pages` returned **404**: Pages had simply never
    been enabled, and the deploy action cannot enable it.

If the configure step fails with **"Resource not accessible by integration"**,
the workflow token is not permitted to enable Pages on that account. Do it
once by hand instead:

**Settings** → **Pages** → Source: **GitHub Actions**

The site then appears at `https://<user>.github.io/<repo>/`.

!!! warning "`site_url` must match where it is served"
    `mkdocs.yml` sets `site_url` to the Pages project path. GitHub serves a
    project repo under `/<repo>/`, and without the trailing path every
    canonical URL and every `sitemap.xml` entry points at the domain root.
    Change it if you move the site behind your own domain.

## Serving it yourself instead

`mkdocs build` renders a self-contained static site into `site/`, which is
gitignored. Serve it from an nginx location on the central LXC, or
`python3 -m http.server` for a quick look.

Nothing about the docs depends on GitHub. If any of this should not be public,
serve `site/` behind your existing auth and delete the workflow.
