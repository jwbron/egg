"""
StatusBar - Progress display for egg launcher

Provides a single-line status display that updates in place,
showing current step and overall progress.
"""

import re
import shutil
import sys

# Pattern to match ANSI escape sequences
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible_len(text: str) -> int:
    """Return the visible length of text, ignoring ANSI escape codes."""
    return len(_ANSI_RE.sub("", text))


class StatusBar:
    """Single-line status bar with progress indicator."""

    def __init__(self, total_steps: int = 0, enabled: bool = True) -> None:
        """Initialize the status bar.

        Args:
            total_steps: Total number of steps for progress tracking
            enabled: Whether to show the status bar (False for verbose mode)
        """
        self.total_steps = total_steps
        self.current_step = 0
        self.enabled = enabled
        self._last_visible_len = 0

    def _get_terminal_width(self) -> int:
        """Get current terminal width."""
        try:
            return shutil.get_terminal_size().columns
        except Exception:
            return 80

    def _clear_line(self) -> None:
        """Clear the current line."""
        if not self.enabled:
            return
        # Move to beginning and clear the line
        sys.stdout.write("\r" + " " * self._last_visible_len + "\r")
        sys.stdout.flush()

    def update(self, message: str, step: int | None = None, increment: bool = True) -> None:
        """Update the status bar with a new message.

        Args:
            message: Current status message
            step: Optional step number (sets step directly, ignores increment)
            increment: Whether to auto-increment step counter. Set to False
                       to update message text without advancing the step.
        """
        if not self.enabled:
            return

        if step is not None:
            self.current_step = step
        elif increment:
            self.current_step += 1

        # Build the status line
        width = self._get_terminal_width()

        if self.total_steps > 0:
            # Show progress as [step/total]
            progress = f"[{self.current_step}/{self.total_steps}]"
            # Calculate progress bar
            pct = min(self.current_step / self.total_steps, 1.0)
            bar_width = 20
            filled = int(bar_width * pct)
            bar = "\033[32m" + "=" * filled + "\033[0m" + "-" * (bar_width - filled)
            prefix = f"\033[1m{progress}\033[0m [{bar}] "
        else:
            # No progress tracking, just show spinner
            spinners = ["|", "/", "-", "\\"]
            spinner = spinners[self.current_step % len(spinners)]
            prefix = f"\033[1m[{spinner}]\033[0m "

        # Truncate message if needed (use visible length to account for ANSI codes)
        prefix_visible = _visible_len(prefix)
        available_width = width - prefix_visible - 1
        if len(message) > available_width:
            message = message[: available_width - 3] + "..."

        line = prefix + message

        # Clear previous and write new
        self._clear_line()
        sys.stdout.write(line)
        sys.stdout.flush()
        self._last_visible_len = _visible_len(line)

    def success(self, message: str) -> None:
        """Show a success message (persists, doesn't get overwritten)."""
        self._clear_line()
        if self.enabled:
            print(f"\033[32m✓\033[0m {message}")
        self._last_visible_len = 0

    def error(self, message: str) -> None:
        """Show an error message (persists, doesn't get overwritten)."""
        self._clear_line()
        print(f"\033[31m✗\033[0m {message}", file=sys.stderr)
        self._last_visible_len = 0

    def warn(self, message: str) -> None:
        """Show a warning message (persists, doesn't get overwritten)."""
        self._clear_line()
        if self.enabled:
            print(f"\033[33m!\033[0m {message}")
        self._last_visible_len = 0

    def finish(self, message: str | None = None) -> None:
        """Finish the status bar and optionally show a final message."""
        self._clear_line()
        if message and self.enabled:
            print(f"\033[32m✓\033[0m {message}")
        self._last_visible_len = 0


# Global instance for convenience
_status_bar: StatusBar | None = None


def init_statusbar(total_steps: int = 0, enabled: bool = True) -> StatusBar:
    """Initialize the global status bar.

    Args:
        total_steps: Total number of steps for progress tracking
        enabled: Whether to show the status bar

    Returns:
        The initialized StatusBar instance
    """
    global _status_bar
    _status_bar = StatusBar(total_steps=total_steps, enabled=enabled)
    return _status_bar


def get_statusbar() -> StatusBar | None:
    """Get the global status bar instance."""
    return _status_bar


def status(message: str, step: int | None = None, increment: bool = True) -> None:
    """Update the global status bar.

    Args:
        message: Status message
        step: Optional step number
        increment: Whether to auto-increment step counter
    """
    if _status_bar:
        _status_bar.update(message, step, increment=increment)


def status_success(message: str) -> None:
    """Show a success message."""
    if _status_bar:
        _status_bar.success(message)


def status_error(message: str) -> None:
    """Show an error message."""
    if _status_bar:
        _status_bar.error(message)


def status_warn(message: str) -> None:
    """Show a warning message."""
    if _status_bar:
        _status_bar.warn(message)


def status_finish(message: str | None = None) -> None:
    """Finish the status bar."""
    if _status_bar:
        _status_bar.finish(message)
