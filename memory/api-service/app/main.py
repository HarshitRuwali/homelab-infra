import logging
import time

from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi import Request

from app.config import get_settings
from app.qdrant_store import ensure_collection, VectorDimensionMismatch
from app.routers import router

settings = get_settings()


def configure_logging() -> None:
    """Configure application logging with daily rotation and 7-day retention."""
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "app.log"
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)


configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure the Qdrant collection exists, matches VECTOR_DIM, and is indexed."""
    logger.info(
        "Preparing Qdrant collection '%s' (expecting %d-dim vectors)…",
        settings.qdrant_collection,
        settings.vector_dim,
    )
    try:
        await ensure_collection()
    except VectorDimensionMismatch:
        # Fail loudly and refuse to serve: a dimension mismatch means this
        # process is pointed at someone else's collection (see the VECTOR_DIM
        # note in .env.example).
        logger.critical("Qdrant collection validation FAILED", exc_info=True)
        raise
    logger.info("FastAPI ready")
    yield
    logger.info("FastAPI shutting down")


app = FastAPI(
    title="Open Memory Stack API",
    description=(
        "RAG pipeline, memory CRUD, LLM inference proxy, and embedding proxy "
        "for self-hosted AI applications."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests_and_responses(request: Request, call_next):
    start_time = time.perf_counter()
    client_host = request.client.host if request.client else "unknown"

    logger.info(
        "Request started | client=%s | method=%s | path=%s | query=%s",
        client_host,
        request.method,
        request.url.path,
        str(request.url.query),
    )

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.exception(
            "Request failed | client=%s | method=%s | path=%s | duration_ms=%.2f",
            client_host,
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = (time.perf_counter() - start_time) * 1000
    logger.info(
        "Response sent | client=%s | method=%s | path=%s | status=%s | duration_ms=%.2f",
        client_host,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response

app.include_router(router)
