# agent-ingest

Agent Ingest that receives OTLP trace spans from the OpenTelemetry Collector,
extracts 5xx error events, deduplicates within a 5-minute window, and pushes
to a Redis queue for the agent worker or other services to consume.

## How it fits

```
OTEL Collector (traces/errors pipeline)
        │  POST /v1/traces  (OTLP HTTP/protobuf)
        ▼
  agent-ingest  (this service)
        │  RPUSH agent:errors
        ▼
     Redis
        │  BRPOP agent:errors
        ▼
  agent-worker
```

## Project structure

```
agent-ingest/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app, endpoints
│   ├── models.py        # ErrorEvent dataclass
│   ├── otlp_parser.py   # Deserialises OTLP protobuf → ErrorEvent
│   └── queue.py         # Redis writer
├── k8s/
│   ├── deployment.yaml          # Deployment, Service, NetworkPolicy
│   └── otel-collector-config.yaml  # Collector config with error filter pipeline
├── Dockerfile
├── requirements.txt
└── README.md
```

## Local development

**Requirements:** Python 3.12+, Docker (for Redis)

```bash
# Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the adapter
REDIS_URL=redis://localhost:6379/0 uvicorn app.main:app --reload --port 4318
```

The adapter is now listening at `http://localhost:4318/v1/traces`.

**Send a test payload:**

```bash
# Health check
curl http://localhost:4318/healthz
curl http://localhost:4318/readyz

# Simulate an OTLP payload (requires grpcurl or a test script — see below)
```

**Inspect the queue:**

```bash
docker exec -it <redis-container-id> redis-cli
> LLEN agent:errors          # queue depth
> LRANGE agent:errors 0 0    # peek at the first item
```

## Key behaviours

**Deduplication:** The same `service + route + exception_type` combination is
suppressed for 5 minutes after the first occurrence. This prevents a burst of
identical errors from spawning many simultaneous fix attempts. The TTL resets
on each new occurrence — adjust `DEDUP_TTL_SECONDS` in `queue.py` to taste.

**Collector retry:** The Collector's `retry_on_failure` config retries for up
to 2 minutes if this service returns 503 (Redis unavailable). Spans are not
lost during a brief Redis hiccup.

**Readiness probe:** The `/readyz` endpoint returns 503 if Redis is unreachable,
which removes the pod from the Service's endpoints. The Collector will get
connection-refused rather than a dangling 503, and its retry logic handles it.

**PII scrubbing:** The Collector's `attributes/scrub_pii` processor strips
`http.url`, `http.request.body`, and `enduser.id` before spans reach this
service. Add any other sensitive keys your API emits to that processor.

## Environment variables

| Variable    | Default                        | Description              |
|-------------|--------------------------------|--------------------------|
| `REDIS_URL` | `redis://localhost:6379/0`     | Redis connection string  |
