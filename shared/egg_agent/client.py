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
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from egg_agent.result import AgentResult

if TYPE_CHECKING:
    # Annotation-only SDK types. With ``from __future__ import annotations``
    # these are never evaluated at runtime, so they don't belong in the
    # runtime import below — only HookMatcher is constructed at runtime.
    from claude_agent_sdk import HookContext, HookInput, HookJSONOutput

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

# Substring of the SDK's CLIJSONDecodeError message identifying the
# JSON message-reader buffer overflow (issue #2804). The overflow
# is deterministic — the same tool call against the same codebase
# produces the same oversized payload — so the consensus-wrapper greps
# for this marker on agent exit to short-circuit retry instead of
# burning the restart budget on a doomed re-run. With the reader buffer
# raised below (#2884) this is now a rare backstop, not the common path:
# it fires only if a single stream message exceeds the (generous) raised
# buffer. See #2823 for the follow-up to pin this marker against the SDK.
_BUFFER_OVERFLOW_MARKER = "exceeded maximum buffer size"

# Cap on a single message in egg's Agent SDK stream-json reader (issue #2884).
#
# The SDK reads the CLI's stdout stream into a JSON buffer; a single message
# larger than this raises CLIJSONDecodeError and kills the agent (exit 255,
# #2804). The SDK default is 1 MiB — but the messages that overflow are *not*
# model-bound. Claude Code attaches the **entire original file** to every
# Edit/Write result as transcript metadata (`toolUseResult.originalFile`) that
# the model never sees; only egg's reader decodes it. So a routine ~2 KB edit
# to the 1.1 MB / 25k-line orchestrator/routes/pipelines.py emits a >1 MB stream
# message and crashes the reader, even though the model's tool_result is just a
# bounded snippet (the #2884 crash, mis-attributed to a large edit at first).
#
# Raising the reader buffer lets egg ingest that metadata-heavy message. It does
# NOT cost model context or tokens (the field never reaches the model, and egg
# logs at most _MAX_TOOL_CONTENT_LOG_LEN of any result) — the only cost is
# transient reader memory for one message. Model-bound result sizes are bounded
# separately and independently (egg MCP @tool caps #2805; Read/Grep predictive
# caps #2876; Claude Code truncates Bash), so a large reader buffer cannot let an
# oversized payload reach the model. 32 MiB covers source files far larger than
# anything in this repo while still bounding a runaway/malformed stream; the
# #2810 fail-fast remains the clean backstop above it. Override with
# EGG_SDK_MAX_BUFFER_BYTES.
_DEFAULT_SDK_MAX_BUFFER_BYTES = 32 * 1024 * 1024

# Hard upper bound on EGG_SDK_MAX_BUFFER_BYTES. Defends against an operator
# typo (e.g. a stray suffix-conversion like ``34359738368000`` ≈ 34 TiB) that
# would otherwise leave the reader effectively unbounded — a runaway or
# malformed stream could then OOM the container before the SDK rejected it.
# 1 GiB is several orders of magnitude over anything a real source file or
# transcript metadata payload could legitimately produce, while still bounding
# the worst-case allocation.
_MAX_SDK_MAX_BUFFER_BYTES = 1024 * 1024 * 1024

# Raw EGG_SDK_MAX_BUFFER_BYTES values we've already warned about, so a steady
# bad value warns once per distinct raw value rather than on every
# ``run_agent_async`` invocation. Mirrors ``tool_output_cap._warned_cap_values``
# (#2884 review feedback).
_warned_sdk_buffer_values: set[str] = set()


def _warn_invalid_sdk_buffer(raw: str, problem: str, fallback: int) -> None:
    """Warn that an invalid EGG_SDK_MAX_BUFFER_BYTES is being clamped, once per value."""
    if raw in _warned_sdk_buffer_values:
        return
    _warned_sdk_buffer_values.add(raw)
    logger.warning(f"EGG_SDK_MAX_BUFFER_BYTES={raw!r} {problem}; using {fallback} bytes")


def _sdk_max_buffer_bytes() -> int:
    """Resolve the Agent SDK reader buffer cap from ``EGG_SDK_MAX_BUFFER_BYTES``.

    A set-but-invalid value (non-integer, non-positive, or absurdly large) is
    logged and clamped to the default or the hard upper bound, so an operator
    typo can't silently re-expose the 1 MiB-overflow crash *or* leave the
    reader effectively unbounded. The unset case is silent (the default is
    expected). Warnings dedup per distinct raw value so a steady misconfig
    doesn't spam logs on every agent spawn (#2884 review feedback).
    """
    raw = os.environ.get("EGG_SDK_MAX_BUFFER_BYTES", "").strip()
    if not raw:
        return _DEFAULT_SDK_MAX_BUFFER_BYTES
    try:
        value = int(raw)
    except ValueError:
        _warn_invalid_sdk_buffer(raw, "is not an integer", _DEFAULT_SDK_MAX_BUFFER_BYTES)
        return _DEFAULT_SDK_MAX_BUFFER_BYTES
    if value <= 0:
        _warn_invalid_sdk_buffer(raw, "must be a positive integer", _DEFAULT_SDK_MAX_BUFFER_BYTES)
        return _DEFAULT_SDK_MAX_BUFFER_BYTES
    if value > _MAX_SDK_MAX_BUFFER_BYTES:
        _warn_invalid_sdk_buffer(
            raw,
            f"exceeds the {_MAX_SDK_MAX_BUFFER_BYTES}-byte upper bound",
            _MAX_SDK_MAX_BUFFER_BYTES,
        )
        return _MAX_SDK_MAX_BUFFER_BYTES
    return value


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
    effort: str | None = None,
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
        intercept_tools: If True (default), block Write/Edit/NotebookEdit
            calls that violate role-based file restrictions. Blocked tools
            return an error to the LLM instead of executing.
            Only active when EGG_AGENT_ROLE is set.
        effort: Reasoning effort level (``low`` / ``medium`` / ``high`` /
            ``max``), passed to the CLI as ``--effort``. ``None``
            (default) omits the flag so the session inherits Claude
            Code's per-model default.

    Returns:
        :class:`AgentResult` with response text and metadata.
    """
    model = model or DEFAULT_MODEL

    # Pin MCP servers to blocking connect (#3137 review). The 0.2 SDK / CLI
    # bump made the spawned Claude Code default ``MCP_CONNECTION_NONBLOCKING``
    # to non-zero, so a slow stdio MCP server is reported as ``pending`` and
    # its tools are not available on the model's first turn. egg's in-process
    # SDK MCP servers (registered below) don't actually connect over stdio so
    # the change does not affect them, but the egg-ddg stdio fallback
    # registered for the LiteLLM→non-Anthropic path does — and the
    # ``SYSTEM_PROMPT_NUDGE`` that steers tool discovery is load-bearing on
    # the first turn. Force blocking-connect so DDG tools are reliably ready
    # before the first model call; ``setdefault`` preserves an operator-set
    # value if one is already on the env. Cheap to keep on for the in-process
    # path too (no-op there).
    os.environ.setdefault("MCP_CONNECTION_NONBLOCKING", "0")

    # Resolve cwd: explicit arg > EGG_REPO_PATH > SDK default (os.getcwd()).
    # Sandbox agents start at HOME (/home/egg) while the repo lives at
    # /home/egg/repos/<repo> (EGG_REPO_PATH).  Defaulting to EGG_REPO_PATH
    # lands the agent in the repo on its first tool call.  See #1993.
    resolved_cwd: str | None = str(cwd) if cwd else (os.environ.get("EGG_REPO_PATH") or None)

    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKError,
            CLINotFoundError,
            HookMatcher,
            PermissionResultAllow,
            PermissionResultDeny,
            ProcessError,
            ResultMessage,
            SystemMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
            query,
        )
        # CLIJSONDecodeError is a subclass of ClaudeSDKError, so it's
        # caught by the existing handler below — issue #2804 relies on
        # its error message preserving the ``exceeded maximum buffer
        # size`` marker so the consensus-wrapper can short-circuit
        # retry on this failure class. Tests pin that the marker
        # propagates into ``result.error`` once raised, and that the
        # consensus-wrapper grep matches ``_BUFFER_OVERFLOW_MARKER``;
        # stability of the marker against future ``claude-agent-sdk``
        # releases is NOT verified (the SDK could change the wording
        # at any minor bump and the wrapper would silently fall back
        # to burning the transient-crash retry budget). See #2823 for
        # the follow-up to pin or smoke-test the marker against the
        # installed SDK.
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

    # Build the can_use_tool callback for role-based file write blocking.
    # When active, Write/Edit/NotebookEdit calls to out-of-scope files are
    # denied and the error message is returned to the LLM as a tool result.
    tool_permission_callback = None
    if intercept_tools:
        from egg_agent.tool_interceptor import (
            check_file_write_permission,
            get_role_from_env,
        )

        role = get_role_from_env()
        if role:

            async def _check_tool_permission(
                tool_name: str, tool_input: dict[str, Any], context: Any
            ) -> Any:
                error = check_file_write_permission(tool_name, tool_input, role)
                if error:
                    logger.warning(
                        "Tool blocked by role restrictions",
                        event_type="tool_intercepted",
                        tool_name=tool_name,
                        tool_use_id=getattr(context, "tool_use_id", None),
                        agent_role=role,
                        error=error,
                    )
                    return PermissionResultDeny(message=error)
                return PermissionResultAllow()

            tool_permission_callback = _check_tool_permission

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model=model,
        cwd=resolved_cwd,
        env=env or {},
        # Read CLAUDE.md and settings.json from the filesystem so the agent
        # picks up sandbox rules (BRC protocol, egg-orch CLI, git safety, etc.).
        # Without this the SDK ignores all filesystem-based configuration.
        setting_sources=["project", "user"],
        disallowed_tools=disallowed,
        can_use_tool=tool_permission_callback,
        # Raise the stream-json reader buffer above the 1 MiB default so a
        # metadata-heavy Edit/Write result (CC attaches the whole original file
        # as non-model-bound transcript metadata) doesn't crash the reader on
        # large files. See _DEFAULT_SDK_MAX_BUFFER_BYTES above (#2884).
        max_buffer_size=_sdk_max_buffer_bytes(),
    )
    if max_turns is not None:
        options.max_turns = max_turns
    if system_prompt is not None:
        options.system_prompt = system_prompt
    if effort is not None:
        # The SDK passes this to the spawned CLI as ``--effort <level>``.
        options.effort = effort

    # --- Register in-process SDK MCP servers with egg's agent tools ---
    # Default-on since issue #1942.  Set ``EGG_MCP_TOOLS=false`` (or
    # ``0`` / ``no`` / ``off``) on the pod env to opt out — the kill
    # switch preserved from #1765's opt-in rollout.  See issue #1765
    # for the original design and #1942 for the default flip.
    #
    # The factory returns one SDK MCP server per namespace (keys: sdlc,
    # brc, phase, progress, task).  The Claude-visible tool name is
    # ``mcp__<server_key>__<raw_@tool_name>`` — keying each server by
    # its namespace is what produces the decision-7 visible names
    # ``mcp__sdlc__register_open_question`` etc.  A single aggregate
    # server would double-prefix (``mcp__egg__mcp__sdlc__...``).
    _mcp_flag_raw = os.environ.get("EGG_MCP_TOOLS", "").strip().lower()
    if _mcp_flag_raw not in ("false", "0", "no", "off"):
        try:
            from egg_agent_tools import (  # noqa: PLC0415
                SYSTEM_PROMPT_NUDGE,
                build_sandbox_mcp_server,
            )

            mcp_servers = build_sandbox_mcp_server()
            # ``mcp_servers`` is already a {namespace: server} dict;
            # merge into any caller-supplied mcp_servers on options.
            existing_servers = getattr(options, "mcp_servers", None) or {}
            options.mcp_servers = {**existing_servers, **mcp_servers}
            # Preserve any caller-supplied system_prompt; append the
            # nudge.  ``options.system_prompt`` is typed
            # ``str | SystemPromptPreset | SystemPromptFile | None`` —
            # we only know how to extend the plain-str case; for preset
            # / file forms the nudge is set as the full prompt (the
            # caller's preset/file remains accessible via the SDK's own
            # plumbing but SystemPromptPreset / SystemPromptFile
            # append semantics are not defined).
            existing_prompt = options.system_prompt
            if isinstance(existing_prompt, str) and existing_prompt:
                options.system_prompt = existing_prompt.rstrip() + "\n\n" + SYSTEM_PROMPT_NUDGE
            elif existing_prompt:
                # SystemPromptPreset / SystemPromptFile — we cannot
                # append to these forms.  Preserve the caller's prompt
                # and skip the nudge to avoid silent data loss.
                logger.warning(
                    "Cannot append MCP tool nudge to non-string system_prompt "
                    f"(type={type(existing_prompt).__name__}); MCP tools are registered but the nudge is omitted",
                    event_type="system",
                    event_subtype="mcp_nudge_skipped",
                )
            else:
                options.system_prompt = SYSTEM_PROMPT_NUDGE
            logger.info(
                "Registered egg MCP tools",
                event_type="system",
                event_subtype="mcp_tools_enabled",
                flag="EGG_MCP_TOOLS",
                namespaces=list(mcp_servers.keys()),
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "Failed to register egg MCP tools; continuing without them",
                event_type="system",
                event_subtype="mcp_tools_error",
                error=str(e),
            )

    # --- Predictive output cap for built-in CC tools (#2876) ---
    # This is model-context/cost discipline, NOT the buffer-crash fix (that is
    # max_buffer_size above, #2884). A whole-file Read returns the file content
    # *to the model* (the 1.1 MB pipelines.py ≈ ~275k tokens), and a whole-repo
    # content Grep dumps matches to the model — both wasteful. Built-in tools run
    # inside the CLI and can't be wrapped the way egg caps its own MCP @tool
    # payloads (#2805), so a PreToolUse hook predicts the volume from the inputs
    # and denies *before* the tool runs, telling the agent how to narrow the call
    # (offset/limit, head_limit, files_with_matches). Capping model-bound output
    # here also keeps the reader buffer from having to absorb it. Always-on;
    # disable via EGG_TOOL_OUTPUT_CAP=false.
    from egg_agent.tool_output_cap import (
        check_builtin_tool_output_risk,
        is_output_cap_disabled,
    )

    if not is_output_cap_disabled():
        # Resolves Read's relative file_paths the same way the tool will: prefer
        # the live cwd the SDK reports on each PreToolUse event, falling back to
        # the launch cwd if absent. Returns {} (no decision) when the call is
        # within bounds, so allowed calls fall through to the normal flow.
        async def _cap_builtin_tool_output(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> HookJSONOutput:
            reason = check_builtin_tool_output_risk(
                input_data.get("tool_name", ""),
                input_data.get("tool_input", {}) or {},
                input_data.get("cwd") or resolved_cwd,
            )
            if reason is None:
                return {}
            logger.info(
                "Predictive output cap denied built-in tool call",
                event_type="tool_intercepted",
                event_subtype="output_cap_deny",
                tool_name=input_data.get("tool_name"),
                tool_use_id=tool_use_id,
                reason=reason,
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        existing_hooks = getattr(options, "hooks", None) or {}
        pre_tool_use = list(existing_hooks.get("PreToolUse", []))
        pre_tool_use.append(HookMatcher(matcher="Read", hooks=[_cap_builtin_tool_output]))
        pre_tool_use.append(HookMatcher(matcher="Grep", hooks=[_cap_builtin_tool_output]))
        options.hooks = {**existing_hooks, "PreToolUse": pre_tool_use}

    # --- DuckDuckGo MCP fallback for the LiteLLM→non-Anthropic path (#2856) ---
    # On that path (signalled by ANTHROPIC_CUSTOM_MODEL_OPTION) the built-in
    # WebSearch/WebFetch tools silently no-op: LiteLLM's drop_params strips the
    # Anthropic server-tool schemas during Anthropic→OpenAI translation. A
    # PreToolUse hook installed by the sandbox entrypoint denies those calls and
    # points the model at mcp__ddg__search / mcp__ddg__fetch_content — the tools
    # exposed by the duckduckgo-mcp-server registered here. The Claude-visible
    # prefix is the dict key ("ddg"), so the tool names match the hook's reason.
    #
    # Skipped in private mode: the web tools are disallowed there (see above) and
    # the DDG server runs in-sandbox and must reach duckduckgo.com directly, which
    # the locked-down private-mode proxy forbids — so the external stdio server
    # would never connect. Only public mode (direct internet) can reach it.
    if not private_mode and os.environ.get("ANTHROPIC_CUSTOM_MODEL_OPTION"):
        existing_servers = getattr(options, "mcp_servers", None) or {}
        options.mcp_servers = {
            **existing_servers,
            "ddg": {"type": "stdio", "command": "duckduckgo-mcp-server"},
        }

        # Belt-and-suspenders: also register the WebSearch/WebFetch deny as a
        # programmatic PreToolUse hook, mirroring how disallowed_tools is set
        # both in settings.json and on ClaudeAgentOptions (the programmatic path
        # is more reliable). The sandbox entrypoint installs the same deny as a
        # filesystem hook (block-builtin-web-tools.sh); registering it here too
        # removes the single point of dependency on setting_sources loading
        # filesystem hooks. If both fire, the duplicate deny is harmless. The
        # reason text must stay in sync with that script.
        async def _deny_web_tools(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> HookJSONOutput:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "WebSearch and WebFetch do not work in this session "
                        "(routing through LiteLLM to a non-Anthropic model; the "
                        "Anthropic built-in tool schemas are stripped). Use "
                        "mcp__ddg__search instead of WebSearch, and "
                        "mcp__ddg__fetch_content instead of WebFetch. Retry your "
                        "operation with those tools."
                    ),
                }
            }

        existing_hooks = getattr(options, "hooks", None) or {}
        pre_tool_use = list(existing_hooks.get("PreToolUse", []))
        pre_tool_use.append(HookMatcher(matcher="WebSearch", hooks=[_deny_web_tools]))
        pre_tool_use.append(HookMatcher(matcher="WebFetch", hooks=[_deny_web_tools]))
        options.hooks = {**existing_hooks, "PreToolUse": pre_tool_use}

        logger.info(
            "Registered DuckDuckGo MCP fallback for LiteLLM web tools",
            event_type="system",
            event_subtype="ddg_mcp_enabled",
        )

    # --- Mid-turn operator message delivery (#3123) ---
    # One propose invocation under the BRC event-pump can run 30+ minutes
    # (a slice coder implements its whole task list in one turn), and the
    # message bus is only consulted between invocations — so an operator
    # correction sent via send_message lands after the contradicting work
    # is done. A throttled PostToolUse hook polls the bus during the turn
    # and surfaces new operator-authored messages as additionalContext.
    # Gated on pipeline context (EGG_PIPELINE_ID + EGG_AGENT_ROLE) so
    # non-pipeline egg_agent callers are untouched; EGG_MIDTURN_MESSAGES=
    # false is the rollback escape hatch.
    from egg_agent.midturn_messages import (
        MidturnMessagePoller,
        is_midturn_messages_disabled,
    )

    midturn_pipeline_id = (os.environ.get("EGG_PIPELINE_ID") or "").strip()
    midturn_role = (os.environ.get("EGG_AGENT_ROLE") or "").strip()
    if not is_midturn_messages_disabled() and midturn_pipeline_id and midturn_role:
        midturn_poller = MidturnMessagePoller(midturn_pipeline_id, midturn_role)

        async def _inject_midturn_messages(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> HookJSONOutput:
            # Fast-path: when the interval gate is clearly closed, skip
            # the thread boundary entirely. The matcher is None so this
            # hook fires on every tool call; under a chatty tool storm
            # the per-call cost (thread creation + GIL handoff) is
            # otherwise paid even when there is no work to do. The
            # lockless predicate may produce false positives under
            # concurrent calls; poll() re-checks atomically.
            if not midturn_poller.is_due_to_poll():
                return {}
            # poll() runs an egg-orch subprocess when the interval has
            # elapsed; keep it off the event loop. Between polls it is a
            # monotonic-clock comparison.
            context_block = await asyncio.to_thread(midturn_poller.poll)
            if not context_block:
                return {}
            logger.info(
                "Injected mid-turn operator messages",
                event_type="system",
                event_subtype="midturn_message_injection",
                tool_use_id=tool_use_id,
                block_chars=len(context_block),
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context_block,
                }
            }

        existing_hooks = getattr(options, "hooks", None) or {}
        # No matcher → fires for every tool; the poller's interval gate
        # makes that effectively free between actual bus polls.
        post_tool_use = list(existing_hooks.get("PostToolUse", []))
        post_tool_use.append(HookMatcher(matcher=None, hooks=[_inject_midturn_messages]))
        options.hooks = {**existing_hooks, "PostToolUse": post_tool_use}

    stdout_parts: list[str] = []
    actual_model: str | None = None
    result_meta: dict[str, Any] = {}

    # Log the effective cwd — when the caller did not pass one and
    # EGG_REPO_PATH is unset, the SDK inherits os.getcwd(), so log
    # that rather than None.  Keeps session-init lines diagnostically
    # useful (see #1954, #1993).
    effective_cwd = resolved_cwd or os.getcwd()
    logger.info(
        "Agent session init",
        event_type="system",
        event_subtype="init",
        model=model,
        effort=effort,
        cwd=effective_cwd,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        timeout=timeout,
        setting_sources=["project", "user"],
        disallowed_tools=disallowed,
        sdk="claude_agent_sdk",
    )

    try:
        async with asyncio.timeout(timeout):
            # can_use_tool requires streaming mode (AsyncIterable prompt).
            # Wrap the string prompt in a single-message async generator.
            if tool_permission_callback is not None:

                async def _prompt_iter(
                    _p: str = prompt,
                ) -> AsyncIterator[dict[str, Any]]:
                    yield {
                        "type": "user",
                        "message": {"role": "user", "content": _p},
                    }

                effective_prompt: str | AsyncIterator[dict[str, Any]] = _prompt_iter()
            else:
                effective_prompt = prompt
            stream = query(prompt=effective_prompt, options=options)
            async for message in stream:
                if isinstance(message, AssistantMessage):
                    if not actual_model and message.model:
                        actual_model = message.model
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            # Serialize tool input for logging (truncated)
                            try:
                                input_str = json.dumps(block.input, default=str)
                            except TypeError, ValueError:
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
                                    except TypeError, ValueError:
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
