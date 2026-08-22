"""
Integration tests for app/main.py FastAPI endpoints.

Uses FastAPI's TestClient — no real server or network needed.
Redis is replaced with fakeredis via monkeypatching.
"""

import gzip
import json
import pytest
import fakeredis

from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from conftest import make_otlp_json


# Patch Redis before importing the app so the module-level client is replaced
@pytest.fixture(autouse=True)
def patch_redis(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr("app.main.redis_client", fake)
    from app.queue import QueueWriter
    monkeypatch.setattr("app.main.queue_writer", QueueWriter(fake))
    return fake


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------
class TestHealthEndpoints:
    def test_liveness_returns_200(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readiness_returns_200_when_redis_ok(self, client):
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readiness_returns_503_when_redis_down(self, client, monkeypatch):
        from redis import ConnectionError as RedisConnectionError
        mock_redis = MagicMock()
        mock_redis.ping.side_effect = RedisConnectionError("down")
        monkeypatch.setattr("app.main.redis_client", mock_redis)
        response = client.get("/readyz")
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# POST /v1/traces — ingest endpoint
# ---------------------------------------------------------------------------
class TestIngestEndpoint:
    def test_500_span_is_enqueued(self, client, patch_redis):
        body, ct = make_otlp_json(500)
        response = client.post("/v1/traces", content=body, headers={"content-type": ct})
        assert response.status_code == 200
        assert response.json()["enqueued"] == 1
        assert response.json()["deduplicated"] == 0

    def test_200_span_is_not_enqueued(self, client, patch_redis):
        body, ct = make_otlp_json(200)
        response = client.post("/v1/traces", content=body, headers={"content-type": ct})
        assert response.status_code == 200
        assert response.json()["enqueued"] == 0

    def test_duplicate_500_is_deduplicated(self, client, patch_redis):
        body, ct = make_otlp_json(500)
        client.post("/v1/traces", content=body, headers={"content-type": ct})
        response = client.post("/v1/traces", content=body, headers={"content-type": ct})
        assert response.json()["enqueued"] == 0
        assert response.json()["deduplicated"] == 1

    def test_empty_body_returns_400(self, client):
        response = client.post("/v1/traces", content=b"")
        assert response.status_code == 400

    def test_unparseable_body_returns_200_with_parse_error(self, client):
        """Collector should not retry on bad payloads — return 200, not 5xx."""
        response = client.post(
            "/v1/traces",
            content=b"garbage",
            headers={"content-type": "application/x-protobuf"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "parse_error"

    def test_redis_down_returns_503(self, client, monkeypatch):
        from redis import ConnectionError as RedisConnectionError
        mock_writer = MagicMock()
        mock_writer.enqueue.side_effect = RedisConnectionError("down")
        monkeypatch.setattr("app.main.queue_writer", mock_writer)
        body, ct = make_otlp_json(500)
        response = client.post("/v1/traces", content=body, headers={"content-type": ct})
        assert response.status_code == 503

    def test_compressed_payload_accepted(self, client, patch_redis):
        body, ct = make_otlp_json(500, compressed=True)
        response = client.post("/v1/traces", content=body, headers={"content-type": ct})
        assert response.status_code == 200
        assert response.json()["enqueued"] == 1

    def test_uncompressed_payload_accepted(self, client, patch_redis):
        body, ct = make_otlp_json(500, compressed=False)
        response = client.post("/v1/traces", content=body, headers={"content-type": ct})
        assert response.status_code == 200
        assert response.json()["enqueued"] == 1

    def test_queue_depth_visible_in_readyz_after_enqueue(self, client, patch_redis):
        body, ct = make_otlp_json(500)
        client.post("/v1/traces", content=body, headers={"content-type": ct})
        response = client.get("/readyz")
        assert response.json()["queue_depth"] == 1
