"""
Agent Worker
============
Long-running process that pops error events from Redis and drives the
full fix pipeline:

  1. Pop event from queue (BRPOP — blocks efficiently, no busy-polling)
  2. Resolve route → repo + team
  3. Clone repo + select relevant source files
  4. Call Claude agent → AgentResult (fix | escalate)
  5. If fix: apply changes, push branch, run tests, open GitHub PR
  6. Always: send PagerDuty alert
  7. Clean up cloned repo

One error event is processed at a time per worker process. Scale horizontally
by running multiple replicas — Redis queue ensures each event is processed once.
"""

import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time

from redis import Redis, ConnectionError as RedisConnectionError

from agent.models import AgentResult, ErrorContext, ErrorEvent, PipelineResult
from agent.router import resolve_route
from agent.file_selector import select_files
from agent.claude_agent import run_agent
from agent.git_ops import apply_and_push
from agent.test_runner import run_tests
from agent.pr_creator import create_pr
#from agent.pagerduty import send_alert

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
QUEUE_KEY = "agent:errors"
ROUTES_CONFIG_PATH = os.environ.get("ROUTES_CONFIG_PATH", "./config/routes.yaml")
CLONE_BASE_DIR = os.environ.get("GITHUB_CLONE_DIR", "/tmp/autofix-clones")


def debug_print(msg: str):
    """Print directly to stdout — bypasses logging config, always visible in docker logs."""
    print(f"[DEBUG] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False

def _handle_signal(sig, frame):
    global _shutdown
    logger.info(f"Received signal {sig} — finishing current job then shutting down")
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def process_event(event: ErrorEvent, clone_dir: str) -> PipelineResult:
    """
    Runs the full fix pipeline for a single error event.
    Each stage is wrapped so a failure in one stage triggers alerting
    without crashing the worker.
    """

    # --- 1. Route resolution ---
    route_config = resolve_route(event, ROUTES_CONFIG_PATH)
    context = ErrorContext(event=event, route_config=route_config)

    # Fallback route — no repo configured, alert the platform team and stop
    if route_config.fallback or not route_config.repo:
        if not route_config.repo:
            logger.warning(
                "No repo configured for route — escalating without fix attempt",
                extra={"error_id": event.error_id, "route": event.route},
            )
        elif route_config.fallback:       
            logger.info(
                "Fallback route matched — escalating without fix attempt",
                extra={"error_id": event.error_id, "team": route_config.team},
            )
        result = PipelineResult(
            context=context,
            agent_result=AgentResult(
                action="escalate",
                diagnosis=f"No repository configured for route {event.route}.",
                reason="Fallback route — manual triage required.",
            ),
        )
        #send_alert(result)
        return result

    # --- 2. Clone repo + select files ---
    repo_local_path = None
    try:
        repo_local_path, file_contents = select_files(context, clone_dir)
    except Exception as e:
        logger.error(
            "Failed to clone repo or select files",
            extra={"error_id": event.error_id, "repo": route_config.repo, "error": str(e)},
        )
        msg = str(e)
        debug_print(f"Error message: {msg}")
        result = PipelineResult(
            context=context,
            agent_result=AgentResult(
                action="escalate",
                diagnosis="Agent could not access the repository.",
                reason=str(e),
            ),
        )
        #send_alert(result)
        return result

    # --- 3. Claude agent ---
    try:
        agent_result = run_agent(context, file_contents)
    except Exception as e:
        logger.error(
            "Claude agent call failed",
            extra={"error_id": event.error_id, "error": str(e)},
        )
        result = PipelineResult(
            context=context,
            agent_result=AgentResult(
                action="escalate",
                diagnosis="Claude agent call failed.",
                reason=str(e),
            ),
        )
        #send_alert(result)
        return result

    result = PipelineResult(context=context, agent_result=agent_result)

    # --- 4. Escalate path: alert immediately, no PR ---
    if agent_result.action == "escalate":
        logger.info(
            "Agent escalated — no fix attempted",
            extra={
                "error_id": event.error_id,
                "diagnosis": agent_result.diagnosis,
                "reason": agent_result.reason,
            },
        )
        #send_alert(result)
        return result

    # --- 5. Fix path: apply changes, push branch ---
    try:
        branch_name = apply_and_push(context, agent_result, repo_local_path)
        result.branch_name = branch_name
    except Exception as e:
        logger.error(
            "Failed to push fix branch",
            extra={"error_id": event.error_id, "error": str(e)},
        )
        result.agent_result = AgentResult(
            action="escalate",
            diagnosis=agent_result.diagnosis,
            reason=f"Git push failed: {e}",
        )
        #send_alert(result)
        return result

    # --- 6. Run tests ---
    try:
        passed, output = run_tests(context, repo_local_path)
        result.test_passed = passed
        result.test_output = output
    except Exception as e:
        logger.warning(
            "Test runner raised unexpectedly",
            extra={"error_id": event.error_id, "error": str(e)},
        )
        result.test_passed = False
        result.test_output = f"Test runner error: {e}"

    # --- 7. Open GitHub PR (always, even if tests failed) ---
    try:
        result = create_pr(result, repo_local_path)
    except Exception as e:
        logger.error(
            "Failed to create GitHub PR",
            extra={"error_id": event.error_id, "branch": result.branch_name, "error": str(e)},
        )
        result.agent_result.reason = f"PR creation failed: {e}"

    # --- 8. PagerDuty alert (always) ---
    #send_alert(result)

    return result


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------
def p(msg: str):
    print(f"[WORKER] {msg}", flush=True)


def run():
    p(f"Starting — REDIS_URL={REDIS_URL} QUEUE={QUEUE_KEY} ROUTES={ROUTES_CONFIG_PATH}")

    os.makedirs(CLONE_BASE_DIR, exist_ok=True)

    try:
        redis = Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=5,
        )
        redis.ping()
        p("Redis connection OK")
    except Exception as e:
        import traceback
        p(f"FATAL: Cannot connect to Redis: {e}")
        traceback.print_exc()
        return

    p("Entering main loop — waiting for events...")

    while not _shutdown:
        try:
            item = redis.brpop(QUEUE_KEY, timeout=5)
        except RedisConnectionError as e:
            p(f"Redis connection lost — retrying in 10s: {e}")
            time.sleep(10)
            continue
        except TimeoutError:
            continue

        if item is None:
            continue

        _, raw = item
        p(f"Got item from queue: {raw[:200]}")

        try:
            event_data = json.loads(raw)
            event = ErrorEvent(**event_data)
        except Exception as e:
            import traceback
            p(f"ERROR: Failed to deserialise queue item: {e}")
            traceback.print_exc()
            continue

        p(f"Processing error_id={event.error_id} route={event.route} status={event.status_code}")

        job_clone_dir = os.path.join(CLONE_BASE_DIR, event.error_id)

        try:
            result = process_event(event, job_clone_dir)
            p(f"Pipeline complete — action={result.agent_result.action} pr={result.pr_url or 'n/a'}")
        except Exception as e:
            import traceback
            p(f"ERROR: Unhandled exception in pipeline: {e}")
            traceback.print_exc()
        finally:
            if os.path.exists(job_clone_dir):
                shutil.rmtree(job_clone_dir, ignore_errors=True)

    p("Shut down cleanly")


if __name__ == "__main__":
    run()
