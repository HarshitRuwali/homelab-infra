"""Qdrant client wrapper — creates, validates and maintains the 'memory' collection."""

import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
    IsEmptyCondition,
    PayloadField,
    PayloadSchemaType,
)

from app.config import get_settings
from app.scope import DEFAULT_PROJECT, LEGACY_AGENT_ID

logger = logging.getLogger(__name__)

settings = get_settings()

_client: AsyncQdrantClient | None = None

# One-shot guard: the collection only needs validating / preparing once per
# process, but ensure_collection() is called on every read and write path.
_prepared = False

# Payload keys that are used as search filters and therefore need an index.
# Without these, every filtered search degrades to a full scan as the
# collection grows.
INDEXED_PAYLOAD_FIELDS = ("agent_id", "project", "type", "file_path", "session_id")


class VectorDimensionMismatch(RuntimeError):
    """Raised when the live collection does not match the configured VECTOR_DIM."""


def get_qdrant() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            timeout=30,
        )
    return _client


def _unnamed_vector_params(info) -> VectorParams | None:
    """Pull the single unnamed VectorParams out of a CollectionInfo, if there is one."""
    vectors = info.config.params.vectors
    if isinstance(vectors, VectorParams):
        return vectors
    if isinstance(vectors, dict):
        # Named-vector collection. This app only ever writes unnamed vectors, so
        # the default ("") entry is the one that matters; fall back to the sole
        # entry when there is exactly one.
        if "" in vectors:
            return vectors[""]
        if len(vectors) == 1:
            return next(iter(vectors.values()))
    return None


async def collection_vector_dim() -> int | None:
    """Return the live collection's vector size, or None if it is absent/unreadable."""
    client = get_qdrant()
    try:
        info = await client.get_collection(settings.qdrant_collection)
    except Exception:
        return None
    params = _unnamed_vector_params(info)
    return params.size if params else None


async def _validate_dimensions() -> None:
    """Fail loudly when the configured VECTOR_DIM disagrees with the live collection.

    Silently writing 768-dim vectors into a 1024-dim collection (or vice versa)
    corrupts the index in a way that is very hard to notice: Qdrant rejects the
    write, but a misconfigured *new* collection would happily accept garbage.
    """
    client = get_qdrant()
    info = await client.get_collection(settings.qdrant_collection)
    params = _unnamed_vector_params(info)
    if params is None:
        logger.warning(
            "Collection '%s' uses named vectors; skipping dimension validation.",
            settings.qdrant_collection,
        )
        return

    if params.size != settings.vector_dim:
        raise VectorDimensionMismatch(
            f"Qdrant collection '{settings.qdrant_collection}' at "
            f"{settings.qdrant_host}:{settings.qdrant_port} has vector size "
            f"{params.size}, but VECTOR_DIM is configured as {settings.vector_dim}. "
            "Refusing to start: writing mismatched vectors would corrupt the "
            "collection. Fix VECTOR_DIM in the .env used by this deployment (the "
            "embedding model at EMBED_PORT decides the true dimension), or point "
            "QDRANT_COLLECTION at a different collection."
        )

    if params.distance != Distance.COSINE:
        logger.warning(
            "Collection '%s' uses distance %s, not COSINE — scores will not be "
            "comparable with previously stored data.",
            settings.qdrant_collection,
            params.distance,
        )


async def _ensure_payload_indexes() -> None:
    """Create keyword payload indexes for the scope filter fields (idempotent)."""
    client = get_qdrant()
    for field in INDEXED_PAYLOAD_FIELDS:
        try:
            await client.create_payload_index(
                collection_name=settings.qdrant_collection,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )
        except Exception as exc:  # already exists, or a transient issue
            logger.debug("Payload index for '%s' not created: %s", field, exc)


async def backfill_legacy_scope() -> int:
    """Stamp the sentinel scope onto pre-multi-agent points.

    Points ingested before scoping existed carry no ``agent_id`` payload key, so
    a scoped search would never match them. This additively sets
    agent_id/project on exactly those points — no vectors are touched, nothing
    is deleted, and it is a no-op on the second run.
    """
    client = get_qdrant()
    unscoped = Filter(
        must=[IsEmptyCondition(is_empty=PayloadField(key="agent_id"))]
    )
    try:
        count = (
            await client.count(
                collection_name=settings.qdrant_collection,
                count_filter=unscoped,
                exact=True,
            )
        ).count
    except Exception as exc:
        logger.warning("Could not count unscoped points: %s", exc)
        return 0

    if not count:
        return 0

    logger.info(
        "Backfilling scope agent_id=%s project=%s onto %d pre-existing point(s)…",
        LEGACY_AGENT_ID,
        DEFAULT_PROJECT,
        count,
    )
    await client.set_payload(
        collection_name=settings.qdrant_collection,
        payload={"agent_id": LEGACY_AGENT_ID, "project": DEFAULT_PROJECT},
        points=unscoped,
        wait=True,
    )
    return count


async def ensure_collection(force: bool = False) -> None:
    """Create the collection if absent; otherwise validate and prepare it.

    Runs its expensive work (dimension validation, payload indexes, legacy
    backfill) only once per process — every /memory read and write calls this.
    """
    global _prepared
    if _prepared and not force:
        return

    client = get_qdrant()
    collections = await client.get_collections()
    names = [c.name for c in collections.collections]

    if settings.qdrant_collection not in names:
        logger.info(
            "Creating Qdrant collection '%s' (dim=%d, cosine)",
            settings.qdrant_collection,
            settings.vector_dim,
        )
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(
                size=settings.vector_dim,
                distance=Distance.COSINE,
            ),
        )
    else:
        await _validate_dimensions()
        await backfill_legacy_scope()

    await _ensure_payload_indexes()
    _prepared = True


__all__ = [
    "get_qdrant",
    "ensure_collection",
    "backfill_legacy_scope",
    "collection_vector_dim",
    "VectorDimensionMismatch",
    "Filter",
    "FieldCondition",
    "MatchValue",
    "MatchAny",
    "PointStruct",
]
