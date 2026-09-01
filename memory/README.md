# Open Memory Stack

Self-hosted semantic memory for AI applications and coding agents. PostgreSQL
for structured metadata, Qdrant for vector search, and a FastAPI service as the
only boundary anything else talks to.

**📖 Documentation: <https://harshitruwali.github.io/homelab-infra/memory/>**

## Why

Most AI apps are useful only for the length of a single chat. This is a small,
self-hosted memory layer that persists context, searches it semantically, and
serves it to local or remote models over a plain HTTP API — with enough
multi-agent scoping that several coding agents can share one store without
overwriting each other.

## Quick start

```bash
cp .env.example .env          # then fill in POSTGRES_PASSWORD
mkdir -p memory-service/data/{qdrant,postgres,redis} api-service/logs
docker compose up -d --build
```

The API listens on `http://localhost:8088`, with Swagger UI at `/docs`.

You also need an embedding model reachable at `AI_VM_HOST:EMBED_PORT` exposing
llama.cpp's native `POST /embedding` — the stack embeds nothing itself. Without
one, `/health` is green but every write returns 502.

Full walkthrough: [Getting started](https://harshitruwali.github.io/homelab-infra/memory/getting-started/).

## What's here

| Path | Contents |
|---|---|
| `docker-compose.yml` | all-in-one stack: Postgres, Qdrant, Redis, API |
| `api-service/` | the FastAPI app, Dockerfile, Alembic migrations, uv project |
| `memory-service/` | datastore-only Compose stack, for split deployments |
| `mcp-server/` | MCP server exposing the API as agent tools |
| `scripts/` | daily ingestion pipeline and a CLI client |
| `docs/` | the MkDocs site published to GitHub Pages |

## Endpoints

```text
GET    /health          POST /memory/store    POST /memory/update
POST   /embed           POST /memory/search   DELETE /memory/delete
POST   /llm/infer
```

Details in the [API reference](https://harshitruwali.github.io/homelab-infra/memory/api/).

## Multi-agent scoping

Every chunk is owned by an `(agent_id, project)` scope folded into its ID, so
two agents storing the same `file_path` do not clobber each other. Requests that
omit a scope get the sentinel `("legacy", "default")` and keep the original
pre-scope chunk IDs, so nothing had to be re-embedded when scoping was added.

See [Memory scoping](https://harshitruwali.github.io/homelab-infra/memory/architecture/scoping/).

## Development

```bash
cd api-service
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Add dependencies with `uv add`; commit `pyproject.toml` and `uv.lock` together.

## Docs

```bash
uvx --with mkdocs-material mkdocs serve    # preview on :8000
```

`.github/workflows/deploy-docs.yml` builds with `--strict` on pull requests and
publishes to GitHub Pages on push to `master`. Pages must be enabled once by
hand: **Settings → Pages → Source: GitHub Actions**.

## Notes

- Secrets live in local `.env` files and are not committed.
- Runtime data under `memory-service/data/` is gitignored, and `docker compose down -v`
  does **not** delete it — those are bind mounts.
- Redis is provisioned by the Compose files but no code path uses it yet.
