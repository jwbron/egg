"""Tool interception for agent file access restrictions.

Pre-execution hooks that check Write/Edit/NotebookEdit file paths
against role-based restrictions, giving agents early feedback instead
of discovering violations at push time.

Only active when EGG_AGENT_ROLE is set (pipeline mode). In interactive
mode (no role set), all operations are allowed.
"""

from __future__ import annotations

import os
from typing import Any

# Tools that write to files and should be checked
_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})

# File path keys in tool input by tool name
_FILE_PATH_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
}


def check_file_write_permission(
    tool_name: str,
    tool_input: dict[str, Any],
    agent_role: str | None = None,
) -> str | None:
    """Check if a tool invocation is allowed for the agent's role.

    Args:
        tool_name: Name of the tool being invoked (e.g., "Write", "Edit").
        tool_input: The tool's input parameters dict.
        agent_role: The agent's role (from EGG_AGENT_ROLE). If None or empty,
            all operations are allowed (interactive/backward-compat mode).

    Returns:
        None if the operation is allowed, or an error message string if blocked.
        The error message includes which role owns the target file path.
    """
    # Skip interception if no role set (interactive mode)
    if not agent_role:
        return None

    # Only intercept write tools
    if tool_name not in _WRITE_TOOLS:
        return None

    # Extract file path from tool input
    path_key = _FILE_PATH_KEYS.get(tool_name)
    if not path_key:
        return None

    file_path = tool_input.get(path_key)
    if not file_path:
        return None

    # Normalize: strip /home/egg/repos/<repo>/ prefix to get repo-relative path
    file_path = _normalize_to_repo_relative(str(file_path))

    # Check against role restrictions
    try:
        from egg_restrictions import check_agent_file_access
    except ImportError:
        # If egg_restrictions not available, allow (fail-open for backward compat)
        return None

    allowed, blocked_files, reason = check_agent_file_access(agent_role, [file_path])
    if allowed:
        return None

    # Find which role owns this file for helpful error message
    owner_role = _find_owning_role(file_path, agent_role)
    owner_hint = f" -- this file belongs to the '{owner_role}' role" if owner_role else ""

    return (
        f"Role '{agent_role}' cannot write to {file_path}{owner_hint}. "
        f"The gateway would reject this at push time. "
        f"Consider delegating this change to the appropriate agent."
    )


def _normalize_to_repo_relative(file_path: str) -> str:
    """Normalize an absolute file path to repo-relative.

    Strips common prefixes like /home/egg/repos/<repo>/ to get
    a path suitable for pattern matching.
    """
    # Strip /home/egg/repos/<repo>/ prefix
    parts = file_path.split("/")
    if len(parts) > 5 and parts[1] == "home" and parts[2] == "egg" and parts[3] == "repos":
        # /home/egg/repos/<repo>/path/to/file -> path/to/file
        return "/".join(parts[5:])
    # Strip leading /
    return file_path.lstrip("/")


def _find_owning_role(file_path: str, excluded_role: str) -> str | None:
    """Find which role is allowed to write to a file.

    Used for helpful error messages to tell agents which role
    should handle the file.
    """
    try:
        from egg_restrictions import AGENT_PATTERNS
    except ImportError:
        return None

    # Check common roles first (most likely targets for delegation)
    priority_roles = ["coder", "tester", "documenter"]
    for role_name in priority_roles:
        if role_name == excluded_role:
            continue
        pattern = AGENT_PATTERNS.get(role_name)
        if pattern and pattern.can_write(file_path):
            return role_name

    # Check remaining roles
    for role_name, pattern in AGENT_PATTERNS.items():
        if role_name == excluded_role or role_name in priority_roles:
            continue
        if pattern.can_write(file_path):
            return role_name

    return None


def get_role_from_env() -> str | None:
    """Get the agent role from environment.

    Returns None if not in pipeline mode.
    """
    role = os.environ.get("EGG_AGENT_ROLE", "").strip()
    return role if role else None
