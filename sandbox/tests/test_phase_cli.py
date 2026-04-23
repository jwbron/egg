"""
Tests for CLI phase commands: ``cmd_phase_start``, ``cmd_phase_complete``,
``cmd_phase_advance``.

Verifies that each command echoes the server's ``result["message"]`` rather
than constructing strings client-side, and that ``cmd_phase_complete`` appends
a CLI-specific advance hint when ``next_phase`` is present.
"""

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

from egg_lib.orch_cli import cmd_phase_advance, cmd_phase_complete, cmd_phase_start


def _make_phase_args(**overrides: object) -> argparse.Namespace:
    """Build a minimal ``argparse.Namespace`` for phase commands."""
    defaults = {
        "pipeline_id": "pipe-1",
        "json": False,
        "target_phase": "plan",
        "reason": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# cmd_phase_start
# ---------------------------------------------------------------------------


class TestCmdPhaseStart:
    """cmd_phase_start echoes server message."""

    @patch("egg_lib.orch_cli.orch_request")
    def test_echoes_server_message(self, mock_request, capsys):
        mock_request.return_value = {
            "success": True,
            "message": "Phase 'implement' marked running (does not spawn agents)",
            "data": {"phase": "implement", "current_phase": "implement", "status": "running"},
        }
        rc = cmd_phase_start(_make_phase_args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Phase 'implement' marked running (does not spawn agents)" in out

    @patch("egg_lib.orch_cli.orch_request")
    def test_error_prints_to_stderr(self, mock_request, capsys):
        mock_request.return_value = {
            "success": False,
            "message": "Phase already running",
        }
        rc = cmd_phase_start(_make_phase_args())
        assert rc == 1
        err = capsys.readouterr().err
        assert "Phase already running" in err


# ---------------------------------------------------------------------------
# cmd_phase_complete
# ---------------------------------------------------------------------------


class TestCmdPhaseComplete:
    """cmd_phase_complete echoes server message and appends advance hint."""

    @patch("egg_lib.orch_cli.orch_request")
    def test_echoes_server_message_with_advance_hint(self, mock_request, capsys):
        mock_request.return_value = {
            "success": True,
            "message": "Phase 'implement' marked complete; call advance_phase to transition",
            "data": {"phase": "implement", "current_phase": "implement", "next_phase": "pr"},
        }
        rc = cmd_phase_complete(_make_phase_args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Phase 'implement' marked complete; call advance_phase to transition" in out
        assert "Run: egg-orch phase advance --target-phase pr" in out

    @patch("egg_lib.orch_cli.orch_request")
    def test_no_advance_hint_for_terminal_phase(self, mock_request, capsys):
        mock_request.return_value = {
            "success": True,
            "message": "Phase 'pr' marked complete; call advance_phase to transition",
            "data": {"phase": "pr", "current_phase": "pr", "next_phase": None},
        }
        rc = cmd_phase_complete(_make_phase_args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "Phase 'pr' marked complete" in out
        assert "Run: egg-orch phase advance" not in out

    @patch("egg_lib.orch_cli.orch_request")
    def test_error_prints_to_stderr(self, mock_request, capsys):
        mock_request.return_value = {
            "success": False,
            "message": "Phase not running",
        }
        rc = cmd_phase_complete(_make_phase_args())
        assert rc == 1
        err = capsys.readouterr().err
        assert "Phase not running" in err


# ---------------------------------------------------------------------------
# cmd_phase_advance
# ---------------------------------------------------------------------------


class TestCmdPhaseAdvance:
    """cmd_phase_advance echoes server message."""

    @patch("egg_lib.orch_cli.orch_request")
    def test_echoes_server_message(self, mock_request, capsys):
        mock_request.return_value = {
            "success": True,
            "message": "Phase advanced to plan",
            "data": {"previous_phase": "refine", "current_phase": "plan"},
        }
        rc = cmd_phase_advance(_make_phase_args(target_phase="plan"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Phase advanced to plan" in out

    @patch("egg_lib.orch_cli.orch_request")
    def test_error_prints_to_stderr(self, mock_request, capsys):
        mock_request.return_value = {
            "success": False,
            "message": "Invalid target phase",
        }
        rc = cmd_phase_advance(_make_phase_args(target_phase="invalid"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "Invalid target phase" in err
