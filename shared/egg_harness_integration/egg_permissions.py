"""Egg permission callback adapter for the harness.

Bridges :func:`egg_agent.tool_interceptor.check_file_write_permission`
into the harness :data:`PermissionCallback` interface so that role-based
file-access restrictions are enforced when the harness executes tools.
"""

from __future__ import annotations

import os
from typing import Any

from egg_harness.permissions import PermissionCallback

# Tools that perform file writes and should be checked.
_WRITE_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "NotebookEdit"})

# Map tool name -> key in tool_input that holds the file path.
_FILE_PATH_KEYS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_egg_permission_callback(
    agent_role: str | None = None,
) -> PermissionCallback | None:
    """Create a permission callback that enforces egg role-based file restrictions.

    If *agent_role* is ``None``, the function falls back to the
    ``EGG_AGENT_ROLE`` environment variable.  If no role can be
    determined (interactive mode), ``None`` is returned and no
    permission checking is applied.

    The returned callback only inspects ``Write``, ``Edit``, and
    ``NotebookEdit`` tool invocations.  For those tools it delegates to
    :func:`egg_agent.tool_interceptor.check_file_write_permission` and
    returns the error string if the operation is blocked.

    Args:
        agent_role: The agent's role identifier (e.g. ``"coder"``).

    Returns:
        A :data:`PermissionCallback` if a role is available, or ``None``
        otherwise.
    """
    role = agent_role or os.environ.get("EGG_AGENT_ROLE", "").strip()
    if not role:
        return None

    # Lazy-import to tolerate environments where egg_agent is not installed.
    try:
        from egg_agent.tool_interceptor import check_file_write_permission
    except ImportError:
        return None

    def _callback(tool_name: str, tool_input: dict[str, Any]) -> str | None:
        """Check file-write permissions for write-oriented tools."""
        if tool_name not in _WRITE_TOOLS:
            return None

        return check_file_write_permission(tool_name, tool_input, role)

    return _callback
