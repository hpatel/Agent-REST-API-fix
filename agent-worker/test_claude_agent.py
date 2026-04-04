"""
Tests for agent/claude_agent.py

Covers:
  - fix response parsed correctly
  - escalate response parsed correctly
  - markdown fence stripping
  - malformed JSON raises ValueError
  - API errors propagate
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from agent.claude_agent import run_agent
from agent.models import AgentResult


def mock_anthropic_response(content: str):
    """Build a minimal mock that looks like an Anthropic API response."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=content)]
    return mock_response


class TestFixResponse:
    def test_fix_action_parsed(self, error_context):
        payload = json.dumps({
            "action": "fix",
            "diagnosis": "null dereference at line 42",
            "files": [{"path": "src/app.py", "content": "fixed content"}],
            "test_notes": "test_foo should pass",
        })
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.return_value = \
                mock_anthropic_response(payload)
            result = run_agent(error_context, "file content here")

        assert result.action == "fix"
        assert result.diagnosis == "null dereference at line 42"
        assert len(result.files) == 1
        assert result.files[0].path == "src/app.py"
        assert result.files[0].content == "fixed content"
        assert result.test_notes == "test_foo should pass"

    def test_multiple_files_parsed(self, error_context):
        payload = json.dumps({
            "action": "fix",
            "diagnosis": "two files need changes",
            "files": [
                {"path": "src/a.py", "content": "a"},
                {"path": "src/b.py", "content": "b"},
            ],
            "test_notes": "",
        })
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.return_value = \
                mock_anthropic_response(payload)
            result = run_agent(error_context, "")

        assert len(result.files) == 2


class TestEscalateResponse:
    def test_escalate_action_parsed(self, error_context):
        payload = json.dumps({
            "action": "escalate",
            "diagnosis": "race condition — needs architectural review",
            "reason": "cannot fix safely",
        })
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.return_value = \
                mock_anthropic_response(payload)
            result = run_agent(error_context, "")

        assert result.action == "escalate"
        assert result.diagnosis == "race condition — needs architectural review"
        assert result.reason == "cannot fix safely"
        assert result.files == []

    def test_escalate_has_no_files(self, error_context):
        payload = json.dumps({
            "action": "escalate",
            "diagnosis": "unclear root cause",
            "reason": "needs human",
        })
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.return_value = \
                mock_anthropic_response(payload)
            result = run_agent(error_context, "")

        assert result.files == []


class TestMarkdownFenceStripping:
    def test_json_fences_stripped(self, error_context):
        raw = '```json\n{"action":"escalate","diagnosis":"d","reason":"r"}\n```'
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.return_value = \
                mock_anthropic_response(raw)
            result = run_agent(error_context, "")
        assert result.action == "escalate"

    def test_plain_fences_stripped(self, error_context):
        raw = '```\n{"action":"escalate","diagnosis":"d","reason":"r"}\n```'
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.return_value = \
                mock_anthropic_response(raw)
            result = run_agent(error_context, "")
        assert result.action == "escalate"


class TestInvalidResponse:
    def test_non_json_raises_value_error(self, error_context):
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.return_value = \
                mock_anthropic_response("sorry I cannot help with that")
            with pytest.raises(ValueError, match="not valid JSON"):
                run_agent(error_context, "")

    def test_api_error_propagates(self, error_context):
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.side_effect = \
                Exception("API error")
            with pytest.raises(Exception, match="API error"):
                run_agent(error_context, "")

    def test_missing_action_defaults_to_escalate(self, error_context):
        payload = json.dumps({"diagnosis": "something went wrong"})
        with patch("agent.claude_agent._get_client") as mock_client_fn:
            mock_client_fn.return_value.messages.create.return_value = \
                mock_anthropic_response(payload)
            result = run_agent(error_context, "")
        assert result.action == "escalate"
