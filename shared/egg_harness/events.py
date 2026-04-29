"""Typed event bus for harness lifecycle events.

Provides a lightweight publish/subscribe mechanism so callers can observe
agent activity (output tokens, tool calls, compactions, errors, etc.)
without coupling to a specific provider implementation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EventBus:
    """Typed callback registry and dispatcher for agent events.

    Register one or more callbacks per event type using the ``on_*``
    methods.  The harness calls the corresponding ``emit_*`` methods as
    events occur; exceptions raised inside callbacks are caught and logged
    so they never disrupt the agent run.

    Example::

        bus = EventBus()
        bus.on_output(lambda text: print(text, end=""))
        bus.on_error(lambda exc: print(f"ERROR: {exc}"))
    """

    _output_callbacks: list[Callable[[str], None]] = field(
        default_factory=list,
    )
    _tool_call_callbacks: list[Callable[[str, dict[str, Any]], None]] = field(
        default_factory=list,
    )
    _tool_result_callbacks: list[Callable[[str, str], None]] = field(
        default_factory=list,
    )
    _compaction_callbacks: list[Callable[[str, int, int], None]] = field(
        default_factory=list,
    )
    _error_callbacks: list[Callable[[Exception], None]] = field(
        default_factory=list,
    )
    _turn_complete_callbacks: list[Callable[[int, dict[str, int]], None]] = field(
        default_factory=list,
    )

    # -- registration ----------------------------------------------------------

    def on_output(self, callback: Callable[[str], None]) -> None:
        """Register a callback invoked with each text output chunk."""
        self._output_callbacks.append(callback)

    def on_tool_call(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a callback invoked when a tool is called.

        Args:
            callback: Receives ``(tool_name, tool_input)`` on each call.
        """
        self._tool_call_callbacks.append(callback)

    def on_tool_result(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback invoked when a tool returns a result.

        Args:
            callback: Receives ``(tool_name, tool_output)`` on each result.
        """
        self._tool_result_callbacks.append(callback)

    def on_compaction(self, callback: Callable[[str, int, int], None]) -> None:
        """Register a callback invoked on context compaction.

        Args:
            callback: Receives ``(summary, tokens_before, tokens_after)``.
        """
        self._compaction_callbacks.append(callback)

    def on_error(self, callback: Callable[[Exception], None]) -> None:
        """Register a callback invoked when an error occurs."""
        self._error_callbacks.append(callback)

    def on_turn_complete(self, callback: Callable[[int, dict[str, int]], None]) -> None:
        """Register a callback invoked at the end of each turn.

        Args:
            callback: Receives ``(turn_number, usage)`` where *usage* is a
                dict of token-count keys (e.g. ``input_tokens``,
                ``output_tokens``).
        """
        self._turn_complete_callbacks.append(callback)

    # -- emission --------------------------------------------------------------

    def emit_output(self, text: str) -> None:
        """Emit a text output event."""
        self._dispatch(self._output_callbacks, text)

    def emit_tool_call(self, name: str, tool_input: dict[str, Any]) -> None:
        """Emit a tool-call event."""
        self._dispatch(self._tool_call_callbacks, name, tool_input)

    def emit_tool_result(self, name: str, output: str) -> None:
        """Emit a tool-result event."""
        self._dispatch(self._tool_result_callbacks, name, output)

    def emit_compaction(self, summary: str, tokens_before: int, tokens_after: int) -> None:
        """Emit a context-compaction event."""
        self._dispatch(self._compaction_callbacks, summary, tokens_before, tokens_after)

    def emit_error(self, exc: Exception) -> None:
        """Emit an error event."""
        self._dispatch(self._error_callbacks, exc)

    def emit_turn_complete(self, turn_number: int, usage: dict[str, int]) -> None:
        """Emit a turn-complete event."""
        self._dispatch(self._turn_complete_callbacks, turn_number, usage)

    # -- internal --------------------------------------------------------------

    @staticmethod
    def _dispatch(callbacks: list[Callable[..., None]], *args: Any) -> None:
        """Invoke each callback, catching and logging any exceptions.

        Callbacks may accept fewer arguments than are provided; the
        dispatcher determines the arity up front via :func:`inspect.signature`
        and only passes the appropriate number of arguments.  This avoids
        masking real ``TypeError`` exceptions raised inside callbacks.
        """
        import inspect

        for cb in callbacks:
            try:
                try:
                    sig = inspect.signature(cb)
                    # Count parameters that accept positional arguments.
                    n_params = sum(
                        1
                        for p in sig.parameters.values()
                        if p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                    )
                    has_var_positional = any(
                        p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
                    )
                    if has_var_positional:
                        cb(*args)
                    else:
                        cb(*args[:n_params])
                except ValueError, TypeError:
                    # inspect.signature can fail for builtins; fall back to
                    # passing all args.
                    cb(*args)
            except Exception:
                logger.exception("Event callback %r raised an exception", cb)
