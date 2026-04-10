"""WebSearch tool — web search stub."""

from __future__ import annotations

from typing import Any

from egg_harness.tools.registry import ToolImpl


class WebSearchTool(ToolImpl):
    def __init__(self) -> None:
        super().__init__(
            name="WebSearch",
            description="Search the web for information.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query", "minLength": 2},
                },
                "required": ["query"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        query = input_data["query"]
        return (
            f"WebSearch is not yet implemented in the egg harness. "
            f"Query was: {query}. "
            f"Use the Bash tool with 'curl' to fetch web content directly, "
            f"or use WebFetch with a known URL."
        )
