# Embedding and LLM Endpoints

## `POST /embed`

Proxy a single text string to the embedding model and return the vector.

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes | Text to embed |

**Response (200):**

| Field | Type | Description |
|---|---|---|
| `vector` | float[] | Embedding vector |
| `dim` | integer | Vector dimension |

**Example:**

```bash
curl -X POST http://localhost:8088/embed \
  -H "Content-Type: application/json" \
  -d '{"text": "hello world"}'
```

The endpoint handles multiple embedding response formats:

- llama.cpp: `{"embedding": [vec]}`
- bge-large: `[{"index": 0, "embedding": [[vec]]}]`
- OpenAI-style: `{"data": [{"embedding": [vec]}]}`

The upstream call is llama.cpp's **native** `POST /embedding` with a `content`
key — not the OpenAI-style `/v1/embeddings` with `input`. A server exposing only
the OpenAI route returns 404 and every embed and memory write fails.

!!! warning "`/embed` does not validate what it got back"
    Unlike the memory endpoints, `/embed` neither rejects an unparseable
    response nor checks the vector against `VECTOR_DIM`. If the model replies
    in a shape it does not recognise, it returns **200** with
    `{"vector": [], "dim": 0}`. Check `dim` rather than assuming success from
    the status code.

    `/memory/store` is strict on both counts and returns 502 instead.

---

## `POST /llm/infer`

Forward a chat completion request to the LLM. Supports both streaming (SSE)
and non-streaming modes.

**Request body:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `messages` | LLMMessage[] | yes | -- | Chat messages |
| `stream` | boolean | no | `true` | Enable SSE streaming |
| `temperature` | float | no | `0.7` | Sampling temperature (0-2) |
| `max_tokens` | integer | no | `2048` | Max output tokens |
| `model` | string | no | `"default"` | Accepted, but **not forwarded** — see below |

!!! warning "`model` is accepted and dropped"
    The schema declares it, but the payload the proxy builds contains only
    `messages`, `stream`, `temperature` and `max_tokens`. Whatever you send as
    `model` never reaches the backend, so the request is served by whichever
    model the LLM server has loaded. Select the model on the server, not here.

Each `LLMMessage` has:

| Field | Type | Description |
|---|---|---|
| `role` | string | `system`, `user`, `assistant`, or `tool` |
| `content` | string | Message text |

**Streaming response:** Server-Sent Events with `data: ` prefix. The response
is a `text/event-stream` with `Cache-Control: no-cache` and
`X-Accel-Buffering: no` headers for proper proxy passthrough.

**Non-streaming response:** Plain JSON matching the LLM's native response format.

**Example (non-streaming):**

```bash
curl -X POST http://localhost:8088/llm/infer \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "system", "content": "You are helpful."},
      {"role": "user", "content": "What is 2+2?"}
    ],
    "stream": false,
    "max_tokens": 64
  }'
```
