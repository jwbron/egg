"""Tests for egg_harness.tools.bash — shell command execution contract."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from egg_harness.tools.bash import execute_bash

# ---------------------------------------------------------------------------
# TestBashExecution — basic command execution
# ---------------------------------------------------------------------------


class TestBashExecution:
    """Basic command execution behaviour."""

    def test_simple_command_execution(self):
        """Running 'echo hello' returns 'hello\\n' as output."""
        result = execute_bash("echo hello")

        assert result.output.strip() == "hello"
        assert result.exit_code == 0

    def test_command_exit_code_preserved(self):
        """The exit code from the subprocess is preserved in the result."""
        result = execute_bash("exit 42")

        assert result.exit_code == 42

    def test_working_directory(self, tmp_path):
        """Commands execute in the specified working directory."""
        result = execute_bash("pwd", cwd=str(tmp_path))

        # Resolve symlinks for comparison (macOS /var -> /private/var, etc.)
        actual = os.path.realpath(result.output.strip())
        expected = os.path.realpath(str(tmp_path))
        assert actual == expected

    def test_stderr_captured(self):
        """Standard error output is captured in the result."""
        result = execute_bash("echo oops >&2")

        # stderr should appear somewhere in the result (output or stderr field).
        combined = result.output + getattr(result, "stderr", "")
        assert "oops" in combined

    def test_command_with_special_characters(self):
        """Commands with quotes, pipes, and other shell metacharacters work."""
        result = execute_bash("echo 'hello world' | tr ' ' '_'")

        assert result.output.strip() == "hello_world"

    def test_empty_command(self):
        """An empty command string is handled gracefully (no crash)."""
        result = execute_bash("")

        # An empty command should either succeed as a no-op or return an error,
        # but must never raise an unhandled exception.
        assert isinstance(result.exit_code, int)


# ---------------------------------------------------------------------------
# TestBashTimeout — timeout and process cleanup
# ---------------------------------------------------------------------------


class TestBashTimeout:
    """Timeout enforcement and process-group cleanup."""

    def test_timeout_kills_process(self):
        """A command exceeding the timeout is killed and returns an error."""
        result = execute_bash("sleep 999", timeout=1)

        # The result should indicate a timeout or non-zero exit.
        assert result.exit_code != 0 or "timeout" in result.output.lower()

    def test_default_timeout_is_120(self):
        """The default timeout value should be 120 seconds.

        We verify this by inspecting the function signature or by mocking
        subprocess.Popen and checking the communicated timeout.
        """
        import inspect

        sig = inspect.signature(execute_bash)
        timeout_param = sig.parameters.get("timeout")

        if timeout_param is not None and timeout_param.default is not inspect.Parameter.empty:
            assert timeout_param.default == 120
        else:
            # If timeout is not a direct parameter default, verify by mocking
            # that the Popen communicate call receives timeout=120.
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 0
            mock_proc.pid = 12345

            with patch("subprocess.Popen", return_value=mock_proc):
                execute_bash("true")

            mock_proc.communicate.assert_called_once()
            call_kwargs = mock_proc.communicate.call_args
            # timeout may be positional or keyword
            if call_kwargs.kwargs.get("timeout"):
                assert call_kwargs.kwargs["timeout"] == 120
            elif len(call_kwargs.args) > 0:
                assert call_kwargs.args[0] == 120


# ---------------------------------------------------------------------------
# TestBashSafety — shell=True prohibition
# ---------------------------------------------------------------------------


class TestBashSafety:
    """Safety invariants for subprocess invocation."""

    def test_no_shell_true_in_implementation(self):
        """subprocess.Popen must NOT be called with shell=True.

        The contract requires ['bash', '-c', command] instead.
        """
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"output", b"")
        mock_proc.returncode = 0
        mock_proc.pid = 99999

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            execute_bash("echo test")

        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args

        # Verify shell is not True (either absent or explicitly False).
        shell_value = call_kwargs.kwargs.get("shell", False)
        assert shell_value is not True, "subprocess.Popen must not use shell=True"

        # Verify the command list pattern: ["bash", "-c", <command>]
        cmd_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("args")
        assert isinstance(cmd_arg, list), "Command should be passed as a list"
        assert cmd_arg[0] == "bash"
        assert "-c" in cmd_arg

    def test_process_group_management(self):
        """subprocess.Popen should be called with preexec_fn=os.setpgrp (or
        equivalent start_new_session) for reliable timeout cleanup.
        """
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_proc.pid = 11111

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            execute_bash("true")

        call_kwargs = mock_popen.call_args

        # Accept either preexec_fn=os.setpgrp or start_new_session=True.
        has_setpgrp = call_kwargs.kwargs.get("preexec_fn") is os.setpgrp
        has_new_session = call_kwargs.kwargs.get("start_new_session") is True
        assert has_setpgrp or has_new_session, (
            "Popen should use preexec_fn=os.setpgrp or start_new_session=True"
        )


# ---------------------------------------------------------------------------
# TestBashOutputCapture — stdout/stderr merging
# ---------------------------------------------------------------------------


class TestBashOutputCapture:
    """Output capture and encoding."""

    def test_multiline_output(self):
        """Multi-line command output is fully captured."""
        result = execute_bash("printf 'line1\\nline2\\nline3'")

        lines = result.output.strip().splitlines()
        assert lines == ["line1", "line2", "line3"]

    def test_binary_safe_output(self):
        """Non-UTF-8 bytes in output are handled without crashing."""
        # printf with octal escapes produces bytes that may not be valid UTF-8
        result = execute_bash("printf '\\xc0\\xc1'")

        # Should not raise; output may be replacement characters or raw bytes.
        assert isinstance(result.output, str)

    def test_large_output(self):
        """Large outputs (>64 KB) are captured without truncation at the
        subprocess level (registry truncation is a separate concern)."""
        result = execute_bash("python3 -c \"print('a' * 100_000)\"")

        assert len(result.output.strip()) == 100_000
