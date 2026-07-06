"""
/memory/*  — semantic memory CRUD operations backed by Qdrant + PostgreSQL.
"""

import hashlib
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from qdrant_client.models import Filter, FieldCondition, MatchValue, PointStruct
from sqlalchemy import delete, select
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

router = APIRouter(prefix="/memory", tags=["memory"])
settings = get_settings()


def _chunk_id(file_path: str, chunk_index: int) -> str:
    """Return a deterministic UUID string from file_path + chunk_index.
    Uses SHA-256 digest converted to a valid UUID (preserves uniqueness)."""
    raw = hashlib.sha256(f"{file_path}:{chunk_index}".encode()).digest()[:16]
    return str(uuid.UUID(bytes=raw, version=4))


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
    return vector


# ── POST /memory/search ───────────────────────────────────────────────────────

@router.post("/search", response_model=MemorySearchResponse)
async def search_memory(
    request: MemorySearchRequest,
    db: AsyncSession = Depends(get_db),
) -> MemorySearchResponse:
    """Embed the query and return the top-k most semantically similar chunks."""
    await ensure_collection()
    vector = await _embed(request.query)

    # Build optional payload filter
    qdrant_filter: Filter | None = None
    if request.filters:
        conditions = [
            FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in request.filters.items()
        ]
        qdrant_filter = Filter(must=conditions)

    client = get_qdrant()
    results = await client.search(
        collection_name=settings.qdrant_collection,
        query_vector=vector,
        limit=request.top_k,
        query_filter=qdrant_filter,
        with_payload=True,
    )

    chunks = [
        MemoryChunkResult(
            chunk_id=str(hit.id),
            file_path=hit.payload.get("file_path", ""),
            chunk_index=hit.payload.get("chunk_index", 0),
            chunk_text=hit.payload.get("chunk_text", ""),
            score=hit.score,
            type=hit.payload.get("type"),
            tags=hit.payload.get("tags", []),
            priority=hit.payload.get("priority"),
            summary=hit.payload.get("summary"),
            updated_at=hit.payload.get("updated_at"),
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
    chunk_id = _chunk_id(request.file_path, request.chunk_index)
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
                },
            )
        ],
    )

    # ── PostgreSQL upsert ─────────────────────────────────────────────────────
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
            },
        )
    )
    await db.execute(stmt)

    # ── memory_files upsert ───────────────────────────────────────────────────
    file_stmt = (
        pg_insert(MemoryFile)
        .values(
            file_path=request.file_path,
            type=request.type,
            tags=request.tags,
            priority=request.priority,
            updated_at=updated_at,
            chunk_count=1,
        )
        .on_conflict_do_update(
            index_elements=["file_path"],
            set_={
                "type": request.type,
                "tags": request.tags,
                "priority": request.priority,
                "updated_at": updated_at,
            },
        )
    )
    await db.execute(file_stmt)

    return MemoryStoreResponse(chunk_id=chunk_id, upserted=True)


# ── POST /memory/update ───────────────────────────────────────────────────────

@router.post("/update", response_model=MemoryStoreResponse)
async def update_memory(
    request: MemoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryStoreResponse:
    """Re-embed and overwrite an existing chunk identified by (file_path, chunk_index)."""
    chunk_id = _chunk_id(request.file_path, request.chunk_index)

    # Fetch existing text from PostgreSQL if not provided
    text = request.chunk_text
    if text is None:
        row = await db.get(MemoryChunk, chunk_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Chunk not found")
        text = row.chunk_text

    store_req = MemoryStoreRequest(
        file_path=request.file_path,
        chunk_index=request.chunk_index,
        chunk_text=text,
        type=request.type,
        tags=request.tags or [],
        priority=request.priority,
        summary=request.summary,
        updated_at=request.updated_at,
    )
    return await store_memory(store_req, db)


# ── DELETE /memory/delete ─────────────────────────────────────────────────────

@router.delete("/delete", response_model=MemoryDeleteResponse)
async def delete_memory(
    request: MemoryDeleteRequest,
    db: AsyncSession = Depends(get_db),
) -> MemoryDeleteResponse:
    """Delete all Qdrant points and PostgreSQL rows for the given file_path."""
    # Fetch chunk IDs from PostgreSQL
    result = await db.execute(
        select(MemoryChunk.chunk_id).where(MemoryChunk.file_path == request.file_path)
    )
    chunk_ids = [row[0] for row in result.fetchall()]

    if chunk_ids:
        # Delete from Qdrant
        client = get_qdrant()
        from qdrant_client.models import PointIdsList
        await client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=PointIdsList(points=chunk_ids),
        )

        # Delete from PostgreSQL (cascade handles entity_mentions)
        await db.execute(
            delete(MemoryChunk).where(MemoryChunk.file_path == request.file_path)
        )

    # Delete memory_files row
    await db.execute(
        delete(MemoryFile).where(MemoryFile.file_path == request.file_path)
    )

    return MemoryDeleteResponse(
        file_path=request.file_path,
        deleted_chunks=len(chunk_ids),
    )
