from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.scope import DEFAULT_PROJECT, LEGACY_AGENT_ID


# ── Embed ─────────────────────────────────────────────────────────────────────

class EmbedRequest(BaseModel):
    text: str = Field(..., description="Text to embed")


class EmbedResponse(BaseModel):
    vector: list[float]
    dim: int


# ── Memory store ──────────────────────────────────────────────────────────────

class MemoryStoreRequest(BaseModel):
    file_path: str = Field(..., description="Canonical path of the source markdown file")
    chunk_index: int = Field(..., ge=0)
    chunk_text: str
    type: str | None = None
    tags: list[str] = Field(default_factory=list)
    priority: str | None = None
    summary: str | None = None
    updated_at: datetime | None = None

    # ── Multi-agent scope ─────────────────────────────────────────────────────
    agent_id: str | None = Field(
        None,
        description=(
            "Owning agent. Part of the chunk identity: two agents may store the "
            "same file_path + chunk_index without clobbering each other. "
            f"Omitted → '{LEGACY_AGENT_ID}' (the pre-multi-agent data set)."
        ),
    )
    project: str | None = Field(
        None,
        description=(
            "Project / worktree the memory belongs to. Part of the chunk identity, "
            "so the same agent can keep per-worktree memories of identical relative paths."
        ),
    )
    session_id: str | None = Field(
        None,
        description="Free-form session marker. Metadata only — NOT part of the chunk identity.",
    )


class MemoryStoreResponse(BaseModel):
    chunk_id: str
    upserted: bool
    agent_id: str = LEGACY_AGENT_ID
    project: str = DEFAULT_PROJECT
    session_id: str | None = None


# ── Memory search ─────────────────────────────────────────────────────────────

class MemorySearchRequest(BaseModel):
    query: str = Field(..., description="Natural-language query for semantic search")
    top_k: int = Field(5, ge=1, le=20)
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional Qdrant payload filters, e.g. {'type': 'project'}. "
            "A list value matches any of its members, e.g. {'agent_id': ['a', 'legacy']}."
        ),
    )

    # ── Multi-agent scope (all optional: omit to search across every scope) ────
    agent_id: str | list[str] | None = Field(
        None,
        description=(
            "Restrict to one agent, or to any agent in the list. "
            "Omitted → search every agent (backward-compatible default)."
        ),
    )
    project: str | list[str] | None = Field(
        None, description="Restrict to one project, or to any project in the list."
    )
    session_id: str | list[str] | None = Field(
        None, description="Restrict to one session, or to any session in the list."
    )
    type: str | list[str] | None = Field(
        None, description="Restrict to one memory type, or to any type in the list."
    )


class MemoryChunkResult(BaseModel):
    chunk_id: str
    file_path: str
    chunk_index: int
    chunk_text: str
    score: float
    type: str | None
    tags: list[str]
    priority: str | None
    summary: str | None
    updated_at: datetime | None
    agent_id: str = LEGACY_AGENT_ID
    project: str = DEFAULT_PROJECT
    session_id: str | None = None


class MemorySearchResponse(BaseModel):
    chunks: list[MemoryChunkResult]
    total: int


# ── Memory update ─────────────────────────────────────────────────────────────

class MemoryUpdateRequest(BaseModel):
    file_path: str
    chunk_index: int
    chunk_text: str | None = None
    type: str | None = None
    tags: list[str] | None = None
    priority: str | None = None
    summary: str | None = None
    updated_at: datetime | None = None

    agent_id: str | None = Field(None, description="Scope of the chunk being updated.")
    project: str | None = Field(None, description="Scope of the chunk being updated.")
    session_id: str | None = None


# ── Memory delete ─────────────────────────────────────────────────────────────

class MemoryDeleteRequest(BaseModel):
    file_path: str = Field(..., description="Delete chunks for this file path")
    agent_id: str | None = Field(
        None,
        description=(
            "Only chunks owned by this agent are deleted. Deleting is scoped by "
            "default so one agent cannot wipe another agent's memory of a "
            "same-named file. Ignored when all_scopes is true."
        ),
    )
    project: str | None = Field(None, description="Project scope to delete within.")
    all_scopes: bool = Field(
        False,
        description=(
            "DANGEROUS: ignore agent_id/project and delete this file_path for every "
            "agent and project. Must be requested explicitly."
        ),
    )


class MemoryDeleteResponse(BaseModel):
    file_path: str
    deleted_chunks: int
    agent_id: str | None = None
    project: str | None = None
    all_scopes: bool = False


# ── LLM infer ─────────────────────────────────────────────────────────────────

class LLMMessage(BaseModel):
    role: str  # system / user / assistant / tool
    content: str


class LLMInferRequest(BaseModel):
    messages: list[LLMMessage]
    stream: bool = True
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(2048, ge=1)
    model: str = "default"


# ── Health ────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    postgres: str
    qdrant: str
    qdrant_collection: str | None = None
    configured_vector_dim: int | None = None
    collection_vector_dim: int | None = None
