"""WebSearch tool — web search stub for the egg harness.

Provides :func:`create_web_search_tool` which returns a tool definition and
async handler.  The current implementation is a stub that returns a
not-available message; a real implementation would integrate with a search
API.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolResult

logger = logging.getLogger(__name__)


def _is_private_mode() -> bool:
    """Check whether the agent is running in private/restricted network mode."""
    if os.environ.get("EGG_PRIVATE_MODE", "").lower() == "true":
        return True
    if os.environ.get("EGG_NETWORK_MODE", "").lower() == "private":
        return True
    return False


def create_web_search_tool() -> tuple[ToolDefinition, ToolHandler]:
    """Create a WebSearch tool definition and handler.

    This is a stub implementation.  In production, it would integrate with
    a search API (e.g. Brave Search, SerpAPI).

    Returns:
        A ``(ToolDefinition, ToolHandler)`` tuple ready for registration.
    """
    definition = ToolDefinition(
        name="WebSearch",
        description=(
            "Search the web and use the results to inform responses. "
            "Provides up-to-date information for current events and "
            "recent data."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to use.",
                    "minLength": 2,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    async def handler(input: dict[str, Any]) -> ToolResult:
        if _is_private_mode():
            return ToolResult(
                output="Web access disabled in private mode",
                is_error=True,
            )

        return ToolResult(
            output="Web search not available in this environment",
            is_error=True,
        )

    return definition, handler
