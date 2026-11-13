# aibox-model-queue

Proxy in front of the AI box's llama.cpp `llama-server` (`qwen3.8-27b` on
`:8090`; bge embeddings are on `:8080`; LM Studio is disabled since 2026-11-11).
It serializes requests for different models, cleans tool schemas the grammar
compiler rejects, and translates Hermes' reasoning off-switch. With a single
resident model the serialization gate never queues; the schema cleaning is what
remains useful.

**Documentation:** <https://harshitruwali.github.io/homelab-infra/aibox-model-queue/>
(source: [`docs/index.md`](docs/index.md)): behaviour, configuration, clients,
authentication, tests.

```bash
MODEL_PROXY_HOST=127.0.0.1,100.88.214.113 ./install.sh   # ubuntu-dev: loopback plus the tailnet address
./install.sh                      # fresh install on 127.0.0.1:8099; a re-run keeps the installed unit's bind
./install.sh http://HOST:PORT     # a different upstream (default http://10.10.50.122:8090)
```

Then point clients at `http://127.0.0.1:8099/v1`. Status:
`curl -s localhost:8099/_proxy/status`.

Tests (need only `aiohttp`):

```bash
docker run --rm -v "$PWD":/w -w /w python:3.13-slim sh -c \
  'pip install -q aiohttp && for t in tests/test_*.py; do python -B "$t" || exit 1; done'
```
