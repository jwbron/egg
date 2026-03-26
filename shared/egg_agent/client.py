"""Claude Agent SDK client for in-process agent execution.

This module wraps ``claude_agent_sdk.query()`` to provide a simple async
interface that returns an :class:`AgentResult`.  It is only usable inside
sandbox containers where ``claude-agent-sdk`` is installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from egg_agent.result import AgentResult

# Maximum length for tool input/output in log events to avoid bloating logs
_MAX_TOOL_CONTENT_LOG_LEN = 2000


def _truncate(value: str, max_len: int = _MAX_TOOL_CONTENT_LOG_LEN) -> str:
    """Truncate a string for logging, appending an indicator if truncated."""
    if len(value) <= max_len:
        return value
    return value[:max_len] + f"... ({len(value)} chars)"


class _StdlibLoggerAdapter:
    """Thin adapter so stdlib logger ignores structured-log kwargs."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, msg: str, **kwargs: Any) -> None:
        # Drop structured kwargs that stdlib doesn't understand
        self._logger.log(level, msg)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, **kwargs)


try:
    from egg_logging import get_logger

    logger: Any = get_logger("egg-agent")
except ImportError:
    logger = _StdlibLoggerAdapter(__name__)

# Default model for sandbox agents
DEFAULT_MODEL = "opus[1m]"


async def run_agent_async(
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int | None = None,
    system_prompt: str | None = None,
    cwd: str | Path | None = None,
    timeout: int = 7200,
    on_output: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
) -> AgentResult:
    """Run a Claude agent using the Agent SDK.

    Args:
        prompt: The prompt to send to Claude.
        model: Model to use (default: ``opus[1m]``).
        max_turns: Maximum conversation turns.
        system_prompt: Optional system prompt override.
        cwd: Working directory for the agent.
        timeout: Maximum execution time in seconds (default: 2 hours).
        on_output: Optional callback for streaming text output.
        env: Optional environment variables to pass to the agent.

    Returns:
        :class:`AgentResult` with response text and metadata.
    """
    model = model or DEFAULT_MODEL

    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKError,
            CLINotFoundError,
            ProcessError,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
            query,
        )
    except ImportError:
        return AgentResult(
            success=False,
            stdout="",
            stderr="claude-agent-sdk is not installed",
            returncode=-1,
            error="claude-agent-sdk is not installed. Only available inside sandbox containers.",
        )

    # In private mode, block web tools at the SDK level so they never reach
    # the API.  settings.json may also contain disallowedTools (set by the
    # entrypoint), but passing them here as a CLI flag is more reliable and
    # eliminates the gateway log noise from stripping them on every request.
    private_mode = os.environ.get("EGG_PRIVATE_MODE", "").lower() in ("true", "1")
    disallowed: list[str] = ["WebFetch", "WebSearch"] if private_mode else []

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model=model,
        cwd=str(cwd) if cwd else None,
        env=env or {},
        # Read CLAUDE.md and settings.json from the filesystem so the agent
        # picks up sandbox rules (BRC protocol, egg-orch CLI, git safety, etc.).
        # Without this the SDK ignores all filesystem-based configuration.
        setting_sources=["project", "user"],
        disallowed_tools=disallowed,
    )
    if max_turns is not None:
        options.max_turns = max_turns
    if system_prompt is not None:
        options.system_prompt = system_prompt

    stdout_parts: list[str] = []
    actual_model: str | None = None
    result_meta: dict[str, Any] = {}

    logger.info(
        "Agent session init",
        event_type="system",
        event_subtype="init",
        model=model,
        cwd=str(cwd) if cwd else None,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        timeout=timeout,
        setting_sources=["project", "user"],
        disallowed_tools=disallowed,
        sdk="claude_agent_sdk",
    )

    try:
        async with asyncio.timeout(timeout):
            stream = query(prompt=prompt, options=options)
            async for message in stream:
                if isinstance(message, AssistantMessage):
                    if not actual_model and message.model:
                        actual_model = message.model
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            # Serialize tool input for logging (truncated)
                            try:
                                input_str = json.dumps(block.input, default=str)
                            except (TypeError, ValueError):
                                input_str = str(block.input)
                            logger.info(
                                "Tool call",
                                event_type="tool_use",
                                tool_name=block.name,
                                tool_use_id=block.id,
                                input=_truncate(input_str),
                            )
                        elif isinstance(block, TextBlock) and block.text:
                            logger.info(
                                "Assistant message",
                                event_type="assistant",
                                event_subtype="text",
                                text=_truncate(block.text),
                            )
                            stdout_parts.append(block.text)
                            if on_output:
                                on_output(block.text)
                elif isinstance(message, UserMessage):
                    # Log tool results from user messages
                    content = message.content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                # Serialize tool result content for logging
                                if isinstance(block.content, str):
                                    result_str = block.content
                                elif block.content is not None:
                                    try:
                                        result_str = json.dumps(block.content, default=str)
                                    except (TypeError, ValueError):
                                        result_str = str(block.content)
                                else:
                                    result_str = ""
                                logger.info(
                                    "Tool result",
                                    event_type="tool_result",
                                    tool_use_id=block.tool_use_id,
                                    is_error=block.is_error or False,
                                    content=_truncate(result_str),
                                )
                elif isinstance(message, SystemMessage):
                    logger.debug(
                        "SystemMessage received",
                        event_type="system",
                        event_subtype=getattr(message, "subtype", None),
                        data=getattr(message, "data", None),
                    )
                elif isinstance(message, ResultMessage):
                    if message.result:
                        stdout_parts.append(message.result)
                        if on_output:
                            on_output(message.result)
                    result_meta = {
                        "cost_usd": message.total_cost_usd,
                        "num_turns": message.num_turns,
                        "duration_ms": message.duration_ms,
                        "session_id": message.session_id,
                    }
                    if message.is_error:
                        logger.info(
                            "Agent completed",
                            event_type="system",
                            event_subtype="result",
                            model=actual_model,
                            session_id=result_meta.get("session_id"),
                            cost_usd=result_meta.get("cost_usd"),
                            num_turns=result_meta.get("num_turns"),
                            duration_ms=result_meta.get("duration_ms"),
                            success=False,
                            error=message.result or "Agent reported error",
                        )
                        return AgentResult(
                            success=False,
                            stdout="\n".join(stdout_parts),
                            stderr=message.result or "",
                            returncode=1,
                            error=message.result or "Agent reported error",
                            metadata={"model": actual_model} if actual_model else None,
                            cost_usd=message.total_cost_usd,
                            num_turns=message.num_turns,
                            duration_ms=message.duration_ms,
                            session_id=message.session_id,
                        )

    except TimeoutError:
        logger.info(
            "Agent completed",
            event_type="system",
            event_subtype="result",
            model=actual_model,
            session_id=result_meta.get("session_id"),
            cost_usd=result_meta.get("cost_usd"),
            num_turns=result_meta.get("num_turns"),
            duration_ms=result_meta.get("duration_ms"),
            success=False,
            error=f"Timed out after {timeout} seconds",
        )
        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr="",
            returncode=-1,
            error=f"Timed out after {timeout} seconds",
            metadata={"model": actual_model} if actual_model else None,
        )

    except (ProcessError, CLINotFoundError, ClaudeSDKError) as e:
        logger.info(
            "Agent completed",
            event_type="system",
            event_subtype="result",
            model=actual_model,
            session_id=result_meta.get("session_id"),
            cost_usd=result_meta.get("cost_usd"),
            num_turns=result_meta.get("num_turns"),
            duration_ms=result_meta.get("duration_ms"),
            success=False,
            error=str(e),
        )
        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr=str(e),
            returncode=-1,
            error=str(e),
            metadata={"model": actual_model} if actual_model else None,
        )

    except Exception as e:
        logger.info(
            "Agent completed",
            event_type="system",
            event_subtype="result",
            model=actual_model,
            session_id=result_meta.get("session_id"),
            cost_usd=result_meta.get("cost_usd"),
            num_turns=result_meta.get("num_turns"),
            duration_ms=result_meta.get("duration_ms"),
            success=False,
            error=str(e),
        )
        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr=str(e),
            returncode=-1,
            error=str(e),
            metadata={"model": actual_model} if actual_model else None,
        )

    logger.info(
        "Agent completed",
        event_type="system",
        event_subtype="result",
        model=actual_model,
        session_id=result_meta.get("session_id"),
        cost_usd=result_meta.get("cost_usd"),
        num_turns=result_meta.get("num_turns"),
        duration_ms=result_meta.get("duration_ms"),
        success=True,
    )

    return AgentResult(
        success=True,
        stdout="\n".join(stdout_parts),
        stderr="",
        returncode=0,
        metadata={"model": actual_model} if actual_model else None,
        cost_usd=result_meta.get("cost_usd"),
        num_turns=result_meta.get("num_turns"),
        duration_ms=result_meta.get("duration_ms"),
        session_id=result_meta.get("session_id"),
    )


def run_agent(
    prompt: str,
    *,
    model: str | None = None,
    **kwargs: Any,
) -> AgentResult:
    """Synchronous wrapper for :func:`run_agent_async`.

    See :func:`run_agent_async` for full documentation.
    """
    return asyncio.run(run_agent_async(prompt, model=model, **kwargs))
