"""Test setup: dummy settings, a fake DB session and a fake Qdrant client."""

import os
import sys
import tempfile

# Settings are read at import time, so the environment must be set before app.* loads.
for key, value in {
    "POSTGRES_USER": "u",
    "POSTGRES_PASSWORD": "p",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "d",
    "QDRANT_HOST": "localhost",
    "QDRANT_PORT": "6333",
    "QDRANT_COLLECTION": "memory",
    "VECTOR_DIM": "4",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "AI_VM_HOST": "ai.test",
    "LLM_PORT": "8080",
    "EMBED_PORT": "8081",
    "APP_HOST": "127.0.0.1",
    "APP_PORT": "8080",
    "LOG_LEVEL": "info",
}.items():
    os.environ.setdefault(key, value)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402

# app.main creates ./logs; keep it out of the source tree.
os.chdir(tempfile.mkdtemp(prefix="memapi-tests-"))
sys.dont_write_bytecode = True

from app import http_client  # noqa: E402
from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import memory as memory_router  # noqa: E402


class FakeSession:
    """Records every statement executed, compiled for the PostgreSQL dialect."""

    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)

    def sql(self):
        return [str(s.compile(dialect=postgresql.dialect())) for s in self.statements]


class FakeQdrant:
    def __init__(self):
        self.upserts = []

    async def upsert(self, collection_name, points):
        self.upserts.append((collection_name, points))


class FakeEmbedServer:
    """httpx transport standing in for the llama.cpp /embedding endpoint."""

    def __init__(self):
        self.requests = []
        self.paths = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        self.paths.append(request.url.path)
        if request.url.path == "/v1/embeddings":
            # OpenAI shape: {"model": m, "input": str | list} -> {"data": [{"index", "embedding": vec}]}
            content = body["input"]
            self.requests.append(content)
            texts = content if isinstance(content, list) else [content]
            data = [
                {"index": i, "embedding": [float(len(t)), float(i), 0.0, 0.0]}
                for i, t in enumerate(texts)
            ]
            return httpx.Response(200, json={"object": "list", "model": body["model"], "data": data})
        content = body["content"]
        self.requests.append(content)
        texts = content if isinstance(content, list) else [content]
        # llama.cpp shape: [{"index": i, "embedding": [[vec]]}], vector = [len, i, 0, 0]
        data = [
            {"index": i, "embedding": [[float(len(t)), float(i), 0.0, 0.0]]}
            for i, t in enumerate(texts)
        ]
        return httpx.Response(200, json=data)


@pytest.fixture
def embed_server(monkeypatch):
    server = FakeEmbedServer()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    monkeypatch.setattr(http_client, "_client", client)
    yield server
    monkeypatch.setattr(http_client, "_client", None)


@pytest.fixture
def qdrant(monkeypatch):
    fake = FakeQdrant()

    async def _noop():
        return None

    monkeypatch.setattr(memory_router, "get_qdrant", lambda: fake)
    monkeypatch.setattr(memory_router, "ensure_collection", _noop)
    return fake


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def client(session, qdrant, embed_server):
    app.dependency_overrides[get_db] = lambda: session
    # No `with`: the lifespan would try to reach Qdrant.
    yield TestClient(app)
    app.dependency_overrides.clear()
