"""Tests for egg_harness.tools.bash — shell command execution via factory pattern."""

from __future__ import annotations

import os

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.tools.bash")

from egg_harness.tools.bash import create_bash_tool
from egg_harness.tools.registry import ToolDefinition, ToolResult

# ---------------------------------------------------------------------------
# TestBashToolCreation — factory returns valid definition + handler
# ---------------------------------------------------------------------------


class TestBashToolCreation:
    """Verify create_bash_tool returns a valid (ToolDefinition, handler) pair."""

    def test_factory_returns_tuple(self):
        defn, handler = create_bash_tool()
        assert isinstance(defn, ToolDefinition)
        assert callable(handler)

    def test_definition_name_is_bash(self):
        defn, _ = create_bash_tool()
        assert defn.name == "Bash"

    def test_definition_has_input_schema(self):
        defn, _ = create_bash_tool()
        assert isinstance(defn.input_schema, dict)
        assert "command" in str(defn.input_schema)

    def test_factory_accepts_cwd_and_timeout(self):
        """create_bash_tool can be called with cwd and timeout kwargs."""
        defn, handler = create_bash_tool(cwd="/tmp", timeout=60)
        assert isinstance(defn, ToolDefinition)


# ---------------------------------------------------------------------------
# TestBashExecution — basic command execution
# ---------------------------------------------------------------------------


class TestBashExecution:
    """Basic command execution behaviour via the handler."""

    @pytest.mark.anyio
    async def test_simple_command_execution(self):
        """Running 'echo hello' returns 'hello' in output."""
        _, handler = create_bash_tool()
        result = await handler({"command": "echo hello"})

        assert isinstance(result, ToolResult)
        assert "hello" in result.output
        assert not result.is_error

    @pytest.mark.anyio
    async def test_command_failure_is_error(self):
        """A command that exits non-zero sets is_error=True."""
        _, handler = create_bash_tool()
        result = await handler({"command": "exit 42"})

        assert result.is_error

    @pytest.mark.anyio
    async def test_working_directory(self, tmp_path):
        """Commands execute in the specified working directory."""
        _, handler = create_bash_tool(cwd=str(tmp_path))
        result = await handler({"command": "pwd"})

        actual = os.path.realpath(result.output.strip())
        expected = os.path.realpath(str(tmp_path))
        assert actual == expected

    @pytest.mark.anyio
    async def test_stderr_captured(self):
        """Standard error output is captured in the result."""
        _, handler = create_bash_tool()
        result = await handler({"command": "echo oops >&2"})

        assert "oops" in result.output

    @pytest.mark.anyio
    async def test_command_with_special_characters(self):
        """Commands with quotes, pipes, and other shell metacharacters work."""
        _, handler = create_bash_tool()
        result = await handler({"command": "echo 'hello world' | tr ' ' '_'"})

        assert "hello_world" in result.output

    @pytest.mark.anyio
    async def test_empty_command(self):
        """An empty command string is handled gracefully (no crash)."""
        _, handler = create_bash_tool()
        result = await handler({"command": ""})

        assert isinstance(result, ToolResult)


# ---------------------------------------------------------------------------
# TestBashTimeout — timeout enforcement
# ---------------------------------------------------------------------------


class TestBashTimeout:
    """Timeout enforcement and process cleanup."""

    @pytest.mark.anyio
    async def test_timeout_kills_process(self):
        """A command exceeding the timeout is killed and returns an error."""
        _, handler = create_bash_tool(timeout=1)
        result = await handler({"command": "sleep 999"})

        assert result.is_error or "timeout" in result.output.lower()

    @pytest.mark.anyio
    async def test_per_command_timeout_override(self):
        """A per-command timeout in the input dict overrides the default."""
        _, handler = create_bash_tool(timeout=300)
        result = await handler({"command": "sleep 999", "timeout": 1})

        assert result.is_error or "timeout" in result.output.lower()

    def test_default_timeout_is_120(self):
        """The factory default timeout should be 120 seconds."""
        import inspect

        sig = inspect.signature(create_bash_tool)
        timeout_param = sig.parameters.get("timeout")
        assert timeout_param is not None
        assert timeout_param.default == 120


# ---------------------------------------------------------------------------
# TestBashOutputCapture — stdout/stderr merging
# ---------------------------------------------------------------------------


class TestBashOutputCapture:
    """Output capture and encoding."""

    @pytest.mark.anyio
    async def test_multiline_output(self):
        """Multi-line command output is fully captured."""
        _, handler = create_bash_tool()
        result = await handler({"command": "printf 'line1\\nline2\\nline3'"})

        lines = result.output.strip().splitlines()
        assert lines == ["line1", "line2", "line3"]

    @pytest.mark.anyio
    async def test_binary_safe_output(self):
        """Non-UTF-8 bytes in output are handled without crashing."""
        _, handler = create_bash_tool()
        result = await handler({"command": "printf '\\xc0\\xc1'"})

        assert isinstance(result.output, str)

    @pytest.mark.anyio
    async def test_large_output(self):
        """Large outputs (>64 KB) are captured without truncation at the
        subprocess level (registry truncation is a separate concern)."""
        _, handler = create_bash_tool()
        result = await handler({"command": "python3 -c \"print('a' * 100_000)\""})

        assert len(result.output.strip()) == 100_000
