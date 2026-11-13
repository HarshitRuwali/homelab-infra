import asyncio
import logging

from app import http_client
from app.logging_filters import HealthAccessFilter


def _record(path, status):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1234", "GET", path, "1.1", status), None,
    )


def test_health_filter_drops_only_successful_health_probes():
    f = HealthAccessFilter()
    assert f.filter(_record("/health", 200)) is False
    assert f.filter(_record("/health?x=1", 200)) is False
    assert f.filter(_record("/health", 503)) is True
    assert f.filter(_record("/memory/search", 200)) is True


def test_shared_client_is_reused_and_closed():
    async def run():
        http_client._client = None
        first = http_client.get_http_client()
        assert http_client.get_http_client() is first
        await http_client.close_http_client()
        assert first.is_closed
        assert http_client._client is None
        await http_client.close_http_client()  # idempotent

    asyncio.run(run())


def test_health_requests_are_not_logged_but_others_are(client, caplog, monkeypatch):
    from app.routers import health

    def _unreachable():
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(health, "get_qdrant", _unreachable)
    caplog.set_level(logging.INFO)
    client.get("/memory/nonexistent")
    assert any("Response sent" in r.message and "/memory/nonexistent" in r.message for r in caplog.records)
    caplog.clear()
    resp = client.get("/health")  # degraded (Qdrant down) but still answers 200
    assert resp.status_code == 200
    assert not any("/health" in r.message and "Response sent" in r.message and "status=200" in r.message
                   for r in caplog.records)
    assert not any("Request started" in r.message and "/health" in r.message for r in caplog.records)
