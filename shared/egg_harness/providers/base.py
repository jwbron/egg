"""Provider abstract base class and streaming event types for the egg harness.

This module defines the contract that all LLM providers must implement,
along with the structured event types emitted during streaming responses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Union


# ---------------------------------------------------------------------------
# Stream event dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A chunk of streaming text content."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolUseStart:
    """Signals the beginning of a tool call."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolUseInputDelta:
    """A chunk of streaming JSON input for an in-progress tool call."""

    partial_json: str


@dataclass(frozen=True, slots=True)
class ToolUseEnd:
    """Signals the end of a tool call with the complete, parsed input."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """A chunk of extended-thinking text."""

    text: str


@dataclass(frozen=True, slots=True)
class MessageStart:
    """Signals the beginning of a new assistant message."""

    message_id: str
    model: str
    role: str


@dataclass(frozen=True, slots=True)
class MessageDelta:
    """Carries message-level changes such as stop reason and cumulative usage.

    Attributes:
        stop_reason: Why the message stopped (e.g. ``"end_turn"``,
            ``"tool_use"``), or ``None`` if not yet terminated.
        usage: Cumulative token counts, e.g.
            ``{"input_tokens": 100, "output_tokens": 42}``.
    """

    stop_reason: str | None
    usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class MessageEnd:
    """Signals that the stream is complete."""


StreamEvent = Union[
    TextDelta,
    ToolUseStart,
    ToolUseInputDelta,
    ToolUseEnd,
    ThinkingDelta,
    MessageStart,
    MessageDelta,
    MessageEnd,
]
"""Union of all possible events yielded during a streaming response."""


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------


class Provider(ABC):
    """Abstract base class for LLM providers.

    Subclasses must implement :pyattr:`name` and :pymeth:`send_message` to
    integrate a specific model API (e.g. Anthropic, OpenAI) with the egg
    harness runtime.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """A short, unique identifier for this provider (e.g. ``"anthropic"``)."""

    @abstractmethod
    def send_message(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16384,
        extra_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a response from the provider.

        This is an async generator: implementations must ``yield``
        :data:`StreamEvent` instances as they arrive from the upstream API.

        Args:
            messages: Conversation history in the provider-neutral format
                (list of message dicts with ``role`` and ``content`` keys).
            tools: Optional tool definitions the model may invoke.
            system: Optional system prompt.
            model: Model identifier override.  When ``None``, the provider
                should fall back to its configured default.
            max_tokens: Maximum number of tokens to generate.
            extra_headers: Additional HTTP headers to pass to the upstream API.

        Yields:
            StreamEvent instances in the order they are received.
        """
        # The yield annotation is required so that Python treats this as an
        # async generator even though the body is abstract.
        yield  # type: ignore[misc]  # pragma: no cover
