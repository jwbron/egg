"""Tests for session-resume plumbing in ``egg_agent.client`` (#3200, slice-6).

Slice-6 / task-6-1 adds a ``resume=<session_id>`` option to
``run_agent_async`` so a BRC event-pump re-invocation can re-enter the
prior Claude Code session by id (the session_id already round-trips on
``AgentResult``).  This module (task-6-2) pins the three behaviours the
acceptance criteria require:

* **resume-by-id** — a non-empty ``resume`` id, *with* the
  ``EGG_SESSION_RESUME`` enable flag on, threads through to
  ``ClaudeAgentOptions.resume`` so the SDK re-enters that session.
* **cold-start fallback** — when no resumable session exists (``resume``
  absent / ``None`` / empty), the call seeds a *fresh* session from the
  protected-root prompt rather than raising.  "never a hard failure".
* **default-off** — two-fold and staged: ``resume`` defaults to ``None``
  (param level) AND a passed-in id is ignored unless ``EGG_SESSION_RESUME``
  is enabled (flag level), so the substrate ships dark ahead of the slice-8
  gate that drives it.

claude-agent-sdk is only installed inside sandbox containers, not in CI,
so — following the convention in ``test_client.py`` / ``test_main.py`` —
we install a compatible mock module when the real one is absent.  The
mock ``ClaudeAgentOptions`` carries the resume-family fields the real SDK
exposes (``resume``, ``continue_conversation``, ``session_id``,
``fork_session``) so the assertions read real attribute state on both
the real-SDK and mock-SDK paths.
"""

import asyncio
import inspect
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from egg_agent.client import run_agent_async

# ── Mock SDK types ──────────────────────────────────────────────────────────
try:
    from claude_agent_sdk import (  # noqa: F401
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
    )
except ImportError:

    @dataclass
    class TextBlock:  # type: ignore[no-redef]
        text: str
        type: str = "text"

    @dataclass
    class ToolUseBlock:  # type: ignore[no-redef]
        id: str
        name: str
        input: dict[str, Any] = field(default_factory=dict)

    @dataclass
    class ToolResultBlock:  # type: ignore[no-redef]
        tool_use_id: str
        content: str | list[dict[str, Any]] | None = None
        is_error: bool | None = None

    @dataclass
    class AssistantMessage:  # type: ignore[no-redef]
        content: list[Any] = field(default_factory=list)
        model: str | None = None

    @dataclass
    class UserMessage:  # type: ignore[no-redef]
        content: str | list[Any] = ""
        uuid: str | None = None
        parent_tool_use_id: str | None = None
        tool_use_result: dict[str, Any] | None = None

    @dataclass
    class ResultMessage:  # type: ignore[no-redef]
        subtype: str = "result"
        duration_ms: int = 0
        duration_api_ms: int = 0
        is_error: bool = False
        num_turns: int = 0
        session_id: str = ""
        stop_reason: str = ""
        total_cost_usd: float | None = None
        usage: Any = None
        result: str | None = None
        structured_output: Any = None

    class ClaudeSDKError(Exception):  # type: ignore[no-redef]
        pass

    class ProcessError(ClaudeSDKError):  # type: ignore[no-redef]
        pass

    class CLINotFoundError(ClaudeSDKError):  # type: ignore[no-redef]
        pass

    class CLIJSONDecodeError(ClaudeSDKError):  # type: ignore[no-redef]
        pass

    @dataclass
    class SystemMessage:  # type: ignore[no-redef]
        subtype: str = ""
        data: Any = None

    @dataclass
    class ClaudeAgentOptions:  # type: ignore[no-redef]
        permission_mode: str = ""
        model: str = ""
        cwd: str | None = None
        env: dict = field(default_factory=dict)
        max_turns: int | None = None
        system_prompt: str | None = None
        setting_sources: list[str] | None = None
        disallowed_tools: list[str] = field(default_factory=list)
        can_use_tool: Any = None
        max_buffer_size: int | None = None
        # Resume family — mirrors the fields the real SDK exposes so the
        # resume plumbing has a real attribute to write/read (#3200).
        resume: str | None = None
        continue_conversation: bool = False
        session_id: str | None = None
        fork_session: bool = False

    @dataclass
    class PermissionResultAllow:  # type: ignore[no-redef]
        behavior: str = "allow"

    @dataclass
    class PermissionResultDeny:  # type: ignore[no-redef]
        behavior: str = "deny"
        message: str = ""
        interrupt: bool = False

    @dataclass
    class ToolPermissionContext:  # type: ignore[no-redef]
        signal: Any = None
        suggestions: list = field(default_factory=list)
        tool_use_id: str | None = None
        agent_id: str | None = None

    @dataclass
    class HookMatcher:  # type: ignore[no-redef]
        matcher: str | None = None
        hooks: list[Any] = field(default_factory=list)
        timeout: float | None = None

    _mock_sdk = ModuleType("claude_agent_sdk")
    _mock_sdk.TextBlock = TextBlock  # type: ignore[attr-defined]
    _mock_sdk.ToolUseBlock = ToolUseBlock  # type: ignore[attr-defined]
    _mock_sdk.ToolResultBlock = ToolResultBlock  # type: ignore[attr-defined]
    _mock_sdk.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
    _mock_sdk.UserMessage = UserMessage  # type: ignore[attr-defined]
    _mock_sdk.ResultMessage = ResultMessage  # type: ignore[attr-defined]
    _mock_sdk.ProcessError = ProcessError  # type: ignore[attr-defined]
    _mock_sdk.CLINotFoundError = CLINotFoundError  # type: ignore[attr-defined]
    _mock_sdk.ClaudeSDKError = ClaudeSDKError  # type: ignore[attr-defined]
    _mock_sdk.CLIJSONDecodeError = CLIJSONDecodeError  # type: ignore[attr-defined]
    _mock_sdk.SystemMessage = SystemMessage  # type: ignore[attr-defined]
    _mock_sdk.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    _mock_sdk.PermissionResultAllow = PermissionResultAllow  # type: ignore[attr-defined]
    _mock_sdk.PermissionResultDeny = PermissionResultDeny  # type: ignore[attr-defined]
    _mock_sdk.ToolPermissionContext = ToolPermissionContext  # type: ignore[attr-defined]
    _mock_sdk.HookMatcher = HookMatcher  # type: ignore[attr-defined]
    _mock_sdk.query = None  # type: ignore[attr-defined]  # Patched per-test

    # In-process MCP server surface (only touched when EGG_MCP_TOOLS is on;
    # tests force it off, but provide stubs so an import can't fail).
    _mock_sdk.create_sdk_mcp_server = (  # type: ignore[attr-defined]
        lambda *, name, version, tools: {"__mock__": name, "version": version, "tools": tools}
    )
    _mock_sdk.tool = (  # type: ignore[attr-defined]
        lambda name, description, input_schema, annotations=None: lambda fn: fn
    )

    sys.modules["claude_agent_sdk"] = _mock_sdk


# ── Helpers ───────────────────────────────────────────────────────────────--
def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _mock_query_success(**kwargs):
    """A typical successful conversation (assistant text + result)."""
    yield AssistantMessage(content=[TextBlock(text="ok")], model="claude-opus-4-6")
    yield ResultMessage(
        subtype="result",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="sess-new",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage=None,
        result="done",
        structured_output=None,
    )


@pytest.fixture(autouse=True)
def _isolated_agent_env(monkeypatch):
    """Keep the resume tests on the plain SDK path with resume default-OFF.

    * ``EGG_MCP_TOOLS=false`` skips in-process MCP server registration.
    * ``intercept_tools=False`` is passed explicitly by every call, but
      clearing ``EGG_AGENT_ROLE`` keeps the role-interceptor branch dead
      even if a caller forgets.
    * ``EGG_SESSION_RESUME`` is cleared so the baseline is the staged-rollout
      default (OFF). Tests that want a warm resume opt in explicitly via
      ``monkeypatch.setenv("EGG_SESSION_RESUME", "1")`` — mirroring the real
      two-fold gate (non-empty session_id AND the enable flag).
    * ``EGG_CONTEXT_DISCIPLINE`` is also cleared: since #3200 slice-9 the master
      flag subsumes ``EGG_SESSION_RESUME`` (``session_resume_enabled()`` ORs it
      in), so an ambient value in the runner's env would silently flip resume ON
      and break the default-OFF baseline these tests assert.
    """
    monkeypatch.setenv("EGG_MCP_TOOLS", "false")
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    monkeypatch.delenv("EGG_PRIVATE_MODE", raising=False)
    monkeypatch.delenv("EGG_SESSION_RESUME", raising=False)
    monkeypatch.delenv("EGG_CONTEXT_DISCIPLINE", raising=False)


def _options_from(mock_query):
    """Return the ClaudeAgentOptions object handed to query()."""
    assert mock_query.called, "query() was never invoked"
    return mock_query.call_args.kwargs["options"]


# ── Tests ───────────────────────────────────────────────────────────────────
class TestResumePlumbing:
    """task-6-2: resume-by-id, cold-start fallback, default-off."""

    def test_default_off_signature(self):
        """``resume`` is opt-in: the parameter defaults to None.

        Asserting on the signature pins the param-level "default off"
        independently of the SDK so the staged-rollout guarantee can't
        silently flip to on. (The second half of default-off — the
        ``EGG_SESSION_RESUME`` enable flag — is exercised by
        ``test_resume_id_ignored_when_flag_disabled``.)
        """
        sig = inspect.signature(run_agent_async)
        assert "resume" in sig.parameters, "run_agent_async must expose a `resume` parameter"
        assert sig.parameters["resume"].default is None, (
            "resume must default to None (opt-in / default-off rollout)"
        )

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_resume_by_id_threads_into_options_when_enabled(self, mock_query, monkeypatch):
        """resume-by-id: a non-empty id + EGG_SESSION_RESUME on -> options.resume."""
        monkeypatch.setenv("EGG_SESSION_RESUME", "1")
        result = _run_async(
            run_agent_async("re-enter prior session", resume="sess-abc", intercept_tools=False)
        )
        assert result.success
        options = _options_from(mock_query)
        assert options.resume == "sess-abc"

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_resume_id_ignored_when_flag_disabled(self, mock_query):
        """Default-off (flag half): a resume id is ignored unless the flag is on.

        The autouse fixture leaves ``EGG_SESSION_RESUME`` unset (the rollout
        default), so even a valid session_id must NOT be forwarded — the run
        cold-starts instead. This is what lets the substrate ship dark ahead
        of the slice-8 gate.
        """
        result = _run_async(
            run_agent_async("staged-rollout off", resume="sess-abc", intercept_tools=False)
        )
        assert result.success
        options = _options_from(mock_query)
        assert getattr(options, "resume", None) is None

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_default_runs_fresh_without_resume(self, mock_query, monkeypatch):
        """No resume arg → fresh session even with the flag enabled."""
        monkeypatch.setenv("EGG_SESSION_RESUME", "1")
        result = _run_async(run_agent_async("fresh from protected root", intercept_tools=False))
        assert result.success
        options = _options_from(mock_query)
        assert getattr(options, "resume", None) is None

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_resume_none_cold_starts_without_raising(self, mock_query, monkeypatch):
        """Explicit resume=None cold-starts (fresh, never an error) even when enabled."""
        monkeypatch.setenv("EGG_SESSION_RESUME", "1")
        result = _run_async(run_agent_async("cold start", resume=None, intercept_tools=False))
        assert result.success
        options = _options_from(mock_query)
        assert getattr(options, "resume", None) is None

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_empty_session_id_cold_starts(self, mock_query, monkeypatch):
        """An empty/falsy session id means "no resumable session" → fresh.

        Covers the "no resumable session_id exists" branch of the cold-start
        fallback (first invocation / expired / consensus reset / pod death
        surface an empty id) without a hard failure. Holds even with the
        enable flag ON, so it isolates the empty-id guard from the flag gate.
        """
        monkeypatch.setenv("EGG_SESSION_RESUME", "1")
        result = _run_async(run_agent_async("cold start", resume="", intercept_tools=False))
        assert result.success
        options = _options_from(mock_query)
        # Empty string must not be forwarded as a real session to resume.
        assert not getattr(options, "resume", None)
