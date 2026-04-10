"""Factory for creating a fully configured egg harness.

Creates a HarnessConfig, Provider, ToolRegistry, and AgentLoop
with all egg-specific integrations wired up.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from egg_harness.config import HarnessConfig, ProviderConfig
from egg_harness.events import EventBus
from egg_harness.session import Session
from egg_harness.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def create_egg_harness(
    *,
    model: str = "opus",
    max_turns: int | None = None,
    timeout: int = 7200,
    cwd: str | None = None,
    system_prompt: str | None = None,
    session_file: str | None = None,
    provider: str = "anthropic",
    endpoint: str | None = None,
    include_egg_tools: bool = True,
    on_output: Callable[[str], None] | None = None,
    extra_event_callbacks: list[Any] | None = None,
) -> dict[str, Any]:
    """Create a fully configured egg harness.

    Returns a dict with all components needed to run:
        - config: HarnessConfig
        - provider: Provider instance
        - tool_registry: ToolRegistry with all tools
        - event_bus: EventBus
        - session: Session
        - system_prompt: str

    Use with AgentLoop:
        components = create_egg_harness(model="opus")
        loop = AgentLoop(**components)
        result = await loop.run("prompt")
    """
    from egg_harness.events import TextOutputEvent

    cwd = cwd or os.getcwd()

    # Build config
    config = HarnessConfig(
        provider=ProviderConfig(
            provider=provider,  # type: ignore[arg-type]
            model=model,
            endpoint=endpoint,
        ),
        max_turns=max_turns,
        timeout=timeout,
        cwd=cwd,
        session_file=session_file,
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

    # Build tool registry with standard tools
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

    # Only include web tools if not in private mode
    private_mode = os.environ.get("EGG_PRIVATE_MODE", "").lower() in ("true", "1")
    if not private_mode:
        registry.register(WebFetchTool())
        registry.register(WebSearchTool())

    # Register egg-native tools
    if include_egg_tools:
        try:
            from egg_harness_integration.egg_tools import (
                EggCheckpointTool,
                EggContractTool,
                EggOrchTool,
                GhCliTool,
                GitOpsTool,
            )

            registry.register(EggOrchTool())
            registry.register(EggContractTool())
            registry.register(EggCheckpointTool())
            registry.register(GitOpsTool())
            registry.register(GhCliTool())
        except ImportError:
            logger.debug("Egg-native tools not available")

    # Set permission callback
    try:
        from egg_harness_integration.egg_permissions import create_permission_callback

        callback = create_permission_callback()
        if callback:
            registry.set_permission_callback(callback)
    except ImportError:
        pass

    # Event bus
    events = EventBus()
    if on_output:

        def _on_text(event: Any) -> None:
            if isinstance(event, TextOutputEvent):
                on_output(event.text)

        events.on_event_sync(_on_text)

    if extra_event_callbacks:
        for cb in extra_event_callbacks:
            events.on_event(cb)

    # System prompt
    try:
        from egg_harness_integration.egg_prompt import build_egg_system_prompt

        effective_prompt = build_egg_system_prompt(
            project_dir=cwd,
            system_prompt_override=system_prompt,
        )
    except ImportError:
        from egg_harness.prompt import load_claude_md

        effective_prompt = system_prompt or load_claude_md(project_dir=cwd)

    # Session
    session = Session(file_path=session_file)

    return {
        "provider": llm_provider,
        "tool_registry": registry,
        "config": config,
        "event_bus": events,
        "system_prompt": effective_prompt,
        "session": session,
    }
