"""
Shared fixtures for agent-ingest tests.
"""

import gzip
import json
import pytest
import fakeredis

from app.queue import QueueWriter
from app.models import ErrorEvent


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_redis():
    """In-memory Redis — no running Redis needed."""
    return fakeredis.FakeRedis(decode_responses=False)


@pytest.fixture
def queue_writer(fake_redis):
    return QueueWriter(fake_redis)


# ---------------------------------------------------------------------------
# OTLP payload builders
# ---------------------------------------------------------------------------
def _make_span(
    status_code: int,
    route: str = "/v2/payments/charge",
    method: str = "POST",
    service: str = "payments-api",
    environment: str = "test",
    with_exception: bool = True,
    exception_type: str = "AttributeError",
    exception_message: str = "NoneType has no attribute 'id'",
    stack_trace: str = 'File "src/payments/processor.py", line 42, in charge',
) -> dict:
    span = {
        "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
        "spanId": "00f067aa0ba902b7",
        "endTimeUnixNano": "1711234567000000000",
        "attributes": [
            {"key": "http.route",       "value": {"stringValue": route}},
            {"key": "http.method",      "value": {"stringValue": method}},
            {"key": "http.status_code", "value": {"intValue": status_code}},
        ],
    }
    if with_exception:
        span["events"] = [{
            "name": "exception",
            "attributes": [
                {"key": "exception.type",       "value": {"stringValue": exception_type}},
                {"key": "exception.message",    "value": {"stringValue": exception_message}},
                {"key": "exception.stacktrace", "value": {"stringValue": stack_trace}},
            ],
        }]
    return span


def make_otlp_json(
    status_code: int = 500,
    compressed: bool = True,
    **span_kwargs,
) -> tuple[bytes, str]:
    """
    Returns (body, content_type) for a minimal OTLP JSON export request.
    Compressed by default to match what the real Collector sends.
    """
    payload = {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name",            "value": {"stringValue": span_kwargs.pop("service", "payments-api")}},
                    {"key": "deployment.environment",  "value": {"stringValue": span_kwargs.pop("environment", "test")}},
                ]
            },
            "scopeSpans": [{
                "spans": [_make_span(status_code, **span_kwargs)]
            }]
        }]
    }
    body = json.dumps(payload).encode()
    if compressed:
        body = gzip.compress(body)
    return body, "application/json"


@pytest.fixture
def error_event():
    """A minimal valid ErrorEvent."""
    return ErrorEvent(
        error_id="4bf92f3577b34da6a3ce929d0e0e4736",
        span_id="00f067aa0ba902b7",
        timestamp=1711234567,
        route="/v2/payments/charge",
        method="POST",
        status_code=500,
        service="payments-api",
        environment="test",
        message="NoneType has no attribute 'id'",
        stack_trace='File "src/payments/processor.py", line 42, in charge',
        exception_type="AttributeError",
    )
