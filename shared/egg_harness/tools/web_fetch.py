"""WebFetch tool — URL content fetching for the egg harness.

Provides :func:`create_web_fetch_tool` which returns a tool definition and
async handler for fetching web pages and converting them to markdown.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from egg_harness.tools.registry import ToolDefinition, ToolHandler, ToolResult

logger = logging.getLogger(__name__)

# Maximum response body size to process (1 MB).
_MAX_RESPONSE_SIZE: int = 1024 * 1024


def _is_private_mode() -> bool:
    """Check whether the agent is running in private/restricted network mode."""
    if os.environ.get("EGG_PRIVATE_MODE", "").lower() == "true":
        return True
    if os.environ.get("EGG_NETWORK_MODE", "").lower() == "private":
        return True
    return False


def create_web_fetch_tool() -> tuple[ToolDefinition, ToolHandler]:
    """Create a WebFetch tool definition and handler.

    The handler uses ``httpx`` to fetch URLs and ``markdownify`` to convert
    HTML content to markdown.  Web access is disabled when the agent is
    running in private mode.

    Returns:
        A ``(ToolDefinition, ToolHandler)`` tuple ready for registration.
    """
    definition = ToolDefinition(
        name="WebFetch",
        description=(
            "Fetches content from a specified URL, converts HTML to "
            "markdown, and returns the processed content."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch content from.",
                    "format": "uri",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "A prompt describing what information to extract "
                        "from the page."
                    ),
                },
            },
            "required": ["url", "prompt"],
            "additionalProperties": False,
        },
    )

    async def handler(input: dict[str, Any]) -> ToolResult:
        if _is_private_mode():
            return ToolResult(
                output="Web access disabled in private mode",
                is_error=True,
            )

        url: str = input["url"]
        prompt: str = input["prompt"]

        try:
            import httpx  # noqa: PLC0415
        except ImportError:
            return ToolResult(
                output="httpx is not installed. Cannot fetch URLs.",
                is_error=True,
            )

        try:
            import markdownify  # noqa: PLC0415
        except ImportError:
            return ToolResult(
                output="markdownify is not installed. Cannot convert HTML.",
                is_error=True,
            )

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=30.0,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                output=f"HTTP error {exc.response.status_code} fetching {url}",
                is_error=True,
            )
        except httpx.RequestError as exc:
            return ToolResult(
                output=f"Request error fetching {url}: {exc}",
                is_error=True,
            )

        content_type = response.headers.get("content-type", "")
        body = response.text

        # Truncate very large responses
        if len(body) > _MAX_RESPONSE_SIZE:
            body = body[:_MAX_RESPONSE_SIZE]

        # Convert HTML to markdown
        if "html" in content_type.lower():
            body = markdownify.markdownify(body, strip=["script", "style"])

        # Include prompt context in the output
        output = f"Prompt: {prompt}\n\nContent from {url}:\n\n{body}"

        return ToolResult(output=output)

    return definition, handler
