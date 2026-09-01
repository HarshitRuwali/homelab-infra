"""
/embed  — proxy embedding requests to the AI VM.
"""

import httpx
from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.schemas import EmbedRequest, EmbedResponse

router = APIRouter(prefix="/embed", tags=["embed"])
settings = get_settings()


@router.post("", response_model=EmbedResponse)
async def embed(request: EmbedRequest) -> EmbedResponse:
    """Generate a float32 embedding vector from the AI VM embedding model."""
    url = f"{settings.embed_url}/embedding"
    payload = {"content": request.text}

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Embed model returned {exc.response.status_code}: {exc.response.text}",
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach AI VM: {exc}")

    data = resp.json()
    # Handle various llama.cpp embed response formats:
    #   bge-large: [{"index": 0, "embedding": [[vec]]}]
    #   llama.cpp: {"embedding": [vec]}
    #   OpenAI:    {"data": [{"embedding": [vec]}]}
    vector: list[float] = []
    if isinstance(data, list) and data and isinstance(data[0], dict):
        emb = data[0].get("embedding", [])
        if isinstance(emb, list) and len(emb) == 1 and isinstance(emb[0], list):
            vector = emb[0]  # unwrap [[vec]] → [vec]
        elif isinstance(emb, list) and emb and isinstance(emb[0], (int, float)):
            vector = emb
    elif isinstance(data, dict):
        vector = data.get("embedding") or data.get("data", [{}])[0].get("embedding", [])
        if isinstance(vector, list) and vector and isinstance(vector[0], list):
            vector = vector[0]
    return EmbedResponse(vector=vector, dim=len(vector))
