# Running Several Agents

Multiple agents can share the same memory stack safely. Each agent's writes are
scoped by `(agent_id, project)`, so they cannot overwrite each other's chunks.

## Cross-agent discovery

By default, `memory_search` is unscoped. An agent searching for "how to deploy"
will find memories stored by any agent. This is intentional: agents benefit
from learning what their peers already know.

To restrict search to the agent's own scope, use the `mine_only` flag (MCP
tool) or set `agent_id` in the API request.

## Legacy memories

Memories ingested before multi-agent support used a flat namespace. They are
assigned the sentinel scope `("legacy", "default")`. When `mine_only` is set,
legacy memories are included by default so an agent does not lose access to the
shared corpus. Set `include_legacy: false` to see only the agent's own chunks.

!!! bug "`include_legacy` only does anything when `mine_only` is true"
    The flag is read inside the `mine_only` branch. On a default, unscoped
    search — where results already span every agent, legacy included —
    `include_legacy: false` is silently ignored rather than filtering legacy
    out. To exclude the legacy corpus you must narrow explicitly, e.g.
    `agent_id: ["claude-code", "codex"]`.

## Practical setup

```bash
# Terminal 1: OpenCode on project A
cd mcp-server
OPEN_MEMORY_AGENT_ID=opencode OPEN_MEMORY_PROJECT=project-a uv run open-memory-mcp

# Terminal 2: Claude Code on project B
cd mcp-server
OPEN_MEMORY_AGENT_ID=claude-code OPEN_MEMORY_PROJECT=project-b uv run open-memory-mcp
```

Both agents share the same Qdrant collection and PostgreSQL database. Their
writes are isolated by scope, but their searches span everything.

## Deleting safely

The MCP `memory_delete` tool is always scoped to the calling agent. It cannot
remove another agent's chunks. The raw API's `DELETE /memory/delete` supports
`all_scopes: true`, but this is a deliberate power-user option and is not
exposed through the MCP server.
