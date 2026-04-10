"""Tool registry for managing and executing tools in the egg harness.

Provides the central :class:`ToolRegistry` that stores tool definitions and
their handlers, enforces permission checks, and truncates oversized output.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

ToolHandler = Callable[[dict[str, Any]], Awaitable["ToolResult"]]
"""Async callable that executes a tool and returns a :class:`ToolResult`."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Schema definition for a single tool.

    Attributes:
        name: Unique tool name (e.g. ``"Bash"``, ``"Read"``).
        description: Human-readable description of what the tool does.
        input_schema: JSON Schema dict describing the expected input.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolResult:
    """Result returned by a tool handler.

    Attributes:
        output: The text output produced by the tool.
        is_error: ``True`` if the tool invocation failed.
    """

    output: str
    is_error: bool = False


class _AttrDict(dict):
    """A dict subclass that allows attribute-style access to keys."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


# Default maximum output size in bytes (100 KB).
_DEFAULT_MAX_OUTPUT_SIZE: int = 100 * 1024

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Registry that maps tool names to definitions and handlers.

    Example::

        registry = ToolRegistry()
        defn, handler = create_bash_tool()
        registry.register(defn, handler)
        result = registry.execute("Bash", {"command": "echo hello"})
    """

    def __init__(
        self,
        *,
        max_output_size: int = _DEFAULT_MAX_OUTPUT_SIZE,
        max_output_bytes: int | None = None,
        can_use_tool: Callable[[str, dict[str, Any]], str | None] | None = None,
    ) -> None:
        self._tools: dict[str, tuple[ToolDefinition, ToolHandler]] = {}
        self._permission_callback: Callable[[str, dict[str, Any]], str | None] | None = None
        # max_output_bytes is an alias for max_output_size
        self._max_output_size = max_output_bytes if max_output_bytes is not None else max_output_size
        # can_use_tool is an alias for set_permission_callback
        if can_use_tool is not None:
            self._permission_callback = can_use_tool

    # -- registration -------------------------------------------------------

    def register(self, definition: Any, handler: Any) -> None:
        """Register a tool with its definition and handler.

        Args:
            definition: The tool's schema definition.  Must have a ``name``
                attribute.
            handler: Callable that implements the tool.  May be sync or async.
        """
        name = definition.name
        self._tools[name] = (definition, handler)

    # -- permission callback ------------------------------------------------

    def set_permission_callback(
        self,
        callback: Callable[[str, dict[str, Any]], str | None],
    ) -> None:
        """Set a permission checker invoked before every tool execution.

        The callback receives ``(tool_name, tool_input)`` and must return
        ``None`` if the invocation is allowed, or an error string describing
        why it was blocked.

        Args:
            callback: Permission checking function.
        """
        self._permission_callback = callback

    # -- execution ----------------------------------------------------------

    def execute(self, name: str, input: dict[str, Any]) -> ToolResult:
        """Execute a tool by name (synchronous).

        Supports both sync and async handlers.  If the handler is async,
        it is executed via :func:`asyncio.run` (or the running loop).

        The method checks permissions, dispatches to the handler, and
        truncates output that exceeds the configured max output size.

        Args:
            name: The registered tool name.
            input: Input parameters for the tool.

        Returns:
            A :class:`ToolResult` with the tool's output (possibly truncated).
        """
        # Unknown tool?
        if name not in self._tools:
            return ToolResult(
                output=f"Unknown tool: {name}",
                is_error=True,
            )

        # Permission check
        if self._permission_callback is not None:
            error = self._permission_callback(name, input)
            if error is not None:
                return ToolResult(output=error, is_error=True)

        _, handler = self._tools[name]

        try:
            raw_result = handler(input)
            # If the handler is async or returns a coroutine, await it
            if inspect.isawaitable(raw_result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    # We are inside an event loop already; create a task
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        raw_result = pool.submit(asyncio.run, raw_result).result()
                else:
                    raw_result = asyncio.run(raw_result)
        except Exception as exc:
            logger.exception("Tool %s raised an exception", name)
            return ToolResult(
                output=f"Tool execution error: {exc}",
                is_error=True,
            )

        # Normalize result: if handler returned a string, wrap in ToolResult
        if isinstance(raw_result, str):
            result = ToolResult(output=raw_result)
        elif isinstance(raw_result, ToolResult):
            result = raw_result
        else:
            result = ToolResult(output=str(raw_result))

        # Truncate oversized output
        if len(result.output.encode("utf-8", errors="replace")) > self._max_output_size:
            truncated = result.output[: self._max_output_size]
            result = ToolResult(
                output=(
                    truncated
                    + f"\n\n[Output truncated — exceeded {self._max_output_size} bytes]"
                ),
                is_error=result.is_error,
            )

        return result

    async def execute_async(self, name: str, input: dict[str, Any]) -> ToolResult:
        """Execute a tool by name (async version).

        Args:
            name: The registered tool name.
            input: Input parameters for the tool.

        Returns:
            A :class:`ToolResult` with the tool's output (possibly truncated).
        """
        # Unknown tool?
        if name not in self._tools:
            return ToolResult(
                output=f"Unknown tool: {name}",
                is_error=True,
            )

        # Permission check
        if self._permission_callback is not None:
            error = self._permission_callback(name, input)
            if error is not None:
                return ToolResult(output=error, is_error=True)

        _, handler = self._tools[name]

        try:
            raw_result = handler(input)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
        except Exception as exc:
            logger.exception("Tool %s raised an exception", name)
            return ToolResult(
                output=f"Tool execution error: {exc}",
                is_error=True,
            )

        # Normalize result
        if isinstance(raw_result, str):
            result = ToolResult(output=raw_result)
        elif isinstance(raw_result, ToolResult):
            result = raw_result
        else:
            result = ToolResult(output=str(raw_result))

        # Truncate oversized output
        if len(result.output.encode("utf-8", errors="replace")) > self._max_output_size:
            truncated = result.output[: self._max_output_size]
            result = ToolResult(
                output=(
                    truncated
                    + f"\n\n[Output truncated — exceeded {self._max_output_size} bytes]"
                ),
                is_error=result.is_error,
            )

        return result

    # Alias for backward compatibility
    execute_sync = execute

    # -- introspection ------------------------------------------------------

    def get_definitions(self) -> list[_AttrDict]:
        """Return tool definitions in Anthropic API format.

        Each entry is an :class:`_AttrDict` with ``name``, ``description``,
        and ``input_schema`` keys, supporting both dict-style and
        attribute-style access.

        Returns:
            A list of tool definition dicts with attribute access.
        """
        definitions: list[_AttrDict] = []
        for defn, _ in self._tools.values():
            definitions.append(
                _AttrDict(
                    name=defn.name,
                    description=getattr(defn, "description", ""),
                    input_schema=getattr(defn, "input_schema", {}),
                )
            )
        return definitions
