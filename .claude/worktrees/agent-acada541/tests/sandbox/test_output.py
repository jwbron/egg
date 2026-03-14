"""Tests for sandbox/egg_lib/output.py - Output utilities."""

import sys
from pathlib import Path
from unittest.mock import patch

sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
sys.path.insert(0, str(sandbox_path))

from egg_lib.output import error, get_quiet_mode, info, set_quiet_mode, success, warn


class TestQuietMode:
    """Tests for quiet mode flag."""

    def test_default_not_quiet(self):
        """Default quiet mode is False."""
        set_quiet_mode(False)
        assert get_quiet_mode() is False

    def test_set_quiet(self):
        """Can set quiet mode to True."""
        set_quiet_mode(True)
        assert get_quiet_mode() is True
        set_quiet_mode(False)  # Reset


class TestOutputVerbose:
    """Tests for output functions in verbose (non-quiet) mode."""

    def setup_method(self):
        set_quiet_mode(False)

    @patch("builtins.print")
    def test_info_verbose(self, mock_print):
        """info() prints [INFO] in verbose mode."""
        info("test message")
        mock_print.assert_called_once()
        call_args = mock_print.call_args[0][0]
        assert "INFO" in call_args
        assert "test message" in call_args

    @patch("builtins.print")
    def test_success_verbose(self, mock_print):
        """success() prints [SUCCESS] in verbose mode."""
        success("it worked")
        mock_print.assert_called_once()
        call_args = mock_print.call_args[0][0]
        assert "SUCCESS" in call_args
        assert "it worked" in call_args

    @patch("builtins.print")
    def test_warn_verbose(self, mock_print):
        """warn() prints [WARNING] in verbose mode."""
        warn("watch out")
        mock_print.assert_called_once()
        call_args = mock_print.call_args[0][0]
        assert "WARNING" in call_args
        assert "watch out" in call_args

    @patch("builtins.print")
    def test_error_verbose(self, mock_print):
        """error() prints [ERROR] to stderr in verbose mode."""
        error("bad thing")
        mock_print.assert_called_once()
        call_args = mock_print.call_args[0][0]
        assert "ERROR" in call_args
        assert "bad thing" in call_args


class TestOutputQuiet:
    """Tests for output functions in quiet mode."""

    def setup_method(self):
        set_quiet_mode(True)

    def teardown_method(self):
        set_quiet_mode(False)

    @patch("egg_lib.output.status")
    def test_info_quiet(self, mock_status):
        """info() calls status() with increment=False in quiet mode."""
        info("loading")
        mock_status.assert_called_once_with("loading", increment=False)

    @patch("egg_lib.output.status_success")
    def test_success_quiet(self, mock_status_success):
        """success() calls status_success() in quiet mode."""
        success("done")
        mock_status_success.assert_called_once_with("done")

    @patch("egg_lib.output.status_warn")
    def test_warn_quiet(self, mock_status_warn):
        """warn() calls status_warn() in quiet mode."""
        warn("careful")
        mock_status_warn.assert_called_once_with("careful")

    @patch("egg_lib.output.status_error")
    def test_error_quiet(self, mock_status_error):
        """error() calls status_error() in quiet mode."""
        error("broken")
        mock_status_error.assert_called_once_with("broken")
