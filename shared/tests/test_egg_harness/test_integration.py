"""Integration tests for end-to-end egg_harness execution.

Tests the full harness pipeline: provider -> stream -> tool chain -> cost
tracking -> result metadata, using mock HTTP endpoints and in-memory tools.
"""

from __future__ import annotations

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.loop")

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

from egg_harness.config import HarnessConfig
from egg_harness.events import EventBus
from egg_harness.loop import AgentLoop
from egg_harness.providers.base import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    StreamEvent,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from egg_harness.result import AgentResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _stream_events(events: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    """Yield a list of StreamEvent objects as an async iterator."""
    for event in events:
        yield event


def _text_turn(
    text: str,
    *,
    message_id: str = "msg_text",
    model: str = "claude-opus-4-6",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> list[StreamEvent]:
    """Build StreamEvents for a complete text turn ending with end_turn."""
    return [
        MessageStart(message_id=message_id, model=model, role="assistant"),
        TextDelta(text=text),
        MessageDelta(
            stop_reason="end_turn",
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        ),
        MessageEnd(
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        ),
    ]


def _tool_turn(
    tool_name: str,
    tool_input_json: str,
    *,
    tool_use_id: str = "tu_int_001",
    message_id: str = "msg_tool",
    model: str = "claude-opus-4-6",
    input_tokens: int = 200,
    output_tokens: int = 100,
) -> list[StreamEvent]:
    """Build StreamEvents for a complete tool_use turn."""
    return [
        MessageStart(message_id=message_id, model=model, role="assistant"),
        ToolUseStart(tool_use_id=tool_use_id, name=tool_name),
        ToolUseInputDelta(tool_use_id=tool_use_id, partial_json=tool_input_json),
        ToolUseEnd(tool_use_id=tool_use_id),
        MessageDelta(
            stop_reason="tool_use",
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        ),
        MessageEnd(
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        ),
    ]


def _multi_tool_turn(
    tools: list[tuple[str, str, str]],
    *,
    message_id: str = "msg_multi_tool",
    model: str = "claude-opus-4-6",
    input_tokens: int = 300,
    output_tokens: int = 150,
) -> list[StreamEvent]:
    """Build StreamEvents for a turn with multiple tool_use blocks.

    *tools* is a list of (tool_name, tool_use_id, tool_input_json) tuples.
    """
    events: list[StreamEvent] = [
        MessageStart(message_id=message_id, model=model, role="assistant"),
    ]
    for tool_name, tool_use_id, tool_input_json in tools:
        events.extend(
            [
                ToolUseStart(tool_use_id=tool_use_id, name=tool_name),
                ToolUseInputDelta(tool_use_id=tool_use_id, partial_json=tool_input_json),
                ToolUseEnd(tool_use_id=tool_use_id),
            ]
        )
    events.extend(
        [
            MessageDelta(
                stop_reason="tool_use",
                usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            ),
            MessageEnd(
                usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            ),
        ]
    )
    return events


class ScriptedProvider:
    """A mock provider that yields pre-scripted response sequences.

    Each call to send_message consumes the next response from the script.
    Tracks call history for assertions.
    """

    def __init__(self, script: list[list[StreamEvent]]) -> None:
        self._script = list(script)
        self._call_index = 0
        self.call_history: list[dict[str, Any]] = []

    async def send_message(
        self,
        messages: list,
        tools: list,
        system: str,
        model: str,
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


class RecordingRegistry:
    """A ToolRegistry stand-in that records calls and returns canned results.

    *results* maps tool name -> callable(tool_input) -> str.
    If the tool name is not found, returns an error result.
    """

    def __init__(
        self,
        handlers: dict[str, Any] | None = None,
    ) -> None:
        self._handlers = handlers or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> MagicMock:
        self.calls.append((tool_name, tool_input))
        result = MagicMock()
        if tool_name in self._handlers:
            handler = self._handlers[tool_name]
            if callable(handler):
                result.output = handler(tool_input)
            else:
                result.output = str(handler)
            result.is_error = False
        else:
            result.output = f"Unknown tool: {tool_name}"
            result.is_error = True
        return result

    def get_definitions(self) -> list:
        return []


# ---------------------------------------------------------------------------
# TestEndToEndHarness
# ---------------------------------------------------------------------------


class TestEndToEndHarness:
    """Integration tests for end-to-end harness execution."""

    @pytest.mark.asyncio
    async def test_simple_text_conversation(self):
        """Mock provider -> single text response -> AgentResult with success."""
        script = [_text_turn("Hello! I am ready to help.")]
        provider = ScriptedProvider(script)
        registry = RecordingRegistry()
        config = HarnessConfig(
            max_turns=10,
            timeout_seconds=30,
            system_prompt="You are a helpful assistant.",
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Hello"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True
        assert result.num_turns is not None
        assert result.num_turns == 1
        assert result.duration_ms is not None
        assert result.duration_ms >= 0
        # No tools should have been called
        assert len(registry.calls) == 0
        # Provider should have been called exactly once
        assert len(provider.call_history) == 1

    @pytest.mark.asyncio
    async def test_tool_chain_read_modify_write(self):
        """Provider asks to read -> edit -> write file across multiple turns.

        Simulates a realistic multi-turn tool chain:
        1. Provider asks to Read a file
        2. Provider processes the content and asks to Edit
        3. Provider asks to Write the modified content
        4. Provider returns final text response
        """
        read_turn = _tool_turn(
            tool_name="Read",
            tool_input_json='{"file_path": "/tmp/test_file.py"}',
            tool_use_id="tu_read_001",
            message_id="msg_read",
            input_tokens=150,
            output_tokens=80,
        )
        edit_turn = _tool_turn(
            tool_name="Edit",
            tool_input_json=json.dumps(
                {
                    "file_path": "/tmp/test_file.py",
                    "old_string": "def old():",
                    "new_string": "def new():",
                }
            ),
            tool_use_id="tu_edit_001",
            message_id="msg_edit",
            input_tokens=250,
            output_tokens=120,
        )
        write_turn = _tool_turn(
            tool_name="Write",
            tool_input_json=json.dumps(
                {
                    "file_path": "/tmp/test_output.py",
                    "content": "def new():\n    pass\n",
                }
            ),
            tool_use_id="tu_write_001",
            message_id="msg_write",
            input_tokens=300,
            output_tokens=90,
        )
        final_turn = _text_turn(
            "I have read the file, edited the function name, and written the output.",
            message_id="msg_final",
            input_tokens=400,
            output_tokens=60,
        )

        script = [read_turn, edit_turn, write_turn, final_turn]
        provider = ScriptedProvider(script)

        file_state = {"content": "def old():\n    pass\n"}

        def read_handler(inp: dict[str, Any]) -> str:
            return file_state["content"]

        def edit_handler(inp: dict[str, Any]) -> str:
            old = inp.get("old_string", "")
            new = inp.get("new_string", "")
            file_state["content"] = file_state["content"].replace(old, new)
            return f"Replaced '{old}' with '{new}'"

        def write_handler(inp: dict[str, Any]) -> str:
            return f"Wrote {len(inp.get('content', ''))} bytes"

        registry = RecordingRegistry(
            handlers={
                "Read": read_handler,
                "Edit": edit_handler,
                "Write": write_handler,
            }
        )

        config = HarnessConfig(
            max_turns=10,
            timeout_seconds=30,
            system_prompt="You are a code editor.",
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[
                {"role": "user", "content": "Rename old() to new() in test_file.py"},
            ],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True

        # All three tools should have been called in order
        assert len(registry.calls) == 3
        tool_names = [name for name, _ in registry.calls]
        assert tool_names == ["Read", "Edit", "Write"]

        # The file state should reflect the edit
        assert "def new():" in file_state["content"]
        assert "def old():" not in file_state["content"]

        # Provider should have been called 4 times (3 tool turns + 1 text)
        assert len(provider.call_history) == 4

        # Turns should be tracked
        assert result.num_turns is not None
        assert result.num_turns == 4

    @pytest.mark.asyncio
    async def test_cost_tracking_across_turns(self):
        """Multiple turns -> accumulated cost reflects all token usage."""
        # Turn 1: tool call with 200 input, 100 output tokens
        turn1 = _tool_turn(
            tool_name="Bash",
            tool_input_json='{"command": "echo hello"}',
            tool_use_id="tu_cost_001",
            message_id="msg_cost_1",
            input_tokens=200,
            output_tokens=100,
        )
        # Turn 2: tool call with 300 input, 150 output tokens
        turn2 = _tool_turn(
            tool_name="Bash",
            tool_input_json='{"command": "echo world"}',
            tool_use_id="tu_cost_002",
            message_id="msg_cost_2",
            input_tokens=300,
            output_tokens=150,
        )
        # Turn 3: text response with 400 input, 200 output tokens
        turn3 = _text_turn(
            "Both commands executed.",
            message_id="msg_cost_3",
            input_tokens=400,
            output_tokens=200,
        )

        script = [turn1, turn2, turn3]
        provider = ScriptedProvider(script)
        registry = RecordingRegistry(handlers={"Bash": "output"})
        config = HarnessConfig(
            max_turns=10,
            timeout_seconds=30,
            system_prompt="Test cost tracking.",
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Run two commands"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True

        # Cost should be non-zero and reflect accumulated usage
        assert result.cost_usd is not None
        assert result.cost_usd > 0.0

        # With 3 turns total: (200+300+400) input, (100+150+200) output
        # The exact cost depends on the model pricing in CostTracker,
        # but it should be strictly positive and reflect multiple turns
        assert result.num_turns is not None
        assert result.num_turns == 3

    @pytest.mark.asyncio
    async def test_event_bus_receives_all_events(self):
        """EventBus callbacks fire during execution for output, tool, and turns."""
        tool_turn_events = _tool_turn(
            tool_name="Bash",
            tool_input_json='{"command": "echo test"}',
            tool_use_id="tu_bus_001",
            message_id="msg_bus_tool",
        )
        text_turn_events = _text_turn(
            "Command output was: test",
            message_id="msg_bus_text",
        )

        script = [tool_turn_events, text_turn_events]
        provider = ScriptedProvider(script)
        registry = RecordingRegistry(handlers={"Bash": "test\n"})

        event_bus = EventBus()
        output_events: list[str] = []
        tool_call_events: list[tuple[str, dict]] = []
        tool_result_events: list[tuple[str, str]] = []
        turn_complete_events: list[str] = []
        error_events: list[Exception] = []

        event_bus.on_output(lambda text: output_events.append(text))
        event_bus.on_tool_call(lambda name, inp: tool_call_events.append((name, inp)))
        event_bus.on_tool_result(lambda name, res: tool_result_events.append((name, res)))
        event_bus.on_turn_complete(lambda: turn_complete_events.append("done"))
        event_bus.on_error(lambda err: error_events.append(err))

        config = HarnessConfig(
            max_turns=10,
            timeout_seconds=30,
            system_prompt="Test event bus.",
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
            event_bus=event_bus,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Run echo test"}],
        )

        assert result.success is True

        # Output events should include the text from the final turn
        assert len(output_events) >= 1
        assert any("test" in text.lower() for text in output_events)

        # Tool call events should have fired for the Bash tool
        assert len(tool_call_events) >= 1
        assert any(name == "Bash" for name, _ in tool_call_events)

        # Tool result events should have fired
        assert len(tool_result_events) >= 1

        # Turn complete events should have fired at least twice
        # (one for tool turn, one for text turn)
        assert len(turn_complete_events) >= 2

        # No errors should have occurred
        assert len(error_events) == 0

    @pytest.mark.asyncio
    async def test_result_metadata_complete(self):
        """All AgentResult fields are populated after a multi-turn execution."""
        tool_events = _tool_turn(
            tool_name="Bash",
            tool_input_json='{"command": "date"}',
            tool_use_id="tu_meta_001",
            message_id="msg_meta_tool",
            input_tokens=250,
            output_tokens=80,
        )
        text_events = _text_turn(
            "Today's date is 2026-04-10.",
            message_id="msg_meta_text",
            input_tokens=350,
            output_tokens=40,
        )

        script = [tool_events, text_events]
        provider = ScriptedProvider(script)
        registry = RecordingRegistry(handlers={"Bash": "Thu Apr 10 12:00:00 UTC 2026"})
        config = HarnessConfig(
            max_turns=10,
            timeout_seconds=60,
            system_prompt="Test metadata completeness.",
        )

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "What is today's date?"}],
        )

        # --- Verify all result fields ---

        # success
        assert isinstance(result.success, bool)
        assert result.success is True

        # stdout / stderr / returncode (process-level fields)
        assert isinstance(result.stdout, str)
        assert isinstance(result.stderr, str)
        assert isinstance(result.returncode, int)

        # cost_usd
        assert result.cost_usd is not None
        assert isinstance(result.cost_usd, float)
        assert result.cost_usd > 0.0

        # num_turns
        assert result.num_turns is not None
        assert isinstance(result.num_turns, int)
        assert result.num_turns == 2

        # duration_ms
        assert result.duration_ms is not None
        assert isinstance(result.duration_ms, int)
        assert result.duration_ms >= 0

        # error should be None on success
        assert result.error is None

        # metadata may or may not be populated, but the field should exist
        assert hasattr(result, "metadata")
