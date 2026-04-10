"""Core agent loop for the egg harness.

Implements the agentic loop:
    prompt -> API call -> parse response -> execute tools -> feed results back -> repeat

Supports:
- Max turns limit
- Wall-clock timeout (default 2 hours)
- Streaming output
- Graceful stop on SIGTERM
- Context tracking and compaction
- Circuit breaker (3 consecutive failures -> exit)
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from typing import Any

from egg_harness.compaction import (
    COMPACTION_PROMPT,
    CompactionState,
    build_compaction_messages,
    estimate_message_tokens,
)
from egg_harness.config import HarnessConfig
from egg_harness.cost import UsageAccumulator
from egg_harness.events import (
    CompactionEvent,
    ErrorEvent,
    EventBus,
    TextOutputEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnCompleteEvent,
)
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
from egg_harness.session import Session
from egg_harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Circuit breaker: abort after this many consecutive API errors
MAX_CONSECUTIVE_ERRORS = 3

# Maximum tool result size (chars) before truncation
MAX_TOOL_RESULT_SIZE = 100_000


class AgentLoop:
    """Core agentic loop."""

    def __init__(
        self,
        *,
        provider: Provider,
        tool_registry: ToolRegistry,
        config: HarnessConfig,
        event_bus: EventBus | None = None,
        system_prompt: str | None = None,
        session: Session | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tool_registry
        self._config = config
        self._events = event_bus or EventBus()
        self._system_prompt = system_prompt
        self._session = session or Session()
        self._usage = UsageAccumulator()
        self._compaction = CompactionState()
        self._stop_requested = False
        self._turn = 0
        self._consecutive_errors = 0
        self._stdout_parts: list[str] = []

    async def run(
        self,
        prompt: str,
        *,
        disallowed_tools: list[str] | None = None,
    ) -> AgentResult:
        """Run the full agent loop until completion.

        Args:
            prompt: Initial user prompt.
            disallowed_tools: Tool names to exclude.

        Returns:
            AgentResult with response and metadata.
        """
        start_time = time.time()

        # Setup SIGTERM handler for graceful shutdown
        original_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, self._handle_sigterm)

        try:
            # Initialize messages with the prompt
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": prompt},
            ]
            self._session.add_message(messages[0])

            # Get tool definitions
            tool_defs = self._tools.get_definitions(exclude=disallowed_tools)

            max_turns = self._config.max_turns or 200
            context_window = self._config.provider.get_context_window()

            while self._turn < max_turns and not self._stop_requested:
                self._turn += 1

                # Check for compaction need before API call
                estimated_tokens = estimate_message_tokens(messages)
                if self._system_prompt:
                    estimated_tokens += len(self._system_prompt) // 4

                if self._compaction.should_compact(
                    current_tokens=estimated_tokens,
                    max_tokens=context_window,
                    threshold=self._config.compaction_threshold,
                    current_turn=self._turn,
                    max_compactions_per_window=self._config.max_compactions_per_n_turns,
                    window_turns=self._config.compaction_window_turns,
                ):
                    # Check for compaction loop
                    if self._compaction.is_loop_detected(
                        self._turn,
                        self._config.max_compactions_per_n_turns,
                        self._config.compaction_window_turns,
                    ):
                        return AgentResult(
                            success=False,
                            stdout="\n".join(self._stdout_parts),
                            stderr="Compaction loop detected",
                            returncode=1,
                            error="Context fills up immediately after compaction. Aborting.",
                            cost_usd=self._usage.total_cost_usd,
                            num_turns=self._turn,
                            duration_ms=int((time.time() - start_time) * 1000),
                            session_id=self._session.session_id,
                            compaction_count=self._compaction.compaction_count,
                        )

                    messages = await self._compact(messages, context_window)

                # Make API call
                try:
                    assistant_content, stop_reason, turn_usage = await self._api_turn(
                        messages, tool_defs
                    )
                    self._consecutive_errors = 0
                except Exception as e:
                    self._consecutive_errors += 1
                    logger.error(f"API error (attempt {self._consecutive_errors}): {e}")
                    await self._events.emit(
                        ErrorEvent(
                            error=str(e),
                            recoverable=self._consecutive_errors < MAX_CONSECUTIVE_ERRORS,
                        )
                    )

                    if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        return AgentResult(
                            success=False,
                            stdout="\n".join(self._stdout_parts),
                            stderr=str(e),
                            returncode=1,
                            error=f"API failed {MAX_CONSECUTIVE_ERRORS} times consecutively: {e}",
                            cost_usd=self._usage.total_cost_usd,
                            num_turns=self._turn,
                            duration_ms=int((time.time() - start_time) * 1000),
                            session_id=self._session.session_id,
                            compaction_count=self._compaction.compaction_count,
                        )

                    # Exponential backoff
                    await asyncio.sleep(min(2**self._consecutive_errors, 30))
                    self._turn -= 1  # Don't count failed turns
                    continue

                # Record usage
                if turn_usage:
                    self._usage.add_turn(
                        model=self._config.provider.resolve_model(),
                        input_tokens=turn_usage.get("input_tokens", 0),
                        output_tokens=turn_usage.get("output_tokens", 0),
                        cache_read_tokens=turn_usage.get("cache_read_input_tokens", 0),
                        cache_write_tokens=turn_usage.get("cache_creation_input_tokens", 0),
                    )

                await self._events.emit(
                    TurnCompleteEvent(
                        turn_number=self._turn,
                        input_tokens=turn_usage.get("input_tokens", 0) if turn_usage else 0,
                        output_tokens=turn_usage.get("output_tokens", 0) if turn_usage else 0,
                    )
                )

                # Add assistant message to history
                assistant_msg = {"role": "assistant", "content": assistant_content}
                messages.append(assistant_msg)
                self._session.add_message(assistant_msg)

                # Check stop reason
                if stop_reason == "end_turn" or stop_reason == "stop":
                    # Model decided to stop — we're done
                    break

                if stop_reason == "max_tokens":
                    logger.warning("Model hit max_tokens limit")
                    break

                if stop_reason != "tool_use":
                    logger.warning(f"Unexpected stop_reason: {stop_reason}")
                    break

                # Execute tool calls
                tool_results = await self._execute_tools(assistant_content)
                if tool_results:
                    user_msg: dict[str, Any] = {
                        "role": "user",
                        "content": tool_results,
                    }
                    messages.append(user_msg)
                    self._session.add_message(user_msg)

            duration_ms = int((time.time() - start_time) * 1000)

            return AgentResult(
                success=True,
                stdout="\n".join(self._stdout_parts),
                stderr="",
                returncode=0,
                metadata={"model": self._config.provider.resolve_model()},
                cost_usd=self._usage.total_cost_usd,
                num_turns=self._turn,
                duration_ms=duration_ms,
                session_id=self._session.session_id,
                compaction_count=self._compaction.compaction_count,
            )

        finally:
            signal.signal(signal.SIGTERM, original_handler)

    async def run_turn(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Run a single interactive turn (may involve multiple API calls if tools are used).

        Used by the interactive mode.

        Args:
            messages: Current conversation messages.

        Returns:
            Updated messages list with assistant response and any tool results.
        """
        tool_defs = self._tools.get_definitions()
        max_tool_rounds = 50  # Safety limit for tool use within a single turn

        for _ in range(max_tool_rounds):
            assistant_content, stop_reason, turn_usage = await self._api_turn(messages, tool_defs)

            if turn_usage:
                self._usage.add_turn(
                    model=self._config.provider.resolve_model(),
                    input_tokens=turn_usage.get("input_tokens", 0),
                    output_tokens=turn_usage.get("output_tokens", 0),
                )

            assistant_msg = {"role": "assistant", "content": assistant_content}
            messages.append(assistant_msg)

            if stop_reason != "tool_use":
                break

            tool_results = await self._execute_tools(assistant_content)
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        return messages

    async def _api_turn(
        self,
        messages: list[dict[str, Any]],
        tool_defs: list[Any],
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, int] | None]:
        """Make one API call and return parsed content blocks, stop reason, and usage.

        Returns:
            (content_blocks, stop_reason, usage_dict)
        """

        content_blocks: list[dict[str, Any]] = []
        stop_reason: str | None = None
        usage: dict[str, int] | None = None

        # Track current tool use accumulation
        current_tool: dict[str, Any] | None = None
        tool_input_json = ""

        async for event in self._provider.send_message(
            messages,
            tools=tool_defs if tool_defs else None,
            system=self._system_prompt,
            max_tokens=self._config.provider.max_tokens,
        ):
            if isinstance(event, MessageStart):
                pass  # Metadata captured

            elif isinstance(event, TextDelta):
                # Accumulate text
                if content_blocks and content_blocks[-1].get("type") == "text":
                    content_blocks[-1]["text"] += event.text
                else:
                    content_blocks.append({"type": "text", "text": event.text})

                self._stdout_parts.append(event.text)
                await self._events.emit(TextOutputEvent(text=event.text))

            elif isinstance(event, ThinkingDelta):
                # Extended thinking — log but don't include in output
                pass

            elif isinstance(event, ToolUseStart):
                current_tool = {
                    "type": "tool_use",
                    "id": event.tool_use_id,
                    "name": event.name,
                    "input": {},
                }
                tool_input_json = ""

            elif isinstance(event, ToolUseInputDelta):
                tool_input_json += event.partial_json

            elif isinstance(event, ToolUseEnd):
                if current_tool:
                    # Parse accumulated JSON input
                    try:
                        current_tool["input"] = (
                            json.loads(tool_input_json) if tool_input_json else {}
                        )
                    except json.JSONDecodeError:
                        current_tool["input"] = {"_raw": tool_input_json}

                    content_blocks.append(current_tool)
                    current_tool = None
                    tool_input_json = ""

            elif isinstance(event, MessageDelta):
                if event.stop_reason:
                    stop_reason = event.stop_reason

            elif isinstance(event, MessageEnd):
                if event.usage:
                    usage = event.usage

        return content_blocks, stop_reason, usage

    async def _execute_tools(self, content_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute tool calls from assistant content blocks.

        Args:
            content_blocks: The assistant message content blocks.

        Returns:
            List of tool_result content blocks for the user message.
        """
        results: list[dict[str, Any]] = []

        for block in content_blocks:
            if block.get("type") != "tool_use":
                continue

            tool_name = block["name"]
            tool_input = block.get("input", {})
            tool_use_id = block["id"]

            await self._events.emit(
                ToolCallEvent(
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    input_data=tool_input,
                )
            )

            result = await self._tools.execute(tool_name, tool_input, tool_use_id)

            await self._events.emit(
                ToolResultEvent(
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    content=result.content[:200],  # Truncate for event
                    is_error=result.is_error,
                )
            )

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
            )

        return results

    async def _compact(
        self,
        messages: list[dict[str, Any]],
        context_window: int,
    ) -> list[dict[str, Any]]:
        """Compact conversation by summarizing history.

        Asks the model to summarize, then replaces conversation with summary.
        """
        pre_tokens = estimate_message_tokens(messages)
        logger.info(f"Compacting context: {pre_tokens} estimated tokens")

        # Ask the model to summarize
        summary_messages = messages + [
            {"role": "user", "content": COMPACTION_PROMPT},
        ]

        try:
            content_blocks, _, usage = await self._api_turn(
                summary_messages,
                [],  # No tools during compaction
            )

            # Extract summary text
            summary = ""
            for block in content_blocks:
                if block.get("type") == "text":
                    summary += block.get("text", "")

            if not summary:
                logger.warning("Compaction produced empty summary, keeping original messages")
                return messages

            # Build new message list from summary
            new_messages = build_compaction_messages(summary, self._system_prompt)

            post_tokens = estimate_message_tokens(new_messages)
            self._compaction.record_compaction(self._turn)

            await self._events.emit(
                CompactionEvent(
                    turn_number=self._turn,
                    pre_token_count=pre_tokens,
                    post_token_count=post_tokens,
                    summary_length=len(summary),
                )
            )

            # Update session
            self._session.set_messages(new_messages)
            self._session.metadata.compaction_count = self._compaction.compaction_count

            logger.info(
                f"Compaction complete: {pre_tokens} -> {post_tokens} estimated tokens "
                f"(summary: {len(summary)} chars)"
            )

            return new_messages

        except Exception as e:
            logger.error(f"Compaction failed: {e}")
            await self._events.emit(ErrorEvent(error=f"Compaction failed: {e}", recoverable=True))
            return messages  # Keep original on failure

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        """Handle SIGTERM for graceful shutdown."""
        logger.info("SIGTERM received, requesting graceful stop")
        self._stop_requested = True
