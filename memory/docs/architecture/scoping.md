# Memory scoping

Every memory chunk belongs to a **scope**: `(agent_id, project)`. The scope is
folded into the chunk's identity, so two agents storing the same `file_path`
and `chunk_index` produce different IDs and do not overwrite each other.

## How chunk IDs are derived

| Scope | Hash input | Example |
|---|---|---|
| Legacy `(legacy, default)` | `"{file_path}:{chunk_index}"` | `notes/daily.md:0` |
| Any other | `"{agent_id}:{project}:{file_path}:{chunk_index}"` | `opencode:my-project:notes/daily.md:0` |

The ID is the **first 16 bytes** of `sha256(input)`, wrapped as a UUID v4 —
Qdrant point IDs must be a UUID or an unsigned integer, so the digest cannot be
used raw. This is deterministic: the same scope, path and index always produce
the same ID, which is what makes a re-ingest an update rather than a duplicate.

## Backward compatibility

Data ingested before scoping existed is assigned the sentinel scope
`("legacy", "default")`. Crucially, the chunk-ID formula for this sentinel scope
is the **original** pre-scope string. A client that never sends `agent_id` or
`project` continues to hit the same chunk IDs it always did.

## Session ID

`session_id` travels alongside every chunk as metadata. It is **not** part of
the chunk identity: two sessions of the same agent editing the same file chunk
converge on one memory, they do not accumulate duplicates.

## Defaults

When a request omits `agent_id`, it normalizes to `"legacy"`. When it omits
`project`, it normalizes to `"default"`. This keeps legacy clients working
without changes.

## Search behavior

- **Omit scope fields** → search every agent and project (default, widest net)
- **Set `agent_id`** → restrict to that agent's chunks
- **Set `agent_id` as a list** → match any agent in the list
- **Set `project`** → restrict to that project

The MCP server's `mine_only` flag is a convenience that sets
`agent_id: [this_agent, "legacy"]` so the agent sees its own memories plus the
shared legacy corpus.
