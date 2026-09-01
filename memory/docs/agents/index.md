# Agents

Open Memory Stack exposes its memory service as native MCP tools, so coding
agents like Claude Code, Codex, and OpenCode can read and write long-term
memory directly during a session.

## How it works

The MCP server is a thin stdio-based client that wraps the FastAPI REST API.
Each connected agent gets its own `(agent_id, project)` scope, so agents cannot
overwrite each other's memories.

```text
Claude Code / Codex / OpenCode
         │
         │  stdio (MCP protocol)
         ▼
  open-memory-mcp
         │
         │  HTTP
         ▼
  FastAPI :8080
```

## Available tools

| Tool | Description |
|---|---|
| `memory_search` | Semantic search across stored memories |
| `memory_store` | Store or overwrite a memory chunk |
| `memory_update` | Partially update an existing chunk |
| `memory_delete` | Delete chunks (scoped to this agent only) |
| `memory_status` | Check service health and active scope |

## Scope isolation

Every write is automatically stamped with this server's `agent_id` and
`project`. The `memory_delete` tool is always scoped -- it cannot remove
another agent's chunks. `memory_search` is unscoped by default, so an agent
can discover what its peers have learned.
