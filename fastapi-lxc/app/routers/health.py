"""
/health  — liveness check that probes PostgreSQL and Qdrant.
"""

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.qdrant_store import collection_vector_dim, get_qdrant
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])
settings = get_settings()


@router.get("/health", response_model=HealthResponse)
async def health(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    # ── PostgreSQL ────────────────────────────────────────────────────────────
    pg_status = "ok"
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        pg_status = f"error: {exc}"

    # ── Qdrant ────────────────────────────────────────────────────────────────
    qdrant_status = "ok"
    live_dim: int | None = None
    try:
        client = get_qdrant()
        await client.get_collections()
        live_dim = await collection_vector_dim()
    except Exception as exc:
        qdrant_status = f"error: {exc}"

    # Surface a VECTOR_DIM / collection divergence instead of letting it corrupt
    # writes silently (see app/qdrant_store.py).
    if qdrant_status == "ok" and live_dim is not None and live_dim != settings.vector_dim:
        qdrant_status = (
            f"error: dimension mismatch — collection '{settings.qdrant_collection}' "
            f"is {live_dim}-dim but VECTOR_DIM={settings.vector_dim}"
        )

    overall = "ok" if pg_status == "ok" and qdrant_status == "ok" else "degraded"
    return HealthResponse(
        status=overall,
        postgres=pg_status,
        qdrant=qdrant_status,
        qdrant_collection=settings.qdrant_collection,
        configured_vector_dim=settings.vector_dim,
        collection_vector_dim=live_dim,
    )
