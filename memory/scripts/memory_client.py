#!/usr/bin/env python3
"""
Memory Layer Client for Open Memory Stack.

Simple utilities to:
  - Search memory semantically
  - Store new chunks
  - Query what's been ingested
"""

import asyncio
import json
import sys
from typing import Any

import httpx

MEMORY_API_URL = "http://localhost:8080"


async def search_memory(query: str, top_k: int = 5, filters: dict[str, Any] | None = None) -> dict:
    """Search the memory layer semantically."""
    payload = {
        "query": query,
        "top_k": top_k,
        "filters": filters or {},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{MEMORY_API_URL}/memory/search", json=payload)
        resp.raise_for_status()
        return resp.json()


async def store_chunk(
    chunk_text: str,
    file_path: str,
    chunk_index: int = 0,
    tags: list[str] | None = None,
    chunk_type: str = "manual",
    summary: str | None = None,
    priority: str | None = None,
) -> dict:
    """Store a single chunk in memory."""
    payload = {
        "file_path": file_path,
        "chunk_index": chunk_index,
        "chunk_text": chunk_text,
        "type": chunk_type,
        "tags": tags or [],
        "priority": priority,
        "summary": summary,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{MEMORY_API_URL}/memory/store", json=payload)
        resp.raise_for_status()
        return resp.json()


async def health_check() -> dict:
    """Check if the memory service is healthy."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{MEMORY_API_URL}/health")
        resp.raise_for_status()
        return resp.json()


async def main():
    """CLI interface for memory queries."""
    if len(sys.argv) < 2:
        print(
            "Usage:\n"
            "  python3 memory_client.py health\n"
            "  python3 memory_client.py search <query> [--top-k 5] [--filter key:value]\n"
            "  python3 memory_client.py store <file_path> <text> [--type TYPE] [--tags tag1,tag2]\n"
        )
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "health":
        result = await health_check()
        print(json.dumps(result, indent=2))

    elif cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        top_k = 5
        filters = {}

        # Parse optional arguments
        for i in range(3, len(sys.argv)):
            if sys.argv[i] == "--top-k" and i + 1 < len(sys.argv):
                top_k = int(sys.argv[i + 1])
            elif sys.argv[i].startswith("--filter"):
                # Parse filter like --filter type:persona
                parts = sys.argv[i + 1].split(":")
                if len(parts) == 2:
                    filters[parts[0]] = parts[1]

        result = await search_memory(query, top_k=top_k, filters=filters)
        print(json.dumps(result, indent=2))

    elif cmd == "store":
        file_path = sys.argv[2] if len(sys.argv) > 2 else ""
        text = sys.argv[3] if len(sys.argv) > 3 else ""
        chunk_type = "manual"
        tags = []

        # Parse optional arguments
        for i in range(4, len(sys.argv)):
            if sys.argv[i] == "--type" and i + 1 < len(sys.argv):
                chunk_type = sys.argv[i + 1]
            elif sys.argv[i] == "--tags" and i + 1 < len(sys.argv):
                tags = sys.argv[i + 1].split(",")

        result = await store_chunk(text, file_path, tags=tags, chunk_type=chunk_type)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
