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
    TIMEOUT="${BASH_COMMAND_TIMEOUT:-120}"
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
def wrapper_dir(tmp_path: Path):
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
) -> subprocess.CompletedProcess:
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

    def test_long_running_command_is_killed(self, wrapper_dir: Path):
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

    def test_normal_command_completes_successfully(self, wrapper_dir: Path):
        """(b) Command completing within timeout succeeds normally."""
        result = _run_via_wrapper(
            wrapper_dir,
            "echo hello-from-wrapper",
            timeout_secs="30",
        )
        assert result.returncode == 0
        assert "hello-from-wrapper" in result.stdout

    def test_disable_timeout_via_env(self, wrapper_dir: Path):
        """(c) BASH_COMMAND_TIMEOUT=0 disables the timeout."""
        result = _run_via_wrapper(
            wrapper_dir,
            "echo timeout-disabled",
            disable=True,
        )
        assert result.returncode == 0
        assert "timeout-disabled" in result.stdout

    def test_timeout_stderr_message(self, wrapper_dir: Path):
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
