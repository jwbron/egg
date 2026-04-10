"""OpenAI-compatible provider for the egg harness.

Streams chat completion responses from any OpenAI-compatible API endpoint
using raw ``httpx`` SSE streaming, mapping the chunks to the harness's
:data:`StreamEvent` types.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from egg_harness.config import ProviderConfig
from egg_harness.providers.anthropic import _validate_endpoint_url
from egg_harness.providers.base import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    Provider,
    StreamEvent,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(Provider):
    """Provider for OpenAI-compatible ``/v1/chat/completions`` endpoints.

    Converts Anthropic-style messages and tool definitions to the OpenAI
    format, then parses the SSE stream back into harness :data:`StreamEvent`
    instances.

    Args:
        config: Provider configuration.  ``config.endpoint`` is required and
            must point to the API base URL (e.g. ``http://localhost:8080``).
            ``config.api_key_env`` names the environment variable holding the
            API key.
        base_url: Alternative to ``config`` — provide the base URL directly.
        api_key: API key to use (stored in the environment variable lookup).
        model: Model name override.
        api_key_env: Environment variable name holding the API key.
        capabilities: Optional capabilities dict for the provider.
    """

    def __init__(
        self,
        config: ProviderConfig | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        api_key_env: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        # Allow construction via base_url shorthand.
        if config is None and base_url is not None:
            if not base_url:
                raise ValueError(
                    "OpenAICompatibleProvider requires a non-empty base_url."
                )
            _validate_endpoint_url(base_url)
            config = ProviderConfig(
                provider_type="openai_compatible",
                model=model or "default",
                endpoint=base_url,
                api_key_env=api_key_env,
            )
        if config is None:
            raise TypeError(
                "OpenAICompatibleProvider requires either 'config' or 'base_url'."
            )

        self._config = config
        self._model = config.model

        if not config.endpoint:
            raise ValueError(
                "OpenAICompatibleProvider requires config.endpoint to be set."
            )
        _validate_endpoint_url(config.endpoint)
        self._endpoint = config.endpoint.rstrip("/")
        self._base_url = self._endpoint

        # Store the API key directly if provided.
        self._api_key = api_key

        # Store capabilities.
        self._capabilities = capabilities or {
            "supports_tools": True,
            "supports_streaming": True,
            "supports_system_prompt": True,
        }

    @property
    def capabilities(self) -> dict[str, Any]:
        """Return provider capabilities."""
        return self._capabilities

    @property
    def name(self) -> str:
        """Return ``"openai_compatible"``."""
        return "openai_compatible"

    def _create_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        headers: dict[str, str],
        body: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        """Create the raw streaming response.

        This method is extracted so tests can patch it to inject mock
        streams. Returns an async iterable of chunk objects, or an
        httpx response that will be parsed.
        """
        # Default implementation: return an async generator that performs
        # the HTTP streaming and parses SSE into chunk dicts.
        return self._http_stream(body=body, headers=headers)

    async def _http_stream(
        self,
        *,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[Any]:
        """Perform the actual HTTP SSE streaming."""
        url = f"{self._endpoint}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue

                    payload = line[len("data: "):]

                    if payload.strip() == "[DONE]":
                        return

                    try:
                        chunk_data = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed SSE chunk: %r", payload)
                        continue

                    # Yield as a simple object with attribute access
                    yield _DictObj(chunk_data)

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
        """Stream a chat completion from the OpenAI-compatible endpoint.

        Yields:
            :data:`StreamEvent` instances mapped from chunk objects.
        """
        resolved_model = model or self._model

        # Build OpenAI-format messages.
        openai_messages = _convert_messages(messages, system)

        # Build request body.
        body: dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "messages": openai_messages,
            "stream": True,
        }

        if tools:
            body["tools"] = _convert_tools(tools)

        # Build headers.
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self._config.extra_headers:
            headers.update(self._config.extra_headers)
        if extra_headers:
            headers.update(extra_headers)

        # Resolve API key: first from direct parameter, then from env var.
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif self._config.api_key_env:
            api_key = os.environ.get(self._config.api_key_env, "")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        # Track in-progress tool calls keyed by tool_call index within the
        # current choice.
        active_tools: dict[int, _ToolCallState] = {}
        sent_message_start = False

        raw = self._create_stream(
            model=resolved_model,
            messages=openai_messages,
            headers=headers,
            body=body,
        )

        # Handle coroutine return from _create_stream
        if hasattr(raw, "__await__") and not hasattr(raw, "__aiter__"):
            raw = await raw

        async for chunk in raw:
            # Emit MessageStart on the first chunk.
            if not sent_message_start:
                sent_message_start = True
                chunk_id = getattr(chunk, "id", "") or ""
                chunk_model = getattr(chunk, "model", resolved_model) or resolved_model
                yield MessageStart(
                    message_id=chunk_id,
                    model=chunk_model,
                    role="assistant",
                )

            choices = getattr(chunk, "choices", []) or []
            for choice in choices:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                # Text content.
                content = getattr(delta, "content", None)
                if content:
                    yield TextDelta(text=content)

                # Tool calls.
                tool_calls = getattr(delta, "tool_calls", None) or []
                for tc in tool_calls:
                    tc_index = getattr(tc, "index", 0)
                    func = getattr(tc, "function", None)

                    if tc_index not in active_tools:
                        # New tool call.
                        tc_id = getattr(tc, "id", None) or f"call_{uuid.uuid4().hex[:24]}"
                        tc_name = getattr(func, "name", "") if func else ""
                        active_tools[tc_index] = _ToolCallState(
                            id=tc_id, name=tc_name
                        )
                        yield ToolUseStart(id=tc_id, name=tc_name)

                    if func:
                        args_chunk = getattr(func, "arguments", "")
                        if args_chunk:
                            active_tools[tc_index].json_chunks.append(args_chunk)
                            yield ToolUseInputDelta(partial_json=args_chunk)

                # Finish reason.
                finish_reason = getattr(choice, "finish_reason", None)
                if finish_reason is not None:
                    # Flush active tool calls before emitting stop.
                    for state in active_tools.values():
                        yield _finish_tool(state)
                    active_tools.clear()

                    usage = getattr(chunk, "usage", None) or {}
                    if isinstance(usage, dict):
                        yield MessageDelta(stop_reason=finish_reason, usage=usage)
                    else:
                        yield MessageDelta(stop_reason=finish_reason, usage={})

        # Flush any remaining tool calls.
        for state in active_tools.values():
            yield _finish_tool(state)
        active_tools.clear()
        yield MessageEnd()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _DictObj:
    """Wrapper that allows attribute-style access to a dict."""

    def __init__(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                setattr(self, key, _DictObj(value))
            elif isinstance(value, list):
                setattr(self, key, [
                    _DictObj(item) if isinstance(item, dict) else item
                    for item in value
                ])
            else:
                setattr(self, key, value)


class _ToolCallState:
    """Mutable accumulator for a single in-flight tool call."""

    __slots__ = ("id", "name", "json_chunks")

    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name
        self.json_chunks: list[str] = []


def _finish_tool(state: _ToolCallState) -> ToolUseEnd:
    """Build a :class:`ToolUseEnd` from accumulated JSON fragments."""
    raw = "".join(state.json_chunks)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        logger.warning(
            "Failed to parse tool input JSON for %s: %r",
            state.name,
            raw,
        )
        parsed = {}
    return ToolUseEnd(id=state.id, name=state.name, input=parsed)


def _map_stop_reason(finish_reason: str) -> str:
    """Map an OpenAI ``finish_reason`` to an Anthropic-style ``stop_reason``."""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "content_filter",
    }
    return mapping.get(finish_reason, finish_reason)


def _convert_messages(
    messages: list[dict[str, Any]],
    system: str | None,
) -> list[dict[str, Any]]:
    """Convert Anthropic-style messages to OpenAI chat format.

    Anthropic messages use structured ``content`` blocks (list of dicts);
    OpenAI expects either a plain string or a list of typed parts.  This
    function handles the translation, including tool-result messages.
    """
    result: list[dict[str, Any]] = []

    # Inject system prompt as the first message.
    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        # Tool result messages (Anthropic format).
        if role == "tool":
            result.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_use_id", ""),
                "content": _flatten_content(content),
            })
            continue

        # Assistant messages with tool_use content blocks.
        if role == "assistant" and isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    text_parts.append(str(block))
                    continue
                block_type = block.get("type", "text")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })

            openai_msg: dict[str, Any] = {"role": "assistant"}
            combined_text = "".join(text_parts)
            if combined_text:
                openai_msg["content"] = combined_text
            else:
                openai_msg["content"] = None
            if tool_calls:
                openai_msg["tool_calls"] = tool_calls
            result.append(openai_msg)
            continue

        # Standard user / assistant messages.
        result.append({
            "role": role,
            "content": _flatten_content(content),
        })

    return result


def _flatten_content(content: Any) -> str:
    """Flatten Anthropic content (string or list of blocks) to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content) if content is not None else ""


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool definitions to OpenAI function-calling format.

    Anthropic format::

        {"name": "...", "description": "...", "input_schema": {...}}

    OpenAI format::

        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    result: list[dict[str, Any]] = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return result
