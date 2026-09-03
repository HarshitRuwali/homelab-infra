# Extending the pipeline

The ingestion script is designed to be extended with new data sources.

## Adding a collector

Create an async function that returns a list of chunk dictionaries:

```python
async def collect_my_data() -> list[dict[str, Any]]:
    chunks = []

    data = fetch_something()  # your logic here
    for text in _chunk_text(data):
        chunks.append({
            "text": text,
            "source_file": "my_source/slug.md",
            "type": "my_category",
            "tags": ["my", "tags"],
            "priority": "medium",
        })

    return chunks
```

Then register it in `ingest_all_data()`:

```python
my_data = await collect_my_data()
all_chunks = persona + tasks + progress + notes + projects + my_data
```

## Chunk size limits

The `_chunk_text()` helper splits text to stay under `MAX_CHUNK_CHARS`
(default 500). This is because the embedding model (bge-large-en-v1.5) has
512 trained position embeddings and rejects longer inputs. Do not increase
this limit without checking your model's token capacity.

## Error handling

The main loop tracks consecutive failures. If you add a source that frequently
fails, consider wrapping it in a try/except so it does not abort the entire
pipeline:

```python
try:
    my_data = await collect_my_data()
except Exception as e:
    logger.warning(f"Skipped my_data source: {e}")
    my_data = []
```
