"""
Tests for app/queue.py

Covers:
  - enqueue writes to Redis list
  - deduplication within TTL window
  - deduplication key is scoped correctly
  - queue depth reporting
"""

import json
import pytest
import fakeredis

from app.queue import QueueWriter, QUEUE_KEY, DEDUP_TTL_SECONDS
from app.models import ErrorEvent


def make_event(
    error_id="abc123",
    route="/v2/payments",
    exception_type="ValueError",
    service="payments-api",
) -> ErrorEvent:
    return ErrorEvent(
        error_id=error_id,
        span_id="span1",
        timestamp=1711234567,
        route=route,
        method="POST",
        status_code=500,
        service=service,
        environment="test",
        message="error",
        stack_trace="",
        exception_type=exception_type,
    )


class TestEnqueue:
    def test_enqueue_adds_item_to_redis_list(self, queue_writer, fake_redis):
        event = make_event()
        result = queue_writer.enqueue(event)
        assert result is True
        assert fake_redis.llen(QUEUE_KEY) == 1

    def test_enqueued_item_is_valid_json(self, queue_writer, fake_redis):
        event = make_event()
        queue_writer.enqueue(event)
        raw = fake_redis.lrange(QUEUE_KEY, 0, 0)[0]
        data = json.loads(raw)
        assert data["error_id"] == "abc123"
        assert data["route"] == "/v2/payments"

    def test_enqueued_item_contains_all_fields(self, queue_writer, fake_redis):
        event = make_event()
        queue_writer.enqueue(event)
        raw = fake_redis.lrange(QUEUE_KEY, 0, 0)[0]
        data = json.loads(raw)
        assert "error_id" in data
        assert "route" in data
        assert "status_code" in data
        assert "service" in data
        assert "stack_trace" in data

    def test_multiple_distinct_events_all_enqueued(self, queue_writer, fake_redis):
        queue_writer.enqueue(make_event(error_id="aaa", route="/a", exception_type="ErrorA"))
        queue_writer.enqueue(make_event(error_id="bbb", route="/b", exception_type="ErrorB"))
        assert fake_redis.llen(QUEUE_KEY) == 2


class TestDeduplication:
    def test_same_event_within_window_is_deduplicated(self, queue_writer, fake_redis):
        event = make_event()
        first  = queue_writer.enqueue(event)
        second = queue_writer.enqueue(event)
        assert first  is True
        assert second is False
        assert fake_redis.llen(QUEUE_KEY) == 1

    def test_same_event_after_ttl_expiry_is_re_enqueued(self, queue_writer, fake_redis):
        import hashlib
        event = make_event()
        queue_writer.enqueue(event)

        # Manually expire the dedup key to simulate TTL passing
        dedup_hash = hashlib.sha256(event.dedup_key.encode()).hexdigest()
        fake_redis.delete(f"dedup:{dedup_hash}")

        result = queue_writer.enqueue(event)
        assert result is True
        assert fake_redis.llen(QUEUE_KEY) == 2

    def test_different_route_is_not_deduplicated(self, queue_writer, fake_redis):
        event_a = make_event(route="/payments", exception_type="ValueError")
        event_b = make_event(route="/auth",     exception_type="ValueError")
        assert queue_writer.enqueue(event_a) is True
        assert queue_writer.enqueue(event_b) is True
        assert fake_redis.llen(QUEUE_KEY) == 2

    def test_different_exception_type_is_not_deduplicated(self, queue_writer, fake_redis):
        event_a = make_event(exception_type="ValueError")
        event_b = make_event(exception_type="KeyError")
        assert queue_writer.enqueue(event_a) is True
        assert queue_writer.enqueue(event_b) is True
        assert fake_redis.llen(QUEUE_KEY) == 2

    def test_different_service_is_not_deduplicated(self, queue_writer, fake_redis):
        event_a = make_event(service="payments-api")
        event_b = make_event(service="auth-api")
        assert queue_writer.enqueue(event_a) is True
        assert queue_writer.enqueue(event_b) is True
        assert fake_redis.llen(QUEUE_KEY) == 2

    def test_dedup_key_set_with_ttl(self, queue_writer, fake_redis):
        import hashlib
        event = make_event()
        queue_writer.enqueue(event)
        dedup_hash = hashlib.sha256(event.dedup_key.encode()).hexdigest()
        ttl = fake_redis.ttl(f"dedup:{dedup_hash}")
        assert 0 < ttl <= DEDUP_TTL_SECONDS


class TestQueueDepth:
    def test_queue_depth_is_zero_initially(self, queue_writer):
        assert queue_writer.queue_depth() == 0

    def test_queue_depth_increments_on_enqueue(self, queue_writer):
        queue_writer.enqueue(make_event(route="/a", exception_type="ErrA"))
        queue_writer.enqueue(make_event(route="/b", exception_type="ErrB"))
        assert queue_writer.queue_depth() == 2
