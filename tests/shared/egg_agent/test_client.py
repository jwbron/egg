"""Tests for egg_agent.client module."""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from egg_agent.client import (
    _BUFFER_OVERFLOW_MARKER,
    _DEFAULT_SDK_MAX_BUFFER_BYTES,
    _MAX_TOOL_CONTENT_LOG_LEN,
    _sdk_max_buffer_bytes,
    _truncate,
    run_agent,
    run_agent_async,
)
from egg_agent.result import AgentResult

# ── Mock SDK types ──────────────────────────────────────────────────────────
#
# claude-agent-sdk is only installed inside sandbox containers, not in CI.
# Create compatible mock types so tests run in both environments.

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
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
        usage: Any = None

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

    class ClaudeSDKError(Exception):
        pass

    class ProcessError(ClaudeSDKError):  # type: ignore[no-redef]
        pass

    class CLINotFoundError(ClaudeSDKError):  # type: ignore[no-redef]
        pass

    class CLIJSONDecodeError(ClaudeSDKError):  # type: ignore[no-redef]
        """Mirrors claude_agent_sdk._errors.CLIJSONDecodeError (issue #2804)."""

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

    # Hook types (#2856): client.py constructs HookMatcher at runtime for the
    # web-tool deny hook, so the mock must provide it with the matcher/hooks
    # fields the code and tests read. HookInput / HookJSONOutput / HookContext
    # are annotation-only in client.py (gated under TYPE_CHECKING there), so
    # they are never imported at runtime and need no mock.
    @dataclass
    class HookMatcher:  # type: ignore[no-redef]
        matcher: str | None = None
        hooks: list[Any] = field(default_factory=list)
        timeout: float | None = None

    # Install mock module so client.py's lazy import finds it
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
    _mock_sdk.query = None  # type: ignore[attr-defined]  # Patched in tests

    # Stubs for the in-process MCP server surface used by egg_agent_tools.
    # build_sandbox_mcp_server() lazily imports create_sdk_mcp_server and
    # _tool_compat.py imports tool — both from claude_agent_sdk.  Without
    # these stubs, EGG_MCP_TOOLS=true tests and the SDK-surface smoke
    # tests fail because the mock module is missing the expected symbols.
    def _mock_create_sdk_mcp_server(*, name: str, version: str, tools: list):  # type: ignore[no-untyped-def]
        return {"__mock__": name, "version": version, "tools": tools}

    _mock_sdk.create_sdk_mcp_server = _mock_create_sdk_mcp_server  # type: ignore[attr-defined]
    _mock_sdk.tool = lambda name, description, input_schema, annotations=None: lambda fn: fn  # type: ignore[attr-defined]

    sys.modules["claude_agent_sdk"] = _mock_sdk


def _run_async(coro):
    """Helper to run async code in tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _collect_async_iter(ait) -> list:
    """Drain an async iterator into a list."""
    items = []
    async for item in ait:
        items.append(item)
    return items


def _make_assistant_msg(text: str, usage: Any = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-opus-4-6-20250313",
        usage=usage,
    )


def _make_result_msg(
    result: str | None = "Final result",
    is_error: bool = False,
    total_cost_usd: float | None = 0.05,
    usage: Any = None,
) -> ResultMessage:
    return ResultMessage(
        subtype="result",
        duration_ms=5000,
        duration_api_ms=4000,
        is_error=is_error,
        num_turns=3,
        session_id="sess-123",
        stop_reason="end_turn",
        total_cost_usd=total_cost_usd,
        usage=usage,
        result=result,
        structured_output=None,
    )


async def _mock_query_success(**kwargs):
    """Async generator yielding a typical successful conversation."""
    yield _make_assistant_msg("Hello from Claude")
    yield _make_result_msg()


async def _mock_query_error(**kwargs):
    """Async generator yielding an error result."""
    yield _make_result_msg(result="Rate limit exceeded", is_error=True)


async def _mock_query_empty(**kwargs):
    """Async generator that yields no messages."""
    return
    yield  # Make this an async generator


# ── Tests ───────────────────────────────────────────────────────────────────


class TestRunAgentAsync:
    """Tests for run_agent_async."""

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_success(self, mock_query):
        """Test successful agent execution."""
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is True
        assert "Hello from Claude" in result.stdout
        assert result.returncode == 0
        assert result.cost_usd == 0.05
        assert result.num_turns == 3
        assert result.session_id == "sess-123"
        assert result.metadata == {"model": "claude-opus-4-6-20250313"}

    @patch("claude_agent_sdk.query", side_effect=_mock_query_error)
    def test_error_result(self, mock_query):
        """Test agent that returns an error."""
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is False
        assert result.error == "Rate limit exceeded"
        assert result.returncode == 1

    @patch("claude_agent_sdk.query", side_effect=_mock_query_empty)
    def test_empty_response(self, mock_query):
        """Test agent with no messages."""
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is True
        assert result.stdout == ""
        assert result.returncode == 0

    @patch("claude_agent_sdk.query")
    def test_on_output_callback(self, mock_query):
        """Test that on_output callback is called with text content."""
        captured: list[str] = []

        async def gen(**kwargs):
            yield _make_assistant_msg("chunk1")
            yield _make_assistant_msg("chunk2")
            yield _make_result_msg(result="done")

        mock_query.side_effect = gen

        _run_async(run_agent_async("test", on_output=captured.append))

        assert "chunk1" in captured
        assert "chunk2" in captured
        assert "done" in captured

    @patch("claude_agent_sdk.query")
    def test_exception_handling(self, mock_query):
        """Test that SDK exceptions are caught and returned as errors."""
        from claude_agent_sdk import ProcessError

        mock_query.side_effect = ProcessError("Process crashed")

        result = _run_async(run_agent_async("test prompt"))

        assert result.success is False
        assert "Process crashed" in result.error
        assert result.returncode == -1

    @patch("claude_agent_sdk.query")
    def test_timeout(self, mock_query):
        """Test that timeout produces a proper error result."""

        async def slow_gen(**kwargs):
            yield _make_assistant_msg("started")
            await asyncio.sleep(10)  # Will be cancelled by timeout
            yield _make_result_msg()

        mock_query.side_effect = slow_gen

        result = _run_async(run_agent_async("test prompt", timeout=1))

        assert result.success is False
        assert "Timed out" in result.error
        assert "started" in result.stdout

    @patch("claude_agent_sdk.query")
    def test_system_message_handling(self, mock_query):
        """Test that SystemMessage is processed without errors."""
        from claude_agent_sdk import SystemMessage

        async def gen(**kwargs):
            yield SystemMessage(subtype="heartbeat", data={"ts": 123})
            yield _make_assistant_msg("after system msg")
            yield _make_result_msg()

        mock_query.side_effect = gen

        result = _run_async(run_agent_async("test prompt"))

        assert result.success is True
        assert "after system msg" in result.stdout

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_structured_logging_init_and_result(self, mock_query):
        """Test that system/init and system/result log events are emitted."""
        with patch("egg_agent.client.logger") as mock_logger:
            _run_async(run_agent_async("test prompt"))

            # Verify system/init log
            init_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent session init"
            ]
            assert len(init_calls) == 1
            init_kwargs = init_calls[0].kwargs
            assert init_kwargs["event_type"] == "system"
            assert init_kwargs["event_subtype"] == "init"

            # Verify system/result log
            result_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent completed"
            ]
            assert len(result_calls) == 1
            result_kwargs = result_calls[0].kwargs
            assert result_kwargs["event_type"] == "system"
            assert result_kwargs["event_subtype"] == "result"
            assert result_kwargs["success"] is True

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_init_log_cwd_fallback(self, mock_query):
        """Test that cwd falls back to os.getcwd() when neither arg nor env is set."""
        with (
            patch("egg_agent.client.logger") as mock_logger,
            patch.dict(os.environ, {}, clear=False) as env,
        ):
            env.pop("EGG_REPO_PATH", None)
            _run_async(run_agent_async("test prompt"))

            init_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent session init"
            ]
            assert len(init_calls) == 1
            assert init_calls[0].kwargs["cwd"] == os.getcwd()

            # Verify cwd on the options object actually passed to query()
            options = mock_query.call_args.kwargs["options"]
            assert options.cwd is None  # SDK defaults to os.getcwd()

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_init_log_cwd_explicit(self, mock_query):
        """Test that cwd uses the provided value when passed explicitly."""
        with patch("egg_agent.client.logger") as mock_logger:
            _run_async(run_agent_async("test prompt", cwd="/tmp/test-dir"))

            init_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent session init"
            ]
            assert len(init_calls) == 1
            assert init_calls[0].kwargs["cwd"] == "/tmp/test-dir"

            # Verify cwd on the options object actually passed to query()
            options = mock_query.call_args.kwargs["options"]
            assert options.cwd == "/tmp/test-dir"

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_init_log_cwd_falls_back_to_egg_repo_path(self, mock_query):
        """When no cwd is passed, EGG_REPO_PATH should be used (see #1993)."""
        with (
            patch("egg_agent.client.logger") as mock_logger,
            patch.dict(os.environ, {"EGG_REPO_PATH": "/home/egg/repos/myrepo"}),
        ):
            _run_async(run_agent_async("test prompt"))

            init_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent session init"
            ]
            assert len(init_calls) == 1
            assert init_calls[0].kwargs["cwd"] == "/home/egg/repos/myrepo"

            # Verify cwd on the options object actually passed to query()
            options = mock_query.call_args.kwargs["options"]
            assert options.cwd == "/home/egg/repos/myrepo"

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_explicit_cwd_wins_over_egg_repo_path(self, mock_query):
        """Explicit cwd argument must take precedence over EGG_REPO_PATH."""
        with (
            patch("egg_agent.client.logger") as mock_logger,
            patch.dict(os.environ, {"EGG_REPO_PATH": "/home/egg/repos/myrepo"}),
        ):
            _run_async(run_agent_async("test prompt", cwd="/tmp/explicit"))

            init_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent session init"
            ]
            assert len(init_calls) == 1
            assert init_calls[0].kwargs["cwd"] == "/tmp/explicit"

            # Verify cwd on the options object actually passed to query()
            options = mock_query.call_args.kwargs["options"]
            assert options.cwd == "/tmp/explicit"

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_empty_egg_repo_path_treated_as_unset(self, mock_query):
        """EGG_REPO_PATH='' (set but empty) should behave like unset."""
        with (
            patch("egg_agent.client.logger") as mock_logger,
            patch.dict(os.environ, {"EGG_REPO_PATH": ""}),
        ):
            _run_async(run_agent_async("test prompt"))

            init_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent session init"
            ]
            assert len(init_calls) == 1
            # Empty EGG_REPO_PATH should fall back to os.getcwd()
            assert init_calls[0].kwargs["cwd"] == os.getcwd()

            # Verify cwd on the options object — should be None so SDK
            # defaults to os.getcwd(), not ""
            options = mock_query.call_args.kwargs["options"]
            assert options.cwd is None

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_mcp_connection_nonblocking_default(self, mock_query):
        """``run_agent_async`` must set ``MCP_CONNECTION_NONBLOCKING=0`` on os.environ
        before the SDK runs, so stdio MCP servers (e.g. the egg-ddg fallback
        on the LiteLLM→non-Anthropic path) finish their handshake before the
        first model turn — see #3137 for the SDK 0.2.x behavior shift."""
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("MCP_CONNECTION_NONBLOCKING", None)
            _run_async(run_agent_async("test prompt"))

            assert os.environ.get("MCP_CONNECTION_NONBLOCKING") == "0"

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_mcp_connection_nonblocking_preserves_operator_override(self, mock_query):
        """``setdefault`` semantics: if an operator already set the var (e.g.
        to ``1`` for debugging a slow MCP server), ``run_agent_async`` must
        not clobber it. Preserving operator intent is a hard requirement of
        the #3137 fix."""
        with patch.dict(os.environ, {"MCP_CONNECTION_NONBLOCKING": "1"}):
            _run_async(run_agent_async("test prompt"))

            assert os.environ.get("MCP_CONNECTION_NONBLOCKING") == "1"

    @patch("claude_agent_sdk.query", side_effect=_mock_query_error)
    def test_structured_logging_on_error(self, mock_query):
        """Test that system/result log is emitted on error paths."""
        with patch("egg_agent.client.logger") as mock_logger:
            result = _run_async(run_agent_async("test prompt"))

            assert result.success is False

            # Verify system/result log was still emitted
            result_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent completed"
            ]
            assert len(result_calls) == 1
            result_kwargs = result_calls[0].kwargs
            assert result_kwargs["success"] is False
            assert result_kwargs["error"] == "Rate limit exceeded"

    @patch("claude_agent_sdk.query")
    def test_structured_logging_on_timeout(self, mock_query):
        """Test that system/result log is emitted on timeout path."""

        async def slow_gen(**kwargs):
            yield _make_assistant_msg("started")
            await asyncio.sleep(10)
            yield _make_result_msg()

        mock_query.side_effect = slow_gen

        with patch("egg_agent.client.logger") as mock_logger:
            result = _run_async(run_agent_async("test prompt", timeout=1))

            assert result.success is False

            # Verify system/result log was emitted with expected fields
            result_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Agent completed"
            ]
            assert len(result_calls) == 1
            result_kwargs = result_calls[0].kwargs
            assert result_kwargs["event_type"] == "system"
            assert result_kwargs["event_subtype"] == "result"
            assert result_kwargs["success"] is False
            assert "Timed out" in result_kwargs["error"]
            # Schema includes metadata fields (None when no ResultMessage received)
            assert "session_id" in result_kwargs
            assert "cost_usd" in result_kwargs
            assert "num_turns" in result_kwargs
            assert "duration_ms" in result_kwargs

    def test_stdlib_logger_fallback_does_not_crash(self):
        """Test that the stdlib logger adapter handles arbitrary kwargs."""
        from egg_agent.client import _StdlibLoggerAdapter

        adapter = _StdlibLoggerAdapter("test-fallback")
        # Should not raise TypeError
        adapter.info("msg", event_type="system", event_subtype="init", model="x")
        adapter.debug("msg", event_type="system", data={"key": "val"})

    @patch("claude_agent_sdk.query")
    def test_structured_logging_tool_use(self, mock_query):
        """Test that tool_use events are logged with structured fields."""

        async def gen(**kwargs):
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="tool_123", name="Bash", input={"command": "ls -la"}),
                ],
                model="claude-opus-4-6-20250313",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tool_123",
                        content="file1.py\nfile2.py",
                        is_error=False,
                    ),
                ],
            )
            yield _make_assistant_msg("Done listing files")
            yield _make_result_msg()

        mock_query.side_effect = gen

        with patch("egg_agent.client.logger") as mock_logger:
            result = _run_async(run_agent_async("test prompt"))

            assert result.success is True

            # Verify tool_use log
            tool_use_calls = [
                c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "Tool call"
            ]
            assert len(tool_use_calls) == 1
            tu_kwargs = tool_use_calls[0].kwargs
            assert tu_kwargs["event_type"] == "tool_use"
            assert tu_kwargs["tool_name"] == "Bash"
            assert tu_kwargs["tool_use_id"] == "tool_123"
            assert "ls -la" in tu_kwargs["input"]

            # Verify tool_result log
            tool_result_calls = [
                c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "Tool result"
            ]
            assert len(tool_result_calls) == 1
            tr_kwargs = tool_result_calls[0].kwargs
            assert tr_kwargs["event_type"] == "tool_result"
            assert tr_kwargs["tool_use_id"] == "tool_123"
            assert tr_kwargs["is_error"] is False
            assert "file1.py" in tr_kwargs["content"]

            # Verify assistant text log
            text_calls = [
                c
                for c in mock_logger.info.call_args_list
                if c.args and c.args[0] == "Assistant message"
            ]
            assert len(text_calls) == 1
            txt_kwargs = text_calls[0].kwargs
            assert txt_kwargs["event_type"] == "assistant"
            assert "Done listing files" in txt_kwargs["text"]

    @patch("claude_agent_sdk.query")
    def test_structured_logging_tool_error(self, mock_query):
        """Test that tool errors are logged with is_error=True."""

        async def gen(**kwargs):
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="tool_456", name="Bash", input={"command": "bad_cmd"}),
                ],
                model="claude-opus-4-6-20250313",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="tool_456",
                        content="command not found: bad_cmd",
                        is_error=True,
                    ),
                ],
            )
            yield _make_result_msg()

        mock_query.side_effect = gen

        with patch("egg_agent.client.logger") as mock_logger:
            _run_async(run_agent_async("test prompt"))

            tool_result_calls = [
                c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "Tool result"
            ]
            assert len(tool_result_calls) == 1
            tr_kwargs = tool_result_calls[0].kwargs
            assert tr_kwargs["is_error"] is True

    @patch("claude_agent_sdk.query")
    def test_structured_logging_multiple_tool_calls(self, mock_query):
        """Test logging of multiple sequential tool calls."""

        async def gen(**kwargs):
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="Read", input={"file_path": "/tmp/a.py"}),
                ],
                model="claude-opus-4-6-20250313",
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="print('hi')")])
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t2",
                        name="Edit",
                        input={"file_path": "/tmp/a.py", "old_string": "hi", "new_string": "hello"},
                    ),
                ],
                model="claude-opus-4-6-20250313",
            )
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="OK")])
            yield _make_result_msg()

        mock_query.side_effect = gen

        with patch("egg_agent.client.logger") as mock_logger:
            _run_async(run_agent_async("test prompt"))

            tool_use_calls = [
                c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "Tool call"
            ]
            assert len(tool_use_calls) == 2
            assert tool_use_calls[0].kwargs["tool_name"] == "Read"
            assert tool_use_calls[1].kwargs["tool_name"] == "Edit"

            tool_result_calls = [
                c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "Tool result"
            ]
            assert len(tool_result_calls) == 2

    def test_truncate_within_limit(self):
        """Test that strings within the limit are returned unchanged."""
        short = "hello world"
        assert _truncate(short) == short

    def test_truncate_exceeds_limit(self):
        """Test that strings exceeding the limit are truncated with indicator."""
        long_str = "x" * (_MAX_TOOL_CONTENT_LOG_LEN + 500)
        result = _truncate(long_str)
        assert len(result) > _MAX_TOOL_CONTENT_LOG_LEN  # includes indicator
        assert result.startswith("x" * _MAX_TOOL_CONTENT_LOG_LEN)
        assert result.endswith(f"... ({len(long_str)} chars)")

    @patch("claude_agent_sdk.query")
    def test_structured_logging_non_string_tool_result(self, mock_query):
        """Test tool result logging with list content and None content."""

        async def gen(**kwargs):
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="t_list", name="Read", input={"path": "/tmp/a"}),
                ],
                model="claude-opus-4-6-20250313",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="t_list",
                        content=[{"type": "text", "text": "output"}],
                    ),
                ],
            )
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="t_none", name="Bash", input={"command": "true"}),
                ],
                model="claude-opus-4-6-20250313",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="t_none", content=None),
                ],
            )
            yield _make_result_msg()

        mock_query.side_effect = gen

        with patch("egg_agent.client.logger") as mock_logger:
            _run_async(run_agent_async("test prompt"))

            tool_result_calls = [
                c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "Tool result"
            ]
            assert len(tool_result_calls) == 2

            # List content should be JSON-serialized
            list_kwargs = tool_result_calls[0].kwargs
            assert list_kwargs["tool_use_id"] == "t_list"
            assert '"type"' in list_kwargs["content"]
            assert '"output"' in list_kwargs["content"]

            # None content should be empty string
            none_kwargs = tool_result_calls[1].kwargs
            assert none_kwargs["tool_use_id"] == "t_none"
            assert none_kwargs["content"] == ""

    @patch("claude_agent_sdk.query")
    def test_structured_logging_parallel_tool_calls(self, mock_query):
        """Test logging of parallel tool calls (multiple ToolUseBlocks in one message)."""

        async def gen(**kwargs):
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="p1", name="Bash", input={"command": "ls"}),
                    ToolUseBlock(id="p2", name="Read", input={"file_path": "/tmp/f"}),
                ],
                model="claude-opus-4-6-20250313",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(tool_use_id="p1", content="file1"),
                    ToolResultBlock(tool_use_id="p2", content="contents"),
                ],
            )
            yield _make_result_msg()

        mock_query.side_effect = gen

        with patch("egg_agent.client.logger") as mock_logger:
            _run_async(run_agent_async("test prompt"))

            tool_use_calls = [
                c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "Tool call"
            ]
            assert len(tool_use_calls) == 2
            assert tool_use_calls[0].kwargs["tool_name"] == "Bash"
            assert tool_use_calls[0].kwargs["tool_use_id"] == "p1"
            assert tool_use_calls[1].kwargs["tool_name"] == "Read"
            assert tool_use_calls[1].kwargs["tool_use_id"] == "p2"

            tool_result_calls = [
                c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "Tool result"
            ]
            assert len(tool_result_calls) == 2
            assert tool_result_calls[0].kwargs["tool_use_id"] == "p1"
            assert tool_result_calls[1].kwargs["tool_use_id"] == "p2"

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": "true"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_private_mode_true_disallows_web_tools(self, mock_query):
        """EGG_PRIVATE_MODE=true should pass disallowed_tools for web access."""
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        call_kwargs = mock_query.call_args.kwargs
        opts = call_kwargs["options"]
        assert opts.disallowed_tools == ["WebFetch", "WebSearch"]

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": "1"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_private_mode_1_disallows_web_tools(self, mock_query):
        """EGG_PRIVATE_MODE=1 should also block web tools."""
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        call_kwargs = mock_query.call_args.kwargs
        opts = call_kwargs["options"]
        assert opts.disallowed_tools == ["WebFetch", "WebSearch"]

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": "false"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_public_mode_no_disallowed_tools(self, mock_query):
        """EGG_PRIVATE_MODE=false should not block any tools."""
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        call_kwargs = mock_query.call_args.kwargs
        opts = call_kwargs["options"]
        assert opts.disallowed_tools == []

    @patch.dict(os.environ, {}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_private_mode_unset_no_disallowed_tools(self, mock_query):
        """When EGG_PRIVATE_MODE is not set, no tools should be blocked."""
        # Ensure the env var is truly absent
        env = os.environ.copy()
        env.pop("EGG_PRIVATE_MODE", None)
        with patch.dict(os.environ, env, clear=True):
            result = _run_async(run_agent_async("test prompt"))
            assert result.success is True
            call_kwargs = mock_query.call_args.kwargs
            opts = call_kwargs["options"]
            assert opts.disallowed_tools == []

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": "0"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_private_mode_0_no_disallowed_tools(self, mock_query):
        """EGG_PRIVATE_MODE=0 is set by sandbox_template.py for public mode."""
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        call_kwargs = mock_query.call_args.kwargs
        opts = call_kwargs["options"]
        assert opts.disallowed_tools == []

    @patch.dict(os.environ, {"EGG_PRIVATE_MODE": ""}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_private_mode_empty_string_no_disallowed_tools(self, mock_query):
        """Empty string EGG_PRIVATE_MODE should not block any tools."""
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        call_kwargs = mock_query.call_args.kwargs
        opts = call_kwargs["options"]
        assert opts.disallowed_tools == []


class TestDdgMcpFallback:
    """Issue #2856: on the LiteLLM→non-Anthropic public-mode path, run_agent_async
    registers the DuckDuckGo MCP server in ClaudeAgentOptions.mcp_servers so the
    PreToolUse hook's redirect targets (mcp__ddg__search / mcp__ddg__fetch_content)
    actually exist.

    These assert the real consumer contract — the server reaching
    ``options.mcp_servers`` — rather than a dict landing in settings.json (the
    original #2857 defect, where ``mcpServers`` was written to a key Claude Code
    ignores). ``EGG_MCP_TOOLS=false`` isolates these from the in-process egg tool
    registration so only the DDG block populates ``mcp_servers``.
    """

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "qwen3-coder-30b[1m]",
            "EGG_PRIVATE_MODE": "false",
            "EGG_MCP_TOOLS": "false",
        },
    )
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_ddg_registered_on_litellm_public_path(self, mock_query):
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        servers = getattr(opts, "mcp_servers", {}) or {}
        assert "ddg" in servers
        assert servers["ddg"] == {"type": "stdio", "command": "duckduckgo-mcp-server"}

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_ddg_not_registered_on_first_party_route(self, mock_query):
        # No ANTHROPIC_CUSTOM_MODEL_OPTION → first-party Claude → built-in tools
        # are live, so the DDG fallback must not be registered.
        env = os.environ.copy()
        env.pop("ANTHROPIC_CUSTOM_MODEL_OPTION", None)
        env["EGG_PRIVATE_MODE"] = "false"
        env["EGG_MCP_TOOLS"] = "false"
        with patch.dict(os.environ, env, clear=True):
            result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        servers = getattr(opts, "mcp_servers", {}) or {}
        assert "ddg" not in servers

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "qwen3-coder-30b[1m]",
            "EGG_PRIVATE_MODE": "true",
            "EGG_MCP_TOOLS": "false",
        },
    )
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_ddg_not_registered_in_private_mode(self, mock_query):
        # Private mode: the in-sandbox server cannot reach duckduckgo.com through
        # the locked-down proxy and the web tools are disallowed anyway, so the DDG
        # fallback must not be registered.
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        servers = getattr(opts, "mcp_servers", {}) or {}
        assert "ddg" not in servers
        assert opts.disallowed_tools == ["WebFetch", "WebSearch"]

    @staticmethod
    def _pre_tool_use_matchers(opts):
        hooks = getattr(opts, "hooks", None) or {}
        return [hm.matcher for hm in hooks.get("PreToolUse", [])]

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "qwen3-coder-30b[1m]",
            "EGG_PRIVATE_MODE": "false",
            "EGG_MCP_TOOLS": "false",
        },
    )
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_web_tool_deny_hook_registered_on_litellm_public_path(self, mock_query):
        # Belt-and-suspenders: the deny is registered programmatically via
        # ClaudeAgentOptions.hooks in addition to the filesystem hook installed
        # by the entrypoint, so it does not depend solely on setting_sources
        # loading filesystem hooks.
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        matchers = self._pre_tool_use_matchers(opts)
        assert "WebSearch" in matchers
        assert "WebFetch" in matchers

        # The hook must emit the modern PreToolUse deny shape and name both DDG
        # MCP tools, matching block-builtin-web-tools.sh.
        hooks = opts.hooks["PreToolUse"]
        web_search_hook = next(hm for hm in hooks if hm.matcher == "WebSearch")
        out = _run_async(web_search_hook.hooks[0]({}, "tool-1", None))
        decision = out["hookSpecificOutput"]
        assert decision["hookEventName"] == "PreToolUse"
        assert decision["permissionDecision"] == "deny"
        assert "decision" not in out
        reason = decision["permissionDecisionReason"]
        assert "mcp__ddg__search" in reason
        assert "mcp__ddg__fetch_content" in reason

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_web_tool_deny_hook_not_registered_on_first_party_route(self, mock_query):
        env = os.environ.copy()
        env.pop("ANTHROPIC_CUSTOM_MODEL_OPTION", None)
        env["EGG_PRIVATE_MODE"] = "false"
        env["EGG_MCP_TOOLS"] = "false"
        with patch.dict(os.environ, env, clear=True):
            result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        matchers = self._pre_tool_use_matchers(opts)
        assert "WebSearch" not in matchers
        assert "WebFetch" not in matchers

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "qwen3-coder-30b[1m]",
            "EGG_PRIVATE_MODE": "true",
            "EGG_MCP_TOOLS": "false",
        },
    )
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_web_tool_deny_hook_not_registered_in_private_mode(self, mock_query):
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        matchers = self._pre_tool_use_matchers(opts)
        assert "WebSearch" not in matchers
        assert "WebFetch" not in matchers


class TestBuiltinOutputCapHook:
    """Issue #2876: a PreToolUse hook predicts oversized built-in tool
    results (Read/Grep) and denies them as model-context/cost discipline,
    telling the agent how to narrow the call. Not the buffer-crash fix —
    that lives in the raised SDK reader buffer (#2884). Always-on (not
    route-gated); disabled via EGG_TOOL_OUTPUT_CAP=false.
    """

    @staticmethod
    def _matchers(opts):
        hooks = getattr(opts, "hooks", None) or {}
        return [hm.matcher for hm in hooks.get("PreToolUse", [])]

    @staticmethod
    def _hook_for(opts, matcher):
        hooks = opts.hooks["PreToolUse"]
        return next(hm for hm in hooks if hm.matcher == matcher).hooks[0]

    @patch.dict(os.environ, {"EGG_MCP_TOOLS": "false", "EGG_TOOL_OUTPUT_CAP": ""}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_read_and_grep_matchers_registered_by_default(self, mock_query):
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        matchers = self._matchers(opts)
        assert "Read" in matchers
        assert "Grep" in matchers

    @patch.dict(os.environ, {"EGG_TOOL_OUTPUT_CAP": "false"}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_kill_switch_removes_matchers(self, mock_query):
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        matchers = self._matchers(opts)
        assert "Read" not in matchers
        assert "Grep" not in matchers

    @patch.dict(os.environ, {"EGG_MCP_TOOLS": "false", "EGG_TOOL_OUTPUT_CAP": ""}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_read_hook_denies_large_file(self, mock_query, tmp_path):
        big = tmp_path / "big.py"
        big.write_bytes(b"x" * (300 * 1024))
        _run_async(run_agent_async("test prompt", cwd=str(tmp_path)))
        opts = mock_query.call_args.kwargs["options"]
        hook = self._hook_for(opts, "Read")
        out = _run_async(
            hook(
                {"tool_name": "Read", "tool_input": {"file_path": "big.py"}},
                "tool-1",
                None,
            )
        )
        decision = out["hookSpecificOutput"]
        assert decision["hookEventName"] == "PreToolUse"
        assert decision["permissionDecision"] == "deny"
        assert "limit" in decision["permissionDecisionReason"]

    @patch.dict(os.environ, {"EGG_MCP_TOOLS": "false", "EGG_TOOL_OUTPUT_CAP": ""}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_read_hook_allows_small_file(self, mock_query, tmp_path):
        small = tmp_path / "small.py"
        small.write_bytes(b"x" * 1024)
        _run_async(run_agent_async("test prompt", cwd=str(tmp_path)))
        opts = mock_query.call_args.kwargs["options"]
        hook = self._hook_for(opts, "Read")
        out = _run_async(
            hook(
                {"tool_name": "Read", "tool_input": {"file_path": "small.py"}},
                "tool-1",
                None,
            )
        )
        assert out == {}

    @patch.dict(os.environ, {"EGG_MCP_TOOLS": "false", "EGG_TOOL_OUTPUT_CAP": ""}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_grep_hook_denies_unbounded_content_grep(self, mock_query):
        _run_async(run_agent_async("test prompt"))
        opts = mock_query.call_args.kwargs["options"]
        hook = self._hook_for(opts, "Grep")
        out = _run_async(
            hook(
                {
                    "tool_name": "Grep",
                    "tool_input": {"pattern": "x", "output_mode": "content"},
                },
                "tool-1",
                None,
            )
        )
        decision = out["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "head_limit" in decision["permissionDecisionReason"]


class TestSdkReaderBuffer:
    """Issue #2884: egg raises the Agent SDK stream-json reader's buffer above
    the 1 MiB default so a metadata-heavy Edit/Write result (CC attaches the
    whole original file as non-model-bound transcript metadata) doesn't crash
    the reader on large files. Tunable via EGG_SDK_MAX_BUFFER_BYTES.
    """

    @patch.dict(os.environ, {"EGG_MCP_TOOLS": "false"}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_max_buffer_size_wired_by_default(self, mock_query):
        os.environ.pop("EGG_SDK_MAX_BUFFER_BYTES", None)
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        opts = mock_query.call_args.kwargs["options"]
        assert opts.max_buffer_size == _DEFAULT_SDK_MAX_BUFFER_BYTES
        # The default must clear the 1 MiB SDK default that crashes on #2884.
        assert opts.max_buffer_size > 1024 * 1024

    @patch.dict(os.environ, {"EGG_MCP_TOOLS": "false", "EGG_SDK_MAX_BUFFER_BYTES": "8388608"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_max_buffer_size_configurable_via_env(self, mock_query):
        _run_async(run_agent_async("test prompt"))
        opts = mock_query.call_args.kwargs["options"]
        assert opts.max_buffer_size == 8 * 1024 * 1024

    def test_env_resolver_rejects_invalid_values(self):
        # Each bad value gets its own dedup-state reset so the per-value warning
        # actually fires; a stale entry from a prior iteration would silently
        # turn this into "passes when the function changes to skip warn".
        import egg_agent.client as client_mod

        for bad in ("not-a-number", "0", "-5", "2mb"):
            client_mod._warned_sdk_buffer_values.clear()
            with (
                patch.dict(os.environ, {"EGG_SDK_MAX_BUFFER_BYTES": bad}),
                patch("egg_agent.client.logger") as mock_logger,
            ):
                assert _sdk_max_buffer_bytes() == _DEFAULT_SDK_MAX_BUFFER_BYTES
                # The docstring promises invalid values are *logged* and ignored;
                # without this assertion a regression that demoted the warning
                # (or dropped it) would still pass the silent-fallback check.
                assert mock_logger.warning.called, (
                    f"expected a logger.warning for invalid value {bad!r}"
                )

    def test_env_resolver_accepts_valid_override(self):
        with patch.dict(os.environ, {"EGG_SDK_MAX_BUFFER_BYTES": "16777216"}):
            assert _sdk_max_buffer_bytes() == 16 * 1024 * 1024

    def test_env_resolver_clamps_absurdly_large_value(self):
        """An operator typo (stray suffix-conversion like 34359738368000 ≈ 34 TiB)
        must be clamped to the 1 GiB hard upper bound rather than silently
        accepted — an effectively-unbounded reader buffer could OOM the
        container on a runaway or malformed stream (#2884 review feedback).
        """
        import egg_agent.client as client_mod

        client_mod._warned_sdk_buffer_values.clear()
        with (
            patch.dict(os.environ, {"EGG_SDK_MAX_BUFFER_BYTES": "34359738368000"}),
            patch("egg_agent.client.logger") as mock_logger,
        ):
            assert _sdk_max_buffer_bytes() == client_mod._MAX_SDK_MAX_BUFFER_BYTES
            assert mock_logger.warning.called

    def test_env_resolver_warning_dedups_per_value(self):
        """A steady bad value warns once, not on every resolver call — the
        resolver runs on every ``run_agent_async`` invocation, so an
        unconditional warning would spam an operator-facing log line per
        agent spawn (#2884 review feedback, mirroring tool_output_cap)."""
        import egg_agent.client as client_mod

        client_mod._warned_sdk_buffer_values.clear()
        with (
            patch.dict(os.environ, {"EGG_SDK_MAX_BUFFER_BYTES": "not-a-number"}),
            patch("egg_agent.client.logger") as mock_logger,
        ):
            for _ in range(5):
                assert _sdk_max_buffer_bytes() == _DEFAULT_SDK_MAX_BUFFER_BYTES
            assert mock_logger.warning.call_count == 1


class TestBufferOverflowErrorHandling:
    """Issue #2804: when the SDK raises CLIJSONDecodeError on a buffer
    overflow, the agent must return a structured failure with the
    overflow marker preserved in ``error`` — the consensus-wrapper
    greps for that string to short-circuit retry. With the reader buffer
    raised (#2884) this is now a rare backstop, but must still be clean.
    """

    @patch("claude_agent_sdk.query")
    def test_buffer_overflow_returns_failure_with_marker(self, mock_query):
        from claude_agent_sdk import CLIJSONDecodeError

        mock_query.side_effect = CLIJSONDecodeError(
            f"JSON message {_BUFFER_OVERFLOW_MARKER} of 1048576 bytes..."
        )

        result = _run_async(run_agent_async("test prompt"))

        assert result.success is False
        assert result.returncode == -1
        # Marker must appear verbatim in ``error`` so the wrapper's grep
        # in is_buffer_overflow() matches. Referencing the module-level
        # constant here means a rename in client.py drives a test
        # failure rather than silently desyncing from the wrapper grep.
        assert _BUFFER_OVERFLOW_MARKER in result.error


class TestToolInterception:
    """Tests for can_use_tool-based tool interception."""

    @patch.dict(os.environ, {"EGG_AGENT_ROLE": "tester"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_intercept_tools_sets_can_use_tool(self, mock_query):
        """When intercept_tools=True and role is set, can_use_tool should be set."""
        _run_async(run_agent_async("test prompt"))
        call_kwargs = mock_query.call_args.kwargs
        opts = call_kwargs["options"]
        assert opts.can_use_tool is not None

    @patch.dict(os.environ, {"EGG_AGENT_ROLE": "tester"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_intercept_tools_callback_blocks_disallowed_write(self, mock_query):
        """Callback should return PermissionResultDeny for out-of-scope writes."""
        _run_async(run_agent_async("test prompt"))
        callback = mock_query.call_args.kwargs["options"].can_use_tool
        # Tester writing to source code should be blocked
        result = _run_async(
            callback("Write", {"file_path": "/home/egg/repos/egg/src/main.py"}, None)
        )
        assert result.behavior == "deny"
        assert "BLOCKED" in result.message
        assert "tester" in result.message

    @patch.dict(os.environ, {"EGG_AGENT_ROLE": "tester"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_intercept_tools_callback_allows_in_scope_write(self, mock_query):
        """Callback should return PermissionResultAllow for in-scope writes."""
        _run_async(run_agent_async("test prompt"))
        callback = mock_query.call_args.kwargs["options"].can_use_tool
        # Tester writing to test files should be allowed
        result = _run_async(
            callback("Write", {"file_path": "/home/egg/repos/egg/tests/test_foo.py"}, None)
        )
        assert result.behavior == "allow"

    @patch.dict(os.environ, {"EGG_AGENT_ROLE": "tester"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_intercept_tools_callback_allows_non_write_tools(self, mock_query):
        """Callback should allow Read, Bash, etc. (non-write tools)."""
        _run_async(run_agent_async("test prompt"))
        callback = mock_query.call_args.kwargs["options"].can_use_tool
        result = _run_async(callback("Bash", {"command": "ls"}, None))
        assert result.behavior == "allow"

    @patch.dict(os.environ, {}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_intercept_tools_no_role_no_callback(self, mock_query):
        """When EGG_AGENT_ROLE is not set, can_use_tool should be None."""
        env = os.environ.copy()
        env.pop("EGG_AGENT_ROLE", None)
        with patch.dict(os.environ, env, clear=True):
            _run_async(run_agent_async("test prompt"))
            call_kwargs = mock_query.call_args.kwargs
            opts = call_kwargs["options"]
            assert opts.can_use_tool is None

    @patch.dict(os.environ, {"EGG_AGENT_ROLE": "tester"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_intercept_tools_logs_tool_use_id(self, mock_query):
        """Blocked tool calls should log tool_use_id from context."""
        _run_async(run_agent_async("test prompt"))
        callback = mock_query.call_args.kwargs["options"].can_use_tool
        # Create a context with tool_use_id
        from claude_agent_sdk import ToolPermissionContext

        ctx = ToolPermissionContext(tool_use_id="toolu_abc123")
        with patch("egg_agent.client.logger") as mock_logger:
            result = _run_async(
                callback(
                    "Write",
                    {"file_path": "/home/egg/repos/egg/src/main.py"},
                    ctx,
                )
            )
            assert result.behavior == "deny"
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args.kwargs
            assert call_kwargs["tool_use_id"] == "toolu_abc123"

    @patch.dict(os.environ, {"EGG_AGENT_ROLE": "tester"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_intercept_tools_disabled_no_callback(self, mock_query):
        """When intercept_tools=False, can_use_tool should be None."""
        _run_async(run_agent_async("test prompt", intercept_tools=False))
        call_kwargs = mock_query.call_args.kwargs
        opts = call_kwargs["options"]
        assert opts.can_use_tool is None

    @patch.dict(os.environ, {"EGG_AGENT_ROLE": "tester"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_intercept_tools_wraps_prompt_as_async_iterable(self, mock_query):
        """When can_use_tool is set, prompt must be an AsyncIterable (streaming mode)."""
        from collections.abc import AsyncIterator

        _run_async(run_agent_async("hello agent"))
        call_kwargs = mock_query.call_args.kwargs
        prompt = call_kwargs["prompt"]
        assert isinstance(prompt, AsyncIterator)
        # mock doesn't consume the generator, so we can drain it here
        messages = _run_async(_collect_async_iter(prompt))
        assert len(messages) == 1
        assert messages[0]["type"] == "user"
        assert messages[0]["message"]["role"] == "user"
        assert messages[0]["message"]["content"] == "hello agent"

    @patch.dict(os.environ, {}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_no_callback_passes_string_prompt(self, mock_query):
        """Without can_use_tool, prompt should remain a plain string."""
        env = os.environ.copy()
        env.pop("EGG_AGENT_ROLE", None)
        with patch.dict(os.environ, env, clear=True):
            _run_async(run_agent_async("hello agent"))
            call_kwargs = mock_query.call_args.kwargs
            assert call_kwargs["prompt"] == "hello agent"


class TestRunAgentSync:
    """Tests for run_agent synchronous wrapper."""

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_sync_wrapper(self, mock_query):
        """Test that run_agent returns the same result as run_agent_async."""
        result = run_agent("test prompt")

        assert result.success is True
        assert "Hello from Claude" in result.stdout


# ── EGG_MCP_TOOLS wire-up tests (issues #1765, #1942) ──────────────────────
class TestMcpToolsFlag:
    """Capture ClaudeAgentOptions kwargs via a patched constructor and
    verify the gating behaviour of EGG_MCP_TOOLS.  Default is on since
    issue #1942 — set the env to a falsy value to opt out."""

    def _patch_options(self, captured: list):
        from claude_agent_sdk import ClaudeAgentOptions as _Real

        class _Capturing(_Real):  # type: ignore[misc,valid-type]
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                captured.append(self)

        return _Capturing

    def _require_tools(self):
        try:
            from egg_agent_tools import SYSTEM_PROMPT_NUDGE

            return SYSTEM_PROMPT_NUDGE
        except ImportError:
            pytest.skip("egg_agent_tools not importable")

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_mcp_tools_default_on_when_env_unset(self, mock_query, monkeypatch):
        monkeypatch.delenv("EGG_MCP_TOOLS", raising=False)
        nudge = self._require_tools()
        captured: list = []
        capturing_cls = self._patch_options(captured)
        with patch("claude_agent_sdk.ClaudeAgentOptions", capturing_cls):
            _run_async(run_agent_async("hi"))
        assert len(captured) == 1
        opts = captured[0]
        assert getattr(opts, "mcp_servers", None)
        assert opts.system_prompt == nudge

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", "No"])
    def test_mcp_tools_opt_out_explicit_falsy(self, mock_query, monkeypatch, value):
        monkeypatch.setenv("EGG_MCP_TOOLS", value)
        captured: list = []
        capturing_cls = self._patch_options(captured)
        with patch("claude_agent_sdk.ClaudeAgentOptions", capturing_cls):
            _run_async(run_agent_async("hi"))
        assert len(captured) == 1
        opts = captured[0]
        mcp = getattr(opts, "mcp_servers", None)
        assert not mcp

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_mcp_tools_flag_on_preserves_caller_prompt(self, mock_query, monkeypatch):
        monkeypatch.setenv("EGG_MCP_TOOLS", "true")
        nudge = self._require_tools()
        captured: list = []
        capturing_cls = self._patch_options(captured)
        with patch("claude_agent_sdk.ClaudeAgentOptions", capturing_cls):
            _run_async(run_agent_async("hi", system_prompt="existing-prompt"))
        opts = captured[0]
        assert getattr(opts, "mcp_servers", None)
        assert opts.system_prompt.endswith(nudge)
        assert opts.system_prompt.startswith("existing-prompt")

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_mcp_tools_flag_on_no_caller_prompt(self, mock_query, monkeypatch):
        monkeypatch.setenv("EGG_MCP_TOOLS", "yes")
        nudge = self._require_tools()
        captured: list = []
        capturing_cls = self._patch_options(captured)
        with patch("claude_agent_sdk.ClaudeAgentOptions", capturing_cls):
            _run_async(run_agent_async("hi"))
        assert captured[0].system_prompt == nudge


class TestCanUseToolPassesMcpNames:
    """The can_use_tool callback only targets Write/Edit/NotebookEdit.
    MCP tool names (mcp__*) must pass through with PermissionResultAllow."""

    def test_can_use_tool_passes_mcp_names(self, monkeypatch):
        monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        from egg_agent.tool_interceptor import (
            check_file_write_permission,
            get_role_from_env,
        )

        role = get_role_from_env()
        assert role == "coder"

        async def _check(tool_name, tool_input, context):
            err = check_file_write_permission(tool_name, tool_input, role)
            return PermissionResultDeny(message=err) if err else PermissionResultAllow()

        for name in (
            "mcp__brc__propose",
            "mcp__sdlc__register_open_question",
            "mcp__phase__get_context",
            "mcp__task__complete",
            "mcp__progress__emit",
        ):
            assert isinstance(_run_async(_check(name, {}, object())), PermissionResultAllow), (
                f"MCP tool denied: {name}"
            )


class TestMidturnMessageHook:
    """Issue #3123: pipeline sessions get a PostToolUse hook that polls the
    message bus mid-turn and injects new operator messages, so a correction
    can land inside a 30+ minute propose invocation instead of after it.
    Gated on pipeline context (EGG_PIPELINE_ID + EGG_AGENT_ROLE) with the
    EGG_MIDTURN_MESSAGES=false escape hatch.
    """

    @staticmethod
    def _post_tool_use_hooks(mock_query):
        opts = mock_query.call_args.kwargs["options"]
        hooks = getattr(opts, "hooks", None) or {}
        return hooks.get("PostToolUse", [])

    @patch.dict(
        os.environ,
        {
            "EGG_PIPELINE_ID": "pipeline-test",
            "EGG_AGENT_ROLE": "coder",
            "EGG_MCP_TOOLS": "false",
        },
    )
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_hook_registered_in_pipeline_session(self, mock_query):
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        post_hooks = self._post_tool_use_hooks(mock_query)
        assert len(post_hooks) == 1
        # No matcher → fires on every tool (the poller's interval gate
        # makes that effectively free between actual bus polls).
        assert post_hooks[0].matcher is None
        assert len(post_hooks[0].hooks) == 1

    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_hook_not_registered_outside_pipeline(self, mock_query):
        env = os.environ.copy()
        env.pop("EGG_PIPELINE_ID", None)
        env.pop("EGG_AGENT_ROLE", None)
        env["EGG_MCP_TOOLS"] = "false"
        with patch.dict(os.environ, env, clear=True):
            result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        assert self._post_tool_use_hooks(mock_query) == []

    @patch.dict(
        os.environ,
        {
            "EGG_PIPELINE_ID": "pipeline-test",
            "EGG_AGENT_ROLE": "coder",
            "EGG_MIDTURN_MESSAGES": "false",
            "EGG_MCP_TOOLS": "false",
        },
    )
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_escape_hatch_disables_hook(self, mock_query):
        result = _run_async(run_agent_async("test prompt"))
        assert result.success is True
        assert self._post_tool_use_hooks(mock_query) == []


class TestExitCodeSurfaceExcludesExTempfail:
    """Back the consensus-wrapper one-shot arm's exit-75 reservation.

    The one-shot event arm in ``orchestrator/consensus_wrapper.py`` reserves
    exit code 75 (``EX_TEMPFAIL``) for the "freshness re-check inconclusive"
    outcome and passes the agent's own rc through raw on the fresh path
    (``exit "$one_shot_rc"``). For the slice-3 supervisor to distinguish
    "re-derive next-action" (75) from a genuine agent outcome, the agent's
    exit-code surface must never itself emit 75 -- otherwise the two meanings
    collide. The wrapper documents this as a comment-level invariant; this
    test backs it with a real assertion (reviewer note, PR #3167).

    ``egg_agent`` runs the SDK in-process, so its exit-code surface is a
    bounded set of ``AgentResult.returncode`` literals set in ``run_agent`` /
    ``run_agent_async`` -- there is no subprocess rc passthrough. We scan the
    module source for every integer ``returncode`` literal -- both keyword
    (``returncode=75``) and positional (``AgentResult(False, "", "", 75)``)
    construction -- and assert the set excludes 75; a future change that
    introduced ``returncode=75`` anywhere in the agent path would fail here,
    flagging the collision before it ships. (A dynamic value such as
    ``returncode=some_var`` is inherently invisible to a literal scan; the
    realistic regression -- a hard-coded 75 -- is what this catches.)
    """

    # EX_TEMPFAIL from sysexits.h; the value reserved by the one-shot arm.
    _EX_TEMPFAIL = 75

    # Positional index of ``returncode`` in the ``AgentResult`` dataclass
    # signature (success, stdout, stderr, returncode, ...). Kept in sync with
    # shared/egg_agent/result.py so the scan catches positional construction.
    _RETURNCODE_POSITIONAL_INDEX = 3

    @staticmethod
    def _int_literal(value) -> int | None:
        """Return the int value of a literal AST node, or None if not a literal.

        Handles ``0``/``1`` (Constant) and ``-1`` (UnaryOp(USub, Constant), since
        the unary minus is a separate node). Returns None for dynamic values
        (``returncode=some_var``), which a literal scan inherently cannot resolve.
        """
        import ast

        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            return value.value
        if (
            isinstance(value, ast.UnaryOp)
            and isinstance(value.op, ast.USub)
            and isinstance(value.operand, ast.Constant)
            and isinstance(value.operand.value, int)
        ):
            return -value.operand.value
        return None

    def _returncode_literals(self) -> set[int]:
        """All integer ``returncode`` literals assigned in client.py.

        Catches both keyword (``returncode=75``) and positional
        (``AgentResult(False, "", "", 75)``) construction so the exclusion can't
        be silently defeated by switching call style. Dynamic values
        (``returncode=some_var``) are inherently invisible to a literal scan and
        are not the realistic regression this guards against.
        """
        import ast
        import inspect

        import egg_agent.client as client_mod

        source = inspect.getsource(client_mod)
        tree = ast.parse(source)
        codes: set[int] = set()
        for node in ast.walk(tree):
            # Keyword form: ``returncode=<int>`` anywhere in the module.
            if isinstance(node, ast.keyword) and node.arg == "returncode":
                literal = self._int_literal(node.value)
                if literal is not None:
                    codes.add(literal)
            # Positional form: ``AgentResult(success, stdout, stderr, <int>, ...)``.
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AgentResult"
                and len(node.args) > self._RETURNCODE_POSITIONAL_INDEX
            ):
                literal = self._int_literal(node.args[self._RETURNCODE_POSITIONAL_INDEX])
                if literal is not None:
                    codes.add(literal)
        return codes

    def test_returncode_literals_exclude_ex_tempfail(self):
        codes = self._returncode_literals()
        # Sanity: the scan actually found the known surface, so an empty set
        # can't make the exclusion pass vacuously.
        assert codes, "AST scan found no returncode= literals in egg_agent.client"
        assert self._EX_TEMPFAIL not in codes, (
            f"egg_agent.client emits returncode={self._EX_TEMPFAIL} (EX_TEMPFAIL), "
            "which collides with the one-shot arm's reserved freshness-inconclusive "
            "exit code (consensus_wrapper.py). See PR #3167."
        )

    def test_main_propagates_returncode_without_remapping_to_ex_tempfail(self):
        """``main()`` returns ``result.returncode`` verbatim -- no remap to 75.

        ``__main__`` does ``sys.exit(main())``, so whatever ``main()`` returns
        is the process exit code (negatives wrap, e.g. -1 -> 255). This pins
        that ``main()`` neither invents 75 nor alters the rc the agent path
        produced, keeping the wrapper's reservation honest end-to-end.
        """
        from unittest.mock import patch as _patch

        from egg_agent.__main__ import main

        for rc in (-1, 0, 1):
            mock_result = AgentResult(success=rc == 0, stdout="", stderr="", returncode=rc)
            with (
                _patch("egg_agent.__main__.run_agent", return_value=mock_result),
                _patch("sys.argv", ["egg_agent", "test prompt"]),
            ):
                returned = main()
            assert returned == rc, f"main() remapped agent rc {rc} to {returned}"
            assert returned != self._EX_TEMPFAIL, (
                f"main() returned EX_TEMPFAIL ({self._EX_TEMPFAIL}) for agent rc {rc}"
            )


# ── Window-occupancy capture (#3200 slice-1, AC-1) ───────────────────────────
#
# Contract under test (plan task-1-1 / task-1-2/3); field name ``window_occupancy``
# matches shared/egg_agent/result.py:
#   * AgentResult carries an OPTIONAL cumulative window-occupancy field,
#     default None, non-breaking for existing constructors.
#   * window_occupancy == input_tokens + cache_read_input_tokens
#     + cache_creation_input_tokens  -- i.e. WINDOW occupancy, NOT billed/
#     effective input and NOT output_tokens. (A cache-dominated turn bills
#     almost nothing but the window is nearly full; the reseed trigger must see
#     the full window.)
#   * Computation is defensive: absent/non-dict usage -> None (no raise);
#     missing/None/non-int sub-fields count as 0.
#   * Every AgentResult build site on the ResultMessage path (success AND
#     error) is populated.
#   * An optional ``token_usage`` breakout dict mirrors the raw components
#     (input/cache_read/cache_creation/output) for the phase-10 measurement
#     surfaces; None exactly when occupancy is None.
#
# Written against the observable AgentResult surface (not the private
# _compute_occupancy helper) so they pin the behaviour, not the factoring.


def _usage(
    *,
    input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    """Build a Claude-shaped usage dict with only the requested keys present.

    Keys whose value is None are omitted entirely, so the same helper covers
    both the "key absent" and "key explicitly None" partial-usage cases.
    """
    raw = {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "output_tokens": output_tokens,
    }
    return {k: v for k, v in raw.items() if v is not None}


class TestAgentResultOccupancyField:
    """task-1-1: the dataclass field itself (default + non-breaking)."""

    def test_window_occupancy_defaults_to_none(self):
        """A freshly built AgentResult has window_occupancy == None by default."""
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.window_occupancy is None

    def test_token_usage_defaults_to_none(self):
        """The raw-breakout field also defaults to None (non-breaking)."""
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.token_usage is None

    def test_existing_positional_construction_still_builds(self):
        """The four legacy positional args still construct without occupancy.

        Pins the non-breaking requirement: the new fields must be appended as
        optional trailing fields, never inserted among the existing ones.
        """
        result = AgentResult(True, "out", "err", 0)
        assert result.success is True
        assert result.stdout == "out"
        assert result.window_occupancy is None
        assert result.token_usage is None

    def test_window_occupancy_is_settable(self):
        """The field accepts an int when supplied explicitly."""
        result = AgentResult(
            success=True, stdout="", stderr="", returncode=0, window_occupancy=12_345
        )
        assert result.window_occupancy == 12_345


class TestOccupancyCapture:
    """task-1-2/task-1-3: client threads occupancy off the final turn's usage.

    Occupancy is sourced from the last ``AssistantMessage.usage`` (the resident
    window for that turn), NOT ``ResultMessage.usage`` (which is cumulative
    across every turn and would overcount by ~num_turns). Where it sharpens the
    test, the ResultMessage carries a deliberately larger cumulative usage to
    prove the aggregate is ignored (#3200).
    """

    @patch("claude_agent_sdk.query")
    def test_full_usage_sums_window_components(self, mock_query):
        """Populated final-turn usage -> occupancy is the sum of the three parts."""

        async def gen(**kwargs):
            yield _make_assistant_msg(
                "hi",
                usage=_usage(
                    input_tokens=1_000,
                    cache_creation_input_tokens=2_000,
                    cache_read_input_tokens=70_000,
                ),
            )
            # Cumulative aggregate is much larger; it must NOT be used.
            yield _make_result_msg(
                usage=_usage(
                    input_tokens=3_000,
                    cache_creation_input_tokens=6_000,
                    cache_read_input_tokens=210_000,
                )
            )

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is True
        assert result.window_occupancy == 73_000

    @patch("claude_agent_sdk.query")
    def test_absent_usage_yields_none_without_raising(self, mock_query):
        """No per-turn usage -> occupancy None, and no exception is raised."""

        async def gen(**kwargs):
            yield _make_assistant_msg("hi", usage=None)
            yield _make_result_msg(usage=None)

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is True
        assert result.window_occupancy is None
        # token_usage tracks occupancy: both None when the SDK reports no usage.
        assert result.token_usage is None

    @patch("claude_agent_sdk.query")
    def test_partial_usage_sums_present_components(self, mock_query):
        """Missing sub-fields count as 0; the present ones still sum."""

        async def gen(**kwargs):
            # cache_creation absent entirely; only input + cache_read present.
            yield _make_assistant_msg(
                "hi", usage=_usage(input_tokens=500, cache_read_input_tokens=4_500)
            )
            yield _make_result_msg()

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        assert result.window_occupancy == 5_000

    @patch("claude_agent_sdk.query")
    def test_explicit_none_subfield_treated_as_zero(self, mock_query):
        """A present-but-None sub-field is coerced to 0, not a TypeError."""

        async def gen(**kwargs):
            yield _make_assistant_msg(
                "hi",
                usage={
                    "input_tokens": 100,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": 900,
                },
            )
            yield _make_result_msg()

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        assert result.window_occupancy == 1_000

    @patch("claude_agent_sdk.query")
    def test_cache_dominated_turn_includes_cache_read(self, mock_query):
        """Cache-dominated case: occupancy reflects the full window, not input.

        billed/effective input here is ~50 tokens, but the resident window is
        ~120k. If occupancy only counted input_tokens the reseed trigger would
        fire far too late -- this is the core reason occupancy != billed input.
        """

        async def gen(**kwargs):
            yield _make_assistant_msg(
                "hi",
                usage=_usage(
                    input_tokens=50,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=120_000,
                ),
            )
            yield _make_result_msg()

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        assert result.window_occupancy == 120_050
        # The whole point: occupancy is dominated by cache_read, not input.
        assert result.window_occupancy != 50

    @patch("claude_agent_sdk.query")
    def test_output_tokens_excluded_from_occupancy(self, mock_query):
        """output_tokens is billed but is NOT part of window occupancy."""

        async def gen(**kwargs):
            yield _make_assistant_msg(
                "hi",
                usage=_usage(
                    input_tokens=1_000,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    output_tokens=9_999,
                ),
            )
            yield _make_result_msg()

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        # Only input is a window component here; output_tokens must be ignored.
        assert result.window_occupancy == 1_000
        # ...but the raw breakout still preserves output for measurement.
        assert result.token_usage is not None
        assert result.token_usage["output_tokens"] == 9_999

    @patch("claude_agent_sdk.query")
    def test_token_usage_breakout_preserves_raw_components(self, mock_query):
        """token_usage mirrors the raw component counts for phase-10 surfaces."""

        async def gen(**kwargs):
            yield _make_assistant_msg(
                "hi",
                usage=_usage(
                    input_tokens=1_000,
                    cache_creation_input_tokens=2_000,
                    cache_read_input_tokens=70_000,
                    output_tokens=300,
                ),
            )
            yield _make_result_msg()

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        assert result.token_usage == {
            "input_tokens": 1_000,
            "cache_read_input_tokens": 70_000,
            "cache_creation_input_tokens": 2_000,
            "output_tokens": 300,
        }
        # The breakout's three window components reconcile with the total.
        assert result.window_occupancy == (
            result.token_usage["input_tokens"]
            + result.token_usage["cache_read_input_tokens"]
            + result.token_usage["cache_creation_input_tokens"]
        )

    @patch("claude_agent_sdk.query")
    def test_multistep_uses_final_turn_not_cumulative_aggregate(self, mock_query):
        """A multi-step session: occupancy == the LAST turn's window.

        This is the case the original (ResultMessage-sourced) implementation got
        wrong. Each AssistantMessage carries its own growing per-turn window; the
        ResultMessage reports the session-cumulative sum, which is far larger.
        Occupancy must equal the final AssistantMessage's window (~150k), not the
        cumulative aggregate (~450k) -- otherwise the reseed threshold would fire
        after a couple of steps regardless of the true resident window.
        """

        async def gen(**kwargs):
            # Three turns, each with a growing per-turn window.
            yield _make_assistant_msg(
                "step 1", usage=_usage(input_tokens=2_000, cache_read_input_tokens=98_000)
            )
            yield _make_assistant_msg(
                "step 2", usage=_usage(input_tokens=3_000, cache_read_input_tokens=120_000)
            )
            # Final turn -> the resident window we care about: 150_000.
            yield _make_assistant_msg(
                "step 3", usage=_usage(input_tokens=5_000, cache_read_input_tokens=145_000)
            )
            # ResultMessage usage is cumulative across all three turns (~450k).
            yield _make_result_msg(
                usage=_usage(input_tokens=10_000, cache_read_input_tokens=363_000)
            )

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is True
        # Final turn's window, not the cumulative aggregate.
        assert result.window_occupancy == 150_000
        # Guard against regressing to the ResultMessage aggregate.
        assert result.window_occupancy != 373_000
        assert result.token_usage["cache_read_input_tokens"] == 145_000

    @patch("claude_agent_sdk.query")
    def test_error_result_also_captures_occupancy(self, mock_query):
        """The error build site populates occupancy too (every site, task-1-2).

        Occupancy is the last AssistantMessage's window even when the run ends in
        an error ResultMessage.
        """

        async def gen(**kwargs):
            yield _make_assistant_msg(
                "partial", usage=_usage(input_tokens=10, cache_read_input_tokens=40)
            )
            yield _make_result_msg(
                result="Rate limit exceeded",
                is_error=True,
            )

        mock_query.side_effect = gen
        result = _run_async(run_agent_async("test prompt"))

        assert result.success is False
        assert result.window_occupancy == 50
