"""Tests for egg_harness.loop — AgentLoop core execution engine.

Covers the agent loop lifecycle: messages -> provider -> stream -> tool
execution -> back to provider, plus stop reasons, timeouts, turn counting,
and result metadata.
"""

from __future__ import annotations

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.loop")

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

from egg_harness.config import HarnessConfig, ProviderConfig
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
from egg_harness.tools.registry import ToolRegistry, ToolResult

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
        MessageEnd(),
    ]


def _make_tool_use_events(
    tool_name: str = "Bash",
    tool_use_id: str = "tu_001",
    tool_input: dict[str, Any] | None = None,
    tool_input_json: str = '{"command": "echo hi"}',
    *,
    model: str = "claude-opus-4-6",
    message_id: str = "msg_002",
    input_tokens: int = 150,
    output_tokens: int = 80,
) -> list[StreamEvent]:
    """Build a complete sequence of StreamEvents for a tool_use response."""
    if tool_input is None:
        import json

        tool_input = json.loads(tool_input_json)
    return [
        MessageStart(message_id=message_id, model=model, role="assistant"),
        ToolUseStart(id=tool_use_id, name=tool_name),
        ToolUseInputDelta(partial_json=tool_input_json),
        ToolUseEnd(id=tool_use_id, name=tool_name, input=tool_input),
        MessageDelta(
            stop_reason="tool_use",
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        ),
        MessageEnd(),
    ]


def _make_mock_provider(
    responses: list[list[StreamEvent]],
) -> MagicMock:
    """Create a mock Provider whose send_message yields successive responses."""
    provider = MagicMock()
    provider.name = "mock"
    call_count = 0

    async def _send_message(
        *,
        messages: list,
        tools: list | None = None,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 16384,
        extra_headers: dict | None = None,
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
    """Create a mock ToolRegistry that returns canned results."""
    from egg_harness.tools.registry import ToolResult

    registry = MagicMock(spec=ToolRegistry)

    async def _execute(tool_name: str, tool_input: dict[str, Any]) -> ToolResult:
        if results and tool_name in results:
            return ToolResult(output=results[tool_name], is_error=False)
        return ToolResult(output=f"Unknown tool: {tool_name}", is_error=True)

    registry.execute = _execute
    registry.get_definitions.return_value = []
    return registry


def _make_failing_registry(
    error_cls: type[Exception] = RuntimeError,
    error_msg: str = "tool exploded",
) -> MagicMock:
    """Create a mock ToolRegistry whose execute returns an error ToolResult.

    Mirrors the real ToolRegistry.execute() which catches handler exceptions
    and returns ToolResult(is_error=True) rather than propagating them.
    """
    registry = MagicMock(spec=ToolRegistry)

    async def _failing_execute(tool_name: str, tool_input: dict[str, Any]) -> ToolResult:
        return ToolResult(output=f"{error_cls.__name__}: {error_msg}", is_error=True)

    registry.execute = _failing_execute
    registry.get_definitions.return_value = []
    return registry


def _make_default_config(**overrides) -> HarnessConfig:
    """Build a HarnessConfig with sensible test defaults."""
    provider = overrides.pop("provider", None) or ProviderConfig(
        provider_type="anthropic", model="claude-opus-4-6"
    )
    defaults: dict[str, Any] = {
        "provider": provider,
        "max_turns": 100,
        "timeout": 30,
    }
    defaults.update(overrides)
    return HarnessConfig(**defaults)


# ---------------------------------------------------------------------------
# TestAgentLoopBasic
# ---------------------------------------------------------------------------


class TestAgentLoopBasic:
    """Core loop: messages -> provider -> stream -> tool execution -> result."""

    @pytest.mark.anyio
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
            "Hi",
            messages=[{"role": "user", "content": "Hi"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True
        assert result.num_turns is not None
        assert result.num_turns >= 1

    @pytest.mark.anyio
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
            "Run echo hi",
            messages=[{"role": "user", "content": "Run echo hi"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True
        assert result.num_turns is not None
        assert result.num_turns >= 2

    @pytest.mark.anyio
    async def test_max_turns_stops_loop(self):
        """After max_turns, loop stops even if provider wants more tool_use."""
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
            "Keep going",
            messages=[{"role": "user", "content": "Keep going"}],
        )

        assert isinstance(result, AgentResult)
        assert result.num_turns is not None
        assert result.num_turns <= 3

    @pytest.mark.anyio
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

        try:
            result = await loop.run("", messages=[])
            assert isinstance(result, AgentResult)
        except ValueError, TypeError:
            pass


# ---------------------------------------------------------------------------
# TestAgentLoopStopReasons
# ---------------------------------------------------------------------------


class TestAgentLoopStopReasons:
    """Verify loop behaviour for each stop_reason value."""

    @pytest.mark.anyio
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
            "Bye",
            messages=[{"role": "user", "content": "Bye"}],
        )

        assert result.success is True
        assert result.num_turns is not None
        assert result.num_turns == 1

    @pytest.mark.anyio
    async def test_max_tokens_continues(self):
        """stop_reason='max_tokens' -> loop may continue or stop."""
        max_tokens_events = _make_text_response_events(
            text="partial output...", stop_reason="max_tokens"
        )
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
            "Tell me a story",
            messages=[{"role": "user", "content": "Tell me a story"}],
        )

        assert isinstance(result, AgentResult)
        assert result.num_turns is not None
        assert result.num_turns >= 1

    @pytest.mark.anyio
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
            "Run a command",
            messages=[{"role": "user", "content": "Run a command"}],
        )

        assert result.success is True
        assert result.num_turns is not None
        assert result.num_turns >= 2


# ---------------------------------------------------------------------------
# TestAgentLoopTimeout
# ---------------------------------------------------------------------------


class TestAgentLoopTimeout:
    """Verify wall-clock timeout enforcement."""

    @pytest.mark.anyio
    async def test_wall_clock_timeout(self):
        """Loop should stop after timeout and return a timeout result."""

        async def _slow_send_message(
            *,
            messages,
            tools=None,
            system=None,
            model=None,
            max_tokens=16384,
            extra_headers=None,
        ) -> AsyncIterator[StreamEvent]:
            await asyncio.sleep(10)
            async for event in _stream_events(_make_text_response_events(text="too late")):
                yield event

        provider = MagicMock()
        provider.name = "mock"
        provider.send_message = _slow_send_message
        registry = _make_mock_registry()
        config = _make_default_config(timeout=1)

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            "Hi",
            messages=[{"role": "user", "content": "Hi"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is False or (
            result.error is not None and "timeout" in result.error.lower()
        )

    @pytest.mark.anyio
    async def test_no_timeout_by_default(self):
        """Default config should not time out on a fast response."""
        events = _make_text_response_events(text="Quick response.")
        provider = _make_mock_provider([events])
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            "Hi",
            messages=[{"role": "user", "content": "Hi"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True
        if result.error is not None:
            assert "timeout" not in result.error.lower()


# ---------------------------------------------------------------------------
# TestAgentLoopToolExecution
# ---------------------------------------------------------------------------


class TestAgentLoopToolExecution:
    """Verify tool execution, error handling, and multi-tool support."""

    @pytest.mark.anyio
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
            *,
            messages,
            tools=None,
            system=None,
            model=None,
            max_tokens=16384,
            extra_headers=None,
        ) -> AsyncIterator[StreamEvent]:
            call_args_list.append(messages)
            responses = [tool_events, text_events]
            idx = min(len(call_args_list) - 1, len(responses) - 1)
            async for event in _stream_events(responses[idx]):
                yield event

        provider = MagicMock()
        provider.name = "mock"
        provider.send_message = _capturing_send_message
        registry = _make_mock_registry(results={"Bash": "test\n"})
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        await loop.run(
            "Run echo test",
            messages=[{"role": "user", "content": "Run echo test"}],
        )

        assert len(call_args_list) >= 2
        second_call_messages = call_args_list[1]
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
        assert tool_result_found

    @pytest.mark.anyio
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
            "Run something",
            messages=[{"role": "user", "content": "Run something"}],
        )

        assert isinstance(result, AgentResult)
        assert result.num_turns is not None
        assert result.num_turns >= 1

    @pytest.mark.anyio
    async def test_multiple_tools_in_one_turn(self):
        """Provider requests multiple tools -> all executed in one turn."""
        multi_tool_events: list[StreamEvent] = [
            MessageStart(
                message_id="msg_multi",
                model="claude-opus-4-6",
                role="assistant",
            ),
            ToolUseStart(id="tu_501", name="Bash"),
            ToolUseInputDelta(partial_json='{"command": "echo first"}'),
            ToolUseEnd(id="tu_501", name="Bash", input={"command": "echo first"}),
            ToolUseStart(id="tu_502", name="Read"),
            ToolUseInputDelta(partial_json='{"file_path": "/tmp/test.txt"}'),
            ToolUseEnd(id="tu_502", name="Read", input={"file_path": "/tmp/test.txt"}),
            MessageDelta(
                stop_reason="tool_use",
                usage={"input_tokens": 200, "output_tokens": 120},
            ),
            MessageEnd(),
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
            "Read and run",
            messages=[{"role": "user", "content": "Read and run"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is True


# ---------------------------------------------------------------------------
# TestAgentLoopResult
# ---------------------------------------------------------------------------


class TestAgentLoopResult:
    """Verify AgentResult fields are populated correctly."""

    @pytest.mark.anyio
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
            "Cost check",
            messages=[{"role": "user", "content": "Cost check"}],
        )

        assert isinstance(result, AgentResult)
        assert result.cost_usd is not None
        assert result.cost_usd >= 0.0

    @pytest.mark.anyio
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
            "Two turns",
            messages=[{"role": "user", "content": "Two turns"}],
        )

        assert result.num_turns is not None
        assert result.num_turns >= 2

    @pytest.mark.anyio
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
            "Duration check",
            messages=[{"role": "user", "content": "Duration check"}],
        )

        assert result.duration_ms is not None
        assert isinstance(result.duration_ms, int)
        assert 0 <= result.duration_ms < 30_000

    @pytest.mark.anyio
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
            "Finish",
            messages=[{"role": "user", "content": "Finish"}],
        )

        assert result.success is True
        assert result.error is None

    @pytest.mark.anyio
    async def test_result_failure_on_error(self):
        """Error during execution -> loop catches and returns AgentResult(success=False)."""

        async def _error_send_message(
            *,
            messages,
            tools=None,
            system=None,
            model=None,
            max_tokens=16384,
            extra_headers=None,
        ) -> AsyncIterator[StreamEvent]:
            raise ConnectionError("Provider connection lost")
            yield  # noqa: RET504

        provider = MagicMock()
        provider.name = "mock"
        provider.send_message = _error_send_message
        registry = _make_mock_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        # _run_loop has a broad `except Exception` handler (loop.py:244) that
        # catches all provider errors and returns a structured result.
        result = await loop.run(
            "Break",
            messages=[{"role": "user", "content": "Break"}],
        )
        assert isinstance(result, AgentResult)
        assert result.success is False
        assert result.error is not None
        assert "Provider error" in result.error


# ---------------------------------------------------------------------------
# SIGTERM graceful shutdown tests
# ---------------------------------------------------------------------------


class TestAgentLoopSigterm:
    """Tests for SIGTERM signal handling in AgentLoop."""

    @pytest.mark.anyio
    async def test_shutdown_flag_causes_exit(self):
        """When _shutdown_requested is True, the loop exits on the next check."""
        # First response is a tool_use so the loop doesn't end on stop_reason.
        # The tool execution sets the shutdown flag, and the shutdown check
        # at the top of the next iteration catches it.
        tool_events = _make_tool_use_events(
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            tool_input_json='{"command": "echo hi"}',
        )
        followup = _make_text_response_events("continuing")
        provider = _make_mock_provider([tool_events, followup])
        registry = _make_mock_registry(results={"Bash": "output"})
        config = _make_default_config()
        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )

        original_execute = registry.execute

        async def _execute_and_set_flag(name, inp):
            result = await original_execute(name, inp)
            loop._shutdown_requested = True
            return result

        registry.execute = _execute_and_set_flag

        result = await loop.run("test")

        assert isinstance(result, AgentResult)
        assert result.success is False
        assert "Shutdown" in (result.error or "")

    @pytest.mark.anyio
    async def test_shutdown_during_tool_execution_stops_loop(self):
        """If shutdown is requested between tool calls, the loop exits."""
        # Provider requests a tool, loop executes it, then checks shutdown.
        tool_events = _make_tool_use_events(
            tool_name="Bash",
            tool_input={"command": "echo test"},
            tool_input_json='{"command": "echo test"}',
        )
        followup_events = _make_text_response_events("continuing")

        provider = _make_mock_provider([tool_events, followup_events])
        registry = _make_mock_registry(results={"Bash": "output"})
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )

        # Set shutdown after the loop starts (simulating SIGTERM during tool
        # execution). We do this by wrapping execute to set the flag.
        original_execute = registry.execute

        async def _execute_and_shutdown(name, inp):
            result = await original_execute(name, inp)
            loop._shutdown_requested = True
            return result

        registry.execute = _execute_and_shutdown

        result = await loop.run(
            "test",
            messages=[{"role": "user", "content": "run a command"}],
        )
        assert isinstance(result, AgentResult)
        # The loop should exit due to the shutdown flag set during tool execution.
        assert result.success is False
        assert "Shutdown" in (result.error or "")

    @pytest.mark.anyio
    async def test_sigterm_handler_restored_after_run(self):
        """After run() completes, the original SIGTERM handler is restored."""
        provider = _make_mock_provider(
            [
                _make_text_response_events("done"),
            ]
        )
        registry = _make_mock_registry()
        config = _make_default_config()
        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )

        import signal
        import threading

        # Only test handler restoration on main thread
        if threading.current_thread() is not threading.main_thread():
            pytest.skip("Signal tests require main thread")

        original = signal.getsignal(signal.SIGTERM)
        await loop.run("test")
        after = signal.getsignal(signal.SIGTERM)
        # The handler should be restored to whatever it was before run()
        assert after == original

    @pytest.mark.anyio
    async def test_handle_sigterm_sets_flag(self):
        """The _handle_sigterm method sets _shutdown_requested."""
        provider = _make_mock_provider([_make_text_response_events("hi")])
        registry = _make_mock_registry()
        config = _make_default_config()
        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        assert loop._shutdown_requested is False
        loop._handle_sigterm(15, None)
        assert loop._shutdown_requested is True


# ---------------------------------------------------------------------------
# Circuit breaker tests
# ---------------------------------------------------------------------------


class TestAgentLoopCircuitBreaker:
    """Tests for the consecutive tool failure circuit breaker."""

    @pytest.mark.anyio
    async def test_three_consecutive_failures_trips(self):
        """3 consecutive tool failures should trip the circuit breaker."""
        # Provider keeps requesting the same tool each turn
        tool_events = _make_tool_use_events(
            tool_name="Bash",
            tool_input={"command": "fail"},
            tool_input_json='{"command": "fail"}',
        )
        provider = _make_mock_provider([tool_events] * 5)
        # Registry always returns is_error=True
        registry = _make_failing_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )

        result = await loop.run(
            "test",
            messages=[{"role": "user", "content": "do stuff"}],
        )

        assert isinstance(result, AgentResult)
        assert result.success is False
        assert "Circuit breaker" in (result.error or "")
        assert "3" in (result.error or "")

    @pytest.mark.anyio
    async def test_success_resets_failure_counter(self):
        """A successful tool call should reset the consecutive failure counter."""
        # Turn 1: tool call fails
        tool_events_fail = _make_tool_use_events(
            tool_name="Bash",
            tool_input={"command": "fail"},
            tool_input_json='{"command": "fail"}',
        )
        # Turn 2: tool call succeeds (text response after)
        tool_events_ok = _make_tool_use_events(
            tool_name="Read",
            tool_input={"file_path": "/tmp/x"},
            tool_input_json='{"file_path": "/tmp/x"}',
        )
        final = _make_text_response_events("done")

        provider = _make_mock_provider(
            [
                tool_events_fail,  # turn 1: fail
                tool_events_ok,  # turn 2: succeed
                tool_events_fail,  # turn 3: fail
                tool_events_fail,  # turn 4: fail
                final,  # turn 5: done
            ]
        )

        # Registry: Bash fails, Read succeeds
        call_count = {"total": 0}

        async def _selective_execute(name, inp):
            call_count["total"] += 1
            if name == "Bash":
                return ToolResult(output="command not found", is_error=True)
            return ToolResult(output="file contents", is_error=False)

        registry = MagicMock(spec=ToolRegistry)
        registry.execute = _selective_execute
        registry.get_definitions.return_value = []

        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )

        result = await loop.run(
            "test",
            messages=[{"role": "user", "content": "do stuff"}],
        )

        # Should NOT trip the circuit breaker because the success in turn 2
        # resets the counter. After reset: fail, fail = 2 consecutive (< 3).
        assert isinstance(result, AgentResult)
        assert result.error is None, f"Unexpected error: {result.error}"


# ---------------------------------------------------------------------------
# TestAgentLoopConversationPreservation
# ---------------------------------------------------------------------------


class TestAgentLoopConversationPreservation:
    """Verify that result.messages preserves conversation on all exit paths.

    Regression tests for N-NEW-6: interactive mode lost intermediate tool
    messages because error paths did not pass ``conversation`` to
    ``_build_result``.
    """

    @pytest.mark.anyio
    async def test_timeout_preserves_conversation(self):
        """When the loop times out during streaming, result.messages should
        contain the conversation history (not be None)."""
        # First call succeeds (tool_use), tool executes, second call
        # is slow and triggers the timeout.
        tool_events = _make_tool_use_events()
        call_count = 0

        async def _slow_on_second_call(
            *,
            messages: list,
            tools: list | None = None,
            system: str | None = None,
            model: str | None = None,
            max_tokens: int = 16384,
            extra_headers: dict | None = None,
        ) -> AsyncIterator[StreamEvent]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                async for event in _stream_events(tool_events):
                    yield event
            else:
                await asyncio.sleep(10)
                yield TextDelta(text="too late")

        provider = MagicMock()
        provider.name = "mock"
        provider.send_message = _slow_on_second_call
        registry = _make_mock_registry(results={"Bash": "hi"})
        config = _make_default_config(timeout=1)

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            "Go",
            messages=[{"role": "user", "content": "Go"}],
        )
        assert result.success is False
        assert result.messages is not None, (
            "Timeout path must preserve conversation in result.messages"
        )
        # Should have: initial user, assistant (tool_use), user (tool_result)
        assert len(result.messages) >= 3

    @pytest.mark.anyio
    async def test_circuit_breaker_preserves_conversation(self):
        """When the circuit breaker trips, result.messages should contain
        the full conversation including tool_use and tool_result messages."""
        # 3 consecutive failing tool calls trips the circuit breaker.
        tool_events = _make_tool_use_events()
        provider = _make_mock_provider([tool_events])
        registry = _make_failing_registry()
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            "Go",
            messages=[{"role": "user", "content": "Go"}],
        )
        assert result.success is False
        assert "Circuit breaker" in (result.error or "")
        assert result.messages is not None, (
            "Circuit breaker path must preserve conversation in result.messages"
        )
        # Verify tool_use and tool_result messages are present.
        roles = [m["role"] for m in result.messages]
        assert "assistant" in roles, "Should have assistant messages with tool_use"
        assert roles.count("user") >= 2, "Should have user messages with tool_result blocks"

    @pytest.mark.anyio
    async def test_provider_error_preserves_conversation(self):
        """When the provider raises an exception mid-conversation,
        result.messages should preserve whatever was accumulated."""
        # First response succeeds (tool_use), second raises.
        tool_events = _make_tool_use_events()
        call_count = 0

        async def _sometimes_failing_send(
            *,
            messages: list,
            tools: list | None = None,
            system: str | None = None,
            model: str | None = None,
            max_tokens: int = 16384,
            extra_headers: dict | None = None,
        ) -> AsyncIterator[StreamEvent]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                async for event in _stream_events(tool_events):
                    yield event
            else:
                raise ConnectionError("Connection lost")
                yield  # noqa: RET504

        provider = MagicMock()
        provider.name = "mock"
        provider.send_message = _sometimes_failing_send
        registry = _make_mock_registry(results={"Bash": "done"})
        config = _make_default_config()

        loop = AgentLoop(
            provider=provider,
            tool_registry=registry,
            config=config,
        )
        result = await loop.run(
            "Go",
            messages=[{"role": "user", "content": "Go"}],
        )
        assert result.success is False
        assert result.messages is not None, (
            "Provider error path must preserve conversation in result.messages"
        )
        # Should have at least: initial user, assistant (tool_use), user (tool_result)
        assert len(result.messages) >= 3
