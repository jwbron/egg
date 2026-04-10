"""Anthropic provider for the egg harness.

Streams Claude model responses via the ``anthropic`` Python SDK, mapping
the raw SSE events to the harness's :data:`StreamEvent` types.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from egg_harness.config import ProviderConfig
from egg_harness.providers.base import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    Provider,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)

logger = logging.getLogger(__name__)


class AnthropicProvider(Provider):
    """Provider implementation for the Anthropic Messages API.

    Uses :class:`anthropic.AsyncAnthropic` to open a streaming request and
    yields :data:`StreamEvent` instances as the server sends SSE frames.

    Args:
        config: Provider configuration.  When ``config.endpoint`` is set the
            client will use it as ``base_url`` (typically the gateway proxy).
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

        # Security: API keys must flow through the gateway proxy, never from
        # the agent environment directly.
        if "ANTHROPIC_API_KEY" in os.environ:
            raise RuntimeError(
                "ANTHROPIC_API_KEY must not be set in the environment. "
                "API keys are injected by the gateway proxy."
            )

        # Build client kwargs.
        client_kwargs: dict[str, Any] = {}
        if config.endpoint:
            # Validate it looks like a URL.
            if not config.endpoint.startswith(("http://", "https://")):
                raise ValueError(
                    f"Invalid gateway endpoint URL: {config.endpoint!r}"
                )
            client_kwargs["base_url"] = config.endpoint

        # The gateway injects the real API key via a proxy header; the SDK
        # still requires *some* value so we pass a placeholder.
        client_kwargs["api_key"] = "gateway-managed"

        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        self._model = config.model

    @property
    def name(self) -> str:
        """Return ``"anthropic"``."""
        return "anthropic"

    async def send_message(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16384,
        extra_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response from the Anthropic Messages API.

        Yields:
            :data:`StreamEvent` instances mapped from the raw SDK stream.
        """
        resolved_model = model or self._model

        # Merge extra headers from config and from caller.
        headers: dict[str, str] = {}
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        if extra_headers:
            headers.update(extra_headers)

        # Enable prompt caching beta if any message carries cache_control.
        if _has_cache_control(messages, system):
            existing_beta = headers.get("anthropic-beta", "")
            caching_beta = "prompt-caching-2024-04-01"
            if caching_beta not in existing_beta:
                parts = [p for p in existing_beta.split(",") if p.strip()]
                parts.append(caching_beta)
                headers["anthropic-beta"] = ",".join(parts)

        # Build optional keyword arguments for the API call.
        kwargs: dict[str, Any] = {}
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if headers:
            kwargs["extra_headers"] = headers

        # Track tool-use content blocks for accumulating partial JSON.
        # Keyed by content block index.
        tool_blocks: dict[int, _ToolBlockState] = {}

        # The Anthropic SDK uses @overload on the `stream` parameter.  When
        # extra kwargs are forwarded via **kwargs mypy cannot resolve the
        # overload, so we silence the resulting false-positive.
        async with self._client.messages.create(  # type: ignore[attr-defined]
            model=resolved_model,
            max_tokens=max_tokens,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            **kwargs,
        ) as raw_stream:
            async for event in raw_stream:
                mapped = _map_event(event, tool_blocks)
                if mapped is not None:
                    yield mapped


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _ToolBlockState:
    """Mutable accumulator for a single tool-use content block."""

    __slots__ = ("id", "name", "json_chunks")

    def __init__(self, block_id: str, block_name: str) -> None:
        self.id = block_id
        self.name = block_name
        self.json_chunks: list[str] = []


def _has_cache_control(
    messages: list[dict[str, Any]],
    system: str | None,
) -> bool:
    """Return True if any message or content block carries ``cache_control``."""
    for msg in messages:
        if "cache_control" in msg:
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    return True
    # system can be a string (no cache_control possible) or a list of blocks.
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and "cache_control" in block:
                return True
    return False


def _map_event(
    event: Any,
    tool_blocks: dict[int, _ToolBlockState],
) -> StreamEvent | None:
    """Map a single Anthropic SDK stream event to a :data:`StreamEvent`.

    Returns ``None`` when the event should be silently skipped.
    """
    event_type = event.type

    # -- message_start ---------------------------------------------------------
    if event_type == "message_start":
        msg = event.message
        return MessageStart(
            message_id=msg.id,
            model=msg.model,
            role=msg.role,
        )

    # -- content_block_start ---------------------------------------------------
    if event_type == "content_block_start":
        block = event.content_block
        idx = event.index

        if block.type == "tool_use":
            tool_blocks[idx] = _ToolBlockState(block.id, block.name)
            return ToolUseStart(id=block.id, name=block.name)

        # text and thinking blocks: wait for deltas.
        return None

    # -- content_block_delta ---------------------------------------------------
    if event_type == "content_block_delta":
        delta = event.delta

        if delta.type == "text_delta":
            return TextDelta(text=delta.text)

        if delta.type == "input_json_delta":
            idx = event.index
            state = tool_blocks.get(idx)
            if state is not None:
                state.json_chunks.append(delta.partial_json)
            return ToolUseInputDelta(partial_json=delta.partial_json)

        if delta.type == "thinking_delta":
            return ThinkingDelta(text=delta.thinking)

        return None

    # -- content_block_stop ----------------------------------------------------
    if event_type == "content_block_stop":
        idx = event.index
        state = tool_blocks.pop(idx, None)
        if state is not None:
            raw_json = "".join(state.json_chunks)
            try:
                parsed_input = json.loads(raw_json) if raw_json else {}
            except json.JSONDecodeError:
                logger.warning(
                    "Failed to parse tool input JSON for %s: %r",
                    state.name,
                    raw_json,
                )
                parsed_input = {}
            return ToolUseEnd(id=state.id, name=state.name, input=parsed_input)

        return None

    # -- message_delta ---------------------------------------------------------
    if event_type == "message_delta":
        delta = event.delta
        usage_data = event.usage
        usage_dict: dict[str, int] = {}
        if usage_data is not None:
            if hasattr(usage_data, "output_tokens"):
                usage_dict["output_tokens"] = usage_data.output_tokens
            if hasattr(usage_data, "input_tokens"):
                usage_dict["input_tokens"] = usage_data.input_tokens
        return MessageDelta(
            stop_reason=getattr(delta, "stop_reason", None),
            usage=usage_dict,
        )

    # -- message_stop ----------------------------------------------------------
    if event_type == "message_stop":
        return MessageEnd()

    return None
