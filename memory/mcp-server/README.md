# Open Memory MCP server

Exposes the [Open Memory Stack](../api-service) FastAPI service as native MCP
tools, so Claude Code / Codex / OpenCode can read and write long-term semantic
memory directly.

It is a thin HTTP client — it holds no state and talks only to the FastAPI
service over `OPEN_MEMORY_API_URL`.

Full documentation:
<https://harshitruwali.github.io/homelab-infra/memory/agents/>

> **Note:** `.env.example` documents the environment variables but is **not
> loaded** by the server. Pass them through the MCP client's `env` block.

## Why the agent_id matters

The memory service scopes every chunk by `(agent_id, project)`. This server
stamps its configured scope onto **every write**, so two agents that store the
same `file_path` + `chunk_index` get two independent memories instead of
silently clobbering each other. Give every connected client a distinct
`OPEN_MEMORY_AGENT_ID`.

## Tools

| Tool | Purpose |
| --- | --- |
| `memory_search` | Semantic search. Unscoped by default; `mine_only=true` restricts to this client (plus the shared `legacy` corpus unless `include_legacy=false`, which only takes effect when `mine_only` is set). |
| `memory_store` | Store/overwrite a chunk under this client's scope. |
| `memory_update` | Update a chunk this client owns; omitted fields keep their stored values. |
| `memory_delete` | Delete this client's chunks for a `file_path`. Always scoped — it cannot touch another agent's data. |
| `memory_status` | Service health plus the active `agent_id` / `project` / API URL. |

## Install

```bash
cd mcp-server
uv sync                       # or: python -m venv .venv && .venv/bin/pip install -e .
uv run open-memory-mcp        # smoke test: starts on stdio, Ctrl-C to exit
```

## Register with Claude Code

Fastest path:

```bash
claude mcp add open-memory \
  --env OPEN_MEMORY_API_URL=http://localhost:8080 \
  --env OPEN_MEMORY_AGENT_ID=claude-code \
  --env OPEN_MEMORY_PROJECT=open-memory-stack \
  -- uv run --directory /path/to/homelab-infra/memory/mcp-server open-memory-mcp
```

Or write it by hand. In `~/.claude.json` (user scope) or `.mcp.json` at the
project root (shared scope):

```json
{
  "mcpServers": {
    "open-memory": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/homelab-infra/memory/mcp-server",
        "open-memory-mcp"
      ],
      "env": {
        "OPEN_MEMORY_API_URL": "http://localhost:8080",
        "OPEN_MEMORY_AGENT_ID": "claude-code",
        "OPEN_MEMORY_PROJECT": "open-memory-stack"
      }
    }
  }
}
```

Without `uv`, point `command` at the venv interpreter instead:

```json
{
  "mcpServers": {
    "open-memory": {
      "type": "stdio",
      "command": "/path/to/homelab-infra/memory/mcp-server/.venv/bin/python",
      "args": ["-m", "open_memory_mcp.server"],
      "env": {
        "OPEN_MEMORY_API_URL": "http://localhost:8080",
        "OPEN_MEMORY_AGENT_ID": "claude-code",
        "OPEN_MEMORY_PROJECT": "open-memory-stack"
      }
    }
  }
}
```

Verify with `claude mcp list`, or `/mcp` inside a session.

## Register with OpenCode

OpenCode uses `opencode.json` (project) or `~/.config/opencode/opencode.json`
(global). Same command, different key names — `type: "local"`, `command` is a
single argv array, and the server must be explicitly `enabled`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "open-memory": {
      "type": "local",
      "command": [
        "uv",
        "run",
        "--directory",
        "/path/to/homelab-infra/memory/mcp-server",
        "open-memory-mcp"
      ],
      "enabled": true,
      "environment": {
        "OPEN_MEMORY_API_URL": "http://localhost:8080",
        "OPEN_MEMORY_AGENT_ID": "opencode",
        "OPEN_MEMORY_PROJECT": "open-memory-stack"
      }
    }
  }
}
```

## Register with Codex

Codex reads `~/.codex/config.toml`:

```toml
[mcp_servers.open-memory]
command = "uv"
args = ["run", "--directory", "/path/to/homelab-infra/memory/mcp-server", "open-memory-mcp"]

[mcp_servers.open-memory.env]
OPEN_MEMORY_API_URL = "http://localhost:8080"
OPEN_MEMORY_AGENT_ID = "codex"
OPEN_MEMORY_PROJECT = "open-memory-stack"
```

## Running several agents at once

Register the server once per agent with a different `OPEN_MEMORY_AGENT_ID`
(and `OPEN_MEMORY_PROJECT` per git worktree). Reads still see everything by
default, so agents can learn from each other; writes stay isolated.

Leaving `OPEN_MEMORY_AGENT_ID` unset falls back to the machine hostname, which
is fine for a single agent per host but defeats the purpose when several run
side by side.
