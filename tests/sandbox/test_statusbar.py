"""Tests for sandbox statusbar module."""

import sys
from io import StringIO
from pathlib import Path

import pytest

# statusbar is in the sandbox directory
sandbox_path = Path(__file__).parent.parent.parent / "sandbox"
if str(sandbox_path) not in sys.path:
    sys.path.insert(0, str(sandbox_path))

from statusbar import (
    StatusBar,
    init_statusbar,
    get_statusbar,
    status,
    status_error,
    status_finish,
    status_success,
    status_warn,
)


class TestStatusBar:
    """Tests for StatusBar class."""

    def test_init_defaults(self):
        """Default initialization."""
        bar = StatusBar()
        assert bar.total_steps == 0
        assert bar.current_step == 0
        assert bar.enabled is True

    def test_init_disabled(self):
        """Disabled status bar."""
        bar = StatusBar(enabled=False)
        assert bar.enabled is False

    def test_init_with_steps(self):
        """Initialize with step count."""
        bar = StatusBar(total_steps=10)
        assert bar.total_steps == 10

    def test_update_increments_step(self):
        """Update auto-increments step."""
        bar = StatusBar(total_steps=5)
        bar.update("Step 1")
        assert bar.current_step == 1
        bar.update("Step 2")
        assert bar.current_step == 2

    def test_update_explicit_step(self):
        """Update with explicit step number."""
        bar = StatusBar(total_steps=5)
        bar.update("Jump to step 3", step=3)
        assert bar.current_step == 3

    def test_update_disabled_noop(self, capsys):
        """Update is a no-op when disabled."""
        bar = StatusBar(enabled=False)
        bar.update("Should not show")
        assert bar.current_step == 0

    def test_success_prints(self, capsys):
        """Success message is printed."""
        bar = StatusBar()
        bar.success("Done!")
        captured = capsys.readouterr()
        assert "Done!" in captured.out

    def test_success_disabled_noop(self, capsys):
        """Success is silent when disabled."""
        bar = StatusBar(enabled=False)
        bar.success("Done!")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_error_prints_stderr(self, capsys):
        """Error message is printed to stderr."""
        bar = StatusBar()
        bar.error("Failed!")
        captured = capsys.readouterr()
        assert "Failed!" in captured.err

    def test_warn_prints(self, capsys):
        """Warning message is printed."""
        bar = StatusBar()
        bar.warn("Careful!")
        captured = capsys.readouterr()
        assert "Careful!" in captured.out

    def test_warn_disabled_noop(self, capsys):
        """Warning is silent when disabled."""
        bar = StatusBar(enabled=False)
        bar.warn("Careful!")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_finish_with_message(self, capsys):
        """Finish with a message."""
        bar = StatusBar()
        bar.finish("All done")
        captured = capsys.readouterr()
        assert "All done" in captured.out

    def test_finish_without_message(self, capsys):
        """Finish without a message clears line only."""
        bar = StatusBar()
        bar.finish()
        captured = capsys.readouterr()
        # Only whitespace/control chars from line clearing, no visible message
        assert captured.out.strip() == ""

    def test_terminal_width(self):
        """Terminal width detection doesn't raise."""
        bar = StatusBar()
        width = bar._get_terminal_width()
        assert isinstance(width, int)
        assert width > 0


class TestGlobalStatusBar:
    """Tests for global status bar functions."""

    def test_init_statusbar(self):
        """init_statusbar creates global instance."""
        bar = init_statusbar(total_steps=5)
        assert isinstance(bar, StatusBar)
        assert bar.total_steps == 5

    def test_get_statusbar(self):
        """get_statusbar returns the global instance."""
        bar = init_statusbar(total_steps=3)
        assert get_statusbar() is bar

    def test_status_delegates(self):
        """status() delegates to global bar."""
        bar = init_statusbar(total_steps=5)
        status("Testing")
        assert bar.current_step == 1

    def test_status_no_bar(self):
        """status() does nothing when no bar initialized."""
        import statusbar

        statusbar._status_bar = None
        status("Nothing")  # Should not raise

    def test_status_success_delegates(self, capsys):
        """status_success delegates to global bar."""
        init_statusbar()
        status_success("Complete")
        captured = capsys.readouterr()
        assert "Complete" in captured.out

    def test_status_error_delegates(self, capsys):
        """status_error delegates to global bar."""
        init_statusbar()
        status_error("Error!")
        captured = capsys.readouterr()
        assert "Error!" in captured.err

    def test_status_warn_delegates(self, capsys):
        """status_warn delegates to global bar."""
        init_statusbar()
        status_warn("Warning!")
        captured = capsys.readouterr()
        assert "Warning!" in captured.out

    def test_status_finish_delegates(self, capsys):
        """status_finish delegates to global bar."""
        init_statusbar()
        status_finish("Done")
        captured = capsys.readouterr()
        assert "Done" in captured.out
