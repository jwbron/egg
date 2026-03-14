"""Tests for egg_agent.client module."""

import asyncio
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from unittest.mock import patch


# ── Mock SDK types ──────────────────────────────────────────────────────────
#
# claude-agent-sdk is only installed inside sandbox containers, not in CI.
# Create compatible mock types so tests run in both environments.

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
    )
except ImportError:

    @dataclass
    class TextBlock:  # type: ignore[no-redef]
        text: str
        type: str = "text"

    @dataclass
    class AssistantMessage:  # type: ignore[no-redef]
        content: list[Any] = field(default_factory=list)
        model: str | None = None

    @dataclass
    class ResultMessage:  # type: ignore[no-redef]
        subtype: str = "result"
        duration_ms: int = 0
        duration_api_ms: int = 0
        is_error: bool = False
        num_turns: int = 0
        session_id: str = ""
        stop_reason: str = ""
        total_cost_usd: float | None = None
        usage: Any = None
        result: str | None = None
        structured_output: Any = None

    class ClaudeSDKError(Exception):
        pass

    class ProcessError(ClaudeSDKError):  # type: ignore[no-redef]
        pass

    class CLINotFoundError(ClaudeSDKError):  # type: ignore[no-redef]
        pass

    @dataclass
    class ClaudeAgentOptions:  # type: ignore[no-redef]
        permission_mode: str = ""
        model: str = ""
        cwd: str | None = None
        env: dict = field(default_factory=dict)
        max_turns: int | None = None
        system_prompt: str | None = None

    # Install mock module so client.py's lazy import finds it
    _mock_sdk = ModuleType("claude_agent_sdk")
    _mock_sdk.TextBlock = TextBlock  # type: ignore[attr-defined]
    _mock_sdk.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
    _mock_sdk.ResultMessage = ResultMessage  # type: ignore[attr-defined]
    _mock_sdk.ProcessError = ProcessError  # type: ignore[attr-defined]
    _mock_sdk.CLINotFoundError = CLINotFoundError  # type: ignore[attr-defined]
    _mock_sdk.ClaudeSDKError = ClaudeSDKError  # type: ignore[attr-defined]
    _mock_sdk.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    _mock_sdk.query = None  # type: ignore[attr-defined]  # Patched in tests
    sys.modules["claude_agent_sdk"] = _mock_sdk

from egg_agent.client import run_agent, run_agent_async


def _run_async(coro):
    """Helper to run async code in tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_assistant_msg(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-opus-4-6-20250313",
    )


def _make_result_msg(
    result: str | None = "Final result",
    is_error: bool = False,
    total_cost_usd: float | None = 0.05,
) -> ResultMessage:
    return ResultMessage(
        subtype="result",
        duration_ms=5000,
        duration_api_ms=4000,
        is_error=is_error,
        num_turns=3,
        session_id="sess-123",
        stop_reason="end_turn",
        total_cost_usd=total_cost_usd,
        usage=None,
        result=result,
        structured_output=None,
    )


async def _mock_query_success(**kwargs):
    """Async generator yielding a typical successful conversation."""
    yield _make_assistant_msg("Hello from Claude")
    yield _make_result_msg()


async def _mock_query_error(**kwargs):
    """Async generator yielding an error result."""
    yield _make_result_msg(result="Rate limit exceeded", is_error=True)


async def _mock_query_empty(**kwargs):
    """Async generator that yields no messages."""
    return
    yield  # Make this an async generator


# ── Tests ───────────────────────────────────────────────────────────────────


class TestRunAgentAsync:
    """Tests for run_agent_async."""

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_success(self, mock_query):
        """Test successful agent execution."""
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is True
        assert "Hello from Claude" in result.stdout
        assert result.returncode == 0
        assert result.cost_usd == 0.05
        assert result.num_turns == 3
        assert result.session_id == "sess-123"
        assert result.metadata == {"model": "claude-opus-4-6-20250313"}

    @patch("claude_agent_sdk.query", side_effect=_mock_query_error)
    def test_error_result(self, mock_query):
        """Test agent that returns an error."""
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is False
        assert result.error == "Rate limit exceeded"
        assert result.returncode == 1

    @patch("claude_agent_sdk.query", side_effect=_mock_query_empty)
    def test_empty_response(self, mock_query):
        """Test agent with no messages."""
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is True
        assert result.stdout == ""
        assert result.returncode == 0

    @patch("claude_agent_sdk.query")
    def test_on_output_callback(self, mock_query):
        """Test that on_output callback is called with text content."""
        captured: list[str] = []

        async def gen(**kwargs):
            yield _make_assistant_msg("chunk1")
            yield _make_assistant_msg("chunk2")
            yield _make_result_msg(result="done")

        mock_query.side_effect = gen

        _run_async(run_agent_async("test", on_output=captured.append))

        assert "chunk1" in captured
        assert "chunk2" in captured
        assert "done" in captured

    @patch("claude_agent_sdk.query")
    def test_exception_handling(self, mock_query):
        """Test that SDK exceptions are caught and returned as errors."""
        from claude_agent_sdk import ProcessError

        mock_query.side_effect = ProcessError("Process crashed")

        result = _run_async(run_agent_async("test prompt"))

        assert result.success is False
        assert "Process crashed" in result.error
        assert result.returncode == -1

    @patch("claude_agent_sdk.query")
    def test_timeout(self, mock_query):
        """Test that timeout produces a proper error result."""

        async def slow_gen(**kwargs):
            yield _make_assistant_msg("started")
            await asyncio.sleep(10)  # Will be cancelled by timeout
            yield _make_result_msg()

        mock_query.side_effect = slow_gen

        result = _run_async(run_agent_async("test prompt", timeout=1))

        assert result.success is False
        assert "Timed out" in result.error
        assert "started" in result.stdout


class TestRunAgentSync:
    """Tests for run_agent synchronous wrapper."""

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_sync_wrapper(self, mock_query):
        """Test that run_agent returns the same result as run_agent_async."""
        result = run_agent("test prompt")

        assert result.success is True
        assert "Hello from Claude" in result.stdout
