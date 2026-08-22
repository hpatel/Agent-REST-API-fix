"""
Redis queue writer and deduplicator.

Queue layout:
  agent:errors          — LIST, right-push / left-pop by the agent worker
  dedup:<sha256>        — STRING with 5-min TTL, prevents re-queuing the same
                          error burst (same service + route + exception type)

The agent worker should BRPOP from agent:errors so it blocks efficiently
rather than busy-polling.
"""

import hashlib
import logging
from redis import Redis, ConnectionError as RedisConnectionError
from .models import ErrorEvent

logger = logging.getLogger(__name__)

QUEUE_KEY = "agent:errors"
DEDUP_TTL_SECONDS = 300  # 5 minutes


class QueueWriter:
    def __init__(self, redis_client: Redis):
        self._redis = redis_client

    def enqueue(self, event: ErrorEvent) -> bool:
        """
        Push the event to the agent queue unless a duplicate is still within
        the dedup window.

        Returns True if the event was enqueued, False if it was deduplicated.
        Raises on Redis connection failure (let the caller handle / 500).
        """
        dedup_hash = hashlib.sha256(event.dedup_key.encode()).hexdigest()
        dedup_redis_key = f"dedup:{dedup_hash}"

        # SET NX (only set if not exists) + EX (TTL) in a single atomic command
        was_set = self._redis.set(dedup_redis_key, "1", nx=True, ex=DEDUP_TTL_SECONDS)

        if not was_set:
            logger.debug(
                "Deduplicated error event",
                extra={"dedup_key": event.dedup_key, "error_id": event.error_id},
            )
            return False

        self._redis.rpush(QUEUE_KEY, event.to_json())
        logger.info(
            "Enqueued error event",
            extra={
                "error_id": event.error_id,
                "route": event.route,
                "service": event.service,
                "status_code": event.status_code,
            },
        )
        return True

    def queue_depth(self) -> int:
        """Returns current number of items in the queue (useful for healthcheck)."""
        return self._redis.llen(QUEUE_KEY)
