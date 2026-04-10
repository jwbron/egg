"""Permission callback types and utilities.

Provides composable permission callbacks for controlling which tools an
agent is allowed to invoke.  Callbacks follow a simple convention: return
``None`` to allow the tool call, or an error string to block it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

PermissionCallback = Callable[[str, dict[str, Any]], str | None]
"""Permission callback signature.

Takes ``(tool_name, tool_input)`` and returns ``None`` if the call is
allowed, or an error message string if it should be blocked.
"""

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def create_disallow_list_callback(
    disallowed_tools: list[str],
) -> PermissionCallback:
    """Create a permission callback that blocks a fixed set of tools.

    Args:
        disallowed_tools: Tool names that should be rejected.

    Returns:
        A :data:`PermissionCallback` that returns an error string for any
        tool in *disallowed_tools* and ``None`` for everything else.
    """
    blocked = set(disallowed_tools)

    def _callback(tool_name: str, tool_input: dict[str, Any]) -> str | None:
        if tool_name in blocked:
            return f"Tool '{tool_name}' is not available in this environment."
        return None

    return _callback


def compose_permissions(*callbacks: PermissionCallback) -> PermissionCallback:
    """Compose multiple permission callbacks into a single callback.

    The returned callback executes each constituent callback in order and
    returns the first non-``None`` (blocking) result.  If every callback
    returns ``None``, the composed callback also returns ``None`` (allowed).

    Args:
        *callbacks: Permission callbacks to compose.

    Returns:
        A single :data:`PermissionCallback` that enforces all of the given
        callbacks with first-block-wins semantics.
    """

    def _composed(tool_name: str, tool_input: dict[str, Any]) -> str | None:
        for cb in callbacks:
            result = cb(tool_name, tool_input)
            if result is not None:
                return result
        return None

    return _composed
