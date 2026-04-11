"""LLM provider abstractions for the egg harness.

Re-exports the :class:`Provider` ABC, the :data:`StreamEvent` union type,
every concrete event dataclass, and all provider implementations.

Provider implementations (AnthropicProvider, OpenAICompatibleProvider) are
imported lazily to avoid hard dependency on ``anthropic`` or ``httpx`` at
import time.
"""

from __future__ import annotations

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

__all__ = [
    "AnthropicProvider",
    "CircuitOpenError",
    "MessageDelta",
    "MessageEnd",
    "MessageStart",
    "OpenAICompatibleProvider",
    "Provider",
    "RetryProvider",
    "StreamEvent",
    "TextDelta",
    "ThinkingDelta",
    "ToolUseEnd",
    "ToolUseInputDelta",
    "ToolUseStart",
]


def __getattr__(name: str) -> object:
    """Lazy-import provider implementations to avoid import-time SDK deps."""
    if name == "AnthropicProvider":
        from egg_harness.providers.anthropic import AnthropicProvider

        return AnthropicProvider
    if name == "OpenAICompatibleProvider":
        from egg_harness.providers.openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider
    if name == "RetryProvider":
        from egg_harness.providers.retry import RetryProvider

        return RetryProvider
    if name == "CircuitOpenError":
        from egg_harness.providers.retry import CircuitOpenError

        return CircuitOpenError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
