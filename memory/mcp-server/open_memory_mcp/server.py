"""
Open Memory Stack — MCP server (stdio transport).

Exposes the FastAPI memory service as native tools for Claude Code, Codex,
OpenCode, or any other MCP client.

Every write is stamped with this server's configured ``agent_id`` / ``project``,
so each connected agent automatically owns its own memory scope and cannot
overwrite another agent's chunks. Reads are unscoped by default (discovery is
usually what you want); pass ``mine_only=true`` or an explicit ``agent_id`` to
narrow them.

Configuration (environment variables)
-------------------------------------
OPEN_MEMORY_API_URL   base URL of the FastAPI service (default http://localhost:8080)
OPEN_MEMORY_AGENT_ID  scope all writes to this agent (default: the hostname)
OPEN_MEMORY_PROJECT   scope all writes to this project (default: "default")
OPEN_MEMORY_SESSION_ID optional session marker attached to writes
OPEN_MEMORY_TIMEOUT   HTTP timeout in seconds (default 60)
"""

from __future__ import annotations

import os
import socket
from typing import Any

import httpx

try:  # official SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # official SDK 1.x — same decorator/run API under the old name
    from mcp.server.fastmcp import FastMCP as _Server

# ── Configuration ─────────────────────────────────────────────────────────────

API_URL = os.environ.get("OPEN_MEMORY_API_URL", "http://localhost:8080").rstrip("/")
AGENT_ID = os.environ.get("OPEN_MEMORY_AGENT_ID") or socket.gethostname()
PROJECT = os.environ.get("OPEN_MEMORY_PROJECT") or "default"
SESSION_ID = os.environ.get("OPEN_MEMORY_SESSION_ID") or None
TIMEOUT = float(os.environ.get("OPEN_MEMORY_TIMEOUT", "60"))

# The sentinel scope holding memories ingested before multi-agent support.
LEGACY_AGENT_ID = "legacy"

mcp = _Server(
    "open-memory",
    version="0.1.0",
    instructions=(
        "Semantic long-term memory backed by Qdrant + PostgreSQL. "
        f"Writes from this client are owned by agent '{AGENT_ID}' in project "
        f"'{PROJECT}'. Use memory_search before answering questions about past "
        "work, and memory_store to persist durable facts, decisions and context."
    ),
)


# ── HTTP plumbing ─────────────────────────────────────────────────────────────

async def _request(method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, json=payload)
    except httpx.RequestError as exc:
        return {"error": f"cannot reach Open Memory API at {url}: {exc}"}

    if resp.status_code >= 400:
        return {
            "error": f"HTTP {resp.status_code} from {url}",
            "detail": resp.text[:2000],
        }
    try:
        return resp.json()
    except ValueError:
        return {"error": "non-JSON response", "detail": resp.text[:2000]}


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def memory_search(
    query: str,
    top_k: int = 5,
    mine_only: bool = False,
    include_legacy: bool = True,
    agent_id: str | list[str] | None = None,
    project: str | None = None,
    type: str | None = None,
) -> dict[str, Any]:
    """Semantic search over stored memories.

    Args:
        query: Natural-language description of what you are looking for.
        top_k: Number of chunks to return (1-20).
        mine_only: Restrict results to this client's own agent scope.
            Ignored when an explicit agent_id is given.
        include_legacy: When mine_only is set, also include the shared
            pre-multi-agent 'legacy' memories. Has no effect unless
            mine_only is true.
        agent_id: Which agent(s) wrote the memories to search. Accepts one
            name or a list — e.g. ["claude-code", "codex"] to read what your
            peers have learned. Overrides mine_only. Omit to search every
            agent (the default, and usually what you want).
        project: Restrict to a project.
        type: Restrict to a memory type, e.g. 'daily', 'project', 'thread'.
    """
    scope: str | list[str] | None = agent_id
    if scope is None and mine_only:
        scope = [AGENT_ID, LEGACY_AGENT_ID] if include_legacy else AGENT_ID

    payload = _drop_none(
        {
            "query": query,
            "top_k": max(1, min(top_k, 20)),
            "agent_id": scope,
            "project": project,
            "type": type,
        }
    )
    return await _request("POST", "/memory/search", payload)


@mcp.tool()
async def memory_store(
    file_path: str,
    chunk_text: str,
    chunk_index: int = 0,
    type: str | None = None,
    tags: list[str] | None = None,
    priority: str | None = None,
    summary: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Store (or overwrite) one memory chunk under this client's agent scope.

    Args:
        file_path: Logical path/key for the memory, e.g.
            'notes/2026-08-12-deploy.md'. Together with chunk_index and this
            client's agent scope it identifies the chunk.
        chunk_text: The text to embed and remember.
        chunk_index: Position within file_path when a document is split.
        type: Category, e.g. 'daily', 'project', 'decision'.
        tags: Free-form tags.
        priority: 'high' | 'medium' | 'low'.
        summary: Short summary of the chunk.
        project: Override the configured project scope for this write.
    """
    payload = _drop_none(
        {
            "file_path": file_path,
            "chunk_index": chunk_index,
            "chunk_text": chunk_text,
            "type": type,
            "tags": tags or [],
            "priority": priority,
            "summary": summary,
            "agent_id": AGENT_ID,
            "project": project or PROJECT,
            "session_id": SESSION_ID,
        }
    )
    return await _request("POST", "/memory/store", payload)


@mcp.tool()
async def memory_update(
    file_path: str,
    chunk_index: int = 0,
    chunk_text: str | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
    priority: str | None = None,
    summary: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Update an existing memory chunk owned by this client.

    Omitted fields keep their stored values. Omitting chunk_text re-embeds the
    existing text. Only chunks in this client's agent scope can be updated.
    """
    payload = _drop_none(
        {
            "file_path": file_path,
            "chunk_index": chunk_index,
            "chunk_text": chunk_text,
            "type": type,
            "tags": tags,
            "priority": priority,
            "summary": summary,
            "agent_id": AGENT_ID,
            "project": project or PROJECT,
            "session_id": SESSION_ID,
        }
    )
    return await _request("POST", "/memory/update", payload)


@mcp.tool()
async def memory_delete(
    file_path: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Delete every chunk this client owns for a given file_path.

    The delete is always scoped to this client's agent_id and project — it can
    never remove another agent's memories.
    """
    payload = {
        "file_path": file_path,
        "agent_id": AGENT_ID,
        "project": project or PROJECT,
        "all_scopes": False,
    }
    return await _request("DELETE", "/memory/delete", payload)


@mcp.tool()
async def memory_status() -> dict[str, Any]:
    """Report the memory service health and this client's active memory scope."""
    url = f"{API_URL}/health"
    health: dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url)
            health = resp.json() if resp.status_code < 400 else {
                "status": f"http {resp.status_code}",
                "detail": resp.text[:500],
            }
    except httpx.RequestError as exc:
        health = {"status": "unreachable", "detail": str(exc)}
    except ValueError:
        health = {"status": "bad response"}

    return {
        "api_url": API_URL,
        "agent_id": AGENT_ID,
        "project": PROJECT,
        "session_id": SESSION_ID,
        "health": health,
    }


def main() -> None:
    """Entry point — runs the server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
