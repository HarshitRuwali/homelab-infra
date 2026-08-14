"""
/llm/infer  — proxy inference requests to llama-server on the AI VM.
Supports streaming (Server-Sent Events) and non-streaming responses.
"""

import json
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.schemas import LLMInferRequest

router = APIRouter(prefix="/llm", tags=["llm"])
settings = get_settings()


async def _stream_llm(payload: dict) -> AsyncIterator[str]:
    url = f"{settings.llm_url}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise HTTPException(
                    status_code=502,
                    detail=f"LLM returned {resp.status_code}: {body.decode()}",
                )
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"


@router.post("/infer")
async def infer(request: LLMInferRequest):
    """Forward a chat completion request to llama-server.

    Returns a StreamingResponse (SSE) when stream=True,
    or a plain JSON response when stream=False.
    """
    payload = {
        "messages": [m.model_dump() for m in request.messages],
        "stream": request.stream,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
    }

    if request.stream:
        return StreamingResponse(
            _stream_llm(payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming path
    url = f"{settings.llm_url}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"LLM returned {exc.response.status_code}: {exc.response.text}",
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Cannot reach AI VM: {exc}")

    return resp.json()
