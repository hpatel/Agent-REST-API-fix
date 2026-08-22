import logging
import os
import sys
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException
from redis import Redis, ConnectionError as RedisConnectionError

from .otlp_parser import parse_export_request
from .queue import QueueWriter

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis_client = Redis.from_url(REDIS_URL, decode_responses=False, socket_timeout=2)
queue_writer = QueueWriter(redis_client)


def debug_print(msg: str):
    """Print directly to stdout — bypasses logging config, always visible in docker logs."""
    print(f"[DEBUG] {msg}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    debug_print(f"Agent ingest starting — REDIS_URL={REDIS_URL}")
    try:
        redis_client.ping()
        debug_print("Redis connection OK")
    except RedisConnectionError as e:
        debug_print(f"Redis not reachable: {e}")
    yield


app = FastAPI(title="agent-ingest", lifespan=lifespan)


@app.post("/v1/traces", status_code=200)
async def ingest_traces(request: Request):
    body = await request.body()
    content_type = request.headers.get("content-type", "unknown")

    debug_print(f"POST /v1/traces — content-type={content_type} size={len(body)}")
    debug_print(f"body hex preview: {body[:200].hex()}")

    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    try:
        events = list(parse_export_request(body, content_type))
        debug_print(f"Parsed OK — {len(events)} error event(s) found")
    except Exception as e:
        debug_print(f"PARSE ERROR: {type(e).__name__}: {e}")
        debug_print(traceback.format_exc())
        return {"status": "parse_error", "detail": str(e)}

    enqueued = 0
    deduplicated = 0

    for event in events:
        try:
            queued = queue_writer.enqueue(event)
            if queued:
                enqueued += 1
                debug_print(f"Enqueued: error_id={event.error_id} route={event.route}")
            else:
                deduplicated += 1
                debug_print(f"Deduplicated: route={event.route}")
        except RedisConnectionError as e:
            debug_print(f"Redis error: {e}")
            raise HTTPException(status_code=503, detail="Queue unavailable")

    debug_print(f"Batch done — enqueued={enqueued} deduplicated={deduplicated}")
    return {"status": "ok", "enqueued": enqueued, "deduplicated": deduplicated}


@app.get("/healthz")
async def liveness():
    return {"status": "ok"}


@app.get("/readyz")
async def readiness():
    try:
        redis_client.ping()
        return {"status": "ok", "queue_depth": queue_writer.queue_depth()}
    except RedisConnectionError as e:
        return Response(
            content=f'{{"status":"unavailable","detail":"{e}"}}',
            status_code=503,
            media_type="application/json",
        )
