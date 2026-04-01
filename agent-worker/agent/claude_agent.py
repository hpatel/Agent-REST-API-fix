"""
Calls the Anthropic API with the assembled error context and source files.
Returns a structured AgentResult (fix or escalate).
"""

import json
import logging
import os

import anthropic

from .models import AgentFile, AgentResult, ErrorContext

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _build_system_prompt(context: ErrorContext, file_contents: str) -> str:
    event = context.event
    cfg = context.route_config

    return f"""You are an automated software engineer. You will be given a production \
API error and the source files most likely to contain the root cause.

Your job:
1. Locate the exact fault using the stack trace and source files.
2. Diagnose the root cause in one or two sentences.
3. Write the minimal code change that resolves the error — nothing more.

Rules:
- Do not refactor. Do not improve unrelated code. Do not add comments.
- Return the FULL updated file content for each file you change (not a diff).
- If the fix requires human judgment — race conditions, missing business \
context, architectural changes, or if you are not confident — return \
{{"action": "escalate"}} instead. A cautious escalation is better than a \
wrong fix reaching production.
- Return ONLY valid JSON. No markdown fences, no preamble, no explanation \
outside the JSON object.

Output schema when you can fix it:
{{
  "action": "fix",
  "diagnosis": "one or two sentence root cause explanation",
  "files": [
    {{"path": "relative/path/to/file.py", "content": "full updated file content"}}
  ],
  "test_notes": "which existing test covers this, or what to verify"
}}

Output schema when you cannot safely fix it:
{{
  "action": "escalate",
  "diagnosis": "one or two sentence root cause explanation",
  "reason": "why a safe automated fix is not possible"
}}

Repository language: {cfg.language}
Error route: {event.route}
HTTP method: {event.method}
HTTP status: {event.status_code}
Exception type: {event.exception_type}
Error message: {event.message}
Service: {event.service}
Environment: {event.environment}

Stack trace:
{event.stack_trace or "(no stack trace available)"}

Relevant source files:
{file_contents}"""


def run_agent(context: ErrorContext, file_contents: str) -> AgentResult:
    """
    Calls Claude with the assembled context and parses the JSON response
    into an AgentResult.

    Raises on API errors (let the worker handle retries/alerting).
    """
    client = _get_client()
    system_prompt = _build_system_prompt(context, file_contents)

    logger.info(
        "Calling Claude agent",
        extra={"error_id": context.event.error_id, "route": context.event.route},
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Diagnose this error and return your response as a JSON object."
                    ),
                }
            ],
        )
    except Exception as api_err:
        # Log the full error so we can see exactly what the API rejected
        print(f"[CLAUDE] API call failed: {type(api_err).__name__}: {api_err}", flush=True)
        raise

    raw = response.content[0].text.strip()

    # Strip accidental markdown fences if the model adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(
            "Agent returned non-JSON response",
            extra={"error_id": context.event.error_id, "raw": raw[:500]},
        )
        raise ValueError(f"Agent response was not valid JSON: {e}") from e

    action = data.get("action", "escalate")
    diagnosis = data.get("diagnosis", "No diagnosis provided")

    if action == "fix":
        files = [
            AgentFile(path=f["path"], content=f["content"])
            for f in data.get("files", [])
        ]
        result = AgentResult(
            action="fix",
            diagnosis=diagnosis,
            files=files,
            test_notes=data.get("test_notes", ""),
        )
    else:
        result = AgentResult(
            action="escalate",
            diagnosis=diagnosis,
            reason=data.get("reason", ""),
        )

    logger.info(
        "Agent completed",
        extra={
            "error_id": context.event.error_id,
            "action": result.action,
            "files_changed": len(result.files),
            "diagnosis": result.diagnosis[:120],
        },
    )

    return result
