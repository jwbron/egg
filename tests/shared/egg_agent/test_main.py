"""Tests for egg_agent.__main__ CLI entry point."""

import sys
from dataclasses import dataclass, field
from io import StringIO
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

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
    class SystemMessage:  # type: ignore[no-redef]
        subtype: str = ""
        data: Any = None

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
    _mock_sdk.SystemMessage = SystemMessage  # type: ignore[attr-defined]
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


class TestSessionStatePlumbing:
    """CLI wiring for the warm-resume substrate (#3200, slice-6).

    Locks the ``--resume`` / ``--session-state-file`` args and the
    best-effort ``write_session_state`` write-back: the substrate's write
    side is live in production today (every run with a state-file path
    configured), so these pin the CLI plumbing that ``test_client_resume.py``
    (which stops at ``run_agent``) does not reach.
    """

    @patch("egg_agent.__main__.write_session_state")
    @patch("egg_agent.__main__.decide_resume_session")
    @patch("egg_agent.__main__.run_agent")
    def test_resume_arg_threads_through_gate_to_run_agent(
        self, mock_run_agent, mock_decide, _mock_write
    ):
        """``--resume`` is handed to the slice-8 resume-vs-reseed gate as
        ``explicit_resume``; the gate's *decision* (not the raw arg) drives
        ``run_agent``'s ``resume`` kwarg (#3200 slice-8). With the gate
        electing to resume, that session id reaches ``run_agent``."""
        mock_decide.return_value = MagicMock(session_id="sess-prior")
        mock_run_agent.return_value = AgentResult(
            success=True, stdout="", stderr="", returncode=0, session_id="sess-1"
        )
        with patch("sys.argv", ["egg_agent", "--resume", "sess-prior", "do work"]):
            main()
        assert mock_decide.call_args.kwargs["explicit_resume"] == "sess-prior"
        assert mock_run_agent.call_args.kwargs["resume"] == "sess-prior"

    @patch("egg_agent.__main__.write_session_state")
    @patch("egg_agent.__main__.run_agent")
    def test_resume_defaults_to_none(self, mock_run_agent, _mock_write):
        mock_run_agent.return_value = AgentResult(success=True, stdout="", stderr="", returncode=0)
        with patch("sys.argv", ["egg_agent", "do work"]):
            main()
        assert mock_run_agent.call_args.kwargs["resume"] is None

    @patch("egg_agent.__main__.write_session_state")
    @patch("egg_agent.__main__.run_agent")
    def test_write_back_persists_session_id_and_occupancy(self, mock_run_agent, mock_write):
        mock_run_agent.return_value = AgentResult(
            success=True,
            stdout="",
            stderr="",
            returncode=0,
            session_id="sess-new",
            window_occupancy=12345,
        )
        with patch(
            "sys.argv",
            ["egg_agent", "--session-state-file", "/tmp/state.json", "do work"],
        ):
            main()
        mock_write.assert_called_once()
        args, kwargs = mock_write.call_args
        assert args[0] == "sess-new"
        assert args[1] == 12345
        assert kwargs["path"] == "/tmp/state.json"

    @patch("egg_agent.__main__.write_session_state")
    @patch("egg_agent.__main__.run_agent")
    def test_write_back_path_defaults_to_none(self, mock_run_agent, mock_write):
        mock_run_agent.return_value = AgentResult(
            success=True, stdout="", stderr="", returncode=0, session_id="sess-new"
        )
        with patch("sys.argv", ["egg_agent", "do work"]):
            main()
        assert mock_write.call_args.kwargs["path"] is None

    @patch("egg_agent.__main__.write_session_state", return_value=False)
    @patch("egg_agent.__main__.run_agent")
    def test_write_back_failure_does_not_change_exit_code(self, mock_run_agent, _mock_write):
        """Persistence is bookkeeping — its outcome must not drive the exit code.

        ``write_session_state`` returns ``False`` on a swallowed persistence
        failure; the exit code must still come solely from the agent's
        ``returncode`` (here a non-zero failure), proving the best-effort
        write-back is decoupled from the run's result.
        """
        mock_run_agent.return_value = AgentResult(
            success=False, stdout="", stderr="", returncode=3, session_id="sess-new"
        )
        with patch("sys.argv", ["egg_agent", "do work"]):
            code = main()
        assert code == 3
