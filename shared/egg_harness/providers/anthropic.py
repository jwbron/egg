"""Anthropic provider using the official SDK."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from egg_harness.providers.base import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    Provider,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolDefinition,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)

logger = logging.getLogger(__name__)

# Default gateway proxy endpoint
_DEFAULT_GATEWAY_URL = "http://egg-gateway:8081/v1"


class AnthropicProvider(Provider):
    """Anthropic Messages API provider via official SDK."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str = "gateway-injected",  # Placeholder — gateway injects real key
        default_model: str = "claude-opus-4-20250514",
        max_retries: int = 3,
    ) -> None:
        # SECURITY: Verify API key is NOT in environment (must come through gateway)
        if os.environ.get("ANTHROPIC_API_KEY"):
            logger.warning(
                "ANTHROPIC_API_KEY found in environment — this should not happen in sandbox. "
                "API calls should route through the gateway proxy."
            )

        self._default_model = default_model
        self._base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", _DEFAULT_GATEWAY_URL)

        import anthropic

        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=self._base_url,
            max_retries=max_retries,
        )

    async def send_message(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 16384,
        model: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Send messages via Anthropic Messages API with streaming."""
        effective_model = model or self._default_model

        kwargs: dict[str, Any] = {
            "model": effective_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if system:
            kwargs["system"] = system

        if tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]

        headers = extra_headers or {}
        if headers:
            kwargs["extra_headers"] = headers

        async with self._client.messages.stream(**kwargs) as stream:
            current_tool_id: str | None = None

            async for event in stream:
                event_type = getattr(event, "type", None)

                if event_type == "message_start":
                    msg = event.message
                    yield MessageStart(message_id=msg.id, model=msg.model)

                elif event_type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        current_tool_id = block.id
                        yield ToolUseStart(tool_use_id=block.id, name=block.name)
                    elif block.type == "thinking":
                        pass  # thinking blocks emit deltas

                elif event_type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield TextDelta(text=delta.text)
                    elif delta.type == "thinking_delta":
                        yield ThinkingDelta(text=delta.thinking)
                    elif delta.type == "input_json_delta":
                        yield ToolUseInputDelta(partial_json=delta.partial_json)

                elif event_type == "content_block_stop":
                    if current_tool_id:
                        yield ToolUseEnd(tool_use_id=current_tool_id)
                        current_tool_id = None

                elif event_type == "message_delta":
                    usage = None
                    if hasattr(event, "usage") and event.usage:
                        usage = {
                            "output_tokens": getattr(event.usage, "output_tokens", 0),
                        }
                    yield MessageDelta(
                        stop_reason=getattr(event.delta, "stop_reason", None),
                        usage=usage,
                    )

                elif event_type == "message_stop":
                    # Get final usage from the accumulated message
                    final_message = stream.get_final_message()
                    usage = None
                    if final_message and final_message.usage:
                        usage = {
                            "input_tokens": final_message.usage.input_tokens,
                            "output_tokens": final_message.usage.output_tokens,
                        }
                        # Add cache tokens if present
                        if hasattr(final_message.usage, "cache_read_input_tokens"):
                            usage["cache_read_input_tokens"] = (
                                final_message.usage.cache_read_input_tokens or 0
                            )
                        if hasattr(final_message.usage, "cache_creation_input_tokens"):
                            usage["cache_creation_input_tokens"] = (
                                final_message.usage.cache_creation_input_tokens or 0
                            )
                    yield MessageEnd(usage=usage)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.close()
