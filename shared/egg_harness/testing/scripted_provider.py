"""Scripted provider for deterministic agent-harness testing.

``ScriptedProvider`` was originally an inline helper inside
``shared/tests/test_egg_harness/test_integration.py``.  It is promoted to a
public testing API here so integration tests under ``integration_tests/`` and
any other consumer can hand each agent role a canned LLM trajectory without
hitting a live model.

The class is intentionally minimal — it implements just enough of the
``egg_harness`` provider surface (``name``, ``send_message``) to slot into
``AgentLoop`` for tests.  Call sites construct it with a *script*: a list of
per-call lists of ``StreamEvent`` objects, one inner list per LLM call.  The
provider yields the events from the next entry on each ``send_message``
invocation; once the script is exhausted, subsequent calls keep yielding the
final entry so tests don't have to pad their scripts to the exact call count.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from egg_harness.providers.base import StreamEvent


async def _stream_events(events: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    """Yield a list of StreamEvent objects as an async iterator."""
    for event in events:
        yield event


class ScriptedProvider:
    """A mock provider that yields pre-scripted response sequences."""

    def __init__(self, script: list[list[StreamEvent]]) -> None:
        self._script = list(script)
        self._call_index = 0
        self.call_history: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "scripted"

    async def send_message(
        self,
        *,
        messages: list[Any],
        tools: list[Any] | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16384,
        extra_headers: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.call_history.append(
            {
                "messages": messages,
                "tools": tools,
                "system": system,
                "model": model,
            }
        )
        idx = min(self._call_index, len(self._script) - 1)
        self._call_index += 1
        async for event in _stream_events(self._script[idx]):
            yield event


__all__ = ["ScriptedProvider", "_stream_events"]
