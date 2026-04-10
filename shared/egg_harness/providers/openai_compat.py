"""OpenAI-compatible provider using raw httpx."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from egg_harness.providers.base import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    Provider,
    StreamEvent,
    TextDelta,
    ToolDefinition,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)

logger = logging.getLogger(__name__)


def _convert_messages(messages: list[dict[str, Any]], system: str | None) -> list[dict[str, Any]]:
    """Convert Anthropic-style messages to OpenAI format."""
    converted = []
    if system:
        converted.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            converted.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Handle content blocks (tool results, etc.)
            parts = []
            tool_results = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block["text"])
                    elif block.get("type") == "tool_result":
                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": block.get("content", ""),
                            }
                        )
                    elif block.get("type") == "tool_use":
                        # This is in assistant messages
                        # We need to convert to OpenAI's tool_calls format
                        pass

            if parts:
                converted.append({"role": role, "content": "\n".join(parts)})
            for tr in tool_results:
                converted.append(tr)
        else:
            converted.append({"role": role, "content": str(content) if content else ""})

    return converted


def _convert_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Convert tool definitions to OpenAI function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


class OpenAICompatibleProvider(Provider):
    """OpenAI-compatible provider for vLLM, Ollama, etc."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000/v1",
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "not-needed")
        self._default_model = default_model
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
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
        """Send messages via OpenAI-compatible chat completions with streaming."""
        effective_model = model or self._default_model or "default"

        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": _convert_messages(messages, system),
            "max_tokens": max_tokens,
            "stream": True,
        }

        if tools:
            payload["tools"] = _convert_tools(tools)
            payload["tool_choice"] = "auto"

        headers = extra_headers or {}

        async with self._client.stream(
            "POST",
            "/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            response.raise_for_status()

            tool_calls: dict[int, dict[str, Any]] = {}
            first_chunk = True

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if first_chunk:
                    yield MessageStart(
                        message_id=chunk.get("id", ""),
                        model=chunk.get("model", effective_model),
                    )
                    first_chunk = False

                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason")

                    # Text content
                    if "content" in delta and delta["content"]:
                        yield TextDelta(text=delta["content"])

                    # Tool calls
                    if "tool_calls" in delta:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls:
                                tool_calls[idx] = {
                                    "id": tc.get("id", f"call_{idx}"),
                                    "name": tc.get("function", {}).get("name", ""),
                                    "arguments": "",
                                }
                                yield ToolUseStart(
                                    tool_use_id=tool_calls[idx]["id"],
                                    name=tool_calls[idx]["name"],
                                )

                            args_delta = tc.get("function", {}).get("arguments", "")
                            if args_delta:
                                tool_calls[idx]["arguments"] += args_delta
                                yield ToolUseInputDelta(partial_json=args_delta)

                    if finish_reason:
                        # End all open tool calls
                        for _idx, tc_info in tool_calls.items():
                            yield ToolUseEnd(tool_use_id=tc_info["id"])
                        tool_calls.clear()

                        yield MessageDelta(stop_reason=finish_reason)

            # Usage info from the final chunk (some providers include it)
            yield MessageEnd(usage=None)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()
