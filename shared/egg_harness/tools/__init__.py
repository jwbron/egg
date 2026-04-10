"""Tool system for the egg harness.

Provides the :class:`ToolRegistry` for managing tools, along with factory
functions for all built-in tools (Bash, Read, Write, Edit, Glob, Grep,
WebFetch, WebSearch).

Example::

    from egg_harness.tools import ToolRegistry, create_bash_tool, create_read_tool

    registry = ToolRegistry()

    for defn, handler in [create_bash_tool(), create_read_tool()]:
        registry.register(defn, handler)

    result = await registry.execute("Bash", {"command": "echo hello"})
"""

from __future__ import annotations

from egg_harness.tools.bash import create_bash_tool
from egg_harness.tools.edit import create_edit_tool
from egg_harness.tools.glob_tool import create_glob_tool
from egg_harness.tools.grep import create_grep_tool
from egg_harness.tools.read import create_read_tool
from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolRegistry, ToolResult
from egg_harness.tools.web_fetch import create_web_fetch_tool
from egg_harness.tools.web_search import create_web_search_tool
from egg_harness.tools.write import create_write_tool

__all__ = [
    "ToolDefinition",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "create_bash_tool",
    "create_edit_tool",
    "create_glob_tool",
    "create_grep_tool",
    "create_read_tool",
    "create_web_fetch_tool",
    "create_web_search_tool",
    "create_write_tool",
]
