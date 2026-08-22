# API Error Auto-Fix Agent — Design Document

## Overview

This agent monitors a Flask API instrumented with OpenTelemetry, automatically triaging 5xx errors by mapping the failing URL to a known GitHub repository, invoking a Claude AI agent to diagnose the issue and generate a fix, applying and testing the fix in an isolated branch, opening a GitHub PR, and firing a PagerDuty incident to notify the owning team.

The goal is to reduce mean-time-to-resolution (MTTR) for recurring or diagnosable API errors and create a clear audit trail (the PR) of every automated remediation attempt.

---

## System Architecture

```
Flask API (your existing service)
  └── OpenTelemetry SDK (auto + manual instrumentation)
        └── OTLP exporter (gRPC, port 4317)
              └── OpenTelemetry Collector
                    ├── Observability backend (Jaeger / Tempo / etc.)
                    └── Error processor → agent queue (Redis / SQS)
                                │
                                ▼
                    Error parser & URL router   ← resolves URL → repo + team
                                │
                                ▼
                        Claude AI agent         ← diagnoses error, writes patch
                                │
                    ┌───────────┼────────────┐
                    ▼           ▼            ▼
                 Clone      Apply fix    Run tests
                                │
                                ▼ (always)
                    Create GitHub PR  ──→  PagerDuty alert
```

---

## Component Details

### 1. Flask + OpenTelemetry Instrumentation

**Responsibility:** Emit structured trace spans and exception data from your existing Flask API so the agent has rich, reliable error context — URL, status code, exception type, and stack trace — without custom logging code per route.

**Installation:**

```bash
pip install opentelemetry-sdk \
            opentelemetry-instrumentation-flask \
            opentelemetry-exporter-otlp-proto-grpc
```

**SDK setup (add to your Flask app entrypoint):**

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
import os

resource = Resource.create({
    "service.name": "payments-api",
    "deployment.environment": os.getenv("ENV", "production"),
})
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(endpoint="http://otel-collector:4317", insecure=True)
    )
)
trace.set_tracer_provider(provider)
FlaskInstrumentor().instrument_app(app)
```

This automatically captures `http.url`, `http.method`, `http.status_code`, and `http.route` on every request span.

**Recording exceptions (required for stack traces):**

Auto-instrumentation captures status codes but not exception details. Use a Flask error handler to capture stack traces across all routes without per-route changes:

```python
from opentelemetry import trace as otel_trace

@app.errorhandler(Exception)
def handle_exception(e):
    span = otel_trace.get_current_span()
    if span.is_recording():
        span.record_exception(e)          # captures type + message + stack trace
        span.set_status(otel_trace.StatusCode.ERROR, str(e))
    return jsonify({"error": "internal server error"}), 500
```

Or per-route when you need finer control:

```python
tracer = trace.get_tracer(__name__)

@app.route("/v2/payments/charge", methods=["POST"])
def charge():
    with tracer.start_as_current_span("payments.charge") as span:
        try:
            return jsonify(payment_processor.charge(request.json))
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            raise
```

Without `record_exception`, the agent only sees the URL and status code — not the stack trace it needs to locate the fault in the source files.

---

### 2. OpenTelemetry Collector

**Responsibility:** Receive spans from Flask, fan out to your observability backend, and detect 5xx error spans to push to the agent queue.

**Deployment:** Sidecar container alongside your Flask app (same Kubernetes pod), or a shared Daemonset. Receives on port 4317 (gRPC OTLP).

**Collector config (`otel-collector-config.yaml`):**

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  # Drop non-error spans before routing to the agent pipeline
  filter/errors_only:
    error_mode: ignore
    traces:
      span:
        - 'attributes["http.status_code"] < 500'

  # Optional: scrub PII from span attributes before the agent sees them
  attributes/scrub:
    actions:
      - key: http.url
        action: delete
      - key: http.request.body
        action: delete

  batch:
    send_batch_size: 10
    timeout: 5s

exporters:
  # Normal observability backend
  otlp/backend:
    endpoint: http://jaeger:4317
    tls:
      insecure: true

  # Agent ingest adapter (your small internal service)
  otlphttp/agent_queue:
    endpoint: http://agent-ingest:4318
    tls:
      insecure: true

service:
  pipelines:
    traces/all:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/backend]

    traces/errors:
      receivers: [otlp]
      processors: [filter/errors_only, attributes/scrub, batch]
      exporters: [otlphttp/agent_queue]
```

**Agent ingest adapter:**

A small internal FastAPI service that deserializes OTLP payloads, extracts agent-relevant fields, deduplicates, and writes to the queue. This replaces the old webhook receiver — no auth to validate, no payload schema to define:

```python
# agent_ingest.py
from fastapi import FastAPI, Request
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
import redis, hashlib, json

app = FastAPI()
rdb = redis.Redis(host="redis", port=6379)

@app.post("/v1/traces")
async def ingest(request: Request):
    body = await request.body()
    export_req = trace_service_pb2.ExportTraceServiceRequest()
    export_req.ParseFromString(body)

    for resource_spans in export_req.resource_spans:
        attrs = {a.key: a.value.string_value
                 for a in resource_spans.resource.attributes}
        service = attrs.get("service.name", "unknown")
        environment = attrs.get("deployment.environment", "unknown")

        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                span_attrs = {a.key: a.value.string_value
                              for a in span.attributes}
                exc_event = next(
                    (e for e in span.events if e.name == "exception"), None
                )
                exc_attrs = ({a.key: a.value.string_value for a in exc_event.attributes}
                             if exc_event else {})

                error_event = {
                    "error_id":       span.trace_id.hex(),
                    "timestamp":      span.end_time_unix_nano // 1_000_000_000,
                    "route":          span_attrs.get("http.route", ""),
                    "method":         span_attrs.get("http.method", ""),
                    "status_code":    int(span_attrs.get("http.status_code", 500)),
                    "message":        exc_attrs.get("exception.message", ""),
                    "stack_trace":    exc_attrs.get("exception.stacktrace", ""),
                    "exception_type": exc_attrs.get("exception.type", ""),
                    "service":        service,
                    "environment":    environment,
                }

                # Deduplicate: same route + exception type within a 5-min window
                dedup_key = hashlib.sha256(
                    f"{error_event['route']}:{error_event['exception_type']}".encode()
                ).hexdigest()

                if not rdb.exists(f"dedup:{dedup_key}"):
                    rdb.setex(f"dedup:{dedup_key}", 300, "1")
                    rdb.lpush("agent:errors", json.dumps(error_event))

    return {"status": "ok"}
```

---

### 3. Error Parser & URL Router

**Responsibility:** Pop events off the queue and resolve the failing route to a GitHub repo and owning team.

Match against `http.route` (e.g. `/v2/payments/charge`) rather than the full URL — Flask's route template is more stable for pattern matching and strips query params automatically.

**Routing config (`routes.yaml`):**

```yaml
routes:
  - pattern: "^/v2/payments"
    repo: "acme-org/payments-service"
    team: "payments-team"
    pagerduty_service_id: "P1ABC23"
    test_command: "pytest tests/"
    language: "python"

  - pattern: "^/v1/auth"
    repo: "acme-org/auth-service"
    team: "identity-team"
    pagerduty_service_id: "P9XYZ99"
    test_command: "pytest tests/"
    language: "python"

  - pattern: ".*"
    repo: null
    team: "platform-team"
    pagerduty_service_id: "P0PLATFORM"
    fallback: true
```

Routes evaluate in order, first match wins. Fallback fires PagerDuty without a fix attempt.

**Output:** An `ErrorContext` object with original error fields plus `repo`, `team`, `pagerduty_service_id`, `test_command`, and `language`.

---

### 4. Claude AI Agent

**Responsibility:** Read relevant source files, diagnose the root cause, and write a minimal patch — or escalate if the fix requires human judgment.

**Calling the API — direct Anthropic SDK (no framework needed for this single structured call):**

```python
import anthropic, json

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

def run_agent(error_context: dict, file_contents: str) -> dict:
    system_prompt = f"""You are an automated software engineer. You will be given a
production API error and the source code of the affected repository.

Your job:
1. Locate the fault using the stack trace.
2. Diagnose the root cause precisely.
3. Write the minimal code change that fixes it — nothing more.

Rules:
- Do not refactor. Do not touch unrelated code.
- Prefer the smallest diff that resolves the error.
- If the fix requires human judgment (race conditions, architectural changes,
  missing business context), return {{"action": "escalate"}} instead.
- Always return valid JSON only. No markdown, no preamble.

Repository language: {error_context['language']}
Error route: {error_context['route']}
HTTP status: {error_context['status_code']}
Exception type: {error_context['exception_type']}
Error message: {error_context['message']}
Stack trace:
{error_context['stack_trace']}

Relevant source files:
{file_contents}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": "Diagnose this error and return your response as JSON."
        }]
    )
    return json.loads(response.content[0].text)
```

**Output — fix:**

```json
{
  "action": "fix",
  "diagnosis": "charge() accesses customer.id before null check at line 42",
  "files": [
    {
      "path": "src/payments/payment_processor.py",
      "content": "... full updated file content ..."
    }
  ],
  "test_notes": "Existing test test_charge_with_missing_customer should now pass"
}
```

**Output — escalate:**

```json
{
  "action": "escalate",
  "diagnosis": "Race condition between customer lookup and charge — requires transactional fix",
  "reason": "Architectural review needed"
}
```

**File selection:** Parse the stack trace to extract file paths and line numbers. Load those files in full. Fall back to fuzzy-matching the route path segments against the repo directory structure if no stack trace is present.

---

### 5. Git Operations

- Clone using a GitHub App token. Branch: `autofix/{error_id}-{timestamp}`.
- Never commit to `main` or `master`.
- Write each file from `agent_output.files`, then commit: `[autofix] {short diagnosis}\n\nAuto-generated for error {error_id}.`
- On git failure: fire PagerDuty immediately with the diagnosis, skip PR.

---

### 6. Test Runner

Run `test_command` from routing config inside the cloned repo. Timeout: 5 minutes.

- Pass → open PR with test output as a collapsible section.
- Fail → open PR flagged "tests failing — needs human review." Alert PagerDuty regardless.
- Timeout / command missing → treat as failure.

If local test execution is risky, push the branch and trigger a GitHub Actions workflow via the API instead — status appears on the PR automatically.

---

### 7. GitHub PR Creator

- Title: `[autofix] {short diagnosis} (error {error_id})`
- Body: error details, diagnosis, changed files list, collapsible test output, auto-generated disclaimer.
- Reviewers: read `CODEOWNERS` first; fall back to team from routing config.
- Labels: `autofix`, `needs-review`.
- Auth: GitHub App with `contents: write` and `pull_requests: write`, scoped to routing config repos only.

---

### 8. PagerDuty Alert

Fires regardless of fix outcome — to notify for PR review, and to escalate unfixable errors.

```json
{
  "routing_key": "{pagerduty_integration_key}",
  "event_action": "trigger",
  "dedup_key": "{error_id}",
  "payload": {
    "summary": "[autofix] {short diagnosis} — PR #{pr_number} opened",
    "source": "api-error-agent",
    "severity": "warning",
    "component": "{service}",
    "custom_details": {
      "error_route": "{route}",
      "status_code": "{status_code}",
      "pr_url": "{github_pr_url}",
      "agent_action": "fix | escalate",
      "test_result": "pass | fail | skipped"
    }
  },
  "links": [{ "href": "{github_pr_url}", "text": "View PR" }]
}
```

Use `dedup_key: error_id` to deduplicate PagerDuty incidents for the same error burst.

---

## Error Handling & Failure Modes

| Failure point | Behavior |
|---|---|
| OTEL span missing stack trace | Agent receives route + status only; likely escalates |
| Route matches no config entry | Alert platform fallback team, no fix attempt |
| GitHub clone fails | Alert team, attach diagnosis if available |
| Claude returns `"action": "escalate"` | Alert team with diagnosis, no PR |
| Tests fail | Open PR with failure flag, alert team |
| PR creation fails | Alert team with diff attached to incident |
| PagerDuty unreachable | Log to dead-letter queue, retry up to 3× |

---

## Security Considerations

**GitHub access:** Use a GitHub App (not a PAT). Store the private key in a secrets manager. Enforce branch protection on `main`/`master` as a backstop.

**Claude API key:** Store in secrets manager. Use a dedicated key for this agent. Rotate quarterly.

**OTEL data:** Scrub PII (user IDs, emails in URLs) in the Collector `attributes/scrub` processor before spans reach the agent queue. The agent ingest adapter is internal-only — never expose it publicly.

**Code execution:** Run tests in a Docker container or isolated VM. Never allow test commands network access to production systems.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Flask instrumentation | `opentelemetry-instrumentation-flask` |
| OTEL exporter | `opentelemetry-exporter-otlp-proto-grpc` |
| Collector | OpenTelemetry Collector Contrib |
| Agent ingest adapter | Python (FastAPI) |
| Queue | Redis or AWS SQS |
| AI agent — current | Anthropic Python SDK (direct, no framework) |
| AI agent — future agentic loops | LangGraph (LLM-decides-next-steps, provider-agnostic) |
| Git operations | `GitPython` |
| GitHub API | `PyGitHub` |
| PagerDuty | Events API v2 (direct HTTP) |
| Config | YAML file (simple) or PostgreSQL (at scale) |
| Secrets | AWS Secrets Manager / HashiCorp Vault |

### AI framework strategy

The current Claude integration is a **single-shot structured call** — your code drives all logic, Claude makes one API call, returns JSON. The direct Anthropic SDK is correct here: no overhead, full control, predictable behavior.

For future agents where the LLM autonomously decides which tools to call across multiple steps, use **LangGraph**. It models agents as a state graph where nodes are actions and edges are decided by the LLM at runtime. It handles the agentic loop and state persistence natively, and is provider-agnostic — swapping Claude for GPT-4o or Gemini is a config change, not a rewrite. The two approaches coexist cleanly: direct SDK for structured single calls, LangGraph for autonomous multi-step agents.

---

## Deployment

1. All components run as separate containers: Flask app, OTEL Collector (sidecar), agent ingest adapter, agent worker.
2. Agent worker is a long-running process popping from the queue. Run tests in a sidecar container to isolate LLM-patched code.
3. Agent ingest adapter is internal only — reachable only by the Collector, not the public internet.
4. Observability: emit a structured log event per pipeline run (span received → routed → agent action → test result → PR URL → PagerDuty ID). Your OTEL backend already handles this.

---

## Next Steps

1. Instrument your Flask app and verify spans appear in your OTEL backend before wiring up the agent.
2. Add `record_exception` to your Flask error handler so stack traces are captured on all routes.
3. Define `routes.yaml` — the most impactful configuration decision.
4. Set up a GitHub App with scoped permissions on target repos.
5. Create PagerDuty service integrations for each team and record integration keys.
6. Build and test the agent ingest adapter with sample OTLP payloads before connecting the Collector.
7. Integrate the Claude agent against a small set of known, reproducible errors to validate fix quality before enabling in production.
