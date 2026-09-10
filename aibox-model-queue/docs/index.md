# aibox-model-queue

Serializes inference across a **single-GPU LM Studio host** so that clients
bound to different models queue for the box instead of racing for it.

## The problem

The box holds exactly one model resident. LM Studio JIT-evicts correctly when
asked for a different one, so *sequential* switching is fine (measured on this
hardware: ~11 s to swap in a 16 GB 27B). Concurrency is what breaks.

Two clients bound to different models will eventually have a request in flight
at the same moment. That reproduces deterministically:

```
google/gemma-4-12b-qat  -> ok: 'OK'
unsloth/qwen3.8-27b     -> ERROR: Model is unloaded.
```

One request wins the box; the other is evicted mid-flight. Upstream this
surfaces as `Model is unloaded.` or a 400 `Engine protocol startup was
aborted`, which callers typically classify as non-retryable and abort on.

## Why not a model fallback

Configuring a fallback model makes the loser retry elsewhere, so the request
survives. But a room deliberately bound to the 27B then answers on the 12B
exactly when it is busiest, trading a visible failure for an invisible quality
regression. The reason to bind a room to a bigger model is to get the bigger
model. **This proxy never downgrades a request.**

## Behaviour

| Situation | Result |
|---|---|
| Requests naming the **same** model | Run concurrently (LM Studio handles this well) |
| Request naming a **different** model | Waits for the box to drain, then takes it |
| Queued request behind same-model traffic | FIFO fairness; cannot be starved |
| Non-inference paths (`/v1/models`, …) | Pass straight through, ungated |

The gate is released only after the response body is fully relayed, so
streaming responses hold the box for their whole lifetime.

## Install

```bash
./install.sh          # installs + starts the systemd user unit on :8099
# Or pass the LM Studio endpoint:
./install.sh http://192.168.1.50:1234
```

Then point clients at the proxy instead of the box:

```yaml
base_url: http://127.0.0.1:8099/v1
```

## Configuration

Environment variables (set them in the unit file):

| Variable | Default | Meaning |
|---|---|---|
| `MODEL_PROXY_UPSTREAM` | `http://10.10.50.122:8080` | Fallback LM Studio host when no URL argument is passed to `install.sh`. |
| `MODEL_PROXY_HOST` | `127.0.0.1` | Bind address |
| `MODEL_PROXY_PORT` | `8099` | Bind port |
| `MODEL_PROXY_ACQUIRE_TIMEOUT` | `900` | Seconds a queued request waits before giving up |
| `MODEL_PROXY_LOG` | `INFO` | Log level |

## Observability

```bash
curl -s localhost:8099/_proxy/status
# {"upstream":"...","current_model":"unsloth/qwen3.8-27b","active":1,"queued":["google/gemma-4-12b-qat"]}
```

## As a Hermes plugin

Dropping this directory into `~/.hermes/plugins/` and adding
`aibox-model-queue` to `plugins.enabled` starts the proxy inside the gateway
process. It **only** does so when the port is not already bound, so a systemd
instance always wins.

Running it under systemd is preferred: a shared serializing resource should
outlive any one client process, and the CLI and cron reach the box too.

## Tests

```bash
python tests/test_gate.py
```

Covers same-model concurrency, different-model serialization, no-downgrade, and
FIFO fairness. Each assertion has been mutation-tested: removing serialization
fails `different models serialize`, and removing the fairness check fails
`queued B goes before a later A`.
