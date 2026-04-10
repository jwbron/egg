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

import anthropic  # noqa: EGG200 - harness provider wraps the Anthropic SDK by design

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


def _validate_endpoint_url(url: str) -> None:
    """Validate that an endpoint URL is safe (SSRF mitigation).

    Allows:
    - Known gateway hostnames (egg-gateway, localhost)
    - HTTPS to any host (public APIs)
    - HTTP only to known gateway hosts

    Blocks IPv6-mapped IPv4 addresses, link-local ranges, and DNS
    rebinding via the shared :func:`validate_url_ssrf` helper.

    Raises:
        ValueError: If the URL fails validation.
    """
    from egg_harness.url_validation import validate_url_ssrf

    validate_url_ssrf(url, allow_gateway=True, resolve_dns=False)


class AnthropicProvider(Provider):
    """Provider implementation for the Anthropic Messages API.

    Uses :class:`anthropic.AsyncAnthropic` to open a streaming request and
    yields :data:`StreamEvent` instances as the server sends SSE frames.

    Args:
        config: Provider configuration.  When ``config.endpoint`` is set the
            client will use it as ``base_url`` (typically the gateway proxy).
        gateway_url: Alternative to ``config`` — provide the gateway URL
            directly.  A :class:`ProviderConfig` is created internally.
    """

    # Circuit breaker: trip after consecutive non-retryable failures.
    _CIRCUIT_BREAKER_THRESHOLD: int = 3

    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        gateway_url: str | None = None,
    ) -> None:
        # Allow construction via ``gateway_url`` shorthand.
        if config is None and gateway_url is not None:
            _validate_endpoint_url(gateway_url)
            config = ProviderConfig(
                provider_type="anthropic",
                model="sonnet",
                endpoint=gateway_url,
            )
        if config is None:
            raise TypeError("AnthropicProvider requires either 'config' or 'gateway_url'.")

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
            _validate_endpoint_url(config.endpoint)
            client_kwargs["base_url"] = config.endpoint

        # The gateway injects the real API key via a proxy header; the SDK
        # still requires *some* value so we pass a placeholder.
        client_kwargs["api_key"] = "gateway-managed"

        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        self._model = config.model

        # Circuit breaker state.
        self._consecutive_failures: int = 0

    @property
    def name(self) -> str:
        """Return ``"anthropic"``."""
        return "anthropic"

    def _create_stream(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """Create the raw SDK streaming context manager.

        This method is extracted so tests can patch it to inject mock
        streams.

        Returns:
            An async context manager (from the SDK) that yields SSE events,
            or an async iterator directly.
        """
        return self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            stream=True,
            **kwargs,
        )

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

        This provider does **not** retry internally — retries are handled
        by :class:`RetryProvider` which wraps this provider.  Keeping
        retry outside the generator avoids the impossible problem of
        retracting already-yielded stream events after a partial failure.

        A simple circuit breaker trips after consecutive non-retryable
        failures to prevent cascading calls to a broken endpoint.

        Yields:
            :data:`StreamEvent` instances mapped from the raw SDK stream.
        """
        # Circuit breaker check.
        if self._consecutive_failures >= self._CIRCUIT_BREAKER_THRESHOLD:
            raise RuntimeError(
                f"Circuit breaker open: {self._consecutive_failures} consecutive failures"
            )

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

        try:
            # Track tool-use content blocks for accumulating partial JSON.
            tool_blocks: dict[int, _ToolBlockState] = {}

            raw = self._create_stream(
                model=resolved_model,
                max_tokens=max_tokens,
                messages=messages,
                **kwargs,
            )

            # Handle the result flexibly:
            # - Test mocks return an async iterable directly
            # - Real SDK returns an async context manager
            # - _create_stream could also be async (returns coroutine)
            if hasattr(raw, "__await__") and not hasattr(raw, "__aiter__"):
                raw = await raw

            if hasattr(raw, "__aenter__") and not hasattr(raw, "__aiter__"):
                async with raw as raw_stream:
                    async for event in raw_stream:
                        mapped = _map_event(event, tool_blocks)
                        if mapped is not None:
                            yield mapped
            else:
                async for event in raw:
                    mapped = _map_event(event, tool_blocks)
                    if mapped is not None:
                        yield mapped

            # Success: reset circuit breaker.
            self._consecutive_failures = 0

        except Exception:
            self._consecutive_failures += 1
            raise


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
