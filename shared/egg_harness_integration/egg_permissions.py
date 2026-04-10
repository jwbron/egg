"""Permission integration with egg_restrictions.

Wraps egg_restrictions for the can_use_tool callback interface
expected by the egg_harness tool registry.
"""

from __future__ import annotations

import os
from typing import Any


def create_permission_callback() -> Any:
    """Create a permission callback for the tool registry.

    Returns None if not in pipeline mode (no EGG_AGENT_ROLE set).
    """
    role = os.environ.get("EGG_AGENT_ROLE", "").strip()
    if not role:
        return None

    try:
        from egg_agent.tool_interceptor import check_file_write_permission
    except ImportError:
        return None

    async def _check(tool_name: str, tool_input: dict[str, Any]) -> str | None:
        return check_file_write_permission(tool_name, tool_input, role)

    return _check
