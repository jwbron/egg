"""LLM provider abstractions for the egg harness.

Re-exports the :class:`Provider` ABC, the :data:`StreamEvent` union type,
and every concrete event dataclass so that callers can import directly from
``egg_harness.providers``.
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
    "MessageDelta",
    "MessageEnd",
    "MessageStart",
    "Provider",
    "StreamEvent",
    "TextDelta",
    "ThinkingDelta",
    "ToolUseEnd",
    "ToolUseInputDelta",
    "ToolUseStart",
]
