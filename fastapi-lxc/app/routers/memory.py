"""
/memory/*  — semantic memory CRUD operations backed by Qdrant + PostgreSQL.

Multi-agent scoping
-------------------
Every chunk is owned by a scope ``(agent_id, project)``. That scope is folded
into the chunk ID, the Qdrant payload and the Postgres rows, so two agents that
store the same ``file_path`` + ``chunk_index`` no longer overwrite one another.
See ``app/scope.py`` for the backward-compatibility contract.
"""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from qdrant_client.models import (
    Filter,
    FieldCondition,
    FilterSelector,
    MatchAny,
    MatchValue,
    PointStruct,
)
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import MemoryChunk, MemoryFile
from app.qdrant_store import get_qdrant, ensure_collection
from app.schemas import (
    MemoryChunkResult,
    MemoryDeleteRequest,
    MemoryDeleteResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    MemoryStoreRequest,
    MemoryStoreResponse,
    MemoryUpdateRequest,
)
from app.scope import (
    DEFAULT_PROJECT,
    LEGACY_AGENT_ID,
    is_legacy_scope,
    normalize_agent_id,
    normalize_project,
)

router = APIRouter(prefix="/memory", tags=["memory"])
settings = get_settings()


def _chunk_id(
    file_path: str,
    chunk_index: int,
    agent_id: str = LEGACY_AGENT_ID,
    project: str = DEFAULT_PROJECT,
) -> str:
    """Return a deterministic UUID string identifying a chunk within its scope.

    Backward compatibility: for the legacy sentinel scope the hash input is the
    ORIGINAL pre-scope string ``"{file_path}:{chunk_index}"``, so the 2000-odd
    chunks ingested before scoping existed keep their IDs and their Qdrant
    points stay bound to their Postgres rows. Every other scope hashes
    ``"{agent_id}:{project}:{file_path}:{chunk_index}"``, which is what makes
    concurrent agents collision-free.
    """
    agent_id = normalize_agent_id(agent_id)
    project = normalize_project(project)
    if is_legacy_scope(agent_id, project):
        key = f"{file_path}:{chunk_index}"
    else:
        key = f"{agent_id}:{project}:{file_path}:{chunk_index}"
    raw = hashlib.sha256(key.encode()).digest()[:16]
    return str(uuid.UUID(bytes=raw, version=4))


def _match(value: Any) -> MatchValue | MatchAny:
    """Single value → exact match; list/tuple/set → match-any."""
    if isinstance(value, (list, tuple, set)):
        return MatchAny(any=list(value))
    return MatchValue(value=value)


def _build_filter(conditions: dict[str, Any]) -> Filter | None:
    """Turn a flat {payload_key: value | [values]} mapping into a Qdrant filter."""
    clauses = [
        FieldCondition(key=key, match=_match(value))
        for key, value in conditions.items()
        if value is not None
    ]
    return Filter(must=clauses) if clauses else None


async def _embed(text: str) -> list[float]:
    """Call the AI VM embed endpoint and return the vector."""
    url = f"{settings.embed_url}/embedding"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json={"content": text})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Embed model error {exc.response.status_code}: {exc.response.text}",
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach AI VM: {exc}")

    data = resp.json()
    # Parse bge-large / llama.cpp embed response formats
    vector = None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        emb = data[0].get("embedding", [])
        if isinstance(emb, list) and len(emb) == 1 and isinstance(emb[0], list):
            vector = emb[0]  # [[vec]] → [vec]
        elif isinstance(emb, list) and emb and isinstance(emb[0], (int, float)):
            vector = emb
    elif isinstance(data, dict):
        vector = data.get("embedding") or data.get("data", [{}])[0].get("embedding", [])
        if isinstance(vector, list) and vector and isinstance(vector[0], list):
            vector = vector[0]
    if not vector:
        raise HTTPException(status_code=502, detail="Empty vector returned by embed model")

    if len(vector) != settings.vector_dim:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Embed model returned {len(vector)} dimensions but VECTOR_DIM is "
                f"{settings.vector_dim}. Refusing to write a mismatched vector."
            ),
        )
    return vector


# ── POST /memory/search ───────────────────────────────────────────────────────

@router.post("/search", response_model=MemorySearchResponse)
async def search_memory(
    request: MemorySearchRequest,
    db: AsyncSession = Depends(get_db),
) -> MemorySearchResponse:
    """Embed the query and return the top-k most semantically similar chunks.

    Scope fields are optional; omitting them searches every agent and project,
    which keeps pre-scoping clients working unchanged. Pass a list to match any
    of several values, e.g. ``{"agent_id": ["coder-1", "legacy"]}`` to search an
    agent's own memories plus the shared legacy corpus.
    """
    await ensure_collection()
    vector = await _embed(request.query)

    # First-class scope params are merged on top of the free-form payload filters.
    conditions: dict[str, Any] = dict(request.filters)
    for key, value in (
        ("agent_id", request.agent_id),
        ("project", request.project),
        ("session_id", request.session_id),
        ("type", request.type),
    ):
        if value is not None:
            conditions[key] = value
    qdrant_filter = _build_filter(conditions)

    client = get_qdrant()
    # Uses the Query API (POST /collections/{c}/points/query), which requires
    # Qdrant *server* >= 1.10 — it 404s on older servers. memory-lxc runs 1.12.1
    # and the guard in ensure_collection() fails startup against anything that
    # cannot serve this collection, so reaching here implies a compatible server.
    # `query_points` returns a QueryResponse; the hits live on `.points`, unlike
    # the deprecated `search()` which returned the list directly.
    response = await client.query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        limit=request.top_k,
        query_filter=qdrant_filter,
        with_payload=True,
    )
    results = response.points

    chunks = [
        MemoryChunkResult(
            chunk_id=str(hit.id),
            file_path=hit.payload.get("file_path", ""),
            chunk_index=hit.payload.get("chunk_index", 0),
            chunk_text=hit.payload.get("chunk_text", ""),
            score=hit.score,
            type=hit.payload.get("type"),
            tags=hit.payload.get("tags") or [],
            priority=hit.payload.get("priority"),
            summary=hit.payload.get("summary"),
            updated_at=hit.payload.get("updated_at"),
            # Points that predate scoping (and were not backfilled) report the sentinel.
            agent_id=hit.payload.get("agent_id") or LEGACY_AGENT_ID,
            project=hit.payload.get("project") or DEFAULT_PROJECT,
            session_id=hit.payload.get("session_id"),
        )
        for hit in results
    ]
    return MemorySearchResponse(chunks=chunks, total=len(chunks))


# ── POST /memory/store ────────────────────────────────────────────────────────

@router.post("/store", response_model=MemoryStoreResponse, status_code=201)
async def store_memory(
    request: MemoryStoreRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryStoreResponse:
    """Embed and upsert a single memory chunk into Qdrant + PostgreSQL."""
    await ensure_collection()

    agent_id = normalize_agent_id(request.agent_id)
    project = normalize_project(request.project)
    chunk_id = _chunk_id(request.file_path, request.chunk_index, agent_id, project)

    vector = await _embed(request.chunk_text)
    now = datetime.now(timezone.utc)
    updated_at = request.updated_at or now

    # ── Qdrant upsert ─────────────────────────────────────────────────────────
    client = get_qdrant()
    await client.upsert(
        collection_name=settings.qdrant_collection,
        points=[
            PointStruct(
                id=chunk_id,
                vector=vector,
                payload={
                    "file_path": request.file_path,
                    "chunk_index": request.chunk_index,
                    "chunk_text": request.chunk_text,
                    "type": request.type,
                    "tags": request.tags,
                    "priority": request.priority,
                    "summary": request.summary,
                    "updated_at": updated_at.isoformat(),
                    "agent_id": agent_id,
                    "project": project,
                    "session_id": request.session_id,
                },
            )
        ],
    )

    # ── PostgreSQL upsert ─────────────────────────────────────────────────────
    # The conflict target is still chunk_id, but chunk_id now encodes the scope,
    # so a different agent storing the same path+index inserts a NEW row instead
    # of silently overwriting this one.
    stmt = (
        pg_insert(MemoryChunk)
        .values(
            chunk_id=chunk_id,
            file_path=request.file_path,
            chunk_index=request.chunk_index,
            chunk_text=request.chunk_text,
            type=request.type,
            tags=request.tags,
            priority=request.priority,
            summary=request.summary,
            updated_at=updated_at,
            indexed_at=now,
            agent_id=agent_id,
            project=project,
            session_id=request.session_id,
        )
        .on_conflict_do_update(
            index_elements=["chunk_id"],
            set_={
                "chunk_text": request.chunk_text,
                "type": request.type,
                "tags": request.tags,
                "priority": request.priority,
                "summary": request.summary,
                "updated_at": updated_at,
                "indexed_at": now,
                "agent_id": agent_id,
                "project": project,
                "session_id": request.session_id,
            },
        )
    )
    await db.execute(stmt)

    # ── memory_files upsert ───────────────────────────────────────────────────
    chunk_count = await db.scalar(
        select(func.count())
        .select_from(MemoryChunk)
        .where(
            MemoryChunk.file_path == request.file_path,
            MemoryChunk.agent_id == agent_id,
            MemoryChunk.project == project,
        )
    )
    file_stmt = (
        pg_insert(MemoryFile)
        .values(
            file_path=request.file_path,
            agent_id=agent_id,
            project=project,
            type=request.type,
            tags=request.tags,
            priority=request.priority,
            updated_at=updated_at,
            chunk_count=chunk_count or 1,
        )
        .on_conflict_do_update(
            index_elements=["agent_id", "project", "file_path"],
            set_={
                "type": request.type,
                "tags": request.tags,
                "priority": request.priority,
                "updated_at": updated_at,
                "chunk_count": chunk_count or 1,
            },
        )
    )
    await db.execute(file_stmt)

    return MemoryStoreResponse(
        chunk_id=chunk_id,
        upserted=True,
        agent_id=agent_id,
        project=project,
        session_id=request.session_id,
    )


# ── POST /memory/update ───────────────────────────────────────────────────────

@router.post("/update", response_model=MemoryStoreResponse)
async def update_memory(
    request: MemoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryStoreResponse:
    """Re-embed and overwrite an existing chunk identified by scope + path + index."""
    agent_id = normalize_agent_id(request.agent_id)
    project = normalize_project(request.project)
    chunk_id = _chunk_id(request.file_path, request.chunk_index, agent_id, project)

    existing = await db.get(MemoryChunk, chunk_id)

    text = request.chunk_text
    if text is None:
        if existing is None:
            raise HTTPException(status_code=404, detail="Chunk not found")
        text = existing.chunk_text

    # Fields omitted from the request keep their stored value rather than being
    # nulled out.
    def _keep(field: str, value):
        if value is not None:
            return value
        return getattr(existing, field, None) if existing is not None else None

    store_req = MemoryStoreRequest(
        file_path=request.file_path,
        chunk_index=request.chunk_index,
        chunk_text=text,
        type=_keep("type", request.type),
        tags=_keep("tags", request.tags) or [],
        priority=_keep("priority", request.priority),
        summary=_keep("summary", request.summary),
        updated_at=request.updated_at,
        agent_id=agent_id,
        project=project,
        session_id=request.session_id
        or (existing.session_id if existing is not None else None),
    )
    return await store_memory(store_req, db)


# ── DELETE /memory/delete ─────────────────────────────────────────────────────

@router.delete("/delete", response_model=MemoryDeleteResponse)
async def delete_memory(
    request: MemoryDeleteRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryDeleteResponse:
    """Delete the chunks for a file path *within a scope*.

    Deleting by file_path alone is dangerous once several agents share the
    service, so the delete is scoped to (agent_id, project) unless the caller
    explicitly opts into ``all_scopes``.
    """
    await ensure_collection()

    agent_id = normalize_agent_id(request.agent_id)
    project = normalize_project(request.project)

    chunk_where = [MemoryChunk.file_path == request.file_path]
    file_where = [MemoryFile.file_path == request.file_path]
    payload_conditions: dict[str, Any] = {"file_path": request.file_path}

    if not request.all_scopes:
        chunk_where += [MemoryChunk.agent_id == agent_id, MemoryChunk.project == project]
        file_where += [MemoryFile.agent_id == agent_id, MemoryFile.project == project]
        payload_conditions["agent_id"] = agent_id
        payload_conditions["project"] = project

    result = await db.execute(select(MemoryChunk.chunk_id).where(*chunk_where))
    chunk_ids = [row[0] for row in result.fetchall()]

    # Delete from Qdrant by payload filter rather than by the IDs found in
    # Postgres: that also reaps points whose Postgres row went missing, and it
    # can never reach outside the requested scope.
    qdrant_filter = _build_filter(payload_conditions)
    if qdrant_filter is not None:
        client = get_qdrant()
        await client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=FilterSelector(filter=qdrant_filter),
        )

    if chunk_ids:
        # Postgres cascade handles entity_mentions.
        await db.execute(delete(MemoryChunk).where(*chunk_where))

    await db.execute(delete(MemoryFile).where(*file_where))

    return MemoryDeleteResponse(
        file_path=request.file_path,
        deleted_chunks=len(chunk_ids),
        agent_id=None if request.all_scopes else agent_id,
        project=None if request.all_scopes else project,
        all_scopes=request.all_scopes,
    )
