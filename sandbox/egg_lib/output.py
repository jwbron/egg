"""Output utilities for egg.

This module provides info/success/warn/error functions that
integrate with the statusbar in quiet mode.
"""

import sys

# Import statusbar from parent directory
# Add shared modules to path (relative to sandbox directory)
from pathlib import Path

from .config import Colors

_SCRIPT_DIR = Path(__file__).parent.parent.resolve()
_SHARED_DIR = _SCRIPT_DIR.parent / "shared"
if _SHARED_DIR.exists() and str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from statusbar import status, status_error, status_success, status_warn

# Global quiet mode flag
_quiet_mode = False


def set_quiet_mode(quiet: bool) -> None:
    """Set the global quiet mode flag."""
    global _quiet_mode
    _quiet_mode = quiet


def get_quiet_mode() -> bool:
    """Get the current quiet mode setting."""
    return _quiet_mode


def info(msg: str) -> None:
    """Show info message. In quiet mode, updates statusbar instead."""
    if _quiet_mode:
        status(msg)
    else:
        print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")


def success(msg: str) -> None:
    """Show success message. In quiet mode, shows as persistent success."""
    if _quiet_mode:
        status_success(msg)
    else:
        print(f"{Colors.GREEN}[SUCCESS]{Colors.NC} {msg}")


def warn(msg: str) -> None:
    """Show warning message. Always visible."""
    if _quiet_mode:
        status_warn(msg)
    else:
        print(f"{Colors.YELLOW}[WARNING]{Colors.NC} {msg}")


def error(msg: str) -> None:
    """Show error message. Always visible."""
    if _quiet_mode:
        status_error(msg)
    else:
        print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}", file=sys.stderr)
