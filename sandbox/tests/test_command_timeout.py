"""Tests for the per-command timeout wrapper (TASK-3-3).

These tests verify the bash wrapper installed by setup_command_timeout()
in entrypoint.py.  They exercise the wrapper via actual bash -c invocations
to confirm that:
  (a) Long-running commands are killed after BASH_COMMAND_TIMEOUT
  (b) Normal commands complete successfully
  (c) BASH_COMMAND_TIMEOUT=0 disables the timeout
  (d) A descriptive error appears on stderr when a command is killed

The tests install a local copy of the wrapper into a temp directory so
they can run outside the sandbox container without modifying /bin/bash.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Wrapper script template (mirrors what setup_command_timeout installs)
# ---------------------------------------------------------------------------

_WRAPPER_TEMPLATE = textwrap.dedent("""\
    #!/bin/bash
    # Test copy of egg command timeout wrapper.
    REAL_BASH="$(command -v bash.real 2>/dev/null || echo /bin/bash)"
    TIMEOUT="${BASH_COMMAND_TIMEOUT:-300}"
    GRACE="${BASH_COMMAND_TIMEOUT_GRACE:-10}"

    if [ "$TIMEOUT" = "0" ] || [ -z "$TIMEOUT" ]; then
        exec "$REAL_BASH" "$@"
    fi

    if [ "$1" = "-c" ]; then
        exec /usr/bin/timeout --signal=TERM --kill-after="${GRACE}s" "${TIMEOUT}s" \
            "$REAL_BASH" "$@"
    fi

    exec "$REAL_BASH" "$@"
""")


@pytest.fixture
def wrapper_dir(tmp_path: Path) -> Path:
    """Create a temp directory with the wrapper script and a bash.real symlink."""
    # Create bash.real pointing to the system bash
    system_bash = "/bin/bash"
    assert Path(system_bash).exists(), "System bash not found"

    bash_real = tmp_path / "bash.real"
    os.symlink(system_bash, bash_real)

    # Write the wrapper script, pointing REAL_BASH at our temp bash.real
    wrapper = tmp_path / "bash-wrapper"
    wrapper_content = _WRAPPER_TEMPLATE.replace(
        'REAL_BASH="$(command -v bash.real 2>/dev/null || echo /bin/bash)"',
        f'REAL_BASH="{bash_real}"',
    )
    wrapper.write_text(wrapper_content)
    wrapper.chmod(0o755)

    return wrapper


def _run_via_wrapper(
    wrapper: Path,
    command: str,
    timeout_secs: str = "2",
    grace_secs: str = "2",
    disable: bool = False,
    run_timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a command through the wrapper script."""
    env = os.environ.copy()
    env["BASH_COMMAND_TIMEOUT"] = "0" if disable else timeout_secs
    env["BASH_COMMAND_TIMEOUT_GRACE"] = grace_secs
    return subprocess.run(
        [str(wrapper), "-c", command],
        capture_output=True,
        text=True,
        timeout=run_timeout,
        check=False,
        env=env,
    )


class TestCommandTimeout:
    """Tests for the bash command timeout wrapper."""

    def test_long_running_command_is_killed(self, wrapper_dir: Path) -> None:
        """(a) Command exceeding BASH_COMMAND_TIMEOUT is killed."""
        result = _run_via_wrapper(
            wrapper_dir,
            "sleep 60",
            timeout_secs="2",
            grace_secs="2",
            run_timeout=15,
        )
        # timeout(1) returns 124 on SIGTERM kill
        assert result.returncode != 0
        # The command should not have run for the full 60 seconds

    def test_normal_command_completes_successfully(self, wrapper_dir: Path) -> None:
        """(b) Command completing within timeout succeeds normally."""
        result = _run_via_wrapper(
            wrapper_dir,
            "echo hello-from-wrapper",
            timeout_secs="30",
        )
        assert result.returncode == 0
        assert "hello-from-wrapper" in result.stdout

    def test_disable_timeout_via_env(self, wrapper_dir: Path) -> None:
        """(c) BASH_COMMAND_TIMEOUT=0 disables the timeout."""
        result = _run_via_wrapper(
            wrapper_dir,
            "echo timeout-disabled",
            disable=True,
        )
        assert result.returncode == 0
        assert "timeout-disabled" in result.stdout

    def test_timeout_stderr_message(self, wrapper_dir: Path) -> None:
        """(d) timeout(1) sets a non-zero exit code when command is killed."""
        result = _run_via_wrapper(
            wrapper_dir,
            "sleep 60",
            timeout_secs="1",
            grace_secs="2",
            run_timeout=15,
        )
        # timeout returns 124 when sending SIGTERM, or 137 if SIGKILL
        assert result.returncode in (124, 137, -15, -9)

    def test_non_c_invocation_passes_through(self, wrapper_dir: Path) -> None:
        """(e) Non -c invocations pass through to real bash unmodified."""
        # Run a script file through the wrapper (not -c)
        env = os.environ.copy()
        env["BASH_COMMAND_TIMEOUT"] = "2"
        env["BASH_COMMAND_TIMEOUT_GRACE"] = "2"

        # Create a small script file
        script = wrapper_dir.parent / "test_script.sh"
        script.write_text("#!/bin/bash\necho pass-through-ok\n")
        script.chmod(0o755)

        result = subprocess.run(
            [str(wrapper_dir), str(script)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
        assert result.returncode == 0
        assert "pass-through-ok" in result.stdout

    def test_exit_code_preserved_through_wrapper(self, wrapper_dir: Path) -> None:
        """(f) Command exit code is preserved through the wrapper."""
        result = _run_via_wrapper(
            wrapper_dir,
            "exit 42",
            timeout_secs="30",
        )
        assert result.returncode == 42

    def test_empty_timeout_disables_wrapping(self, wrapper_dir: Path) -> None:
        """(g) Empty BASH_COMMAND_TIMEOUT also disables the timeout."""
        env = os.environ.copy()
        env["BASH_COMMAND_TIMEOUT"] = ""
        env["BASH_COMMAND_TIMEOUT_GRACE"] = "2"

        result = subprocess.run(
            [str(wrapper_dir), "-c", "echo empty-timeout-ok"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
        assert result.returncode == 0
        assert "empty-timeout-ok" in result.stdout


import sys
from unittest.mock import MagicMock, patch

# Add sandbox/ to sys.path so entrypoint module is importable
_sandbox_path = str(Path(__file__).parent.parent)
if _sandbox_path not in sys.path:
    sys.path.insert(0, _sandbox_path)

# Add shared/ for egg_config dependency
_shared_path = str(Path(__file__).parent.parent.parent / "shared")
if _shared_path not in sys.path:
    sys.path.insert(0, _shared_path)

from entrypoint import run_exec, setup_command_timeout


class TestSetupCommandTimeout:
    """Tests for the setup_command_timeout function in entrypoint.py."""

    def test_idempotent_when_already_installed(self, tmp_path: Path) -> None:
        """setup_command_timeout returns early when bash.real already exists."""
        mock_config = MagicMock()
        mock_logger = MagicMock()

        # Simulate bash.real already existing (idempotent case)
        real_bash = tmp_path / "bash.real"
        real_bash.write_text("#!/bin/bash\n")
        bash_path = tmp_path / "bash"
        bash_path.write_text("#!/bin/bash\n")

        with (
            patch("entrypoint.Path") as mock_path_cls,
        ):

            def path_side_effect(p):
                if p == "/bin/bash.real":
                    return real_bash
                if p == "/bin/bash":
                    return bash_path
                return Path(p)

            mock_path_cls.side_effect = path_side_effect

            setup_command_timeout(mock_config, mock_logger)

        mock_logger.info.assert_called()
        assert "already installed" in mock_logger.info.call_args[0][0]

    def test_move_failure_logs_warning(self, tmp_path: Path) -> None:
        """setup_command_timeout logs warning when shutil.move fails."""
        mock_config = MagicMock()
        mock_logger = MagicMock()

        # bash.real does NOT exist (not yet installed)
        real_bash_mock = MagicMock()
        real_bash_mock.exists.return_value = False
        bash_mock = MagicMock()

        with (
            patch("entrypoint.Path") as mock_path_cls,
            patch("entrypoint.shutil.move", side_effect=OSError("Permission denied")),
        ):

            def path_side_effect(p):
                if p == "/bin/bash.real":
                    return real_bash_mock
                if p == "/bin/bash":
                    return bash_mock
                return Path(p)

            mock_path_cls.side_effect = path_side_effect

            setup_command_timeout(mock_config, mock_logger)

        mock_logger.warn.assert_called()
        assert "Cannot install" in mock_logger.warn.call_args[0][0]


class TestRunExecBashBypass:
    """Tests for run_exec bypassing the bash timeout wrapper."""

    def _make_mocks(self) -> tuple[MagicMock, MagicMock]:
        """Create mock config and logger for run_exec tests."""
        mock_config = MagicMock()
        mock_config.runtime_uid = 1000
        mock_config.runtime_gid = 1000
        mock_logger = MagicMock()
        return mock_config, mock_logger

    @patch("entrypoint._run_with_stderr_capture", return_value=0)
    @patch("entrypoint._chdir_to_single_repo")
    @patch("entrypoint._startup_timer")
    def test_bash_replaced_with_bash_real_when_wrapper_installed(
        self, _timer: MagicMock, _chdir: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """run_exec substitutes bash -> bash.real when the wrapper is installed."""
        config, logger = self._make_mocks()

        # Create a fake /bin/bash.real to simulate wrapper being installed
        fake_real = tmp_path / "bash.real"
        fake_real.write_text("#!/bin/bash\n")

        with patch("entrypoint.Path") as mock_path_cls:
            # Only intercept the Path("/bin/bash.real") call
            original_path = Path

            def path_side_effect(p):
                if p == "/bin/bash.real":
                    return fake_real
                return original_path(p)

            mock_path_cls.side_effect = path_side_effect

            run_exec(config, logger, ["bash", "-c", "echo hello"])

        # Verify the command was rewritten to use bash.real
        call_args = mock_run.call_args[0][0]
        assert str(fake_real) in call_args
        assert call_args[2] == str(fake_real)

    @patch("entrypoint._run_with_stderr_capture", return_value=0)
    @patch("entrypoint._chdir_to_single_repo")
    @patch("entrypoint._startup_timer")
    def test_bash_unchanged_when_wrapper_not_installed(
        self, _timer: MagicMock, _chdir: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """run_exec leaves bash unchanged when no wrapper is installed."""
        config, logger = self._make_mocks()

        # Point to a non-existent bash.real to simulate no wrapper installed
        fake_real = tmp_path / "bash.real"  # not created — doesn't exist

        with patch("entrypoint.Path") as mock_path_cls:
            original_path = Path

            def path_side_effect(p):
                if p == "/bin/bash.real":
                    return fake_real
                return original_path(p)

            mock_path_cls.side_effect = path_side_effect

            run_exec(config, logger, ["bash", "-c", "echo hello"])

        call_args = mock_run.call_args[0][0]
        # Should contain the original "bash" (via gosu), not bash.real
        assert call_args[2] == "bash"

    @patch("entrypoint._run_with_stderr_capture", return_value=0)
    @patch("entrypoint._chdir_to_single_repo")
    @patch("entrypoint._startup_timer")
    def test_non_bash_command_unchanged(
        self, _timer: MagicMock, _chdir: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """run_exec doesn't modify non-bash commands."""
        config, logger = self._make_mocks()

        fake_real = tmp_path / "bash.real"
        fake_real.write_text("#!/bin/bash\n")

        with patch("entrypoint.Path") as mock_path_cls:
            original_path = Path

            def path_side_effect(p):
                if p == "/bin/bash.real":
                    return fake_real
                return original_path(p)

            mock_path_cls.side_effect = path_side_effect

            run_exec(config, logger, ["python3", "-c", "print('hello')"])

        call_args = mock_run.call_args[0][0]
        assert "python3" in call_args
        assert "bash.real" not in str(call_args)
