"""Multi-turn interactive REPL for the egg harness.

Provides :func:`run_interactive` which launches a terminal-based
read-eval-print loop, accumulating conversation history across turns
and streaming model output to stdout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from egg_harness.client import DEFAULT_MODEL, _create_standard_tools
from egg_harness.config import HarnessConfig, ProviderConfig, parse_model_spec
from egg_harness.events import EventBus
from egg_harness.loop import AgentLoop
from egg_harness.permissions import create_disallow_list_callback
from egg_harness.tools import ToolRegistry

logger = logging.getLogger(__name__)

# Welcome banner displayed on REPL start.
_WELCOME = "egg harness interactive mode\nType your message and press Enter. Ctrl-D to exit.\n"


async def run_interactive(
    *,
    model: str = DEFAULT_MODEL,
    system_prompt: str | None = None,
    timeout: int = 7200,
) -> int:
    """Run a multi-turn interactive REPL session.

    Creates the provider stack, tool registry, and event bus once,
    then loops reading user input and sending it through the agent
    loop.  Conversation history is accumulated across turns so the
    model retains context.

    Args:
        model: Model specification (default ``"opus[1m]"``).
        system_prompt: Optional system-level instructions.
        timeout: Per-turn timeout in seconds (default 7200).

    Returns:
        Exit code: 0 on normal exit.
    """
    resolved_model, _context_window = parse_model_spec(model)

    # -- Provider stack ------------------------------------------------
    provider_config = ProviderConfig(
        provider_type="anthropic",
        model=resolved_model,
        endpoint=os.environ.get("ANTHROPIC_GATEWAY_URL"),
    )

    from egg_harness.providers.anthropic import AnthropicProvider
    from egg_harness.providers.retry import RetryProvider

    inner_provider = AnthropicProvider(provider_config)
    provider = RetryProvider(inner_provider)

    # -- Tool registry -------------------------------------------------
    registry = ToolRegistry()
    for defn, handler in _create_standard_tools():
        registry.register(defn, handler)

    # Apply private-mode restrictions if active.
    private_mode = os.environ.get("EGG_PRIVATE_MODE", "").lower() in ("true", "1")
    if private_mode:
        callback = create_disallow_list_callback(["WebFetch", "WebSearch"])
        registry.set_permission_callback(callback)

    # -- Event bus (stream output to stdout) ---------------------------
    event_bus = EventBus()

    def _on_output(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    event_bus.on_output(_on_output)

    # -- Harness config ------------------------------------------------
    harness_config = HarnessConfig(
        provider=provider_config,
        timeout=timeout,
    )

    # -- Agent loop (reused across turns) ------------------------------
    loop = AgentLoop(
        provider=provider,
        tool_registry=registry,
        event_bus=event_bus,
        config=harness_config,
    )

    # -- Conversation history ------------------------------------------
    messages: list[dict[str, Any]] = []

    print(_WELCOME)

    while True:
        # -- Read user input -------------------------------------------
        try:
            user_input = input("> ")
        except EOFError:
            print("\nGoodbye.")
            return 0
        except KeyboardInterrupt:
            print("\nInterrupted")
            continue

        if not user_input.strip():
            continue

        # Append user message to history.
        messages.append({"role": "user", "content": user_input})

        # -- Send to agent loop ----------------------------------------
        try:
            result = await loop.run(
                user_input,
                system_prompt=system_prompt,
                messages=messages,
            )
        except KeyboardInterrupt:
            print("\nInterrupted")
            continue
        except asyncio.CancelledError:
            print("\nCancelled")
            continue

        # Append assistant response to history for context continuity.
        if result.stdout:
            messages.append({"role": "assistant", "content": result.stdout})
            # Ensure a trailing newline after the streamed response.
            if not result.stdout.endswith("\n"):
                print()

        if result.error:
            print(f"\n[error] {result.error}", file=sys.stderr)

    return 0  # pragma: no cover — unreachable but satisfies type checker
