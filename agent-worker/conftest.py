"""
Shared fixtures for agent-worker tests.
"""

import os
import shutil
import textwrap
import uuid
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from agent.models import (
    AgentFile, AgentResult, ErrorContext, ErrorEvent, RouteConfig, PipelineResult
)

# Keep all test temp artifacts inside the repo to avoid host temp-dir ACL issues.
LOCAL_TEST_TMP = Path(__file__).resolve().parent / "test_artifacts"
LOCAL_TEST_TMP.mkdir(exist_ok=True)
os.environ["TMP"] = str(LOCAL_TEST_TMP)
os.environ["TEMP"] = str(LOCAL_TEST_TMP)
os.environ["TMPDIR"] = str(LOCAL_TEST_TMP)
tempfile.tempdir = str(LOCAL_TEST_TMP)

# Some sandboxed Windows environments fail during chmod/rmtree on temp cleanup.
# Ignore those cleanup permission errors so tests assert behavior, not cleanup ACLs.
_ORIGINAL_TEMP_CLEANUP = tempfile.TemporaryDirectory.cleanup


def _safe_cleanup(self):
    try:
        _ORIGINAL_TEMP_CLEANUP(self)
    except PermissionError:
        pass


tempfile.TemporaryDirectory.cleanup = _safe_cleanup


def _make_test_dir(prefix: str) -> Path:
    path = LOCAL_TEST_TMP / f"{prefix}{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


# ---------------------------------------------------------------------------
# Core model fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def error_event():
    return ErrorEvent(
        error_id="abc123",
        span_id="span1",
        timestamp=1711234567,
        route="/v2/payments/charge",
        method="POST",
        status_code=500,
        service="payments-api",
        environment="production",
        message="NoneType has no attribute 'id'",
        stack_trace='File "src/payments/processor.py", line 42, in charge\nAttributeError',
        exception_type="AttributeError",
    )


@pytest.fixture
def route_config():
    return RouteConfig(
        pattern="^/v2/payments",
        repo="acme-org/payments-service",
        team="payments-team",
        #pagerduty_routing_key="test-routing-key",
        test_command="pytest tests/ -x -q",
        language="python",
    )


@pytest.fixture
def error_context(error_event, route_config):
    return ErrorContext(event=error_event, route_config=route_config)


@pytest.fixture
def fix_result():
    return AgentResult(
        action="fix",
        diagnosis="charge() dereferences customer before null check at line 42",
        files=[AgentFile(
            path="src/payments/processor.py",
            content="def charge(customer):\n    if customer is None:\n        raise ValueError('customer required')\n    return customer.id\n"
        )],
        test_notes="test_charge_with_null_customer should now pass",
    )


@pytest.fixture
def escalate_result():
    return AgentResult(
        action="escalate",
        diagnosis="Race condition between lookup and charge",
        reason="Requires architectural review",
    )


@pytest.fixture
def pipeline_result(error_context, fix_result):
    return PipelineResult(
        context=error_context,
        agent_result=fix_result,
        branch_name="autofix/abc123-1711234567",
        pr_url="https://github.com/acme-org/payments-service/pull/42",
        pr_number=42,
        test_passed=True,
        test_output="1 passed in 0.5s",
    )


# ---------------------------------------------------------------------------
# Routes config file fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def routes_yaml():
    """Write a temporary routes.yaml and return its path."""
    config = textwrap.dedent("""
        routes:
          - pattern: "^/v2/payments"
            repo: "acme-org/payments-service"
            team: "payments-team"
            test_command: "pytest tests/"
            language: "python"

          - pattern: "^/v1/auth"
            repo: "acme-org/auth-service"
            team: "identity-team"
            test_command: "pytest tests/"
            language: "python"

          - pattern: ".*"
            repo: null
            team: "platform-team"
            test_command: ""
            language: ""
            fallback: true
    """)
    tmp_root = _make_test_dir("routes_config_")
    p = tmp_root / "routes.yaml"
    p.write_text(config)
    yield str(p)
    shutil.rmtree(tmp_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fake repo fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_repo():
    """
    Creates a minimal fake cloned repo on disk with the files the
    file_selector and git_ops modules expect to find.
    """
    tmp_root = _make_test_dir("fake_repo_")
    repo_root = tmp_root / "repo"
    src = repo_root / "src" / "payments"
    src.mkdir(parents=True)

    (src / "processor.py").write_text(
        "def charge(customer):\n    return customer.id\n"
    )
    (repo_root / "tests").mkdir()
    (repo_root / "tests" / "test_processor.py").write_text(
        "def test_charge_with_null_customer():\n    pass\n"
    )

    # Minimal git repo so GitPython doesn't complain
    import git
    git_repo = git.Repo.init(str(repo_root))
    git_repo.index.add(["src/payments/processor.py", "tests/test_processor.py"])
    git_repo.index.commit("initial commit")

    yield str(repo_root)
    shutil.rmtree(tmp_root, ignore_errors=True)
