"""Tests for statusbar module.

Tests for _visible_len() ANSI stripping and the increment parameter
on StatusBar.update() that controls step counter advancement.
"""

from unittest.mock import patch

from statusbar import (
    StatusBar,
    _visible_len,
    get_statusbar,
    init_statusbar,
    status,
    status_error,
    status_finish,
    status_success,
    status_warn,
)


class TestVisibleLen:
    """Tests for _visible_len() ANSI escape code stripping."""

    def test_plain_text(self):
        """Plain text without ANSI codes returns actual length."""
        assert _visible_len("hello") == 5

    def test_empty_string(self):
        assert _visible_len("") == 0

    def test_single_ansi_code(self):
        """ANSI code is excluded from length."""
        assert _visible_len("\033[32mgreen\033[0m") == len("green")

    def test_bold_ansi(self):
        assert _visible_len("\033[1mbold\033[0m") == len("bold")

    def test_multiple_ansi_codes(self):
        """Multiple ANSI codes are all stripped."""
        text = "\033[1m[3/6]\033[0m [\033[32m======\033[0m--------------] Loading"
        visible = "[3/6] [======--------------] Loading"
        assert _visible_len(text) == len(visible)

    def test_ansi_only(self):
        """String with only ANSI codes has zero visible length."""
        assert _visible_len("\033[0m\033[32m\033[1m") == 0

    def test_semicolon_params(self):
        """ANSI codes with semicolon-separated params (e.g. \\033[1;32m)."""
        assert _visible_len("\033[1;32mbold green\033[0m") == len("bold green")


class TestStatusBarIncrement:
    """Tests for the increment parameter on StatusBar.update()."""

    def _make_bar(self, total_steps: int = 6) -> StatusBar:
        bar = StatusBar(total_steps=total_steps, enabled=True)
        # Avoid actual terminal writes
        bar._get_terminal_width = lambda: 120
        return bar

    @patch("sys.stdout")
    def test_default_increment(self, mock_stdout):
        """update() increments step counter by default."""
        bar = self._make_bar()
        bar.update("step one")
        assert bar.current_step == 1
        bar.update("step two")
        assert bar.current_step == 2

    @patch("sys.stdout")
    def test_increment_false_does_not_advance(self, mock_stdout):
        """update(increment=False) leaves step counter unchanged."""
        bar = self._make_bar()
        bar.update("step one")
        assert bar.current_step == 1
        bar.update("info message", increment=False)
        assert bar.current_step == 1
        bar.update("another info", increment=False)
        assert bar.current_step == 1

    @patch("sys.stdout")
    def test_increment_false_then_true(self, mock_stdout):
        """Step counter resumes correctly after increment=False calls."""
        bar = self._make_bar()
        bar.update("step one")
        assert bar.current_step == 1
        bar.update("info", increment=False)
        bar.update("more info", increment=False)
        bar.update("step two")
        assert bar.current_step == 2

    @patch("sys.stdout")
    def test_explicit_step_ignores_increment(self, mock_stdout):
        """Explicit step= always sets the counter, regardless of increment."""
        bar = self._make_bar()
        bar.update("jump", step=5)
        assert bar.current_step == 5
        bar.update("jump back", step=2, increment=False)
        assert bar.current_step == 2

    @patch("sys.stdout")
    def test_counter_stays_within_total(self, mock_stdout):
        """Simulates the original bug: info() calls should not overflow the counter."""
        bar = self._make_bar(total_steps=3)
        # Three real steps
        bar.update("step 1")
        bar.update("step 2")
        bar.update("step 3")
        # Several info-style updates (increment=False) should not push past 3
        for _ in range(10):
            bar.update("info msg", increment=False)
        assert bar.current_step == 3


class TestStatusBarDisabled:
    """Tests for StatusBar when disabled."""

    @patch("sys.stdout")
    def test_update_disabled(self, mock_stdout):
        """update() is a no-op when disabled."""
        bar = StatusBar(total_steps=3, enabled=False)
        bar.update("hello")
        assert bar.current_step == 0  # Not incremented
        mock_stdout.write.assert_not_called()

    @patch("sys.stdout")
    def test_success_disabled(self, mock_stdout):
        """success() is suppressed when disabled."""
        bar = StatusBar(total_steps=3, enabled=False)
        bar.success("done")
        # Should not print (stdout.write not called for the message)
        assert bar._last_visible_len == 0

    @patch("sys.stdout")
    def test_warn_disabled(self, mock_stdout):
        """warn() is suppressed when disabled."""
        bar = StatusBar(total_steps=3, enabled=False)
        bar.warn("careful")
        assert bar._last_visible_len == 0

    @patch("sys.stdout")
    def test_finish_disabled(self, mock_stdout):
        """finish() with message is suppressed when disabled."""
        bar = StatusBar(total_steps=3, enabled=False)
        bar.finish("all done")
        assert bar._last_visible_len == 0


class TestStatusBarMethods:
    """Tests for StatusBar success/error/warn/finish methods."""

    def _make_bar(self, total_steps: int = 3) -> StatusBar:
        bar = StatusBar(total_steps=total_steps, enabled=True)
        bar._get_terminal_width = lambda: 120
        return bar

    @patch("sys.stdout")
    def test_success_prints(self, mock_stdout):
        """success() prints a green checkmark message."""
        bar = self._make_bar()
        bar.success("all good")
        assert bar._last_visible_len == 0

    @patch("sys.stderr")
    @patch("sys.stdout")
    def test_error_prints_to_stderr(self, mock_stdout, mock_stderr):
        """error() prints to stderr."""
        bar = self._make_bar()
        bar.error("something broke")
        assert bar._last_visible_len == 0

    @patch("sys.stdout")
    def test_warn_prints(self, mock_stdout):
        """warn() prints a yellow warning message."""
        bar = self._make_bar()
        bar.warn("watch out")
        assert bar._last_visible_len == 0

    @patch("sys.stdout")
    def test_finish_with_message(self, mock_stdout):
        """finish() with message prints final message."""
        bar = self._make_bar()
        bar.finish("complete")
        assert bar._last_visible_len == 0

    @patch("sys.stdout")
    def test_finish_without_message(self, mock_stdout):
        """finish() without message just clears line."""
        bar = self._make_bar()
        bar.finish()
        assert bar._last_visible_len == 0

    @patch("sys.stdout")
    def test_spinner_mode_no_total(self, mock_stdout):
        """StatusBar with total_steps=0 uses spinner mode."""
        bar = StatusBar(total_steps=0, enabled=True)
        bar._get_terminal_width = lambda: 120
        bar.update("loading")
        assert bar.current_step == 1

    @patch("sys.stdout")
    def test_message_truncation(self, mock_stdout):
        """Long messages are truncated to terminal width."""
        bar = StatusBar(total_steps=3, enabled=True)
        bar._get_terminal_width = lambda: 40
        bar.update("A" * 100)
        assert bar._last_visible_len <= 40

    @patch("sys.stdout")
    def test_clear_line(self, mock_stdout):
        """_clear_line writes spaces to clear previous output."""
        bar = self._make_bar()
        bar._last_visible_len = 20
        bar._clear_line()
        # Should have written \r + spaces + \r
        assert mock_stdout.write.called


class TestGlobalConvenienceFunctions:
    """Tests for global statusbar convenience functions."""

    @patch("sys.stdout")
    def test_get_statusbar(self, mock_stdout):
        """get_statusbar returns the global instance."""
        bar = init_statusbar(total_steps=3, enabled=True)
        assert get_statusbar() is bar

    @patch("sys.stdout")
    def test_status_success(self, mock_stdout):
        """status_success calls bar.success()."""
        bar = init_statusbar(total_steps=3, enabled=True)
        bar._get_terminal_width = lambda: 120
        status_success("done")
        assert bar._last_visible_len == 0

    @patch("sys.stderr")
    @patch("sys.stdout")
    def test_status_error(self, mock_stdout, mock_stderr):
        """status_error calls bar.error()."""
        bar = init_statusbar(total_steps=3, enabled=True)
        bar._get_terminal_width = lambda: 120
        status_error("fail")
        assert bar._last_visible_len == 0

    @patch("sys.stdout")
    def test_status_warn(self, mock_stdout):
        """status_warn calls bar.warn()."""
        bar = init_statusbar(total_steps=3, enabled=True)
        bar._get_terminal_width = lambda: 120
        status_warn("caution")
        assert bar._last_visible_len == 0

    @patch("sys.stdout")
    def test_status_finish(self, mock_stdout):
        """status_finish calls bar.finish()."""
        bar = init_statusbar(total_steps=3, enabled=True)
        bar._get_terminal_width = lambda: 120
        status_finish("all done")
        assert bar._last_visible_len == 0


class TestGlobalStatusIncrement:
    """Tests that the global status() function passes increment through."""

    @patch("sys.stdout")
    def test_status_increment_false(self, mock_stdout):
        bar = init_statusbar(total_steps=6, enabled=True)
        bar._get_terminal_width = lambda: 120
        status("step one")
        assert bar.current_step == 1
        status("info", increment=False)
        assert bar.current_step == 1
