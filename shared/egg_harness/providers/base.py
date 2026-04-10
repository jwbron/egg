"""Abstract provider interface and stream event types."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

# --- Stream Event Types ---


@dataclass
class MessageStart:
    """Signals start of a new message from the model."""

    message_id: str
    model: str


@dataclass
class TextDelta:
    """Incremental text content."""

    text: str


@dataclass
class ThinkingDelta:
    """Extended thinking content (Anthropic-specific)."""

    text: str


@dataclass
class ToolUseStart:
    """Start of a tool use block."""

    tool_use_id: str
    name: str


@dataclass
class ToolUseInputDelta:
    """Incremental JSON input for a tool use."""

    partial_json: str


@dataclass
class ToolUseEnd:
    """End of a tool use block — input is complete."""

    tool_use_id: str


@dataclass
class MessageDelta:
    """Message-level metadata update (e.g., stop_reason)."""

    stop_reason: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class MessageEnd:
    """Final event — message is complete."""

    usage: dict[str, int] | None = None


# Union type for all stream events
StreamEvent = (
    MessageStart
    | TextDelta
    | ThinkingDelta
    | ToolUseStart
    | ToolUseInputDelta
    | ToolUseEnd
    | MessageDelta
    | MessageEnd
)


@dataclass
class Message:
    """A message in the conversation."""

    role: str  # "user", "assistant", "system"
    content: Any  # str or list of content blocks


@dataclass
class ToolResult:
    """Result of a tool execution."""

    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class ToolDefinition:
    """JSON schema definition for a tool."""

    name: str
    description: str
    input_schema: dict[str, Any]


class Provider(abc.ABC):
    """Abstract base class for LLM providers."""

    @abc.abstractmethod
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
        """Send messages and yield stream events.

        Args:
            messages: Conversation messages in provider-native format.
            tools: Tool definitions available to the model.
            system: System prompt.
            max_tokens: Maximum tokens to generate.
            model: Model override (uses provider default if None).
            extra_headers: Additional HTTP headers.

        Yields:
            StreamEvent instances as they arrive from the API.
        """
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Clean up provider resources."""
        ...
