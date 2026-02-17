"""Post-agent auto-commit for uncommitted work in worktrees.

When an agent container exits (normally or via timeout), this module
checks the worktree for uncommitted changes and creates a WIP commit
so that work is never lost.

Called from the session cleanup flow in ``session_manager.py``.
"""

from __future__ import annotations

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
        "-c", "safe.directory=*",
        "-c", "core.hooksPath=/dev/null",
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


def auto_commit_worktree(
    worktree_path: str,
    container_id: str,
    agent_role: str | None = None,
    pipeline_id: str | None = None,
) -> str | None:
    """Create a WIP commit for any uncommitted changes in a worktree.

    Stages all modified/untracked files and commits with a descriptive
    message.  Returns the commit SHA on success, or ``None`` if there
    were no changes or the commit failed.

    Args:
        worktree_path: Absolute path to the worktree directory.
        container_id: Container ID for the commit message.
        agent_role: Agent role (e.g., "coder") for the commit message.
        pipeline_id: Pipeline ID for the commit message.

    Returns:
        Commit SHA string if a commit was made, None otherwise.
    """
    if not Path(worktree_path).is_dir():
        logger.debug(
            "Worktree path does not exist, skipping auto-commit",
            worktree_path=worktree_path,
        )
        return None

    try:
        # Check for uncommitted changes (porcelain output is empty if clean)
        status = _git("status", "--porcelain", cwd=worktree_path)
        if status.returncode != 0 or not status.stdout.strip():
            logger.debug(
                "No uncommitted changes in worktree",
                worktree_path=worktree_path,
                container_id=container_id,
            )
            return None

        # Stage all changes
        add_result = _git("add", "-A", cwd=worktree_path)
        if add_result.returncode != 0:
            logger.warning(
                "Failed to stage changes for auto-commit",
                worktree_path=worktree_path,
                stderr=add_result.stderr,
            )
            return None

        # Build commit message
        role_part = f" ({agent_role})" if agent_role else ""
        pipeline_part = f" [{pipeline_id}]" if pipeline_id else ""
        message = (
            f"WIP: auto-commit uncommitted work{role_part}{pipeline_part}\n\n"
            f"Container {container_id} exited with uncommitted changes.\n"
            f"This commit preserves the agent's work-in-progress.\n\n"
            f"Authored-by: egg"
        )

        # Commit with --no-verify (hooks are disabled anyway)
        commit_result = _git(
            "commit",
            "--no-verify",
            "-m", message,
            "--author", "egg <egg@localhost>",
            cwd=worktree_path,
        )
        if commit_result.returncode != 0:
            logger.warning(
                "Auto-commit failed",
                worktree_path=worktree_path,
                stderr=commit_result.stderr,
            )
            return None

        # Get the commit SHA
        rev = _git("rev-parse", "HEAD", cwd=worktree_path)
        sha = rev.stdout.strip() if rev.returncode == 0 else "unknown"

        logger.info(
            "Auto-committed uncommitted work",
            event_type="post_agent_auto_commit",
            worktree_path=worktree_path,
            container_id=container_id,
            agent_role=agent_role,
            pipeline_id=pipeline_id,
            commit_sha=sha,
        )
        return sha

    except subprocess.TimeoutExpired:
        logger.warning(
            "Auto-commit timed out",
            worktree_path=worktree_path,
            container_id=container_id,
        )
        return None
    except Exception as e:
        logger.warning(
            "Auto-commit failed with unexpected error",
            worktree_path=worktree_path,
            container_id=container_id,
            error=str(e),
        )
        return None
