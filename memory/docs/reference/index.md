# Reference

Look-up material. Nothing here is a walkthrough; each page is the complete list
of one kind of thing.

<div class="grid cards" markdown>

- :material-tune-variant: **[Environment variables](environment.md)**

    Every variable the stack reads, its default, and which service consumes it.

- :material-database-outline: **[Database schema](schema.md)**

    The PostgreSQL tables and the Qdrant collection, and how a chunk maps
    across both.

- :material-book-open-page-variant: **[Building the docs](tooling.md)**

    Serving this site locally, the pinned toolchain, and how it is published.

</div>

## Where the authoritative value lives

When a page here and the code disagree, the code wins. These are the files to
check first:

| Question | File |
|---|---|
| What does this variable default to? | `api-service/.env.example`, `.env.example` |
| What ports are published? | `docker-compose.yml` |
| What shape is this table? | `api-service/alembic/versions/` |
| What tools does an agent see? | `mcp-server/open_memory_mcp/server.py` |
