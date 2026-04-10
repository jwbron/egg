"""High-level client interface for the egg harness.

Provides run_agent() and run_agent_async() as drop-in replacements
for the corresponding functions in egg_agent.client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from egg_harness.config import HarnessConfig, ProviderConfig
from egg_harness.events import EventBus, TextOutputEvent
from egg_harness.loop import AgentLoop
from egg_harness.prompt import load_claude_md
from egg_harness.result import AgentResult
from egg_harness.session import Session
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
    provider: str = "anthropic",
    endpoint: str | None = None,
    session_file: str | None = None,
    tool_registry: ToolRegistry | None = None,
    permission_callback: Callable[[str, dict[str, Any]], Any] | None = None,
) -> AgentResult:
    """Run an agent using the egg harness.

    Drop-in replacement for egg_agent.client.run_agent_async().

    Args:
        prompt: The prompt to send to the model.
        model: Model alias or full ID (default: "opus").
        max_turns: Maximum conversation turns.
        system_prompt: Optional system prompt override.
        cwd: Working directory for the agent.
        timeout: Maximum execution time in seconds.
        on_output: Optional callback for streaming text output.
        env: Optional environment variables.
        intercept_tools: If True, block writes outside role scope.
        provider: LLM provider ("anthropic" or "openai-compatible").
        endpoint: Provider endpoint URL.
        session_file: Path for session persistence.
        tool_registry: Optional pre-configured tool registry.
        permission_callback: Optional permission check callback.

    Returns:
        AgentResult with response text and metadata.
    """
    start_time = time.time()
    model = model or "opus"
    cwd_str = str(cwd) if cwd else os.getcwd()

    # Build config
    config = HarnessConfig(
        provider=ProviderConfig(
            provider=provider,  # type: ignore[arg-type]
            model=model,
            endpoint=endpoint,
        ),
        max_turns=max_turns,
        timeout=timeout,
        cwd=cwd_str,
        env=env or {},
        session_file=session_file,
    )

    # Resolve model
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

    # Build tool registry
    if tool_registry is None:
        registry = _create_default_registry(cwd=cwd_str)
    else:
        registry = tool_registry

    # Set permission callback
    if permission_callback:
        registry.set_permission_callback(permission_callback)
    elif intercept_tools:
        _setup_default_permissions(registry)

    # Filter disallowed tools
    private_mode = os.environ.get("EGG_PRIVATE_MODE", "").lower() in ("true", "1")
    disallowed = ["WebFetch", "WebSearch"] if private_mode else []

    # Event bus
    events = EventBus()
    stdout_parts: list[str] = []

    def _on_text(event: Any) -> None:
        if isinstance(event, TextOutputEvent):
            stdout_parts.append(event.text)
            if on_output:
                on_output(event.text)

    events.on_event_sync(_on_text)

    # System prompt
    effective_system = system_prompt
    if effective_system is None:
        effective_system = load_claude_md(project_dir=cwd_str)

    # Session
    session = Session(file_path=session_file)

    # Agent loop
    loop = AgentLoop(
        provider=llm_provider,
        tool_registry=registry,
        config=config,
        event_bus=events,
        system_prompt=effective_system,
        session=session,
    )

    try:
        async with asyncio.timeout(timeout):
            result = await loop.run(prompt, disallowed_tools=disallowed)
    except TimeoutError:
        duration_ms = int((time.time() - start_time) * 1000)
        await llm_provider.close()
        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr="",
            returncode=-1,
            error=f"Timed out after {timeout} seconds",
            duration_ms=duration_ms,
            session_id=session.session_id,
        )
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await llm_provider.close()
        return AgentResult(
            success=False,
            stdout="\n".join(stdout_parts),
            stderr=str(e),
            returncode=-1,
            error=str(e),
            duration_ms=duration_ms,
            session_id=session.session_id,
        )

    duration_ms = int((time.time() - start_time) * 1000)
    await llm_provider.close()

    # Save session
    if session_file:
        session.save()

    return AgentResult(
        success=result.success,
        stdout="\n".join(stdout_parts) if stdout_parts else result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
        error=result.error,
        metadata={"model": resolved_model, "provider": provider},
        cost_usd=result.cost_usd,
        num_turns=result.num_turns,
        duration_ms=duration_ms,
        session_id=session.session_id,
        compaction_count=result.compaction_count,
    )


def _setup_default_permissions(registry: ToolRegistry) -> None:
    """Setup default permission checking from egg_restrictions."""
    try:
        from egg_agent.tool_interceptor import check_file_write_permission, get_role_from_env

        role = get_role_from_env()
        if role:

            async def _check(tool_name: str, tool_input: dict[str, Any]) -> str | None:
                return check_file_write_permission(tool_name, tool_input, role)

            registry.set_permission_callback(_check)
    except ImportError:
        pass  # Not in egg environment


def run_agent(
    prompt: str,
    *,
    model: str | None = None,
    **kwargs: Any,
) -> AgentResult:
    """Synchronous wrapper for run_agent_async()."""
    return asyncio.run(run_agent_async(prompt, model=model, **kwargs))
