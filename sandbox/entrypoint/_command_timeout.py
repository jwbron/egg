"""Bash command-timeout wrapper installation."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ._config import Config, Logger


def setup_command_timeout(config: Config, logger: Logger) -> None:
    """Install a system-level per-command timeout wrapper for bash.

    Claude Code's Bash tool executes commands via ``bash -c "command"``.
    This function installs a wrapper script that interposes on ``/bin/bash``
    and wraps ``-c`` invocations with the ``timeout`` utility, enforcing a
    configurable maximum execution time per command.

    The timeout prevents runaway commands (e.g. ``grep -rn 'pattern' /``)
    from consuming unbounded CPU and memory.  It sends SIGTERM first, waits
    a grace period, then sends SIGKILL.

    Configuration:
        BASH_COMMAND_TIMEOUT  – seconds (default 300, 0 to disable)
        BASH_COMMAND_TIMEOUT_GRACE – SIGKILL grace period (default 10)

    Non-``-c`` invocations (interactive shells, script sourcing) pass
    through to the real bash binary unmodified.
    """
    real_bash = Path("/bin/bash.real")
    bash_path = Path("/bin/bash")

    # Only install once (idempotent)
    if real_bash.exists():
        logger.info("Command timeout wrapper already installed")
        return

    timeout_secs = os.environ.get("BASH_COMMAND_TIMEOUT", "300")
    try:
        int(timeout_secs)
    except ValueError:
        logger.warn(f"Invalid BASH_COMMAND_TIMEOUT value: {timeout_secs!r}, using 300")
        timeout_secs = "300"

    # Move real bash to bash.real
    try:
        shutil.move(str(bash_path), str(real_bash))
    except OSError as e:
        logger.warn(f"Cannot install command timeout wrapper: {e}")
        return

    # Write the wrapper script
    grace_secs = os.environ.get("BASH_COMMAND_TIMEOUT_GRACE", "10")
    try:
        int(grace_secs)
    except ValueError:
        logger.warn(f"Invalid BASH_COMMAND_TIMEOUT_GRACE value: {grace_secs!r}, using 10")
        grace_secs = "10"
    wrapper = f"""\
#!/bin/bash.real
# egg command timeout wrapper — interposes on /bin/bash to enforce
# per-command timeouts for Claude Code's Bash tool.
#
# Only wraps "bash -c ..." invocations.  Interactive shells and script
# sourcing pass through to the real bash binary unmodified.

REAL_BASH=/bin/bash.real
TIMEOUT="${{BASH_COMMAND_TIMEOUT:-{timeout_secs}}}"
GRACE="${{BASH_COMMAND_TIMEOUT_GRACE:-{grace_secs}}}"

# Pass through if timeout is disabled (0 or empty)
if [ "$TIMEOUT" = "0" ] || [ -z "$TIMEOUT" ]; then
    exec "$REAL_BASH" "$@"
fi

# Only wrap -c invocations (Claude Code's Bash tool pattern)
if [ "$1" = "-c" ]; then
    # Use timeout with SIGTERM first, then SIGKILL after grace period
    exec /usr/bin/timeout --signal=TERM --kill-after="${{GRACE}}s" "${{TIMEOUT}}s" \\
        "$REAL_BASH" "$@"
fi

# All other invocations pass through unchanged
exec "$REAL_BASH" "$@"
"""
    try:
        bash_path.write_text(wrapper)
        os.chmod(str(bash_path), 0o755)  # nosec B103 - executable wrapper script
    except Exception as e:
        # Restore original bash to avoid leaving the system without /bin/bash
        logger.warn(f"Failed to write wrapper, restoring original bash: {e}")
        shutil.move(str(real_bash), str(bash_path))
        return

    os.chmod(str(real_bash), 0o755)  # nosec B103 - executable bash binary

    logger.success(f"Command timeout wrapper installed (BASH_COMMAND_TIMEOUT={timeout_secs}s)")
