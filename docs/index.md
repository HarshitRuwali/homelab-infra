# Open Memory Stack

Self-hosted semantic memory for AI applications and coding agents. PostgreSQL
for structured metadata, Qdrant for vector search, and a FastAPI service as the
only boundary anything else talks to.

## What it does

<div class="grid cards" markdown>

- :material-database-outline: **Remembers**

    Text goes in as chunks, comes back by meaning rather than by keyword. The
    metadata lives in PostgreSQL, the vectors in Qdrant, and one write keeps
    both consistent.

- :material-account-multiple-outline: **Keeps agents apart**

    Every chunk is owned by an `(agent_id, project)` scope folded into its ID,
    so two agents storing the same path do not overwrite each other.

- :material-connection: **Plugs into agents**

    An MCP server exposes the service as native tools, so Claude Code, Codex
    and OpenCode read and write long-term memory directly.

- :material-shield-check-outline: **Fails loudly**

    A vector-dimension mismatch refuses to start rather than quietly corrupting
    the collection, and `/health` reports the divergence.

</div>

## Start here

!!! tip "New to the stack?"
    **[Getting started](getting-started/index.md)** runs the whole thing on one
    machine with Docker Compose, then walks a memory in and back out again:
    [One machine](getting-started/one-machine.md) →
    [Configuration](getting-started/configuration.md) →
    [Your first memory](getting-started/first-memory.md).

| If you want to… | Go to |
|---|---|
| Run everything on one box | [One machine](getting-started/one-machine.md) |
| Split datastores and API across hosts | [Split deployment](getting-started/split-deployment.md) |
| Understand what a request actually does | [Request flow](architecture/request-flow.md) |
| Know why two agents do not clobber each other | [Memory scoping](architecture/scoping.md) |
| Look up a request or response body | [API](api/index.md) |
| Give a coding agent memory | [Agents](agents/index.md) |
| Feed the stack automatically every day | [Ingestion](ingestion/index.md) |
| Fix something that broke | [Troubleshooting](operations/troubleshooting.md) |
| Look up an environment variable | [Environment](reference/environment.md) |
| Read the long-term plan | [Roadmap](roadmap.md) |

## The one-command version

```bash
cp .env.example .env          # then fill in POSTGRES_PASSWORD
mkdir -p memory-lxc/data/{qdrant,postgres,redis} fastapi-lxc/logs
docker compose up -d --build
```

The API comes up on `http://localhost:8088`, with interactive Swagger docs at
`/docs`.

!!! warning "It needs an embedding model to be useful"
    The stack embeds nothing itself. It proxies to a model you run — see
    [Prerequisites](getting-started/index.md#prerequisites). Without one,
    `/health` is green but every `/memory/store` returns **502**.

## Design commitments

These are the decisions the rest of the system follows from. Each one exists
because the alternative broke something.

**A chunk's identity includes its owner.** `chunk_id` used to be
`sha256(file_path + ":" + chunk_index)`. Two agents storing the same relative
path — trivially easy across git worktrees — produced the same ID, and the
upsert silently overwrote one with the other. The scope is now part of the
hash. See [Memory scoping](architecture/scoping.md).

**Backward compatibility is a hash branch, not a migration.** Data written
before scoping existed keeps the *original* pre-scope ID formula under the
sentinel scope `("legacy", "default")`, so nothing had to be re-embedded or
re-keyed. Clients that never learned about scoping still address the exact same
chunks.

**Reads are open, writes are owned.** Search spans every scope unless you
narrow it, so agents can learn from each other. Deletes are scoped by default
and need an explicit `all_scopes` to cross that line.

**A dimension mismatch is a startup failure.** Pointing a `VECTOR_DIM=768`
deployment at a live 1024-dim collection used to start cleanly and then write
garbage. It now refuses to serve. See
[Vector dimensions](operations/vector-dimensions.md).

**Secrets stay in local `.env` files.** Nothing in this repository holds a
credential, and the docs pipeline holds no repository secret either.
