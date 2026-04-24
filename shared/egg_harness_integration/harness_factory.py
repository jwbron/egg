"""Factory function that wires all egg integration pieces together.

Provides :func:`create_egg_harness`, a single entry point that
constructs a fully-configured :class:`AgentLoop` with:

- The Anthropic provider stack (with retry)
- All 8 standard tools + 5 egg-native CLI tools
- Egg system prompt (CLAUDE.md rules + project CLAUDE.md)
- Role-based file permission enforcement
- Anchor-based compaction callbacks
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from egg_harness.config import HarnessConfig, ProviderConfig, parse_model_spec
from egg_harness.events import EventBus
from egg_harness.loop import AgentLoop
from egg_harness.permissions import compose_permissions, create_disallow_list_callback
from egg_harness.tools import (
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

from egg_harness_integration.egg_compaction import create_compaction_callback
from egg_harness_integration.egg_permissions import create_egg_permission_callback
from egg_harness_integration.egg_prompt import build_egg_system_prompt
from egg_harness_integration.egg_tools import register_egg_tools

logger = logging.getLogger(__name__)

# Default model specification.
_DEFAULT_MODEL: str = "opus[1m]"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_egg_harness(
    *,
    model: str = "opus[1m]",
    max_turns: int = 200,
    system_prompt: str | None = None,
    cwd: str | None = None,
    timeout: int = 7200,
    on_output: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
    intercept_tools: bool = True,
) -> tuple[AgentLoop, EventBus, HarnessConfig]:
    """Create a fully-configured egg harness agent loop.

    Assembles the complete agent runtime by:

    1. Parsing the model spec (resolving aliases and extracting max_tokens).
    2. Building the Anthropic provider wrapped in a retry layer.
    3. Registering all 8 standard tools and 5 egg-native CLI tools.
    4. Assembling the system prompt from egg rules and project CLAUDE.md
       (unless *system_prompt* is explicitly provided).
    5. Setting up role-based file permission enforcement.
    6. Hooking anchor-based compaction callbacks into the event bus.

    Args:
        model: Model specification string (default ``"opus[1m]"``).
            Supports aliases (``"opus"``, ``"sonnet"``, ``"haiku"``)
            and context-window suffixes (``"opus[200k]"``).
        max_turns: Maximum number of agent turns (default 200).
        system_prompt: Override system prompt.  When ``None`` the prompt
            is assembled from egg rule files and project CLAUDE.md.
        cwd: Working directory for the agent.  ``None`` uses the current
            directory.
        timeout: Hard wall-clock timeout in seconds (default 7200).
        on_output: Optional callback invoked with each streamed text
            chunk from the model.
        env: Optional extra environment variables for the agent process.
        intercept_tools: Whether to apply permission callbacks that
            enforce role-based file access restrictions (default ``True``).

    Returns:
        A tuple of ``(AgentLoop, EventBus, HarnessConfig)``.
    """
    # -- Model resolution --------------------------------------------------
    resolved_model, _context_window = parse_model_spec(model)

    # -- Provider stack ----------------------------------------------------
    endpoint = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("GATEWAY_URL")

    provider_config = ProviderConfig(
        provider_type="anthropic",
        model=resolved_model,
        endpoint=endpoint,
    )

    # Lazy-import providers to avoid import-time SDK dependencies.
    from egg_harness.providers.anthropic import AnthropicProvider
    from egg_harness.providers.retry import RetryProvider

    inner_provider = AnthropicProvider(provider_config)
    provider = RetryProvider(inner_provider)

    # -- Tool registry -----------------------------------------------------
    # Default cwd to EGG_REPO_PATH so sandbox agents start inside the repo
    # rather than at HOME (/home/egg).  See #1993.
    cwd_str = str(cwd) if cwd is not None else (os.environ.get("EGG_REPO_PATH") or None)

    registry = ToolRegistry()

    # Register the 8 standard tools.
    standard_tools = [
        create_bash_tool(cwd=cwd_str),
        create_read_tool(),
        create_write_tool(),
        create_edit_tool(),
        create_glob_tool(),
        create_grep_tool(),
        create_web_fetch_tool(),
        create_web_search_tool(),
    ]
    for defn, handler in standard_tools:
        registry.register(defn, handler)

    # Register the 5 egg-native CLI tools.
    register_egg_tools(registry)

    # -- Permission enforcement --------------------------------------------
    if intercept_tools:
        callbacks: list[Callable[[str, dict[str, Any]], str | None]] = []

        # Role-based file access restrictions.
        egg_perm = create_egg_permission_callback()
        if egg_perm is not None:
            callbacks.append(egg_perm)

        # Private-mode web tool blocking.
        private_mode = os.environ.get("EGG_PRIVATE_MODE", "").lower() in ("true", "1")
        if private_mode:
            callbacks.append(create_disallow_list_callback(["WebFetch", "WebSearch"]))

        if callbacks:
            registry.set_permission_callback(compose_permissions(*callbacks))

    # -- Event bus ---------------------------------------------------------
    event_bus = EventBus()

    if on_output is not None:
        event_bus.on_output(on_output)

    # Hook compaction callback for anchor updates.
    compaction_cb = create_compaction_callback()
    event_bus.on_compaction(compaction_cb)

    # -- System prompt -----------------------------------------------------
    if system_prompt is None:
        # Look for project CLAUDE.md relative to cwd or EGG_REPO_PATH.
        project_claude_md: str | None = None
        # cwd_str already incorporates the EGG_REPO_PATH fallback (line 116),
        # so the `or` branch is redundant when called from client.py.  It is
        # kept for direct callers of create_egg_harness (e.g. validate_harness_parity.py)
        # that may pass cwd=None without the env-var resolution layer.
        repo_path = cwd_str or os.environ.get("EGG_REPO_PATH")
        if repo_path:
            candidate = os.path.join(repo_path, "CLAUDE.md")
            if os.path.isfile(candidate):
                project_claude_md = candidate

        system_prompt = build_egg_system_prompt(
            project_claude_md=project_claude_md,
        )

    # -- Harness config ----------------------------------------------------
    harness_config = HarnessConfig(
        provider=provider_config,
        max_turns=max_turns,
        timeout=timeout,
        cwd=cwd_str,
        env=env,
        intercept_tools=intercept_tools,
        system_prompt=system_prompt,
    )

    # -- Agent loop --------------------------------------------------------
    loop = AgentLoop(
        provider=provider,
        tool_registry=registry,
        event_bus=event_bus,
        config=harness_config,
    )

    return loop, event_bus, harness_config
