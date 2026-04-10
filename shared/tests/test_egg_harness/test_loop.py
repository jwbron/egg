"""Tests for egg_harness.loop — AgentLoop core execution engine.

Covers the agent loop lifecycle: messages -> provider -> stream -> tool
execution -> back to provider, plus stop reasons, timeouts, turn counting,
and result metadata.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from egg_harness.config import HarnessConfig
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
from egg_harness.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _stream_events(events: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    """Yield a list of StreamEvent objects as an async iterator."""
    for event in events:
        yield event


def _make_text_response_events(
    text: str = "Hello!",
    *,
    model: str = "claude-opus-4-6",
    message_id: str = "msg_001",
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_reason: str = "end_turn",
) -> list[StreamEvent]:
    """Build a complete sequence of StreamEvents for a simple text response."""
    return [
        MessageStart(message_id=message_id, model=model, role="assistant"),
        TextDelta(text=text),
        MessageDelta(
            stop_reason=stop_reason,
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        ),
        MessageEnd(
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        ),
    ]


def _make_tool_use_events(
    tool_name: str = "Bash",
    tool_use_id: str = "tu_001",
    tool_input_json: str = '{"command": "echo hi"}',
    *,
    model: str = "claude-opus-4-6",
    message_id: str = "msg_002",
    input_tokens: int = 150,
    output_tokens: int = 80,
) -> list[StreamEvent]:
    """Build a complete sequence of StreamEvents for a tool_use response."""
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


def _make_mock_provider(
    responses: list[list[StreamEvent]],
) -> MagicMock:
    """Create a mock Provider whose send_message yields successive responses.

    Each call to send_message consumes the next list of StreamEvent from
    *responses*. After all are consumed, further calls yield a text end_turn.
    """
    provider = MagicMock()
    call_count = 0

    async def _send_message(
        messages: list,
        tools: list,
        system: str,
        model: str,
    ) -> AsyncIterator[StreamEvent]:
        nonlocal call_count
        idx = min(call_count, len(responses) - 1)
        call_count += 1
        async for event in _stream_events(responses[idx]):
            yield event

    provider.send_message = _send_message
    return provider


def _make_mock_registry(
    results: dict[str, str] | None = None,
) -> MagicMock:
    """Create a mock ToolRegistry that returns canned results.

    *results* maps tool name to output string. Unknown tools return an error.
    """
    registry = MagicMock(spec=ToolRegistry)

    def _execute(tool_name: str, tool_input: dict[str, Any]) -> MagicMock:
        result = MagicMock()
        if results and tool_name in results:
            result.is_error = False
            result.output = results[tool_name]
        else:
            result.is_error = True
            result.output = f"Unknown tool: {tool_name}"
        return result

    registry.execute.side_effect = _execute
    registry.get_definitions.return_value = []
    return registry


def _make_failing_registry(
    error_cls: type[Exception] = RuntimeError,
    error_msg: str = "tool exploded",
) -> MagicMock:
    """Create a mock ToolRegistry whose execute always raises."""
    registry = MagicMock(spec=ToolRegistry)
    registry.execute.side_effect = error_cls(error_msg)
    registry.get_definitions.return_value = []
    return registry


def _make_default_config(**overrides) -> HarnessConfig:
    """Build a HarnessConfig with sensible test defaults."""
    defaults: dict[str, Any] = {
        "max_turns": 100,
        "timeout_seconds": 30,
        "system_prompt": "You are a helpful assistant.",
    }
    defaults.update(overrides)
    return HarnessConfig(**defaults)


# ---------------------------------------------------------------------------
# TestAgentLoopBasic
# ---------------------------------------------------------------------------


class TestAgentLoopBasic:
    """Core loop: messages -> provider -> stream -> tool execution -> result."""

    @pytest.mark.asyncio
    async def test_single_turn_text_response(self):
        """Provider returns text with end_turn -> loop returns result."""
        events = _make_text_response_events(text="Hello, world!")
        provider = _make_mock_provider([events])
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Hi"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True
        # The provider should have been called exactly once for a text response
        assert result.num_turns is not None
        assert result.num_turns >= 1

    @pytest.mark.asyncio
    async def test_multi_turn_with_tool_use(self):
        """Provider returns tool_use -> tool executed -> result sent back -> text."""
        tool_events = _make_tool_use_events(
            tool_name="Bash",
            tool_use_id="tu_100",
            tool_input_json='{"command": "echo hi"}',
        )
        text_events = _make_text_response_events(
            text="Done! The command output was: hi",
            stop_reason="end_turn",
        )
        provider = _make_mock_provider([tool_events, text_events])
        registry = _make_mock_registry(results={"Bash": "hi\n"})
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Run echo hi"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True
        # Tool should have been executed
        registry.execute.assert_called()
        # Should have taken at least 2 turns (tool_use + text)
        assert result.num_turns is not None
        assert result.num_turns >= 2

    @pytest.mark.asyncio
    async def test_max_turns_stops_loop(self):
        """After max_turns, loop stops even if provider wants more tool_use."""
        # Provider always returns tool_use, never end_turn
        tool_events = _make_tool_use_events(tool_name="Bash")
        provider = _make_mock_provider([tool_events] * 10)
        registry = _make_mock_registry(results={"Bash": "output"})
        config = _make_default_config(max_turns=3)

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Keep going"}],
        )

        assert isinstance(result, AgentResult)
        # Loop should stop after max_turns
        assert result.num_turns is not None
        assert result.num_turns <= 3

    @pytest.mark.asyncio
    async def test_empty_messages_handled(self):
        """Starting with no messages should be handled gracefully."""
        events = _make_text_response_events(text="I have no context.")
        provider = _make_mock_provider([events])
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )

        # Either succeeds or raises a clear error -- must not crash unexpectedly
        try:
            result = await loop.run(messages=[])
            assert isinstance(result, AgentResult)
        except (ValueError, TypeError):
            pass  # Acceptable to reject empty messages


# ---------------------------------------------------------------------------
# TestAgentLoopStopReasons
# ---------------------------------------------------------------------------


class TestAgentLoopStopReasons:
    """Verify loop behaviour for each stop_reason value."""

    @pytest.mark.asyncio
    async def test_end_turn_stops_loop(self):
        """stop_reason='end_turn' -> loop exits normally."""
        events = _make_text_response_events(text="Goodbye.", stop_reason="end_turn")
        provider = _make_mock_provider([events])
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Bye"}],
        )

        assert result.success is True
        assert result.num_turns is not None
        assert result.num_turns == 1

    @pytest.mark.asyncio
    async def test_max_tokens_continues(self):
        """stop_reason='max_tokens' -> could continue or stop depending on impl.

        The loop may treat max_tokens as a reason to continue (requesting more
        output) or to stop. Either is acceptable; we verify no crash.
        """
        max_tokens_events = _make_text_response_events(
            text="partial output...", stop_reason="max_tokens"
        )
        # If the loop continues, provide a final end_turn response
        end_events = _make_text_response_events(
            text=" and here is the rest.", stop_reason="end_turn"
        )
        provider = _make_mock_provider([max_tokens_events, end_events])
        registry = _make_mock_registry()
        config = _make_default_config(max_turns=5)

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Tell me a story"}],
        )

        assert isinstance(result, AgentResult)
        # Must not crash; success depends on implementation choice
        assert result.num_turns is not None
        assert result.num_turns >= 1

    @pytest.mark.asyncio
    async def test_tool_use_continues_loop(self):
        """stop_reason='tool_use' -> execute tool, continue to next turn."""
        tool_events = _make_tool_use_events(
            tool_name="Bash",
            tool_use_id="tu_200",
        )
        text_events = _make_text_response_events(
            text="Tool executed successfully.", stop_reason="end_turn"
        )
        provider = _make_mock_provider([tool_events, text_events])
        registry = _make_mock_registry(results={"Bash": "ok"})
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Run a command"}],
        )

        assert result.success is True
        # Must have done at least 2 turns: tool_use + end_turn
        assert result.num_turns is not None
        assert result.num_turns >= 2
        registry.execute.assert_called_once()


# ---------------------------------------------------------------------------
# TestAgentLoopTimeout
# ---------------------------------------------------------------------------


class TestAgentLoopTimeout:
    """Verify wall-clock timeout enforcement."""

    @pytest.mark.asyncio
    async def test_wall_clock_timeout(self):
        """Loop should stop after timeout_seconds and return a timeout result."""

        async def _slow_send_message(messages, tools, system, model) -> AsyncIterator[StreamEvent]:
            """Provider that takes too long to respond."""
            await asyncio.sleep(10)  # Much longer than the timeout
            async for event in _stream_events(_make_text_response_events(text="too late")):
                yield event

        provider = MagicMock()
        provider.send_message = _slow_send_message
        registry = _make_mock_registry()
        config = _make_default_config(timeout_seconds=1)

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Hi"}],
        )

        assert isinstance(result, AgentResult)
        # Should indicate timeout — either success=False or error field set
        assert result.success is False or (
            result.error is not None and "timeout" in result.error.lower()
        )

    @pytest.mark.asyncio
    async def test_no_timeout_by_default(self):
        """Default config should not time out on a fast response."""
        events = _make_text_response_events(text="Quick response.")
        provider = _make_mock_provider([events])
        registry = _make_mock_registry()
        # Use default timeout (should be large enough for a fast mock)
        config = HarnessConfig(system_prompt="Test")

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Hi"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True
        # The error field should NOT mention timeout
        if result.error is not None:
            assert "timeout" not in result.error.lower()


# ---------------------------------------------------------------------------
# TestAgentLoopToolExecution
# ---------------------------------------------------------------------------


class TestAgentLoopToolExecution:
    """Verify tool execution, error handling, and multi-tool support."""

    @pytest.mark.asyncio
    async def test_tool_result_sent_to_provider(self):
        """After tool execution, the result is included in the next messages."""
        tool_events = _make_tool_use_events(
            tool_name="Bash",
            tool_use_id="tu_300",
            tool_input_json='{"command": "echo test"}',
        )
        text_events = _make_text_response_events(
            text="Command output was: test", stop_reason="end_turn"
        )

        call_args_list: list[list] = []

        async def _capturing_send_message(
            messages, tools, system, model
        ) -> AsyncIterator[StreamEvent]:
            """Provider that captures messages for later inspection."""
            call_args_list.append(messages)
            responses = [tool_events, text_events]
            idx = min(len(call_args_list) - 1, len(responses) - 1)
            async for event in _stream_events(responses[idx]):
                yield event

        provider = MagicMock()
        provider.send_message = _capturing_send_message
        registry = _make_mock_registry(results={"Bash": "test\n"})
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        await loop.run(
            messages=[{"role": "user", "content": "Run echo test"}],
        )

        # The second call should include the tool result in messages
        assert len(call_args_list) >= 2
        second_call_messages = call_args_list[1]
        # Look for a tool_result message in the conversation
        tool_result_found = any(
            msg.get("role") == "tool"
            or msg.get("type") == "tool_result"
            or (
                isinstance(msg.get("content"), list)
                and any(
                    isinstance(c, dict) and c.get("type") == "tool_result" for c in msg["content"]
                )
            )
            for msg in second_call_messages
            if isinstance(msg, dict)
        )
        assert tool_result_found, (
            "Tool result should be included in messages for the second provider call"
        )

    @pytest.mark.asyncio
    async def test_tool_error_handled_gracefully(self):
        """Tool raises exception -> error result sent to provider, no crash."""
        tool_events = _make_tool_use_events(
            tool_name="Bash",
            tool_use_id="tu_400",
        )
        text_events = _make_text_response_events(
            text="The tool failed, but I handled it.", stop_reason="end_turn"
        )
        provider = _make_mock_provider([tool_events, text_events])
        registry = _make_failing_registry(error_cls=RuntimeError, error_msg="command not found")
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Run something"}],
        )

        # The loop should handle the tool error gracefully
        assert isinstance(result, AgentResult)
        # It should still complete (the second provider call returns end_turn)
        assert result.num_turns is not None
        assert result.num_turns >= 1

    @pytest.mark.asyncio
    async def test_multiple_tools_in_one_turn(self):
        """Provider requests multiple tools -> all executed in one turn."""
        # Build events with two tool_use blocks in a single response
        multi_tool_events: list[StreamEvent] = [
            MessageStart(
                message_id="msg_multi",
                model="claude-opus-4-6",
                role="assistant",
            ),
            ToolUseStart(tool_use_id="tu_501", name="Bash"),
            ToolUseInputDelta(
                tool_use_id="tu_501",
                partial_json='{"command": "echo first"}',
            ),
            ToolUseEnd(tool_use_id="tu_501"),
            ToolUseStart(tool_use_id="tu_502", name="Read"),
            ToolUseInputDelta(
                tool_use_id="tu_502",
                partial_json='{"file_path": "/tmp/test.txt"}',
            ),
            ToolUseEnd(tool_use_id="tu_502"),
            MessageDelta(
                stop_reason="tool_use",
                usage={"input_tokens": 200, "output_tokens": 120},
            ),
            MessageEnd(
                usage={"input_tokens": 200, "output_tokens": 120},
            ),
        ]
        text_events = _make_text_response_events(
            text="Both tools executed.", stop_reason="end_turn"
        )
        provider = _make_mock_provider([multi_tool_events, text_events])
        registry = _make_mock_registry(results={"Bash": "first\n", "Read": "file contents"})
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Read and run"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True
        # Both tools should have been called
        assert registry.execute.call_count >= 2


# ---------------------------------------------------------------------------
# TestAgentLoopResult
# ---------------------------------------------------------------------------


class TestAgentLoopResult:
    """Verify AgentResult fields are populated correctly."""

    @pytest.mark.asyncio
    async def test_result_includes_cost(self):
        """AgentResult.cost_usd should be populated from CostTracker."""
        events = _make_text_response_events(
            text="response",
            input_tokens=1000,
            output_tokens=500,
        )
        provider = _make_mock_provider([events])
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Cost check"}],
        )

        assert isinstance(result, AgentResult)
        # cost_usd should be set and non-negative
        assert result.cost_usd is not None
        assert result.cost_usd >= 0.0

    @pytest.mark.asyncio
    async def test_result_includes_turns(self):
        """AgentResult.num_turns should count how many turns were taken."""
        tool_events = _make_tool_use_events()
        text_events = _make_text_response_events(stop_reason="end_turn")
        provider = _make_mock_provider([tool_events, text_events])
        registry = _make_mock_registry(results={"Bash": "ok"})
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Two turns"}],
        )

        assert result.num_turns is not None
        assert result.num_turns >= 2

    @pytest.mark.asyncio
    async def test_result_includes_duration(self):
        """AgentResult.duration_ms should reflect wall-clock time."""
        events = _make_text_response_events(text="fast")
        provider = _make_mock_provider([events])
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Duration check"}],
        )

        assert result.duration_ms is not None
        assert isinstance(result.duration_ms, int)
        # Duration should be non-negative and reasonable (< 30s for a mock)
        assert 0 <= result.duration_ms < 30_000

    @pytest.mark.asyncio
    async def test_result_success_on_normal_end(self):
        """Normal completion with end_turn -> success=True."""
        events = _make_text_response_events(text="All done.", stop_reason="end_turn")
        provider = _make_mock_provider([events])
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Finish"}],
        )

        assert result.success is True
        assert result.error is None

    @pytest.mark.asyncio
    async def test_result_failure_on_error(self):
        """Error during execution -> success=False."""

        async def _error_send_message(messages, tools, system, model) -> AsyncIterator[StreamEvent]:
            """Provider that raises an exception mid-stream."""
            raise ConnectionError("Provider connection lost")
            yield  # noqa: RET504 — make this an async generator

        provider = MagicMock()
        provider.send_message = _error_send_message
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            messages=[{"role": "user", "content": "Break"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is False
        assert result.error is not None
