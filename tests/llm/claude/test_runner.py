"""
Tests for llm.claude.runner module.

Verifies the thin wrapper around egg_agent.client delegates correctly.
"""

import asyncio
from unittest.mock import patch

from llm.claude.config import ClaudeConfig
from llm.claude.runner import run_agent, run_agent_async
from llm.result import AgentResult


def _run_async(coro):
    """Helper to run async code in tests without pytest-asyncio."""
    return asyncio.run(coro)


def _make_sdk_result(**overrides):
    """Create a mock egg_agent AgentResult."""
    from egg_agent.result import AgentResult as SdkResult

    defaults = {
        "success": True,
        "stdout": "Hello",
        "stderr": "",
        "returncode": 0,
        "error": None,
        "metadata": {"model": "claude-opus-4-6-20250313"},
    }
    defaults.update(overrides)
    return SdkResult(**defaults)


class TestRunAgentAsync:
    """Tests for run_agent_async wrapper."""

    @patch("llm.claude.runner._sdk_run_agent_async")
    def test_delegates_to_sdk(self, mock_sdk):
        """Test that run_agent_async calls the SDK with correct params."""
        mock_sdk.return_value = _make_sdk_result()

        result = _run_async(
            run_agent_async(
                "test prompt",
                model="sonnet",
                cwd="/tmp/repo",
                timeout=300,
            )
        )

        mock_sdk.assert_called_once()
        call_kwargs = mock_sdk.call_args
        assert call_kwargs.args[0] == "test prompt"
        assert call_kwargs.kwargs["model"] == "sonnet"
        assert call_kwargs.kwargs["timeout"] == 300

        assert result.success is True
        assert result.stdout == "Hello"
        assert isinstance(result, AgentResult)

    @patch("llm.claude.runner._sdk_run_agent_async")
    def test_uses_config_defaults(self, mock_sdk):
        """Test that ClaudeConfig defaults are applied."""
        mock_sdk.return_value = _make_sdk_result()

        config = ClaudeConfig(timeout=600, cwd="/custom/path")
        _run_async(run_agent_async("test", config=config))

        call_kwargs = mock_sdk.call_args
        assert call_kwargs.kwargs["timeout"] == 600

    @patch("llm.claude.runner._sdk_run_agent_async")
    def test_error_result_propagated(self, mock_sdk):
        """Test that error results from SDK are propagated."""
        mock_sdk.return_value = _make_sdk_result(
            success=False,
            returncode=1,
            error="Rate limited",
        )

        result = _run_async(run_agent_async("test"))

        assert result.success is False
        assert result.error == "Rate limited"
        assert result.returncode == 1


class TestRunAgentSync:
    """Tests for run_agent synchronous wrapper."""

    @patch("llm.claude.runner._sdk_run_agent_async")
    def test_sync_wrapper(self, mock_sdk):
        """Test that run_agent calls run_agent_async synchronously."""
        mock_sdk.return_value = _make_sdk_result()

        result = run_agent("test prompt", model="opus")

        assert result.success is True
        assert result.stdout == "Hello"
