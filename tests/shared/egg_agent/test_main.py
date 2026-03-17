"""Tests for egg_agent.__main__ CLI entry point."""

import sys
from dataclasses import dataclass, field
from io import StringIO
from types import ModuleType
from typing import Any
from unittest.mock import patch

from egg_agent.result import AgentResult

# ── Mock SDK types ──────────────────────────────────────────────────────────
#
# claude-agent-sdk is only installed inside sandbox containers, not in CI.
# Create compatible mock types so tests run in both environments.

try:
    from claude_agent_sdk import (  # noqa: F401
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKError,
        CLINotFoundError,
        ProcessError,
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

    class ClaudeSDKError(Exception):  # type: ignore[no-redef]
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
        setting_sources: list[str] | None = None

    # Install mock module so __main__.py's lazy import finds it
    _mock_sdk = ModuleType("claude_agent_sdk")
    _mock_sdk.TextBlock = TextBlock  # type: ignore[attr-defined]
    _mock_sdk.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
    _mock_sdk.ResultMessage = ResultMessage  # type: ignore[attr-defined]
    _mock_sdk.ProcessError = ProcessError  # type: ignore[attr-defined]
    _mock_sdk.CLINotFoundError = CLINotFoundError  # type: ignore[attr-defined]
    _mock_sdk.ClaudeSDKError = ClaudeSDKError  # type: ignore[attr-defined]
    _mock_sdk.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    _mock_sdk.query = None  # type: ignore[attr-defined]
    sys.modules["claude_agent_sdk"] = _mock_sdk

from egg_agent.__main__ import main


class TestMain:
    """Tests for the CLI entry point."""

    @patch("egg_agent.__main__.run_agent")
    def test_streams_output_in_realtime(self, mock_run_agent):
        """Verify on_output callback is passed so output streams to stdout."""
        mock_run_agent.return_value = AgentResult(
            success=True,
            stdout="Hello",
            stderr="",
            returncode=0,
        )

        with patch("sys.argv", ["egg_agent", "test prompt"]):
            main()

        # Verify on_output was passed to run_agent
        _, kwargs = mock_run_agent.call_args
        assert kwargs.get("on_output") is not None, (
            "on_output callback must be passed to run_agent for realtime streaming"
        )

    @patch("egg_agent.__main__.run_agent")
    def test_on_output_writes_to_stdout(self, mock_run_agent):
        """Verify the on_output callback writes text to stdout."""

        def capturing_run_agent(prompt, **kwargs):
            # Invoke the callback during run_agent (while sys.stdout is patched)
            cb = kwargs.get("on_output")
            if cb:
                cb("streaming text")
            return AgentResult(
                success=True,
                stdout="",
                stderr="",
                returncode=0,
            )

        mock_run_agent.side_effect = capturing_run_agent

        captured = StringIO()
        with patch("sys.argv", ["egg_agent", "test prompt"]), patch("sys.stdout", captured):
            main()

        assert "streaming text" in captured.getvalue()

    @patch("egg_agent.__main__.run_agent")
    def test_no_duplicate_stdout(self, mock_run_agent):
        """Output should not be printed twice (once via callback, once at end)."""
        mock_run_agent.return_value = AgentResult(
            success=True,
            stdout="already streamed",
            stderr="",
            returncode=0,
        )

        captured = StringIO()
        with patch("sys.argv", ["egg_agent", "test prompt"]), patch("sys.stdout", captured):
            main()

        # stdout should NOT contain the result.stdout since it was already
        # streamed via on_output during execution
        assert "already streamed" not in captured.getvalue()

    @patch("egg_agent.__main__.run_agent")
    def test_stderr_still_printed(self, mock_run_agent):
        """Stderr should still be printed at the end."""
        mock_run_agent.return_value = AgentResult(
            success=False,
            stdout="",
            stderr="error msg",
            returncode=1,
        )

        captured_err = StringIO()
        with patch("sys.argv", ["egg_agent", "test prompt"]), patch("sys.stderr", captured_err):
            code = main()

        assert code == 1
        assert "error msg" in captured_err.getvalue()
