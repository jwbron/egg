"""Tool registration dataclass shared by all namespace modules.

Lives in its own module to avoid circular imports between
``tools/__init__.py`` (which imports the namespace modules) and the
namespace modules (which need the dataclass).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolRegistration:
    """Metadata linking an MCP tool to its CLI counterpart and handler.

    Attributes:
        name: The SDK-exposed tool name (``mcp__<namespace>__<verb>``).
        namespace: Logical grouping (``sdlc``/``brc``/``phase``/…).
        handler: The pure request→response function backing the tool.
        sdk_tool: The ``SdkMcpTool`` instance produced by
            :func:`claude_agent_sdk.tool` (or a stub when the SDK isn't
            installed, e.g. during host-side tests).
        cli_command: Tuple naming the shell CLI counterpart, e.g.
            ``("egg-contract", "add-decision")``. ``None`` for tools with
            no CLI analog — these are skipped in the drift test.
    """

    name: str
    namespace: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    sdk_tool: Any
    cli_command: tuple[str, ...] | None = None
