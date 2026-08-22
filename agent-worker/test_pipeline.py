"""
Integration tests for worker.py process_event()

Covers the full pipeline with all external calls mocked:
  - fix path: clone → agent fix → push → tests pass → PR created 
  - test failure → PR still opened
"""

import pytest
from unittest.mock import patch, MagicMock

from agent.models import (
    AgentFile, AgentResult, ErrorContext, ErrorEvent, RouteConfig, PipelineResult
)


def run_pipeline(event, mock_overrides=None):
    """Helper — runs process_event with sensible defaults for all external mocks."""
    from worker import process_event
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        with _all_mocks(mock_overrides or {}):
            return process_event(event, tmpdir)


def _all_mocks(overrides: dict):
    """Context manager that patches all external calls with safe defaults."""
    from contextlib import ExitStack
    import contextlib

    defaults = {
        "worker.resolve_route": MagicMock(return_value=_default_route()),
        "worker.select_files":  MagicMock(return_value=("/tmp/fake-repo", "def charge(): pass")),
        "worker.run_agent":     MagicMock(return_value=_default_fix_result()),
        "worker.apply_and_push": MagicMock(return_value="autofix/abc123-111"),
        "worker.run_tests":     MagicMock(return_value=(True, "1 passed")),
        "worker.create_pr":     MagicMock(side_effect=lambda r, _: _with_pr(r)),
  }
    defaults.update(overrides)

    @contextlib.contextmanager
    def ctx():
        with ExitStack() as stack:
            for target, mock in defaults.items():
                stack.enter_context(patch(target, mock))
            yield defaults

    return ctx()


def _default_route():
    return RouteConfig(
        pattern="^/v2/payments",
        repo="acme-org/payments-service",
        team="payments-team",
        test_command="pytest tests/",
        language="python",
    )


def _default_fix_result():
    return AgentResult(
        action="fix",
        diagnosis="null dereference at line 42",
        files=[AgentFile(path="src/app.py", content="fixed")],
        test_notes="",
    )


def _with_pr(result: PipelineResult) -> PipelineResult:
    result.pr_url = "https://github.com/acme-org/payments-service/pull/1"
    result.pr_number = 1
    return result


def _make_event(route="/v2/payments/charge"):
    return ErrorEvent(
        error_id="abc123",
        span_id="",
        timestamp=0,
        route=route,
        method="POST",
        status_code=500,
        service="payments-api",
        environment="production",
        message="error",
        stack_trace='File "src/app.py", line 42',
        exception_type="AttributeError",
    )


# ---------------------------------------------------------------------------
# Fix path
# ---------------------------------------------------------------------------
class TestFixPath:
    def test_pr_is_created(self):
        event = _make_event()
        with _all_mocks({}) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = process_event(event, tmpdir)
        assert result.pr_url == "https://github.com/acme-org/payments-service/pull/1"
        mocks["worker.create_pr"].assert_called_once()

        with _all_mocks({}) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                process_event(event, tmpdir)

    def test_branch_name_set_on_result(self):
        event = _make_event()
        with _all_mocks({}) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = process_event(event, tmpdir)
        assert result.branch_name == "autofix/abc123-111"

    def test_test_result_captured(self):
        event = _make_event()
        with _all_mocks({}) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = process_event(event, tmpdir)
        assert result.test_passed is True
        assert "1 passed" in result.test_output


# ---------------------------------------------------------------------------
# Escalate path
# ---------------------------------------------------------------------------
class TestEscalatePath:
    def test_no_pr_on_escalate(self):
        event = _make_event()
        escalate = AgentResult(
            action="escalate",
            diagnosis="race condition",
            reason="needs review",
        )
        with _all_mocks({"worker.run_agent": MagicMock(return_value=escalate)}) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = process_event(event, tmpdir)
        mocks["worker.create_pr"].assert_not_called()
        assert result.pr_url == ""

    def test_no_git_ops_on_escalate(self):
        event = _make_event()
        escalate = AgentResult(action="escalate", diagnosis="x", reason="y")
        with _all_mocks({"worker.run_agent": MagicMock(return_value=escalate)}) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                process_event(event, tmpdir)
        mocks["worker.apply_and_push"].assert_not_called()


# ---------------------------------------------------------------------------
# Fallback route
# ---------------------------------------------------------------------------
class TestFallbackRoute:
    def test_fallback_skips_fix_attempt(self):
        event = _make_event("/unknown/endpoint")
        fallback = RouteConfig(
            pattern=".*",
            repo=None,
            team="platform-team",
            test_command="",
            language="",
            fallback=True,
        )
        with _all_mocks({"worker.resolve_route": MagicMock(return_value=fallback)}) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                process_event(event, tmpdir)
        mocks["worker.run_agent"].assert_not_called()


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
class TestFailureModes:
    def test_git_push_failure_escalates(self):
        event = _make_event()
        with _all_mocks({
            "worker.apply_and_push": MagicMock(side_effect=Exception("push failed"))
        }) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = process_event(event, tmpdir)
        assert result.agent_result.action == "escalate"
        mocks["worker.create_pr"].assert_not_called()

    def test_test_failure_still_opens_pr(self):
        event = _make_event()
        with _all_mocks({
            "worker.run_tests": MagicMock(return_value=(False, "1 failed"))
        }) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = process_event(event, tmpdir)
        mocks["worker.create_pr"].assert_called_once()
        assert result.test_passed is False

    def test_clone_failure_escalates(self):
        event = _make_event()
        with _all_mocks({
            "worker.select_files": MagicMock(side_effect=Exception("clone failed"))
        }) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = process_event(event, tmpdir)
        mocks["worker.run_agent"].assert_not_called()

    def test_claude_api_failure_escalates(self):
        event = _make_event()
        with _all_mocks({
            "worker.run_agent": MagicMock(side_effect=Exception("API error"))
        }) as mocks:
            from worker import process_event
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                result = process_event(event, tmpdir)
        assert result.agent_result.action == "escalate"
