# Installing the MCP Server

The MCP server is a Python package installed with `uv`.

## Prerequisites

- `uv` package manager
- A running Open Memory Stack API (default: `http://localhost:8080`)

## Install

```bash
cd mcp-server
uv sync
```

This creates a virtual environment and installs dependencies (`mcp` SDK, `httpx`).

## Run

```bash
uv run open-memory-mcp
```

The server runs over stdio and is designed to be launched by an MCP client.
Do not run it interactively.

## Configuration

All configuration is via environment variables.

!!! warning "The server does not read a `.env` file"
    `mcp-server/.env.example` documents the knobs, but nothing loads it. The
    values must be passed by the MCP client, in the `env` block of its config.
    Copying it to `.env` has no effect.

| Variable | Default | Description |
|---|---|---|
| `OPEN_MEMORY_API_URL` | `http://localhost:8080` | FastAPI base URL |
| `OPEN_MEMORY_AGENT_ID` | hostname | Owning agent for all writes |
| `OPEN_MEMORY_PROJECT` | `default` | Owning project for all writes |
| `OPEN_MEMORY_SESSION_ID` | *(none)* | Optional session marker |
| `OPEN_MEMORY_TIMEOUT` | `60` | HTTP timeout in seconds |

## Wiring into an agent

Each client has its own config file **and its own key names**. They are not
interchangeable — the three blocks below differ by more than indentation.

=== "Claude Code"

    Fastest path is the CLI:

    ```bash
    claude mcp add open-memory \
      --env OPEN_MEMORY_API_URL=http://localhost:8080 \
      --env OPEN_MEMORY_AGENT_ID=claude-code \
      --env OPEN_MEMORY_PROJECT=open-memory-stack \
      -- uv run --directory /path/to/homelab-infra/memory/mcp-server open-memory-mcp
    ```

    Or by hand, in `~/.claude.json` (user scope) or `.mcp.json` at the project
    root (shared scope):

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

    !!! warning "Not `~/.claude/settings.json`"
        MCP servers are configured in `~/.claude.json` or `.mcp.json`.
        `settings.json` is a different file and a server placed there is
        silently ignored.

    Verify with `claude mcp list`, or `/mcp` inside a session.

=== "OpenCode"

    `opencode.json` (project) or `~/.config/opencode/opencode.json` (global).
    The key is `mcp`, not `mcpServers`; `command` is a single argv array;
    environment goes in `environment`; and the server must be explicitly
    `enabled`:

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

=== "Codex"

    `~/.codex/config.toml`:

    ```toml
    [mcp_servers.open-memory]
    command = "uv"
    args = ["run", "--directory", "/path/to/homelab-infra/memory/mcp-server", "open-memory-mcp"]

    [mcp_servers.open-memory.env]
    OPEN_MEMORY_API_URL = "http://localhost:8080"
    OPEN_MEMORY_AGENT_ID = "codex"
    OPEN_MEMORY_PROJECT = "open-memory-stack"
    ```

Without `uv`, point `command` at the venv interpreter instead and run the
module directly:

```json
{
  "command": "/path/to/homelab-infra/memory/mcp-server/.venv/bin/python",
  "args": ["-m", "open_memory_mcp.server"]
}
```

Give every client a **distinct** `OPEN_MEMORY_AGENT_ID` — see
[Running several agents](multi-agent.md).
