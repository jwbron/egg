"""Tool registry for the egg harness."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from egg_harness.providers.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

# Maximum output length for tool results (chars)
MAX_TOOL_OUTPUT = 100_000


class ToolRegistry:
    """Registry for tool definitions and implementations."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolImpl] = {}
        self._permission_callback: Callable[..., Awaitable[str | None]] | None = None

    def register(self, tool: ToolImpl) -> None:
        """Register a tool implementation."""
        self._tools[tool.name] = tool

    def set_permission_callback(
        self, callback: Callable[[str, dict[str, Any]], Awaitable[str | None]]
    ) -> None:
        """Set a callback for permission checking. Returns None if allowed, error string if blocked."""
        self._permission_callback = callback

    def get_definitions(self, *, exclude: list[str] | None = None) -> list[ToolDefinition]:
        """Get all tool definitions, optionally excluding some."""
        excluded = set(exclude or [])
        return [t.definition for t in self._tools.values() if t.name not in excluded]

    async def execute(self, name: str, input_data: dict[str, Any], tool_use_id: str) -> ToolResult:
        """Execute a tool by name."""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(
                tool_use_id=tool_use_id,
                content=f"Unknown tool: {name}",
                is_error=True,
            )

        # Check permissions
        if self._permission_callback:
            error = await self._permission_callback(name, input_data)
            if error:
                return ToolResult(
                    tool_use_id=tool_use_id,
                    content=error,
                    is_error=True,
                )

        try:
            result = await tool.execute(input_data)
            # Truncate large outputs
            if len(result) > MAX_TOOL_OUTPUT:
                result = result[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(result)} total chars)"
            return ToolResult(tool_use_id=tool_use_id, content=result)
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return ToolResult(
                tool_use_id=tool_use_id,
                content=f"Error executing {name}: {e}",
                is_error=True,
            )

    def has_tool(self, name: str) -> bool:
        return name in self._tools


class ToolImpl:
    """Base class for tool implementations."""

    def __init__(self, name: str, description: str, input_schema: dict[str, Any]) -> None:
        self.name = name
        self.definition = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        raise NotImplementedError
