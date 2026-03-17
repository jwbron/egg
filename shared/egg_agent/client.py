"""Claude Agent SDK client for in-process agent execution.

This module wraps ``claude_agent_sdk.query()`` to provide a simple async
interface that returns an :class:`AgentResult`.  It is only usable inside
sandbox containers where ``claude-agent-sdk`` is installed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from egg_agent.result import AgentResult

try:
    from egg_logging import get_logger

    logger = get_logger("egg-agent")
except ImportError:
    logger = logging.getLogger(__name__)

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

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model=model,
        cwd=str(cwd) if cwd else None,
        env=env or {},
        # Read CLAUDE.md and settings.json from the filesystem so the agent
        # picks up sandbox rules (BRC protocol, egg-orch CLI, git safety, etc.).
        # Without this the SDK ignores all filesystem-based configuration.
        setting_sources=["project", "user"],
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
                        if isinstance(block, TextBlock) and block.text:
                            stdout_parts.append(block.text)
                            if on_output:
                                on_output(block.text)
                elif isinstance(message, SystemMessage):
                    logger.debug(
                        "SystemMessage received",
                        event_type="system",
                        subtype=getattr(message, "subtype", None),
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
        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr="",
            returncode=-1,
            error=f"Timed out after {timeout} seconds",
            metadata={"model": actual_model} if actual_model else None,
        )

    except (ProcessError, CLINotFoundError, ClaudeSDKError) as e:
        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr=str(e),
            returncode=-1,
            error=str(e),
            metadata={"model": actual_model} if actual_model else None,
        )

    except Exception as e:
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
