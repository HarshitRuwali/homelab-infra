from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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


class MemoryStoreResponse(BaseModel):
    chunk_id: str
    upserted: bool


# ── Memory search ─────────────────────────────────────────────────────────────

class MemorySearchRequest(BaseModel):
    query: str = Field(..., description="Natural-language query for semantic search")
    top_k: int = Field(5, ge=1, le=20)
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional Qdrant payload filters, e.g. {'type': 'project'}",
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


# ── Memory delete ─────────────────────────────────────────────────────────────

class MemoryDeleteRequest(BaseModel):
    file_path: str = Field(..., description="Delete all chunks for this file path")


class MemoryDeleteResponse(BaseModel):
    file_path: str
    deleted_chunks: int


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
