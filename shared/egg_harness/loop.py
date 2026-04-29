"""Agent loop core for the egg harness.

Implements the main agentic loop that orchestrates provider calls, tool
execution, and turn management.  Handles turn limits, wall-clock timeouts,
circuit-breaking on repeated tool failures, cost tracking, and graceful
SIGTERM shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from egg_harness.compaction import CompactionLoopError, CompactionManager
from egg_harness.config import HarnessConfig, ProviderConfig, resolve_model
from egg_harness.cost import CostTracker
from egg_harness.events import EventBus
from egg_harness.providers.base import (
    MessageDelta,
    MessageEnd,
    MessageStart,
    Provider,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from egg_harness.result import AgentResult
from egg_harness.tools.registry import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

# Maximum consecutive tool failures before the circuit breaker trips.
_MAX_CONSECUTIVE_FAILURES: int = 3

# Grace period (seconds) for an in-flight tool to finish on SIGTERM.
_SHUTDOWN_TOOL_GRACE_SECONDS: int = 30


@dataclass
class _PendingToolCall:
    """Accumulator for a tool call being streamed incrementally."""

    id: str
    name: str
    input_json_parts: list[str] = field(default_factory=list)

    @property
    def input(self) -> dict[str, Any]:
        """Parse accumulated JSON fragments into a dict."""
        raw = "".join(self.input_json_parts)
        if not raw:
            return {}
        return json.loads(raw)  # type: ignore[no-any-return]


class AgentLoop:
    """Core agentic loop that drives provider calls and tool execution.

    The loop sends messages to the LLM provider, streams the response,
    executes any requested tools, appends results back into the
    conversation, and repeats until the model signals completion or a
    limit is reached.

    Args:
        provider: The LLM provider to use for message generation.
        tool_registry: Registry of available tools and their handlers.
        event_bus: Optional event bus for lifecycle notifications.
        config: Optional harness configuration.  When ``None``, a
            sensible default is constructed from the provider.
    """

    def __init__(
        self,
        provider: Provider,
        tool_registry: ToolRegistry,
        event_bus: EventBus | None = None,
        config: HarnessConfig | None = None,
    ) -> None:
        self._provider = provider
        self._tool_registry = tool_registry
        self._event_bus = event_bus or EventBus()
        self._config = config or HarnessConfig(
            provider=ProviderConfig(
                provider_type=provider.name,
                model="sonnet",
            ),
        )
        self._shutdown_requested = False
        self._original_sigterm_handler: Any = None

        # Build compaction manager from config.
        model = self._config.provider.model if self._config.provider else "sonnet"
        resolved = resolve_model(model)
        self._compaction = CompactionManager(
            model=resolved,
            threshold=self._config.compaction_threshold,
            keep_recent_tokens=self._config.keep_recent_tokens,
            event_bus=self._event_bus,
        )

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def run(
        self,
        prompt: str | None = None,
        *,
        system_prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> AgentResult:
        """Execute the agentic loop until completion or a limit is hit.

        Args:
            prompt: The user prompt to start the conversation.
            system_prompt: Optional system-level instructions.
            messages: Optional pre-built message list.  When provided,
                *prompt* is ignored for the initial message construction.

        Returns:
            An :class:`AgentResult` describing the outcome.
        """
        session_id = str(uuid.uuid4())
        start_time = time.monotonic()
        cost_tracker = CostTracker()

        # -- signal handling -------------------------------------------
        self._shutdown_requested = False
        self._install_sigterm_handler()

        try:
            return await self._run_loop(
                prompt=prompt,
                system_prompt=system_prompt,
                messages=messages,
                session_id=session_id,
                start_time=start_time,
                cost_tracker=cost_tracker,
            )
        finally:
            self._restore_sigterm_handler()

    # -----------------------------------------------------------------
    # Internal loop
    # -----------------------------------------------------------------

    async def _run_loop(
        self,
        *,
        prompt: str | None,
        system_prompt: str | None,
        messages: list[dict[str, Any]] | None,
        session_id: str,
        start_time: float,
        cost_tracker: CostTracker,
    ) -> AgentResult:
        """Inner loop body, separated to keep signal setup in run()."""
        config = self._config
        max_turns = config.max_turns
        timeout = config.timeout
        model = config.provider.model if config.provider else "sonnet"

        # Use system_prompt from run() arg, falling back to config.
        if system_prompt is None:
            system_prompt = getattr(config, "system_prompt", None)

        # Build initial conversation.
        if messages is not None:
            conversation: list[dict[str, Any]] = list(messages)
        elif prompt is not None:
            conversation = [{"role": "user", "content": prompt}]
        else:
            conversation = []

        response_text_parts: list[str] = []
        turn = 0
        consecutive_failures = 0
        total_tokens = 0

        while turn < max_turns:
            # -- timeout check -----------------------------------------
            elapsed = time.monotonic() - start_time
            remaining = timeout - elapsed
            if remaining <= 0:
                return self._build_result(
                    success=False,
                    response_text="".join(response_text_parts),
                    error="Timeout exceeded",
                    cost_tracker=cost_tracker,
                    turn=turn,
                    start_time=start_time,
                    session_id=session_id,
                    conversation=conversation,
                )

            # -- shutdown check ----------------------------------------
            if self._shutdown_requested:
                return self._build_result(
                    success=False,
                    response_text="".join(response_text_parts),
                    error="Shutdown requested",
                    cost_tracker=cost_tracker,
                    turn=turn,
                    start_time=start_time,
                    session_id=session_id,
                    conversation=conversation,
                )

            turn += 1

            # -- send message to provider ------------------------------
            tool_defs = self._tool_registry.get_definitions()
            stream = self._provider.send_message(
                messages=conversation,
                tools=tool_defs or None,
                system=system_prompt,
                model=model,
            )

            try:
                turn_text, tool_calls, stop_reason, usage = await asyncio.wait_for(
                    self._consume_stream(stream),
                    timeout=remaining,
                )
            except TimeoutError:
                return self._build_result(
                    success=False,
                    response_text="".join(response_text_parts),
                    error="Timeout exceeded",
                    cost_tracker=cost_tracker,
                    turn=turn,
                    start_time=start_time,
                    session_id=session_id,
                    conversation=conversation,
                )
            except Exception as exc:
                logger.exception("Provider stream error on turn %d", turn)
                return self._build_result(
                    success=False,
                    response_text="".join(response_text_parts),
                    error=f"Provider error: {exc}",
                    cost_tracker=cost_tracker,
                    turn=turn,
                    start_time=start_time,
                    session_id=session_id,
                    conversation=conversation,
                )

            # Accumulate response text across turns.
            if turn_text:
                response_text_parts.append(turn_text)

            # -- track cost --------------------------------------------
            if usage:
                cost_tracker.add_usage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                    cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
                    model=model,
                )
                total_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

            # -- emit turn complete ------------------------------------
            self._event_bus.emit_turn_complete(turn, usage or {})

            # -- context compaction ------------------------------------
            self._compaction.current_turn = turn
            if self._compaction.should_compact(total_tokens):
                try:
                    conversation, _summary = self._compaction.compact(conversation, system_prompt)
                except CompactionLoopError:
                    logger.warning(
                        "Compaction loop protection triggered on turn %d",
                        turn,
                    )

            # -- build assistant message content blocks ----------------
            assistant_content: list[dict[str, Any]] = []
            if turn_text:
                assistant_content.append({"type": "text", "text": turn_text})
            for tc in tool_calls:
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    }
                )

            # Append assistant message to conversation.
            if assistant_content:
                conversation.append({"role": "assistant", "content": assistant_content})

            # -- decide what to do next --------------------------------
            if not tool_calls:
                # No tool calls: the model is done (or hit end_turn /
                # stop_sequence).
                if stop_reason == "max_tokens":
                    # Truncated response -- continue to get more.
                    continue
                # Normal completion.
                return self._build_result(
                    success=True,
                    response_text="".join(response_text_parts),
                    error=None,
                    cost_tracker=cost_tracker,
                    turn=turn,
                    start_time=start_time,
                    session_id=session_id,
                    conversation=conversation,
                )

            # -- execute tool calls sequentially -----------------------
            tool_result_blocks: list[dict[str, Any]] = []
            turn_had_failure = False

            for tc in tool_calls:
                # Shutdown check before each tool execution.
                if self._shutdown_requested:
                    return self._build_result(
                        success=False,
                        response_text="".join(response_text_parts),
                        error="Shutdown requested",
                        cost_tracker=cost_tracker,
                        turn=turn,
                        start_time=start_time,
                        session_id=session_id,
                        conversation=conversation,
                    )

                tool_input = tc.input
                self._event_bus.emit_tool_call(tc.name, tool_input)

                # Execute with a grace period for shutdown.
                try:
                    result = await asyncio.wait_for(
                        self._execute_tool(tc.name, tool_input),
                        timeout=max(
                            _SHUTDOWN_TOOL_GRACE_SECONDS,
                            timeout - (time.monotonic() - start_time),
                        ),
                    )
                except TimeoutError:
                    result = ToolResult(
                        output="Tool execution timed out",
                        is_error=True,
                    )

                self._event_bus.emit_tool_result(tc.name, result.output)

                if result.is_error:
                    turn_had_failure = True

                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result.output,
                    }
                )

            # Append tool results as a user message.
            conversation.append({"role": "user", "content": tool_result_blocks})

            # -- circuit breaker for consecutive failures --------------
            if turn_had_failure:
                consecutive_failures += 1
            else:
                consecutive_failures = 0

            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                return self._build_result(
                    success=False,
                    response_text="".join(response_text_parts),
                    error=(
                        f"Circuit breaker tripped: "
                        f"{_MAX_CONSECUTIVE_FAILURES} consecutive "
                        f"tool failures"
                    ),
                    cost_tracker=cost_tracker,
                    turn=turn,
                    start_time=start_time,
                    session_id=session_id,
                    conversation=conversation,
                )

        # Exhausted max_turns.
        return self._build_result(
            success=False,
            response_text="".join(response_text_parts),
            error="Max turns exceeded",
            cost_tracker=cost_tracker,
            turn=turn,
            start_time=start_time,
            session_id=session_id,
            conversation=conversation,
        )

    # -----------------------------------------------------------------
    # Tool execution helper
    # -----------------------------------------------------------------

    async def _execute_tool(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        """Execute a tool via the registry."""
        try:
            result = await self._tool_registry.execute(name, tool_input)
        except Exception as exc:
            logger.exception("Tool %s raised an exception", name)
            return ToolResult(
                output=f"Tool execution error: {exc}",
                is_error=True,
            )
        return result

    # -----------------------------------------------------------------
    # Stream consumption
    # -----------------------------------------------------------------

    async def _consume_stream(
        self,
        stream: Any,
    ) -> tuple[
        str,
        list[_PendingToolCall],
        str | None,
        dict[str, int] | None,
    ]:
        """Consume all events from a single provider response stream.

        Returns:
            A tuple of ``(response_text, tool_calls, stop_reason, usage)``.
        """
        text_parts: list[str] = []
        tool_calls: list[_PendingToolCall] = []
        current_tool: _PendingToolCall | None = None
        stop_reason: str | None = None
        usage: dict[str, int] | None = None

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
                self._event_bus.emit_output(event.text)

            elif isinstance(event, ToolUseStart):
                current_tool = _PendingToolCall(id=event.id, name=event.name)
                tool_calls.append(current_tool)

            elif isinstance(event, ToolUseInputDelta):
                if current_tool is not None:
                    current_tool.input_json_parts.append(event.partial_json)

            elif isinstance(event, ToolUseEnd):
                # ToolUseEnd may carry the final parsed input; when
                # present, replace any streamed fragments with the
                # authoritative value.  When ``input`` is None the
                # end event is purely a delimiter and we keep the
                # fragments accumulated from ToolUseInputDelta events.
                if event.input is not None:
                    for tc in tool_calls:
                        if tc.id == event.id:
                            tc.input_json_parts = [json.dumps(event.input)]
                            break
                current_tool = None

            elif isinstance(event, MessageDelta):
                if event.stop_reason is not None:
                    stop_reason = event.stop_reason
                if event.usage:
                    usage = event.usage

            elif isinstance(event, ThinkingDelta):
                # Thinking tokens are not surfaced to the user.
                pass

            elif isinstance(event, (MessageStart, MessageEnd)):
                pass

        return "".join(text_parts), tool_calls, stop_reason, usage

    # -----------------------------------------------------------------
    # Result builder
    # -----------------------------------------------------------------

    @staticmethod
    def _build_result(
        *,
        success: bool,
        response_text: str,
        error: str | None,
        cost_tracker: CostTracker,
        turn: int,
        start_time: float,
        session_id: str,
        conversation: list[dict[str, Any]] | None = None,
        compaction_count: int | None = None,
    ) -> AgentResult:
        """Construct an :class:`AgentResult` from loop state."""
        duration_ms = int((time.monotonic() - start_time) * 1000)
        return AgentResult(
            success=success,
            stdout=response_text,
            stderr=error or "",
            returncode=0 if success else 1,
            error=error,
            cost_usd=cost_tracker.total_cost_usd,
            num_turns=turn,
            duration_ms=duration_ms,
            session_id=session_id,
            compaction_count=compaction_count,
            messages=conversation,
        )

    # -----------------------------------------------------------------
    # Signal handling
    # -----------------------------------------------------------------

    def _install_sigterm_handler(self) -> None:
        """Register a SIGTERM handler that requests graceful shutdown."""
        try:
            self._original_sigterm_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._handle_sigterm)
        except OSError, ValueError:
            # signal.signal() can only be called from the main thread;
            # if we are not on the main thread, skip handler
            # installation silently.
            self._original_sigterm_handler = None

    def _restore_sigterm_handler(self) -> None:
        """Restore the SIGTERM handler that was active before run()."""
        if self._original_sigterm_handler is None:
            return
        try:
            signal.signal(signal.SIGTERM, self._original_sigterm_handler)
        except OSError, ValueError:
            pass
        finally:
            self._original_sigterm_handler = None

    def _handle_sigterm(
        self,
        signum: int,
        frame: Any,  # noqa: ARG002
    ) -> None:
        """SIGTERM handler: request graceful shutdown."""
        logger.info("SIGTERM received -- requesting graceful shutdown")
        self._shutdown_requested = True
