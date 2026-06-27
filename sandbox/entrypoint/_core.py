"""Leaf helpers and earliest-captured constants for the entrypoint package."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

_CONTAINER_START_TIME = time.time()

_SUBPROCESS_STDERR_LOG = Path("/tmp/egg-subprocess-stderr.log")


def run_cmd(
    cmd: list[str],
    check: bool = True,
    capture: bool = False,
    timeout: int = 30,
    as_user: tuple[int, int] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, optionally as a different user via gosu."""
    if as_user:
        uid, gid = as_user
        cmd = ["gosu", f"{uid}:{gid}"] + cmd

    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def chown_recursive(path: Path, uid: int, gid: int) -> None:
    """Recursively change ownership of a path, tolerating read-only mounts."""
    result = subprocess.run(
        ["chown", "-R", f"{uid}:{gid}", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Filter out read-only filesystem errors (from bind mounts like .git shadow)
        real_errors = [
            line
            for line in result.stderr.strip().splitlines()
            if "Read-only file system" not in line
        ]
        if real_errors:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )


def _read_subprocess_stderr_tail(max_lines: int = 20) -> str:
    """Read the last N lines from the subprocess stderr log, if it exists."""
    try:
        if _SUBPROCESS_STDERR_LOG.exists():
            content = _SUBPROCESS_STDERR_LOG.read_text(errors="replace").strip()
            if content:
                lines = content.splitlines()[-max_lines:]
                return "\n".join(lines)
    except Exception:
        pass
    return ""
