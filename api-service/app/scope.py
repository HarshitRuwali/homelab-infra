"""
Multi-agent scoping constants and helpers.

Every memory chunk belongs to a *scope*: ``(agent_id, project)``.  ``session_id``
travels alongside as metadata but is deliberately NOT part of the identity of a
chunk — two sessions of the same agent editing the same file chunk should
converge on one memory, not accumulate duplicates.

Backward compatibility
----------------------
The collection was originally populated by a single agent that sent no scope at
all.  Those rows/points are assigned the sentinel scope
``(LEGACY_AGENT_ID, DEFAULT_PROJECT)`` and, crucially, the chunk-ID derivation
keeps its *original* pre-scope formula for exactly that sentinel scope.  A
client that never learned about scoping therefore keeps hitting the very same
chunk IDs it always did.
"""

from __future__ import annotations

# Sentinel scope applied to pre-multi-agent data and to any request that omits
# a scope.  Do not change these without a data migration: they are baked into
# the Postgres server defaults, the Qdrant payload backfill, and the legacy
# chunk-ID branch.
LEGACY_AGENT_ID = "legacy"
DEFAULT_PROJECT = "default"


def normalize_agent_id(agent_id: str | None) -> str:
    """Empty / missing agent_id collapses to the legacy sentinel."""
    return (agent_id or "").strip() or LEGACY_AGENT_ID


def normalize_project(project: str | None) -> str:
    """Empty / missing project collapses to the default sentinel."""
    return (project or "").strip() or DEFAULT_PROJECT


def is_legacy_scope(agent_id: str, project: str) -> bool:
    """True when this scope must reuse the original, pre-scope chunk-ID formula."""
    return agent_id == LEGACY_AGENT_ID and project == DEFAULT_PROJECT


__all__ = [
    "LEGACY_AGENT_ID",
    "DEFAULT_PROJECT",
    "normalize_agent_id",
    "normalize_project",
    "is_legacy_scope",
]
