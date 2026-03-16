"""Tests for the interactive runner (llm.runner).

Covers the execvpe failure path added to run_interactive().
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add sandbox/ to sys.path so llm module is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from llm.runner import run_interactive


class TestRunInteractiveExecFailure:
    """Tests for run_interactive when execvpe fails."""

    @patch("llm.runner.shutil.which", return_value="/usr/bin/claude")
    @patch("llm.runner.os.execvpe", side_effect=OSError(13, "Permission denied"))
    def test_exits_255_on_exec_failure(self, mock_exec, mock_which, capsys):
        with pytest.raises(SystemExit) as exc_info:
            run_interactive()

        assert exc_info.value.code == 255
        captured = capsys.readouterr()
        assert "Failed to execute" in captured.err
        assert "Permission denied" in captured.err
