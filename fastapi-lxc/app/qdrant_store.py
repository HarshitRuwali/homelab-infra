"""Qdrant client wrapper — creates and manages the 'memory' collection."""

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
)

from app.config import get_settings

settings = get_settings()

_client: AsyncQdrantClient | None = None


def get_qdrant() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = AsyncQdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            timeout=30,
        )
    return _client


async def ensure_collection() -> None:
    """Create the 'memory' collection if it does not already exist."""
    client = get_qdrant()
    collections = await client.get_collections()
    names = [c.name for c in collections.collections]
    if settings.qdrant_collection not in names:
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(
                size=settings.vector_dim,
                distance=Distance.COSINE,
            ),
        )


__all__ = [
    "get_qdrant",
    "ensure_collection",
    "Filter",
    "FieldCondition",
    "MatchValue",
    "PointStruct",
]
