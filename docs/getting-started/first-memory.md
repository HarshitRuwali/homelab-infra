# Your first memory

`/health` being green only proves PostgreSQL and Qdrant are reachable. The
embedding service is not probed, so the first real test is a write.

Examples below use the all-in-one port (`8088`). A split deployment uses `8080`.

## Store a chunk

```bash
curl -X POST http://localhost:8088/memory/store \
  -H 'Content-Type: application/json' \
  -d '{
    "file_path": "notes/first.md",
    "chunk_index": 0,
    "chunk_text": "The memory service embeds text with bge-large and stores vectors in Qdrant.",
    "type": "note",
    "tags": ["demo"],
    "priority": "low"
  }'
```

```json
{
  "chunk_id": "0e0e2b53-...-...",
  "upserted": true,
  "agent_id": "legacy",
  "project": "default",
  "session_id": null
}
```

`agent_id: "legacy"` is expected. Sending no scope gets the sentinel scope —
that is the backward-compatibility default, not an error. See
[Memory scoping](../architecture/scoping.md).

## Search it back

```bash
curl -X POST http://localhost:8088/memory/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "where do the vectors live?", "top_k": 3}'
```

```json
{
  "chunks": [
    {
      "chunk_id": "0e0e2b53-...-...",
      "file_path": "notes/first.md",
      "chunk_index": 0,
      "chunk_text": "The memory service embeds text with bge-large and stores vectors in Qdrant.",
      "score": 0.72,
      "type": "note",
      "tags": ["demo"],
      "agent_id": "legacy",
      "project": "default"
    }
  ],
  "total": 1
}
```

The query never appears in the stored text — matching is by meaning, which is
the whole point. Scores are cosine similarity; anything above ~0.5 is usually a
real match, but the useful threshold depends on your embedding model.

## Clean up

```bash
curl -X DELETE http://localhost:8088/memory/delete \
  -H 'Content-Type: application/json' \
  -d '{"file_path": "notes/first.md"}'
```

```json
{"file_path": "notes/first.md", "deleted_chunks": 1, "agent_id": "legacy", "project": "default", "all_scopes": false}
```

Deleting without a scope deletes within the sentinel scope only. It cannot
reach another agent's chunks unless you pass `all_scopes: true`.

## When the write fails

A **502** on `/memory/store` means the embedding service, not the memory stack:

```json
{"detail": "Cannot reach AI VM: [Errno 111] Connection refused"}
```

```bash
# Is the embedding service up and shaped the way the stack expects?
curl -X POST http://<model host>:8081/embedding \
  -H 'Content-Type: application/json' \
  -d '{"content": "test"}'
```

The stack calls llama.cpp's **native** `POST /embedding` with a `content` key —
not the OpenAI-style `/v1/embeddings` with `input`. A server exposing only the
OpenAI route returns 404 and every write fails.

Other failure shapes are in [Troubleshooting](../operations/troubleshooting.md).

## Next

- [Agents](../agents/index.md) — let a coding agent do this over MCP instead of curl
- [API](../api/index.md) — every endpoint, request and response
- [Ingestion](../ingestion/index.md) — fill the store automatically
