# aibox-model-queue

A proxy between chat clients and the AI box's inference server. It serializes
requests for different models, cleans tool schemas the grammar compiler rejects,
and translates Hermes' reasoning off-switch.

## Current role

Since 2026-11-11 the AI box (`10.10.50.122`) runs llama.cpp:

| Endpoint | Server | Serves |
|---|---|---|
| `http://10.10.50.122:8090` | `llama-server` | chat, `qwen3.8-27b` (one resident model, several parallel slots) |
| `http://10.10.50.122:8080` | `llama-embed` | bge embeddings |

LM Studio is disabled. This proxy's default upstream is therefore `:8090`.
`:8080` is the embeddings server and must not be used as a chat upstream.

With one resident model and no swaps, **the gate never queues** (7-day journal
review, 2026-11-13: every admitted request named `qwen3.8-27b`, zero queueing
events). The gate is kept because it costs nothing and matters again if several
models are ever configured. While there is one model, the proxy's value is:

- tool-schema cleaning and the reasoning off-switch translation (below), and
- a stable URL (`:8099`) for Hermes, OpenCode, Pi and the other tailnet clients.

The sections on the gate describe the LM Studio problem it was built for and how
it behaves; they apply unchanged to a multi-model llama-server setup.

## The problem the gate solves

Under LM Studio the box held exactly one model resident. LM Studio JIT-evicts
correctly when asked for a different one, so *sequential* switching was fine
(measured: ~11 s to swap in a 16 GB 27B). Concurrency broke it.

Two clients bound to different models would eventually have a request in flight
at the same moment:

```
google/gemma-4-12b-qat  -> ok: 'OK'
unsloth/qwen3.8-27b     -> ERROR: Model is unloaded.
```

One request won the box; the other was evicted mid-flight, surfacing as
`Model is unloaded.` or a 400 `Engine protocol startup was aborted`, which
callers typically classify as non-retryable.

### Why not a model fallback

A fallback model makes the loser retry elsewhere, but a room deliberately bound
to the 27B then answers on the 12B exactly when it is busiest, trading a visible
failure for an invisible quality regression. **This proxy never downgrades a
request.**

## Behaviour

| Situation | Result |
|---|---|
| Requests naming the **same** model | Run concurrently |
| Request naming a **different** model | Waits for the box to drain, then takes it |
| Queued request behind same-model traffic | FIFO fairness; cannot be starved |
| Non-inference paths (`/v1/models`, ...) | Pass straight through, ungated |
| Client hangs up mid-stream | Handler is cancelled, upstream request abandoned, slot freed |
| Upstream silent for `MODEL_PROXY_READ_TIMEOUT` | `504` to the client, slot freed |
| Upstream unreachable | `502` |
| Body larger than `MODEL_PROXY_MAX_BODY_MB` | `413` with a JSON error naming the setting |
| `MODEL_PROXY_TOKEN` set and header missing or wrong | `401` |
| Upstream answers `>= 400` | Relayed unchanged; first 300 bytes of the body logged at WARNING |

The gate is released only after the response body is fully relayed, so
streaming responses hold the box for their whole lifetime.

## Reasoning off-switch

Hermes turns thinking off for a custom provider by sending an OpenRouter-style
body field, `reasoning: {"enabled": false}`. The upstream ignored that field and
kept thinking, but honours a top-level `reasoning_effort: "none"` (measured
2026-09-21 on `qwen3.8-27b` under LM Studio: 53 completion tokens with the Hermes
field, 4 with the top-level one).

On completion paths, a request asking for thinking off gets
`reasoning_effort: "none"` added before it is forwarded.

- Only the **off** case is translated. Requests that leave thinking on are
  relayed byte-for-byte.
- An explicit top-level `reasoning_effort` from the client always wins.
- `/v1/embeddings` is never rewritten.

Whether llama-server needs the translation is unverified: since the cutover the
`reasoning_off_translated` counter on `/_proxy/status` has stayed at 0.

## Tool-schema cleaning

The llama.cpp grammar compiler (inside LM Studio before, and inside
`llama-server` now) rejects some valid tool schemas, failing the whole request
with `Failed to initialize samplers: failed to parse grammar`. Measured
2026-10-09: a regex `pattern` using `\S` or `\s` (T3 Code's MCP tools) gives
"unknown escape", and an array of strings with `maxLength` >= 2000 (Hermes'
`clarify`) gives "number of repetitions exceeds sane defaults".

The proxy drops `pattern`, `minLength`, `maxLength`, `minItems` and `maxItems`
from every `tools[].function.parameters` schema on chat/completions paths. They
only validate; the tool's own server still checks its arguments. The walk
follows schema structure, so a parameter *named* `pattern` (OpenCode's `grep`)
survives.

It also gives an object schema without `properties` an empty one. LM Studio's
request validator rejected `{"type": "object"}` alone (400 `invalid_type ...
properties Required`), which is what no-argument tools send from Pi under T3
Code (measured 2026-10-09). A missing `parameters` becomes
`{"type": "object", "properties": {}}`. This part is harmless on llama-server.

`/_proxy/status` counts both kinds of fix in `tool_schema_keywords_dropped`
(name kept for compatibility).

## Install

```bash
MODEL_PROXY_HOST=127.0.0.1,100.88.214.113 ./install.sh   # ubuntu-dev: loopback plus the tailnet address
./install.sh                      # fresh install on 127.0.0.1:8099; a re-run keeps the installed unit's bind
./install.sh http://HOST:PORT     # a different upstream
```

When `MODEL_PROXY_HOST` or `MODEL_PROXY_PORT` is unset and the unit file already
exists, `install.sh` reuses the values in it, so re-running it never narrows the
bind. The final `ok` line lists the effective bind addresses, and every one of
them is probed, not only loopback.

`install.sh` copies `proxy.py`, `__init__.py`, `plugin.yaml` and `systemd/` into
`MODEL_QUEUE_INSTALL_DIR` (default `~/.hermes/plugins/aibox-model-queue`, which is
also where Hermes loads the plugin from), writes the unit to
`~/.config/systemd/user/hermes-model-queue.service`, restarts it, and checks that
`/_proxy/status` answers. It does not remove a systemd drop-in; if
`hermes-model-queue.service.d/` holds an `upstream-*.conf` from the 2026-11-11
cutover it can be deleted, the default now points at `llama-server`.

Then point clients at the proxy instead of the box:

```yaml
base_url: http://127.0.0.1:8099/v1
```

### Clients

Found 2026-11-13 by searching the agent configs on ubuntu-dev and the proxy
journal:

| Client | Where it runs | Address used |
|---|---|---|
| Hermes (default, `jobs`, `dev`, `infra` profiles) | ubuntu-dev | `http://127.0.0.1:8099/v1` |
| Pi (`~/.pi/agent/models.json`) | ubuntu-dev | `http://127.0.0.1:8099/v1` |
| OpenCode (`~/.config/opencode/opencode.json`) | ubuntu-dev | `http://127.0.0.1:8099/v1` |
| skullsaints (tailnet `100.71.146.71`, about 80 percent of requests) | another host | `http://100.88.214.113:8099/v1` |

Because skullsaints reaches the proxy over the tailnet, the unit on ubuntu-dev
binds `127.0.0.1,100.88.214.113`. The code default is loopback only; the tailnet
bind is an explicit choice made by `MODEL_PROXY_HOST`.

### Authentication

The proxy has no authentication unless `MODEL_PROXY_TOKEN` is set. When it is,
every request, including `/_proxy/status`, must carry
`Authorization: Bearer <token>`, otherwise it gets `401`. The comparison is
constant time and the token is never logged or returned by the status endpoint.
The `Authorization` header is not forwarded to the upstream while a token is set.

Set it in `~/.config/hermes-model-queue.env` (mode 600, outside the repository;
the unit reads it with `EnvironmentFile=-`):

```
MODEL_PROXY_TOKEN=<random string>
```

Then give each client the same token as its API key (Hermes `api_key`, OpenCode
`apiKey`, Pi `apiKey`). Without the file the proxy stays open, which is the
current state on the tailnet. Only enable the token after the clients send it.

## Configuration

Environment variables, set in the unit or in `~/.config/hermes-model-queue.env`:

| Variable | Default | Meaning |
|---|---|---|
| `MODEL_PROXY_UPSTREAM` | `http://10.10.50.122:8090` | llama.cpp `llama-server` (chat). `install.sh` takes a URL argument that overrides it |
| `MODEL_PROXY_HOST` | `127.0.0.1` | Bind address(es), comma-separated (e.g. `127.0.0.1,100.88.214.113` to also serve the tailnet) |
| `MODEL_PROXY_PORT` | `8099` | Bind port |
| `MODEL_PROXY_TOKEN` | unset | Bearer token required on every request; unset means no authentication |
| `MODEL_PROXY_READ_TIMEOUT` | `1800` | Seconds the upstream may stay silent (between bytes, not total request time) before `504`. A non-streaming completion sends nothing until it finishes, so keep this above the longest one; lower it to free a hung slot sooner |
| `MODEL_PROXY_MAX_BODY_MB` | `32` | Largest request body accepted, in MiB; larger gets `413` |
| `MODEL_PROXY_ACQUIRE_TIMEOUT` | `900` | Seconds a queued request waits before giving up |
| `MODEL_PROXY_LOG` | `INFO` | Log level |

## Observability

```bash
curl -s localhost:8099/_proxy/status
# {"upstream":"http://10.10.50.122:8090","auth":false,"current_model":null,"active":0,"queued":[],
#  "reasoning_off_translated":0,"reasoning_off_forwarded":0,"tool_schema_keywords_dropped":0}
```

Journal: `journalctl --user -u hermes-model-queue`. A `switching box to <model>`
line appears per admission and `queueing <model> behind in-flight <other>` only
when the gate actually made a request wait. Upstream errors are logged as
`upstream 400 for POST /v1/chat/completions (model=...): <first 300 bytes>`.

### About the HTTP 400s seen in the journal

The 7-day review (2026-11-13) counted 261 `400` answers out of 2,843 chat
requests (9.2 percent). All 261 came from one tailnet client (skullsaints), at
four-minute intervals, on 6, 7 and 9 November, which is before the llama.cpp
cutover; the response sizes were a constant 477 or 752 bytes, and no `400` has
been logged since the cutover. The upstream bodies were not logged at the time,
so the exact message is unrecorded; the pattern (one client, fixed interval, repeated
sizes, none since the backend change) points to a recurring request the old
backend rejected rather than a proxy fault, but that is inference, not
measurement. The proxy now logs the upstream error
body so any recurrence is identifiable.

## As a Hermes plugin

Dropping this directory into `~/.hermes/plugins/` and adding `aibox-model-queue`
to `plugins.enabled` starts the proxy inside the gateway process. It **only**
does so when the port is not already bound, so a systemd instance always wins.

Running it under systemd is preferred: a shared serializing resource should
outlive any one client process, and the CLI and cron reach the box too.

## Tests

The tests need only Python 3 and `aiohttp`. In a container:

```bash
docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh -c \
  'pip install -q aiohttp && for t in tests/test_*.py; do python -B "$t" || exit 1; done'
```

| File | Covers |
|---|---|
| `test_gate.py` | same-model concurrency, different-model serialization, no-downgrade, FIFO fairness |
| `test_reasoning.py` | off-switch translation, as a unit and through the handler |
| `test_schema.py` | schema cleaning, including a parameter named `pattern` surviving |
| `test_disconnect.py` | client hang-up frees the slot and abandons the upstream |
| `test_release_cancel.py` | a cancelled handler does not leak its gate slot |
| `test_limits.py` | defaults (`:8090`, loopback, no token), bearer token and its non-forwarding, `413`, `504`/`502`, upstream error logging |
| `test_install.py` | `install.sh` keeps an installed unit's bind and port, probes every bound address |
