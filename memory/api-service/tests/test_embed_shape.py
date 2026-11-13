"""EMBED_MODEL selects the embedding API shape for /embed, /memory/store and the batch path."""

from app.routers import embed as embed_router
from app.routers import memory as memory_router


def _chunk(i):
    return {"file_path": "notes/a.md", "chunk_index": i, "chunk_text": f"text {i}"}


def test_native_shape_when_embed_model_unset(client, embed_server, monkeypatch):
    monkeypatch.setattr(memory_router.settings, "embed_model", "")
    r = client.post("/memory/store/batch", json={"chunks": [_chunk(0), _chunk(1)]})
    assert r.status_code == 201, r.text
    assert embed_server.paths == ["/embedding"]


def test_openai_shape_when_embed_model_set_batch(client, embed_server, monkeypatch):
    monkeypatch.setattr(memory_router.settings, "embed_model", "bge-test")
    r = client.post("/memory/store/batch", json={"chunks": [_chunk(0), _chunk(1), _chunk(2)]})
    assert r.status_code == 201, r.text
    assert embed_server.paths == ["/v1/embeddings"]
    assert embed_server.requests == [["text 0", "text 1", "text 2"]]
    assert r.json()["stored"] == 3


def test_openai_shape_when_embed_model_set_embed_endpoint(client, embed_server, monkeypatch):
    monkeypatch.setattr(embed_router.settings, "embed_model", "bge-test")
    r = client.post("/embed", json={"text": "hello"})
    assert r.status_code == 200, r.text
    assert embed_server.paths == ["/v1/embeddings"]
    assert r.json()["dim"] == 4
