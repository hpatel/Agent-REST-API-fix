"""
Runs the repository's test suite in a sandboxed subprocess.

Returns (passed: bool, output: str).
A failed or timed-out test suite does NOT block PR creation — the PR is
opened with a clear failure flag so a human can review.
"""

import logging
import os
import subprocess

from agent.models import ErrorContext

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = int(os.environ.get("TEST_TIMEOUT_SECONDS", "300"))


def run_tests(context: ErrorContext, repo_local_path: str) -> tuple[bool, str]:
    """
    Runs context.route_config.test_command inside repo_local_path.

    Returns:
        (passed, output)  —  output is the combined stdout+stderr (truncated
                              to 10k chars for the PR body).
    """
    test_command = context.route_config.test_command
    event = context.event

    if not test_command:
        logger.warning(
            "No test command configured for route — skipping tests",
            extra={"error_id": event.error_id, "route": context.route_config.pattern},
        )
        return False, "No test command configured for this service."

    logger.info(
        "Running tests",
        extra={
            "error_id": event.error_id,
            "command": test_command,
            "cwd": repo_local_path,
        },
    )

    try:
        result = subprocess.run(
            test_command,
            shell=True,
            cwd=repo_local_path,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
            # Minimal environment — no production credentials in the test sandbox
            env={
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", "/root"),
                "PYTHONPATH": repo_local_path,
            },
        )
        passed = result.returncode == 0
        output = (result.stdout + result.stderr).strip()

    except subprocess.TimeoutExpired:
        logger.warning(
            "Test run timed out",
            extra={"error_id": event.error_id, "timeout": DEFAULT_TIMEOUT},
        )
        passed = False
        output = f"Test run timed out after {DEFAULT_TIMEOUT} seconds."

    except Exception as e:
        logger.error(
            "Test runner error",
            extra={"error_id": event.error_id, "error": str(e)},
        )
        passed = False
        output = f"Test runner failed to execute: {e}"

    # Truncate for the PR body
    if len(output) > 10_000:
        output = output[:10_000] + "\n\n... [truncated — see CI for full output]"

    logger.info(
        "Test run complete",
        extra={
            "error_id": event.error_id,
            "passed": passed,
            "output_chars": len(output),
        },
    )

    return passed, output
