"""WebFetch tool — fetch and process web content."""

from __future__ import annotations

from typing import Any

from egg_harness.tools.registry import ToolImpl


class WebFetchTool(ToolImpl):
    def __init__(self) -> None:
        super().__init__(
            name="WebFetch",
            description="Fetch content from a URL and process it.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch", "format": "uri"},
                    "prompt": {
                        "type": "string",
                        "description": "Prompt to process the content with",
                    },
                },
                "required": ["url", "prompt"],
            },
        )

    async def execute(self, input_data: dict[str, Any]) -> str:
        url = input_data["url"]
        # prompt is accepted but not used in MVP (no LLM processing of fetched content)
        _ = input_data.get("prompt", "Summarize the content")

        try:
            import httpx

            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                content = response.text

                # Basic HTML to text conversion (simple approach for MVP)
                if "<html" in content.lower() or "<body" in content.lower():
                    content = _strip_html(content)

                # Truncate to reasonable size
                if len(content) > 50_000:
                    content = content[:50_000] + "\n... (truncated)"

                return f"Content from {url}:\n\n{content}"
        except Exception as e:
            return f"Error fetching {url}: {e}"


def _strip_html(html: str) -> str:
    """Very basic HTML to text — strips tags."""
    import re

    # Remove script and style elements
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text
