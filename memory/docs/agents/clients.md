# Client Configuration

Each MCP client connects as a distinct agent. The key configuration is the
`(agent_id, project)` pair that scopes all writes.

## Agent ID

`OPEN_MEMORY_AGENT_ID` identifies the owning agent. If omitted, the server
uses the machine's hostname. Use a stable, descriptive name:

| Agent | Recommended `agent_id` |
|---|---|
| OpenCode | `opencode` |
| Claude Code | `claude-code` |
| Codex | `codex` |

The agent ID is part of the chunk identity. Changing it means the agent will
start writing to a new scope and will no longer find its old memories by
default.

## Project

`OPEN_MEMORY_PROJECT` groups memories by project or worktree. The same agent
can maintain separate memories for different projects:

```bash
# Agent working on project A
OPEN_MEMORY_AGENT_ID=opencode OPEN_MEMORY_PROJECT=project-a uv run open-memory-mcp

# Agent working on project B
OPEN_MEMORY_AGENT_ID=opencode OPEN_MEMORY_PROJECT=project-b uv run open-memory-mcp
```

The chunks are stored under different scopes and will not collide, even though
the agent ID is the same.

## Session ID

`OPEN_MEMORY_SESSION_ID` is optional metadata attached to every write. It is
not part of the chunk identity -- two sessions writing the same chunk converge
on one memory. Use it to trace which session last updated a chunk.

## API URL

When the FastAPI service runs on a non-default port or a remote host, set
`OPEN_MEMORY_API_URL` accordingly:

```bash
OPEN_MEMORY_API_URL=http://memory-host.example.com:8088 uv run open-memory-mcp
```
