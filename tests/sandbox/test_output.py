"""Tests for sandbox egg_lib output module."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from egg_lib.output import (
    error,
    get_quiet_mode,
    info,
    set_quiet_mode,
    success,
    warn,
)


class TestQuietMode:
    """Tests for quiet mode toggle."""

    def test_default_not_quiet(self):
        """Default quiet mode is off."""
        set_quiet_mode(False)
        assert get_quiet_mode() is False

    def test_set_quiet(self):
        """Set quiet mode on."""
        set_quiet_mode(True)
        assert get_quiet_mode() is True
        # Reset
        set_quiet_mode(False)

    def test_toggle_quiet(self):
        """Toggle quiet mode."""
        set_quiet_mode(True)
        assert get_quiet_mode() is True
        set_quiet_mode(False)
        assert get_quiet_mode() is False


class TestOutputFunctions:
    """Tests for info/success/warn/error output functions."""

    def test_info_normal_mode(self, capsys):
        """Info message in normal mode prints to stdout."""
        set_quiet_mode(False)
        info("Test info message")
        captured = capsys.readouterr()
        assert "Test info message" in captured.out
        assert "INFO" in captured.out

    def test_success_normal_mode(self, capsys):
        """Success message in normal mode."""
        set_quiet_mode(False)
        success("Test success")
        captured = capsys.readouterr()
        assert "Test success" in captured.out
        assert "SUCCESS" in captured.out

    def test_warn_normal_mode(self, capsys):
        """Warning message in normal mode."""
        set_quiet_mode(False)
        warn("Test warning")
        captured = capsys.readouterr()
        assert "Test warning" in captured.out
        assert "WARNING" in captured.out

    def test_error_normal_mode(self, capsys):
        """Error message in normal mode prints to stderr."""
        set_quiet_mode(False)
        error("Test error")
        captured = capsys.readouterr()
        assert "Test error" in captured.err
        assert "ERROR" in captured.err

    def test_info_quiet_mode(self, capsys):
        """Info in quiet mode delegates to statusbar."""
        set_quiet_mode(True)
        with patch("egg_lib.output.status") as mock_status:
            info("Quiet info")
            mock_status.assert_called_once_with("Quiet info")
        set_quiet_mode(False)

    def test_success_quiet_mode(self):
        """Success in quiet mode delegates to statusbar."""
        set_quiet_mode(True)
        with patch("egg_lib.output.status_success") as mock:
            success("Quiet success")
            mock.assert_called_once_with("Quiet success")
        set_quiet_mode(False)

    def test_warn_quiet_mode(self):
        """Warning in quiet mode delegates to statusbar."""
        set_quiet_mode(True)
        with patch("egg_lib.output.status_warn") as mock:
            warn("Quiet warning")
            mock.assert_called_once_with("Quiet warning")
        set_quiet_mode(False)

    def test_error_quiet_mode(self):
        """Error in quiet mode delegates to statusbar."""
        set_quiet_mode(True)
        with patch("egg_lib.output.status_error") as mock:
            error("Quiet error")
            mock.assert_called_once_with("Quiet error")
        set_quiet_mode(False)
