"""Tests for egg_agent.client module."""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from egg_agent.client import _MAX_TOOL_CONTENT_LOG_LEN, _truncate, run_agent, run_agent_async

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
        # issue #2804: bump the SDK's JSON message buffer
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


def _make_assistant_msg(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-opus-4-6-20250313",
    )


def _make_result_msg(
    result: str | None = "Final result",
    is_error: bool = False,
    total_cost_usd: float | None = 0.05,
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
        usage=None,
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


class TestMaxBufferSize:
    """Issue #2804: SDK message-reader buffer bump.

    The SDK default is 1 MB; we bump it to 4 MB (env-overridable) so
    moderate-but-large tool results that slip past the PostToolUse hook
    don't crash the reader.
    """

    @patch.dict(os.environ, {}, clear=False)
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_default_max_buffer_size_is_4mb(self, mock_query):
        env = os.environ.copy()
        env.pop("EGG_AGENT_MAX_BUFFER_SIZE", None)
        with patch.dict(os.environ, env, clear=True):
            _run_async(run_agent_async("test prompt"))
        opts = mock_query.call_args.kwargs["options"]
        assert opts.max_buffer_size == 4 * 1024 * 1024

    @patch.dict(os.environ, {"EGG_AGENT_MAX_BUFFER_SIZE": "16777216"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_env_override_raises_buffer(self, mock_query):
        _run_async(run_agent_async("test prompt"))
        opts = mock_query.call_args.kwargs["options"]
        assert opts.max_buffer_size == 16_777_216

    @patch.dict(os.environ, {"EGG_AGENT_MAX_BUFFER_SIZE": "not-a-number"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_garbage_env_falls_back_to_default(self, mock_query):
        _run_async(run_agent_async("test prompt"))
        opts = mock_query.call_args.kwargs["options"]
        assert opts.max_buffer_size == 4 * 1024 * 1024

    @patch.dict(os.environ, {"EGG_AGENT_MAX_BUFFER_SIZE": "0"})
    @patch("claude_agent_sdk.query", side_effect=_mock_query_success)
    def test_zero_env_falls_back_to_default(self, mock_query):
        """Zero is nonsensical — fall back rather than disabling the buffer."""
        _run_async(run_agent_async("test prompt"))
        opts = mock_query.call_args.kwargs["options"]
        assert opts.max_buffer_size == 4 * 1024 * 1024


class TestBufferOverflowErrorHandling:
    """Issue #2804: when the SDK raises CLIJSONDecodeError on a buffer
    overflow, the agent must return a structured failure with the
    overflow marker preserved in ``error`` — the consensus-wrapper
    greps for that string to short-circuit retry.
    """

    @patch("claude_agent_sdk.query")
    def test_buffer_overflow_returns_failure_with_marker(self, mock_query):
        from claude_agent_sdk import CLIJSONDecodeError

        mock_query.side_effect = CLIJSONDecodeError(
            "JSON message exceeded maximum buffer size of 4194304 bytes..."
        )

        result = _run_async(run_agent_async("test prompt"))

        assert result.success is False
        assert result.returncode == -1
        # Marker must appear verbatim in ``error`` so the wrapper's grep
        # in is_buffer_overflow() matches.
        assert "exceeded maximum buffer size" in result.error


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
