"""High-level client API for the egg harness.

Provides :func:`run_agent_async` and :func:`run_agent` with a signature
that mirrors ``egg_agent.client.run_agent_async``, backed by the harness
loop, provider stack, and tool registry.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from egg_harness.config import HarnessConfig, ProviderConfig, parse_model_spec
from egg_harness.events import EventBus
from egg_harness.loop import AgentLoop
from egg_harness.permissions import (
    create_disallow_list_callback,
)
from egg_harness.result import AgentResult
from egg_harness.tools import (
    ToolDefinition,
    ToolHandler,
    ToolRegistry,
    create_bash_tool,
    create_edit_tool,
    create_glob_tool,
    create_grep_tool,
    create_read_tool,
    create_web_fetch_tool,
    create_web_search_tool,
    create_write_tool,
)

logger = logging.getLogger(__name__)

# Default model spec matching egg_agent convention.
DEFAULT_MODEL = "opus[1m]"


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------


def _create_standard_tools(
    cwd: str | None = None,
) -> list[tuple[ToolDefinition, ToolHandler]]:
    """Create all eight standard tool pairs.

    Args:
        cwd: Working directory for the Bash tool.  ``None`` uses the
            current directory at execution time.

    Returns:
        A list of ``(ToolDefinition, ToolHandler)`` tuples for Bash,
        Read, Write, Edit, Glob, Grep, WebFetch, and WebSearch.
    """
    return [
        create_bash_tool(cwd=cwd),
        create_read_tool(),
        create_write_tool(),
        create_edit_tool(),
        create_glob_tool(),
        create_grep_tool(),
        create_web_fetch_tool(),
        create_web_search_tool(),
    ]


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------


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
    intercept_tools: bool = True,
) -> AgentResult:
    """Run an agent using the egg harness loop.

    This function mirrors the ``egg_agent.client.run_agent_async``
    signature so callers can switch between the SDK-based runner and
    the harness-based runner without changing their call site.

    Args:
        prompt: The user prompt to send to the agent.
        model: Model specification (default ``"opus[1m]"``).  Supports
            aliases and context-window suffixes like ``"sonnet[200k]"``.
        max_turns: Maximum conversation turns.  ``None`` uses the
            :class:`HarnessConfig` default (200).
        system_prompt: Optional system-level instructions.
        cwd: Working directory for the agent.  ``None`` uses the
            current directory.
        timeout: Maximum execution time in seconds (default 7200).
        on_output: Optional callback invoked with each text output
            chunk as it streams from the model.
        env: Optional extra environment variables to inject.
        intercept_tools: If ``True`` (default) and disallowed tools
            are configured, apply a permission callback that blocks
            them.

    Returns:
        An :class:`AgentResult` describing the outcome.
    """
    model = model or DEFAULT_MODEL
    resolved_model, _context_window = parse_model_spec(model)

    # Resolve cwd to a string for tools.
    cwd_str = str(cwd) if cwd is not None else None

    # -- Provider stack ------------------------------------------------
    provider_config = ProviderConfig(
        provider_type="anthropic",
        model=resolved_model,
        endpoint=os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("GATEWAY_URL"),
    )

    # Lazy-import providers to avoid import-time SDK dependencies.
    from egg_harness.providers.anthropic import AnthropicProvider
    from egg_harness.providers.retry import RetryProvider

    inner_provider = AnthropicProvider(provider_config)
    provider = RetryProvider(inner_provider)

    # -- Tool registry -------------------------------------------------
    registry = ToolRegistry()
    for defn, handler in _create_standard_tools(cwd=cwd_str):
        registry.register(defn, handler)

    # -- Permission enforcement ----------------------------------------
    if intercept_tools:
        private_mode = os.environ.get("EGG_PRIVATE_MODE", "").lower() in ("true", "1")
        disallowed: list[str] = []
        if private_mode:
            disallowed.extend(["WebFetch", "WebSearch"])

        if disallowed:
            callback = create_disallow_list_callback(disallowed)
            registry.set_permission_callback(callback)

    # -- Event bus -----------------------------------------------------
    event_bus = EventBus()
    if on_output is not None:
        event_bus.on_output(on_output)

    # -- Harness config ------------------------------------------------
    harness_config = HarnessConfig(
        provider=provider_config,
        max_turns=max_turns if max_turns is not None else 200,
        timeout=timeout,
        cwd=cwd_str,
        env=env,
        intercept_tools=intercept_tools,
    )

    # -- Run the loop --------------------------------------------------
    loop = AgentLoop(
        provider=provider,
        tool_registry=registry,
        event_bus=event_bus,
        config=harness_config,
    )

    return await loop.run(prompt, system_prompt=system_prompt)


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
