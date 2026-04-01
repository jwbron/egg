"""Post-agent uncommitted work detection for worktrees.

Previously, this module auto-committed and pushed uncommitted changes
when an agent container exited.  With per-agent worktree isolation
(#1481), uncommitted work persists in the worktree on disk and the
orchestrator detects it for HITL recovery.  Auto-commit is now a
logged no-op -- see ``auto_commit_worktree()``.

Called from the session cleanup flow in ``session_manager.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_logging import get_logger

logger = get_logger("gateway.post-agent-commit")


def _git(*args: str, cwd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a git command with security configs."""
    cmd = [
        "/usr/bin/git",
        "-c",
        "safe.directory=*",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
    ]
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _parse_changed_files(porcelain_output: str) -> list[str]:
    """Parse file paths from ``git status --porcelain`` output.

    Handles renames (``R  old -> new`` keeps the new path) and
    strips the two-character status prefix.

    Limitations:
        Filenames containing literal `` -> `` would be split incorrectly,
        and filenames with intentional leading/trailing spaces are stripped.
        Git uses quoting for such paths in porcelain output, which this
        parser does not handle.  These are extreme edge cases unlikely to
        occur in practice.

    Returns:
        List of relative file paths with changes.
    """
    files: list[str] = []
    for line in porcelain_output.splitlines():
        if not line or len(line) < 4:
            continue
        # Porcelain format: XY <path> or XY <old> -> <new>
        path_part = line[3:]
        if " -> " in path_part:
            # Rename: keep the destination path
            path_part = path_part.split(" -> ", 1)[1]
        files.append(path_part.strip())
    return files


def _push_via_gateway(
    worktree_path: str,
    session_token: str,
    gateway_url: str,
    branch: str,
    timeout: int = 30,
) -> bool:
    """Push the auto-commit via the gateway API.

    Uses the gateway's ``/api/v1/git/push`` endpoint so that all push
    policy (branch ownership, phase restrictions) is enforced.

    Returns:
        True if push succeeded, False otherwise.
    """
    try:
        import json
        import urllib.request

        # repo_path is the worktree path, which is a valid git working
        # directory.  The gateway's git_push() handler resolves remotes and
        # branches from the working directory, so this works correctly for
        # worktrees (not just the main .git dir).
        payload = json.dumps(
            {
                "repo_path": worktree_path,
                "remote": "origin",
                "refspec": branch,
            }
        ).encode()

        req = urllib.request.Request(
            f"{gateway_url}/api/v1/git/push",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {session_token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(resp.status == 200)
    except Exception as e:
        logger.warning(
            "Push via gateway failed",
            worktree_path=worktree_path,
            error=str(e),
        )
        return False


def auto_commit_worktree(
    worktree_path: str,
    container_id: str,
    agent_role: str | None = None,
    pipeline_id: str | None = None,
    phase: str | None = None,
    session_token: str | None = None,
    gateway_url: str | None = None,
    consensus_confirmed: bool = False,
) -> str | None:
    """Auto-commit is disabled — per-agent worktrees preserve uncommitted work.

    With per-agent worktree isolation (#1481), each agent's uncommitted work
    persists in its own worktree on disk after container exit. The orchestrator
    detects uncommitted changes and creates a HITL decision for recovery.
    Auto-committing and pushing bypassed BRC consensus (#1480) and is no longer
    needed.

    Returns:
        Always None (no commit is created).
    """
    if not Path(worktree_path).is_dir():
        return None

    # Check if there are uncommitted changes — log for visibility but don't commit
    try:
        status = _git("status", "--porcelain", cwd=worktree_path)
        if status.returncode == 0 and status.stdout.strip():
            changed_files = _parse_changed_files(status.stdout)
            logger.info(
                "Auto-commit disabled — uncommitted changes preserved in worktree",
                event_type="post_agent_auto_commit_disabled",
                worktree_path=worktree_path,
                container_id=container_id,
                agent_role=agent_role,
                pipeline_id=pipeline_id,
                phase=phase,
                changed_file_count=len(changed_files),
                changed_files=changed_files[:10],  # Log first 10 for debugging
            )
        else:
            logger.debug(
                "No uncommitted changes in worktree",
                worktree_path=worktree_path,
                container_id=container_id,
            )
    except Exception as e:
        logger.debug(
            "Failed to check worktree status",
            worktree_path=worktree_path,
            error=str(e),
        )

    return None
