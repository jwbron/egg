"""
Claude Code runner — thin wrapper around egg_agent SDK client.

This module delegates to :mod:`egg_agent.client` for the actual Agent SDK
interaction.  The public API (``run_agent``, ``run_agent_async``) is
preserved for backward compatibility.
"""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from egg_agent.client import DEFAULT_MODEL
from egg_agent.client import run_agent_async as _sdk_run_agent_async

from llm.claude.config import ClaudeConfig
from llm.result import AgentResult

logger = logging.getLogger(__name__)


async def run_agent_async(
    prompt: str,
    *,
    config: ClaudeConfig | None = None,
    timeout: int | None = None,
    cwd: Path | str | None = None,
    on_output: Callable[[str], None] | None = None,
    model: str | None = None,
) -> AgentResult:
    """Run agent via the Claude Agent SDK.

    Args:
        prompt: The prompt to send to Claude
        config: Agent configuration (uses defaults if None)
        timeout: Override config timeout (seconds)
        cwd: Working directory for the agent
        on_output: Optional callback for streaming output line-by-line
        model: Model to use (default: opus[1m]). Can be a model alias ('opus', 'sonnet')
               or a full model identifier.

    Returns:
        AgentResult with response and status.
    """
    config = config or ClaudeConfig()
    effective_timeout = timeout or config.timeout
    cwd_path = Path(cwd) if cwd else config.cwd
    effective_model = model or DEFAULT_MODEL

    sdk_result = await _sdk_run_agent_async(
        prompt,
        model=effective_model,
        cwd=cwd_path,
        timeout=effective_timeout,
        on_output=on_output,
    )

    return sdk_result


def run_agent(
    prompt: str,
    *,
    model: str | None = None,
    **kwargs: Any,
) -> AgentResult:
    """Synchronous wrapper for run_agent_async.

    See run_agent_async for full documentation.
    """
    return asyncio.run(run_agent_async(prompt, model=model, **kwargs))
