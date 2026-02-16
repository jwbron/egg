"""Tests for llm.runner module.

Tests the binary-not-found handling in run_interactive.
"""

from unittest.mock import patch

import pytest
from llm.runner import run_interactive


class TestRunInteractiveBinaryNotFound:
    """Tests for run_interactive when claude binary is not found."""

    @patch("llm.runner.shutil.which", return_value=None)
    def test_exits_when_claude_binary_not_found(self, mock_which, capsys):
        """Test that run_interactive calls sys.exit(1) when claude binary is missing."""
        with pytest.raises(SystemExit) as exc_info:
            run_interactive()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "'claude' not found in PATH" in captured.err
