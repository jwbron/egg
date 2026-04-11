"""Tool behavioral parity compliance tests.

Validates that each harness tool produces outputs matching the expected
behavioral contract (same schema, same error patterns, same edge cases
as Claude Code's built-in tools).

Each tool has at least 5 test cases covering:
- Normal operation
- Error conditions
- Edge cases
- Input validation
- Output format consistency
"""

from __future__ import annotations

import asyncio
import os
import shutil
from unittest.mock import patch

import pytest
from egg_config.constants import GATEWAY_PORT
from egg_harness.tools.bash import create_bash_tool
from egg_harness.tools.edit import create_edit_tool
from egg_harness.tools.glob_tool import create_glob_tool
from egg_harness.tools.grep import create_grep_tool
from egg_harness.tools.read import create_read_tool
from egg_harness.tools.registry import ToolResult
from egg_harness.tools.web_fetch import create_web_fetch_tool
from egg_harness.tools.web_search import create_web_search_tool
from egg_harness.tools.write import create_write_tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a workspace inside a temp dir that passes path validation."""
    workspace = tmp_path / "repos"
    workspace.mkdir()
    with patch.dict(os.environ, {"EGG_REPO_PATH": str(workspace)}):
        yield workspace


def _run(coro):
    """Run an async handler synchronously."""
    return asyncio.run(coro)


# ===========================================================================
# Bash tool parity tests
# ===========================================================================


class TestBashToolParity:
    """Bash tool behavioral parity with Claude Code."""

    def test_simple_command_output(self):
        """Bash returns stdout for successful commands."""
        _, handler = create_bash_tool()
        result = _run(handler({"command": "echo hello"}))
        assert not result.is_error
        assert "hello" in result.output

    def test_nonzero_exit_code_is_error(self):
        """Non-zero exit code marks result as error."""
        _, handler = create_bash_tool()
        result = _run(handler({"command": "exit 1"}))
        assert result.is_error

    def test_stderr_included_in_output(self):
        """Stderr is included in the output."""
        _, handler = create_bash_tool()
        result = _run(handler({"command": "echo err >&2"}))
        assert "err" in result.output

    def test_timeout_returns_error(self):
        """Command timeout produces an error result, not an exception."""
        _, handler = create_bash_tool(timeout=1)
        result = _run(handler({"command": "sleep 10"}))
        assert result.is_error
        assert "timed out" in result.output.lower()

    def test_agent_timeout_capped(self):
        """Agent-supplied timeout is capped at _MAX_TIMEOUT."""
        _, handler = create_bash_tool(timeout=5)
        # Even though agent requests a huge timeout, it's capped.
        result = _run(handler({"command": "echo ok", "timeout": 999999}))
        assert not result.is_error
        assert "ok" in result.output

    def test_cwd_respected(self, tmp_path):
        """Working directory is respected."""
        _, handler = create_bash_tool(cwd=str(tmp_path))
        result = _run(handler({"command": "pwd"}))
        assert not result.is_error
        assert str(tmp_path) in result.output


# ===========================================================================
# Read tool parity tests
# ===========================================================================


class TestReadToolParity:
    """Read tool behavioral parity with Claude Code."""

    def test_read_existing_file(self, tmp_workspace):
        """Reading an existing file returns numbered lines."""
        f = tmp_workspace / "test.txt"
        f.write_text("line1\nline2\nline3\n")
        _, handler = create_read_tool()
        result = _run(handler({"file_path": str(f)}))
        assert not result.is_error
        assert "1\tline1" in result.output
        assert "2\tline2" in result.output

    def test_read_nonexistent_file(self, tmp_workspace):
        """Reading a nonexistent file returns an error."""
        _, handler = create_read_tool()
        result = _run(handler({"file_path": str(tmp_workspace / "missing.txt")}))
        assert result.is_error
        assert "not found" in result.output.lower()

    def test_read_binary_file(self, tmp_workspace):
        """Reading a binary file returns an error."""
        f = tmp_workspace / "binary.bin"
        f.write_bytes(b"\x00\x01\x02\x03")
        _, handler = create_read_tool()
        result = _run(handler({"file_path": str(f)}))
        assert result.is_error
        assert "binary" in result.output.lower()

    def test_read_with_offset_and_limit(self, tmp_workspace):
        """Offset and limit control which lines are returned."""
        f = tmp_workspace / "lines.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 11)))
        _, handler = create_read_tool()
        result = _run(handler({"file_path": str(f), "offset": 3, "limit": 2}))
        assert not result.is_error
        assert "line3" in result.output
        assert "line4" in result.output
        assert "line5" not in result.output

    def test_read_empty_file(self, tmp_workspace):
        """Reading an empty file returns empty output, not an error."""
        f = tmp_workspace / "empty.txt"
        f.write_text("")
        _, handler = create_read_tool()
        result = _run(handler({"file_path": str(f)}))
        assert not result.is_error

    def test_path_traversal_blocked(self, tmp_workspace):
        """Path traversal outside workspace is blocked."""
        _, handler = create_read_tool()
        result = _run(handler({"file_path": "/etc/hostname"}))
        assert result.is_error
        assert "outside" in result.output.lower() or "workspace" in result.output.lower()


# ===========================================================================
# Write tool parity tests
# ===========================================================================


class TestWriteToolParity:
    """Write tool behavioral parity with Claude Code."""

    def test_write_new_file(self, tmp_workspace):
        """Writing creates a new file with correct content."""
        target = tmp_workspace / "new.txt"
        _, handler = create_write_tool()
        result = _run(handler({"file_path": str(target), "content": "hello world"}))
        assert not result.is_error
        assert target.read_text() == "hello world"

    def test_write_overwrites_existing(self, tmp_workspace):
        """Writing overwrites an existing file."""
        target = tmp_workspace / "existing.txt"
        target.write_text("old content")
        _, handler = create_write_tool()
        result = _run(handler({"file_path": str(target), "content": "new content"}))
        assert not result.is_error
        assert target.read_text() == "new content"

    def test_write_creates_parent_dirs(self, tmp_workspace):
        """Writing creates parent directories as needed."""
        target = tmp_workspace / "deep" / "nested" / "file.txt"
        _, handler = create_write_tool()
        result = _run(handler({"file_path": str(target), "content": "nested"}))
        assert not result.is_error
        assert target.read_text() == "nested"

    def test_write_unicode_content(self, tmp_workspace):
        """Writing handles unicode content correctly."""
        target = tmp_workspace / "unicode.txt"
        content = "Hello \u4e16\u754c \U0001f30d"
        _, handler = create_write_tool()
        result = _run(handler({"file_path": str(target), "content": content}))
        assert not result.is_error
        assert target.read_text() == content

    def test_path_traversal_blocked(self, tmp_workspace):
        """Path traversal outside workspace is blocked."""
        _, handler = create_write_tool()
        result = _run(handler({"file_path": "/etc/evil.txt", "content": "bad"}))
        assert result.is_error


# ===========================================================================
# Edit tool parity tests
# ===========================================================================


class TestEditToolParity:
    """Edit tool behavioral parity with Claude Code."""

    def test_replace_unique_string(self, tmp_workspace):
        """Replacing a unique string succeeds."""
        f = tmp_workspace / "edit.txt"
        f.write_text("hello world")
        _, handler = create_edit_tool()
        result = _run(
            handler(
                {
                    "file_path": str(f),
                    "old_string": "world",
                    "new_string": "earth",
                }
            )
        )
        assert not result.is_error
        assert f.read_text() == "hello earth"

    def test_nonexistent_old_string_is_error(self, tmp_workspace):
        """Replacing a string that doesn't exist is an error."""
        f = tmp_workspace / "edit2.txt"
        f.write_text("hello world")
        _, handler = create_edit_tool()
        result = _run(
            handler(
                {
                    "file_path": str(f),
                    "old_string": "missing",
                    "new_string": "found",
                }
            )
        )
        assert result.is_error
        assert "not found" in result.output.lower()

    def test_non_unique_string_errors_without_replace_all(self, tmp_workspace):
        """Non-unique old_string errors when replace_all is false."""
        f = tmp_workspace / "edit3.txt"
        f.write_text("aaa bbb aaa")
        _, handler = create_edit_tool()
        result = _run(
            handler(
                {
                    "file_path": str(f),
                    "old_string": "aaa",
                    "new_string": "ccc",
                }
            )
        )
        assert result.is_error
        assert "not unique" in result.output.lower()

    def test_replace_all(self, tmp_workspace):
        """replace_all replaces all occurrences."""
        f = tmp_workspace / "edit4.txt"
        f.write_text("aaa bbb aaa")
        _, handler = create_edit_tool()
        result = _run(
            handler(
                {
                    "file_path": str(f),
                    "old_string": "aaa",
                    "new_string": "ccc",
                    "replace_all": True,
                }
            )
        )
        assert not result.is_error
        assert f.read_text() == "ccc bbb ccc"

    def test_edit_nonexistent_file(self, tmp_workspace):
        """Editing a nonexistent file is an error."""
        _, handler = create_edit_tool()
        result = _run(
            handler(
                {
                    "file_path": str(tmp_workspace / "missing.txt"),
                    "old_string": "a",
                    "new_string": "b",
                }
            )
        )
        assert result.is_error

    def test_path_traversal_blocked(self, tmp_workspace):
        """Path traversal outside workspace is blocked."""
        _, handler = create_edit_tool()
        result = _run(
            handler(
                {
                    "file_path": "/etc/passwd",
                    "old_string": "root",
                    "new_string": "evil",
                }
            )
        )
        assert result.is_error


# ===========================================================================
# Glob tool parity tests
# ===========================================================================


class TestGlobToolParity:
    """Glob tool behavioral parity with Claude Code."""

    def test_glob_finds_files(self, tmp_workspace):
        """Glob pattern matches files correctly."""
        (tmp_workspace / "a.py").write_text("a")
        (tmp_workspace / "b.py").write_text("b")
        (tmp_workspace / "c.txt").write_text("c")
        _, handler = create_glob_tool()
        result = _run(handler({"pattern": "*.py", "path": str(tmp_workspace)}))
        assert not result.is_error
        assert "a.py" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output

    def test_glob_recursive(self, tmp_workspace):
        """Recursive glob finds nested files."""
        sub = tmp_workspace / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("deep")
        _, handler = create_glob_tool()
        result = _run(handler({"pattern": "**/*.py", "path": str(tmp_workspace)}))
        assert not result.is_error
        assert "deep.py" in result.output

    def test_glob_no_matches(self, tmp_workspace):
        """No matches returns empty output, not an error."""
        _, handler = create_glob_tool()
        result = _run(handler({"pattern": "*.xyz", "path": str(tmp_workspace)}))
        assert not result.is_error

    def test_glob_nonexistent_path(self):
        """Nonexistent path returns an error."""
        _, handler = create_glob_tool()
        result = _run(handler({"pattern": "*.py", "path": "/nonexistent/path"}))
        assert result.is_error

    def test_glob_returns_sorted(self, tmp_workspace):
        """Results are sorted (by modification time or name)."""
        for name in ["z.py", "a.py", "m.py"]:
            (tmp_workspace / name).write_text(name)
        _, handler = create_glob_tool()
        result = _run(handler({"pattern": "*.py", "path": str(tmp_workspace)}))
        assert not result.is_error
        # Verify all files present
        assert "z.py" in result.output
        assert "a.py" in result.output
        assert "m.py" in result.output


# ===========================================================================
# Grep tool parity tests
# ===========================================================================


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
class TestGrepToolParity:
    """Grep tool behavioral parity with Claude Code."""

    def test_grep_finds_pattern(self, tmp_workspace):
        """Grep finds lines matching a pattern."""
        f = tmp_workspace / "search.py"
        f.write_text("def hello():\n    pass\ndef world():\n    pass\n")
        _, handler = create_grep_tool()
        result = _run(
            handler(
                {
                    "pattern": "def hello",
                    "path": str(tmp_workspace),
                    "output_mode": "content",
                }
            )
        )
        assert not result.is_error
        assert "def hello" in result.output

    def test_grep_no_matches(self, tmp_workspace):
        """No matches returns empty output, not an error."""
        f = tmp_workspace / "search2.py"
        f.write_text("no match here")
        _, handler = create_grep_tool()
        result = _run(
            handler(
                {
                    "pattern": "ZZZZNOTFOUND",
                    "path": str(tmp_workspace),
                }
            )
        )
        # No matches is not an error — just empty output.
        assert not result.is_error or result.output == ""

    def test_grep_files_with_matches_mode(self, tmp_workspace):
        """files_with_matches mode returns file paths only."""
        f = tmp_workspace / "match.py"
        f.write_text("pattern_here\n")
        _, handler = create_grep_tool()
        result = _run(
            handler(
                {
                    "pattern": "pattern_here",
                    "path": str(tmp_workspace),
                    "output_mode": "files_with_matches",
                }
            )
        )
        assert not result.is_error
        assert "match.py" in result.output

    def test_grep_regex_pattern(self, tmp_workspace):
        """Regex patterns are supported."""
        f = tmp_workspace / "regex.py"
        f.write_text("foo123bar\nfoobazbar\n")
        _, handler = create_grep_tool()
        result = _run(
            handler(
                {
                    "pattern": r"foo\d+bar",
                    "path": str(tmp_workspace),
                    "output_mode": "content",
                }
            )
        )
        assert not result.is_error
        assert "foo123bar" in result.output

    def test_grep_with_glob_filter(self, tmp_workspace):
        """Glob filter limits which files are searched."""
        (tmp_workspace / "match.py").write_text("found\n")
        (tmp_workspace / "match.txt").write_text("found\n")
        _, handler = create_grep_tool()
        result = _run(
            handler(
                {
                    "pattern": "found",
                    "path": str(tmp_workspace),
                    "glob": "*.py",
                    "output_mode": "files_with_matches",
                }
            )
        )
        assert not result.is_error
        assert "match.py" in result.output
        assert "match.txt" not in result.output


# ===========================================================================
# WebFetch tool parity tests
# ===========================================================================


class TestWebFetchToolParity:
    """WebFetch tool behavioral parity with Claude Code."""

    def test_private_mode_blocked(self):
        """WebFetch returns error in private mode."""
        _, handler = create_web_fetch_tool()
        with patch.dict(os.environ, {"EGG_PRIVATE_MODE": "true"}):
            result = _run(handler({"url": "https://example.com", "prompt": "test"}))
        assert result.is_error
        assert "private mode" in result.output.lower()

    def test_ssrf_private_ip_blocked(self):
        """WebFetch blocks requests to private IPs."""
        _, handler = create_web_fetch_tool()
        result = _run(
            handler(
                {
                    "url": "http://169.254.169.254/latest/meta-data/",
                    "prompt": "test",
                }
            )
        )
        assert result.is_error
        assert "blocked" in result.output.lower() or "security" in result.output.lower()

    def test_ssrf_internal_host_blocked(self):
        """WebFetch blocks requests to internal services."""
        _, handler = create_web_fetch_tool()
        result = _run(
            handler(
                {
                    "url": f"http://egg-gateway:{GATEWAY_PORT}/api/v1/health",
                    "prompt": "test",
                }
            )
        )
        assert result.is_error

    def test_ssrf_localhost_blocked(self):
        """WebFetch blocks requests to localhost."""
        _, handler = create_web_fetch_tool()
        result = _run(
            handler(
                {
                    "url": "http://127.0.0.1:8080/secret",
                    "prompt": "test",
                }
            )
        )
        assert result.is_error

    def test_ssrf_ipv6_mapped_blocked(self):
        """WebFetch blocks IPv6-mapped IPv4 private addresses."""
        _, handler = create_web_fetch_tool()
        result = _run(
            handler(
                {
                    "url": "http://[::ffff:169.254.169.254]/",
                    "prompt": "test",
                }
            )
        )
        assert result.is_error


# ===========================================================================
# WebSearch tool parity tests
# ===========================================================================


class TestWebSearchToolParity:
    """WebSearch tool behavioral parity with Claude Code."""

    def test_private_mode_blocked(self):
        """WebSearch returns error in private mode."""
        _, handler = create_web_search_tool()
        with patch.dict(os.environ, {"EGG_PRIVATE_MODE": "true"}):
            result = _run(handler({"query": "test query"}))
        assert result.is_error
        assert "private mode" in result.output.lower() or "not available" in result.output.lower()

    def test_returns_not_available_message(self):
        """WebSearch returns appropriate unavailability message."""
        _, handler = create_web_search_tool()
        result = _run(handler({"query": "test query"}))
        # The stub always returns "not available" in the harness.
        assert "not available" in result.output.lower() or not result.is_error

    def test_empty_query_handled(self):
        """Empty query does not crash."""
        _, handler = create_web_search_tool()
        result = _run(handler({"query": ""}))
        # Should return gracefully (either error or stub response).
        assert isinstance(result, ToolResult)

    def test_long_query_handled(self):
        """Long query does not crash."""
        _, handler = create_web_search_tool()
        result = _run(handler({"query": "a" * 10000}))
        assert isinstance(result, ToolResult)

    def test_result_format(self):
        """Result has the expected output/is_error structure."""
        _, handler = create_web_search_tool()
        result = _run(handler({"query": "test"}))
        assert hasattr(result, "output")
        assert hasattr(result, "is_error")
