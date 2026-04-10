"""Interactive terminal mode for the egg harness.

Minimal MVP -- no rich TUI. Multi-turn conversation with streaming output.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from egg_harness.config import HarnessConfig, ProviderConfig
from egg_harness.events import EventBus, TextOutputEvent
from egg_harness.loop import AgentLoop
from egg_harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _create_default_registry(cwd: str | None = None) -> ToolRegistry:
    """Create a tool registry with all standard tools."""
    from egg_harness.tools.bash import BashTool
    from egg_harness.tools.edit import EditTool
    from egg_harness.tools.glob_tool import GlobTool
    from egg_harness.tools.grep import GrepTool
    from egg_harness.tools.read import ReadTool
    from egg_harness.tools.web_fetch import WebFetchTool
    from egg_harness.tools.web_search import WebSearchTool
    from egg_harness.tools.write import WriteTool

    registry = ToolRegistry()
    registry.register(BashTool(cwd=cwd))
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(GlobTool(default_path=cwd))
    registry.register(GrepTool(default_path=cwd))
    registry.register(WebFetchTool())
    registry.register(WebSearchTool())
    return registry


async def run_interactive(
    *,
    model: str = "opus",
    provider: str = "anthropic",
    endpoint: str | None = None,
) -> int:
    """Run the interactive REPL."""
    print(f"egg harness interactive mode (model: {model}, provider: {provider})")
    print("Type your message and press Enter. Ctrl-D to exit, Ctrl-C to interrupt.\n")

    config = HarnessConfig(
        provider=ProviderConfig(
            provider=provider,  # type: ignore[arg-type]
            model=model,
            endpoint=endpoint,
        ),
        max_turns=1,  # Single turn per user message in interactive
    )

    resolved_model = config.provider.resolve_model()

    # Create provider
    if provider == "anthropic":
        from egg_harness.providers.anthropic import AnthropicProvider

        llm_provider = AnthropicProvider(default_model=resolved_model)
    else:
        from egg_harness.providers.openai_compat import OpenAICompatibleProvider

        llm_provider = OpenAICompatibleProvider(
            base_url=endpoint or "http://localhost:8000/v1",
            default_model=resolved_model,
        )

    registry = _create_default_registry()
    events = EventBus()

    # Print text as it streams
    def on_text(event: Any) -> None:
        if isinstance(event, TextOutputEvent):
            sys.stdout.write(event.text)
            sys.stdout.flush()

    events.on_event_sync(on_text)

    # Load system prompt
    import os

    from egg_harness.prompt import load_claude_md

    system_prompt = load_claude_md(project_dir=os.getcwd())

    messages: list[dict[str, Any]] = []

    try:
        while True:
            try:
                user_input = input("\n> ")
            except EOFError:
                print("\nGoodbye!")
                break

            if not user_input.strip():
                continue

            messages.append({"role": "user", "content": user_input})

            # Create a loop for this turn
            loop = AgentLoop(
                provider=llm_provider,
                tool_registry=registry,
                config=config,
                event_bus=events,
                system_prompt=system_prompt,
            )

            # Run one agentic turn (may involve multiple API calls if tools used)
            print()  # newline before response
            try:
                result_messages = await loop.run_turn(messages)
                messages = result_messages
            except KeyboardInterrupt:
                print("\n(interrupted)")
                continue
            except Exception as e:
                print(f"\nError: {e}", file=sys.stderr)
                continue

    finally:
        await llm_provider.close()

    return 0
