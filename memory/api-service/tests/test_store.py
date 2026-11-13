"""/memory/store and /memory/store/batch share one code path."""

CHUNK = {"file_path": "notes/a.md", "chunk_index": 0, "chunk_text": "hello"}


def test_single_store_runs_two_statements_and_no_separate_count(client, session, qdrant, embed_server):
    resp = client.post("/memory/store", json=CHUNK)
    assert resp.status_code == 201
    body = resp.json()
    assert body["upserted"] is True and body["agent_id"] == "legacy"

    sql = session.sql()
    # One chunk upsert + one memory_files upsert. The old code ran a third,
    # separate SELECT count(*) statement first.
    assert len(sql) == 2
    assert sql[0].startswith("INSERT INTO memory_chunks")
    assert sql[1].startswith("INSERT INTO memory_files")
    assert "count(*)" in sql[1]  # chunk_count is computed inside the upsert
    assert len(qdrant.upserts) == 1 and len(qdrant.upserts[0][1]) == 1
    assert embed_server.requests == ["hello"]  # a single text is sent as a string, as before


def test_batch_store_one_upsert_per_file_and_one_embed_call(client, session, qdrant, embed_server):
    chunks = [
        {"file_path": "notes/a.md", "chunk_index": i, "chunk_text": f"text {i}"} for i in range(5)
    ] + [{"file_path": "notes/b.md", "chunk_index": 0, "chunk_text": "other"}]
    resp = client.post("/memory/store/batch", json={"chunks": chunks})
    assert resp.status_code == 201
    body = resp.json()
    assert body["stored"] == 6
    assert [c["chunk_id"] for c in body["chunks"]] == list(dict.fromkeys(c["chunk_id"] for c in body["chunks"]))

    sql = session.sql()
    # one multi-row chunk insert + one memory_files upsert for each of the 2 files
    assert len(sql) == 3
    assert sql[0].startswith("INSERT INTO memory_chunks")
    assert sum(s.startswith("INSERT INTO memory_files") for s in sql) == 2
    assert len(qdrant.upserts) == 1 and len(qdrant.upserts[0][1]) == 6
    assert len(embed_server.requests) == 1 and len(embed_server.requests[0]) == 6


def test_batch_ids_match_single_store_ids(client):
    single = client.post("/memory/store", json={**CHUNK, "agent_id": "x", "project": "p"}).json()
    batch = client.post(
        "/memory/store/batch", json={"chunks": [{**CHUNK, "agent_id": "x", "project": "p"}]}
    ).json()
    assert batch["chunks"][0]["chunk_id"] == single["chunk_id"]


def test_batch_duplicate_chunk_is_stored_once_later_wins(client, qdrant, session):
    first = {**CHUNK, "chunk_text": "old"}
    second = {**CHUNK, "chunk_text": "new"}
    resp = client.post("/memory/store/batch", json={"chunks": [first, second]})
    assert resp.status_code == 201
    assert resp.json()["stored"] == 2  # one response per request entry
    points = qdrant.upserts[0][1]
    assert len(points) == 1 and points[0].payload["chunk_text"] == "new"


def test_batch_embeds_in_groups(client, embed_server):
    chunks = [{"file_path": "f.md", "chunk_index": i, "chunk_text": "x"} for i in range(60)]
    resp = client.post("/memory/store/batch", json={"chunks": chunks})
    assert resp.status_code == 201
    assert [len(r) for r in embed_server.requests] == [25, 25, 10]


def test_batch_validation(client):
    assert client.post("/memory/store/batch", json={"chunks": []}).status_code == 422
    too_many = [{"file_path": "f.md", "chunk_index": i, "chunk_text": "x"} for i in range(101)]
    assert client.post("/memory/store/batch", json={"chunks": too_many}).status_code == 422


def test_embed_dimension_mismatch_is_502(client, monkeypatch):
    from app.routers import memory

    monkeypatch.setattr(memory.settings, "vector_dim", 8)
    resp = client.post("/memory/store", json=CHUNK)
    assert resp.status_code == 502
    assert "VECTOR_DIM" in resp.json()["detail"]


def _file_stmts(session):
    return [s for s in session.statements if str(s.compile()).startswith("INSERT INTO memory_files")]


def test_batch_file_metadata_comes_from_last_occurrence(client, session, qdrant):
    chunks = [
        {**CHUNK, "chunk_index": 0, "chunk_text": "x", "tags": ["x"]},
        {**CHUNK, "chunk_index": 1, "chunk_text": "y", "tags": ["y"]},
        {**CHUNK, "chunk_index": 0, "chunk_text": "z", "tags": ["z"]},
    ]
    assert client.post("/memory/store/batch", json={"chunks": chunks}).status_code == 201
    files = _file_stmts(session)
    assert len(files) == 1
    assert files[0].compile().params["tags"] == ["z"]  # same as three sequential stores
    # the later a#0 wins and keeps its later position
    assert [p.payload["chunk_text"] for p in qdrant.upserts[0][1]] == ["y", "z"]


def test_batch_chunk_rows_are_sorted_by_chunk_id(client, session):
    chunks = [{**CHUNK, "chunk_index": i, "chunk_text": f"t{i}"} for i in (3, 0, 2, 1, 4)]
    assert client.post("/memory/store/batch", json={"chunks": chunks}).status_code == 201
    params = session.statements[0].compile().params
    ids = [v for k, v in sorted(params.items()) if k.startswith("chunk_id_m")]
    assert len(ids) == 5 and ids == sorted(ids)
