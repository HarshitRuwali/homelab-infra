from fastapi import APIRouter

from app.routers import embed, health, llm, memory

router = APIRouter()
router.include_router(health.router)
router.include_router(embed.router)
router.include_router(memory.router)
router.include_router(llm.router)
