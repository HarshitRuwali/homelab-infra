"""
/health  — liveness check that probes PostgreSQL and Qdrant.
"""

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.qdrant_store import get_qdrant
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
    try:
        client = get_qdrant()
        await client.get_collections()
    except Exception as exc:
        qdrant_status = f"error: {exc}"

    overall = "ok" if pg_status == "ok" and qdrant_status == "ok" else "degraded"
    return HealthResponse(status=overall, postgres=pg_status, qdrant=qdrant_status)
