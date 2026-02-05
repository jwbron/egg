"""Tests for egg_logging.cli module."""

import sys
from unittest.mock import MagicMock, patch

from egg_logging.cli import (
    _run_wrapper,
    main,
)


class TestRunWrapper:
    """Tests for _run_wrapper helper function."""

    @patch.object(sys, "argv", ["egg-git", "status"])
    def test_passes_args_to_wrapper(self):
        """Test that arguments are passed to the wrapper."""
        mock_wrapper_class = MagicMock()
        mock_wrapper = MagicMock()
        mock_wrapper_class.return_value = mock_wrapper
        mock_wrapper.run.return_value = MagicMock(
            exit_code=0,
            stdout="output",
            stderr="",
        )

        result = _run_wrapper(mock_wrapper_class, "git")

        mock_wrapper.run.assert_called_once_with("status")
        assert result == 0

    @patch.object(sys, "argv", ["egg-git", "push", "origin", "main"])
    def test_passes_multiple_args(self):
        """Test that multiple arguments are passed correctly."""
        mock_wrapper_class = MagicMock()
        mock_wrapper = MagicMock()
        mock_wrapper_class.return_value = mock_wrapper
        mock_wrapper.run.return_value = MagicMock(
            exit_code=0,
            stdout="",
            stderr="",
        )

        _run_wrapper(mock_wrapper_class, "git")

        mock_wrapper.run.assert_called_once_with("push", "origin", "main")

    @patch.object(sys, "argv", ["egg-git", "status"])
    @patch("builtins.print")
    def test_prints_stdout(self, mock_print):
        """Test that stdout is printed."""
        mock_wrapper_class = MagicMock()
        mock_wrapper = MagicMock()
        mock_wrapper_class.return_value = mock_wrapper
        mock_wrapper.run.return_value = MagicMock(
            exit_code=0,
            stdout="command output",
            stderr="",
        )

        _run_wrapper(mock_wrapper_class, "git")

        mock_print.assert_any_call("command output", end="")

    @patch.object(sys, "argv", ["egg-git", "status"])
    def test_returns_exit_code(self):
        """Test that exit code is returned."""
        mock_wrapper_class = MagicMock()
        mock_wrapper = MagicMock()
        mock_wrapper_class.return_value = mock_wrapper
        mock_wrapper.run.return_value = MagicMock(
            exit_code=42,
            stdout="",
            stderr="",
        )

        result = _run_wrapper(mock_wrapper_class, "git")

        assert result == 42

    @patch.object(sys, "argv", ["egg-git", "status"])
    @patch.dict("os.environ", {"EGG_LOGGING_PASSTHROUGH": "1"})
    @patch("subprocess.run")
    def test_passthrough_mode(self, mock_subprocess):
        """Test that passthrough mode skips wrapper."""
        mock_subprocess.return_value = MagicMock(returncode=0)

        mock_wrapper_class = MagicMock()
        result = _run_wrapper(mock_wrapper_class, "git")

        # Wrapper should not be instantiated
        mock_wrapper_class.assert_not_called()
        # subprocess.run should be called directly
        mock_subprocess.assert_called_once_with(["git", "status"], check=False)
        assert result == 0


class TestMainDispatcher:
    """Tests for main() dispatcher function."""

    @patch.object(sys, "argv", ["cli", "git", "status"])
    @patch("egg_logging.cli.git_main")
    def test_dispatches_to_git(self, mock_git_main):
        """Test that git command is dispatched correctly."""
        mock_git_main.return_value = 0

        result = main()

        mock_git_main.assert_called_once()
        assert result == 0

    @patch.object(sys, "argv", ["cli", "gh", "pr", "list"])
    @patch("egg_logging.cli.gh_main")
    def test_dispatches_to_gh(self, mock_gh_main):
        """Test that gh command is dispatched correctly."""
        mock_gh_main.return_value = 0

        result = main()

        mock_gh_main.assert_called_once()
        assert result == 0

    @patch.object(sys, "argv", ["cli", "claude", "-p", "hello"])
    @patch("egg_logging.cli.claude_main")
    def test_dispatches_to_claude(self, mock_claude_main):
        """Test that claude command is dispatched correctly."""
        mock_claude_main.return_value = 0

        result = main()

        mock_claude_main.assert_called_once()
        assert result == 0

    @patch.object(sys, "argv", ["cli"])
    @patch("builtins.print")
    def test_missing_tool_shows_usage(self, mock_print):
        """Test that missing tool shows usage."""
        result = main()

        assert result == 1

    @patch.object(sys, "argv", ["cli", "unknown"])
    @patch("builtins.print")
    def test_unknown_tool_shows_error(self, mock_print):
        """Test that unknown tool shows error."""
        result = main()

        assert result == 1
