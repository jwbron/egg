"""Event bus and callback system for the egg harness."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ToolCallEvent:
    """A tool was called."""

    tool_name: str
    tool_use_id: str
    input_data: dict[str, Any]


@dataclass
class ToolResultEvent:
    """A tool returned a result."""

    tool_name: str
    tool_use_id: str
    content: str
    is_error: bool


@dataclass
class TextOutputEvent:
    """Text output from the model."""

    text: str


@dataclass
class TurnCompleteEvent:
    """A turn completed."""

    turn_number: int
    input_tokens: int
    output_tokens: int


@dataclass
class CompactionEvent:
    """Context was compacted."""

    turn_number: int
    pre_token_count: int
    post_token_count: int
    summary_length: int


@dataclass
class ErrorEvent:
    """An error occurred."""

    error: str
    recoverable: bool


@dataclass
class SessionEvent:
    """Session lifecycle event."""

    event_type: str  # "start", "end", "save", "resume"
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# Union type
HarnessEvent = (
    ToolCallEvent
    | ToolResultEvent
    | TextOutputEvent
    | TurnCompleteEvent
    | CompactionEvent
    | ErrorEvent
    | SessionEvent
)

# Callback type: async function that takes an event
EventCallback = Callable[[HarnessEvent], Awaitable[None]]


class EventBus:
    """Simple event bus for harness lifecycle events."""

    def __init__(self) -> None:
        self._callbacks: list[EventCallback] = []
        self._sync_callbacks: list[Callable[[HarnessEvent], None]] = []

    def on_event(self, callback: EventCallback) -> None:
        """Register an async event callback."""
        self._callbacks.append(callback)

    def on_event_sync(self, callback: Callable[[HarnessEvent], None]) -> None:
        """Register a synchronous event callback."""
        self._sync_callbacks.append(callback)

    async def emit(self, event: HarnessEvent) -> None:
        """Emit an event to all registered callbacks."""
        for cb in self._sync_callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.warning(f"Sync event callback error: {e}")

        for cb in self._callbacks:
            try:
                await cb(event)
            except Exception as e:
                logger.warning(f"Async event callback error: {e}")
