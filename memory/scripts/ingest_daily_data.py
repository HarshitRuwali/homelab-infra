#!/usr/bin/env python3
"""
Daily Data Ingestion Pipeline for Open Memory Stack.

Collects daily context from:
  - Tasks (top 4 daily list)
  - Work progress (from cron output)
  - Persona/identity (from Hermes memory files)
  - Active projects (from ~/projects)
  - Obsidian daily notes

All data is chunked, tagged, and ingested into the memory layer via /memory/store.
"""

import asyncio
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# ── Configuration ──────────────────────────────────────────────────────────

MEMORY_API_URL = "http://localhost:8080"
# Give up once this many chunks fail back-to-back. Each failed store burns the
# server-side 30s embed timeout, so without this a dead AI VM costs
# len(all_chunks) * 30s and blows the cron timeout instead of reporting.
MAX_CONSECUTIVE_FAILURES = 3
# The embed model (bge-large-en-v1.5) has 512 trained position embeddings and
# rejects longer inputs — it does not truncate. Dense content (JSON, code) runs
# ~1.26 chars/token, so 1000 chars could reach ~794 tokens and fail. 500 chars
# stays under the cap even at a pathological 1 char/token.
MAX_CHUNK_CHARS = 500
HERMES_HOME = Path.home() / ".hermes"
OBSIDIAN_HOME = Path.home() / "obsidian-self"
PROJECTS_HOME = Path.home()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ── Utilities ──────────────────────────────────────────────────────────────

async def store_chunk(
    chunk_text: str,
    file_path: str,
    chunk_index: int,
    tags: list[str],
    chunk_type: str = "daily_data",
    summary: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    """Post a memory chunk to /memory/store."""
    payload = {
        "file_path": file_path,
        "chunk_index": chunk_index,
        "chunk_text": chunk_text,
        "type": chunk_type,
        "tags": tags,
        "priority": priority,
        "summary": summary,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            resp = await client.post(f"{MEMORY_API_URL}/memory/store", json=payload)
            resp.raise_for_status()
            result = resp.json()
            logger.info(
                f"✓ Stored chunk {chunk_index} of {file_path}: {result.get('chunk_id', 'unknown')}"
            )
            return result
        except httpx.HTTPStatusError as e:
            logger.error(f"✗ HTTP error storing chunk: {e.response.status_code} {e.response.text}")
            raise
        except httpx.RequestError as e:
            logger.error(f"✗ Connection error: {e}")
            raise


# ── Data Collection ────────────────────────────────────────────────────────

def _chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split large text into chunks to stay under embedding token limits."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        # A paragraph can blow the cap on its own (no blank line to split on),
        # in which case it used to pass through whole. Hard-split it: bge-large
        # rejects anything over 512 tokens outright rather than truncating.
        while len(paragraph) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        if len(current) + len(paragraph) + 2 <= max_chars:
            current += paragraph + "\n\n"
        else:
            if current.strip():
                chunks.append(current.strip())
            current = paragraph + "\n\n"

    if current.strip():
        chunks.append(current.strip())
    
    return chunks if chunks else [text[:max_chars]]


async def collect_persona_data() -> list[dict[str, Any]]:
    """Collect persona/identity from Hermes memory files."""
    chunks = []
    memory_files = [
        "MEMORY.md",
        "USER.md",
        "MEMORY_CAREER.md",
        "MEMORY_PROJECTS.md",
    ]

    for filename in memory_files:
        file_path = HERMES_HOME / "memories" / filename
        if not file_path.exists():
            continue

        content = file_path.read_text()
        if not content.strip():
            continue

        # Smart chunking for large files
        text_chunks = _chunk_text(content)
        for text_chunk in text_chunks:
            chunks.append({
                "text": text_chunk,
                "source_file": f"hermes/memories/{filename}",
                "type": "persona",
                "tags": ["persona", "identity", "context"],
                "priority": "high",
            })

    return chunks


async def collect_tasks_data() -> list[dict[str, Any]]:
    """Collect current tasks (placeholder; integrate with your task system)."""
    chunks = []

    # Try to read from Hermes state or recent session output
    state_file = HERMES_HOME / "state.db"
    if state_file.exists():
        # For now, we'll document the collection point
        chunks.append({
            "text": "Daily task collection point - integrate with task tracking system",
            "source_file": "hermes/state",
            "type": "tasks",
            "tags": ["tasks", "daily", "planning"],
            "priority": "high",
        })

    return chunks


async def collect_work_progress_data() -> list[dict[str, Any]]:
    """Collect recent work progress from cron outputs or project directories."""
    chunks = []

    # Scan recent git commits
    projects = ["aegis", "pgpulse", "open-memory-stack", "harshitruwali.github.io"]
    for proj_name in projects:
        proj_path = PROJECTS_HOME / proj_name
        if not proj_path.exists():
            continue

        git_dir = proj_path / ".git"
        if not git_dir.exists():
            continue

        try:
            import subprocess
            result = subprocess.run(
                ["git", "log", "--oneline", "-10"],
                cwd=proj_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                commits = result.stdout.strip()
                if commits:
                    chunks.append({
                        "text": f"Recent commits in {proj_name}:\n{commits}",
                        "source_file": f"projects/{proj_name}/.git",
                        "type": "work_progress",
                        "tags": ["work", "git", proj_name],
                        "priority": "medium",
                    })
        except Exception as e:
            logger.warning(f"Could not collect git data for {proj_name}: {e}")

    return chunks


async def collect_daily_notes() -> list[dict[str, Any]]:
    """Collect recent Obsidian daily notes."""
    chunks = []

    # Look for daily note patterns in Obsidian
    daily_patterns = ["Daily", "Journal", "Log"]
    for folder_name in daily_patterns:
        folder_path = OBSIDIAN_HOME / folder_name
        if not folder_path.exists():
            continue

        # Find markdown files modified today
        today = datetime.now().date()
        for md_file in folder_path.glob("*.md"):
            mtime = datetime.fromtimestamp(md_file.stat().st_mtime).date()
            if mtime == today:
                content = md_file.read_text()
                if content.strip():
                    chunks.append({
                        "text": content,
                        "source_file": f"obsidian/{folder_name}/{md_file.name}",
                        "type": "daily_notes",
                        "tags": ["obsidian", "daily", "notes"],
                        "priority": "medium",
                    })

    return chunks


async def collect_random_projects() -> list[dict[str, Any]]:
    """Collect current README or context from active projects."""
    chunks = []

    projects = ["aegis", "pgpulse", "open-memory-stack"]
    for proj_name in projects:
        proj_path = PROJECTS_HOME / proj_name
        if not proj_path.exists():
            continue

        readme_path = proj_path / "README.md"
        if readme_path.exists():
            content = readme_path.read_text()
            # Smart chunking to avoid token limits
            text_chunks = _chunk_text(content)
            for text_chunk in text_chunks:
                chunks.append({
                    "text": text_chunk,
                    "source_file": f"projects/{proj_name}/README.md",
                    "type": "project_context",
                    "tags": ["projects", proj_name],
                    "priority": "low",
                })

    return chunks


# ── Main Ingestion Logic ───────────────────────────────────────────────────

async def ingest_all_data():
    """Collect and ingest all daily data into memory layer."""
    logger.info("Starting daily data ingestion pipeline...")

    try:
        # Collect all data sources
        logger.info("Collecting persona data...")
        persona = await collect_persona_data()

        logger.info("Collecting tasks...")
        tasks = await collect_tasks_data()

        logger.info("Collecting work progress...")
        progress = await collect_work_progress_data()

        logger.info("Collecting daily notes...")
        notes = await collect_daily_notes()

        logger.info("Collecting project context...")
        projects = await collect_random_projects()

        all_chunks = persona + tasks + progress + notes + projects
        logger.info(f"Collected {len(all_chunks)} data chunks")

        # Ingest each chunk
        stored_count = 0
        failed_count = 0
        consecutive_failures = 0
        aborted = False

        for idx, chunk_data in enumerate(all_chunks):
            try:
                await store_chunk(
                    chunk_text=chunk_data["text"],
                    file_path=chunk_data["source_file"],
                    chunk_index=idx,
                    tags=chunk_data.get("tags", []),
                    chunk_type=chunk_data.get("type", "daily_data"),
                    summary=chunk_data.get("summary"),
                    priority=chunk_data.get("priority"),
                )
                stored_count += 1
                consecutive_failures = 0
            except Exception as e:
                logger.error(f"Failed to store chunk {idx}: {e}")
                failed_count += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    remaining = len(all_chunks) - idx - 1
                    logger.error(
                        f"✗ Aborting: {consecutive_failures} consecutive store failures "
                        f"(backend looks down) — last error: {e}. "
                        f"{remaining} chunks skipped."
                    )
                    aborted = True
                    break

        if aborted:
            logger.error(
                f"✗ Ingestion aborted early: {stored_count} stored, {failed_count} failed, "
                f"{len(all_chunks)} total"
            )
            sys.exit(1)

        logger.info(f"✓ Ingestion complete: {stored_count} stored, {failed_count} failed")
        return {"stored": stored_count, "failed": failed_count, "total": len(all_chunks)}

    except Exception as e:
        logger.error(f"✗ Ingestion pipeline failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    result = asyncio.run(ingest_all_data())
    print(json.dumps(result))
