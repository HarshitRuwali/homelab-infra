"""One shared httpx.AsyncClient for the AI VM proxies.

The client is created in the application lifespan and closed on shutdown, so
requests reuse keep-alive connections instead of opening a new TCP connection
each time. Every call site passes its own ``timeout=`` (embed 30 s, LLM 120 s
or none for streaming); the client default only applies when none is given.
"""

import httpx

_client: httpx.AsyncClient | None = None


def init_http_client() -> httpx.AsyncClient:
    """Create the shared client if it does not exist yet and return it."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
    return _client


def get_http_client() -> httpx.AsyncClient:
    """Return the shared client, creating it lazily outside the lifespan (tests, scripts)."""
    return init_http_client()


async def close_http_client() -> None:
    """Close the shared client; safe to call twice."""
    global _client
    client, _client = _client, None
    if client is not None:
        await client.aclose()
