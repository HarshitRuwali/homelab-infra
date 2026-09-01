# Building the Docs

The documentation site is built with MkDocs and the Material theme.

## Quick start

No global installation needed. Use `uvx` to run MkDocs in an ephemeral
environment:

```bash
uvx --with mkdocs-material mkdocs serve
```

The site reloads automatically at `http://localhost:8000`.

## Build a static site

```bash
uvx --with mkdocs-material mkdocs build --strict
```

Output lands in `./site/`. The `--strict` flag treats warnings as errors.

## CI build

The GitHub Actions pipeline uses pinned dependencies from
`docs/requirements.txt` to ensure reproducible builds:

```yaml
pip install -r docs/requirements.txt
mkdocs build --strict
```

## Dependencies

| Package | Version | Notes |
|---|---|---|
| `mkdocs` | 1.6.1 | Pinned to 1.x (2.0 breaks plugin system) |
| `mkdocs-material` | 9.7.7 | Theme with full extension support |
| `pymdown-extensions` | 11.0.1 | Markdown extensions (superfences, tabs, etc.) |
| `markdown` | 3.10.3 | Pinned for reproducible rendering |

## Directory structure

```text
docs/
├── index.md              # Landing page
├── requirements.txt      # Pinned Python deps
├── roadmap.md            # Long-term plan
├── stylesheets/
│   └── extra.css         # Mermaid theme overrides
├── getting-started/
├── architecture/
├── api/
├── agents/
├── ingestion/
├── operations/
└── reference/
```

## Adding a page

1. Create the Markdown file under the appropriate directory
2. Add it to `nav` in `mkdocs.yml`
3. Verify with `mkdocs build --strict`
