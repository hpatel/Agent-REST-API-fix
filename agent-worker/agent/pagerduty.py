"""
Fires a PagerDuty Events API v2 incident for every pipeline run —
whether the agent produced a fix (PR needs review) or escalated
(human must diagnose).

Uses dedup_key = error_id so burst duplicates collapse in PagerDuty.
"""

import logging
import os

import httpx

from .models import ErrorContext, PipelineResult

logger = logging.getLogger(__name__)

PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
MAX_RETRIES = 3


def _build_payload(result: PipelineResult) -> dict:
    ctx = result.context
    event = ctx.event
    agent = result.agent_result

    if agent.action == "fix":
        summary = f"[autofix] {agent.diagnosis[:100]} — PR #{result.pr_number} opened"
    else:
        summary = f"[autofix] Escalation: {agent.diagnosis[:100]}"

    test_result = (
        "pass" if result.test_passed
        else "fail" if result.test_passed is False
        else "skipped"
    )

    custom_details = {
        "error_route": f"{event.method} {event.route}",
        "status_code": str(event.status_code),
        "exception_type": event.exception_type,
        "service": event.service,
        "environment": event.environment,
        "error_id": event.error_id,
        "agent_action": agent.action,
        "diagnosis": agent.diagnosis,
        "test_result": test_result,
    }

    if result.pr_url:
        custom_details["pr_url"] = result.pr_url

    if agent.action == "escalate" and agent.reason:
        custom_details["escalation_reason"] = agent.reason

    payload = {
        "routing_key": ctx.route_config.pagerduty_routing_key,
        "event_action": "trigger",
        "dedup_key": event.error_id,
        "payload": {
            "summary": summary,
            "source": "api-error-agent",
            "severity": "warning",
            "component": event.service,
            "custom_details": custom_details,
        },
    }

    if result.pr_url:
        payload["links"] = [{"href": result.pr_url, "text": "View PR"}]

    return payload


def send_alert(result: PipelineResult) -> bool:
    """
    Sends a PagerDuty alert. Returns True on success, False on failure.
    Logs errors but never raises — a PagerDuty failure should not crash the worker.
    """
    payload = _build_payload(result)
    event = result.context.event

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = httpx.post(
                PAGERDUTY_EVENTS_URL,
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            logger.info(
                "PagerDuty alert sent",
                extra={
                    "error_id": event.error_id,
                    "action": result.agent_result.action,
                    "pr_number": result.pr_number or "n/a",
                    "dedup_key": event.error_id,
                },
            )
            return True

        except httpx.HTTPStatusError as e:
            logger.warning(
                "PagerDuty returned error status",
                extra={
                    "error_id": event.error_id,
                    "attempt": attempt,
                    "status": e.response.status_code,
                    "body": e.response.text[:200],
                },
            )
        except httpx.RequestError as e:
            logger.warning(
                "PagerDuty request failed",
                extra={"error_id": event.error_id, "attempt": attempt, "error": str(e)},
            )

    logger.error(
        "PagerDuty alert failed after all retries — event lost",
        extra={"error_id": event.error_id},
    )
    return False
