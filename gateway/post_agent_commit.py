"""Post-agent auto-commit for uncommitted work in worktrees.

When an agent container exits (normally or via timeout), this module
checks the worktree for uncommitted changes and creates a WIP commit
so that work is never lost.  Phase-restricted files are restored (not
committed) to prevent persisting phase-violating changes.

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
) -> str | None:
    """Create a WIP commit for any uncommitted changes in a worktree.

    When a ``phase`` is provided, files are checked against phase-based
    file restrictions using ``check_phase_file_restrictions``.  Blocked
    files are restored via ``git checkout`` and only allowed files are
    committed.  If a ``session_token`` and ``gateway_url`` are provided,
    the commit is pushed through the gateway API.

    Args:
        worktree_path: Absolute path to the worktree directory.
        container_id: Container ID for the commit message.
        agent_role: Agent role (e.g., "coder") for the commit message.
        pipeline_id: Pipeline ID for the commit message.
        phase: SDLC phase for file restriction filtering.
        session_token: Gateway session token for pushing.
        gateway_url: Gateway base URL (e.g., ``http://egg-gateway:9848``).

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

        changed_files = _parse_changed_files(status.stdout)

        # Determine which files are allowed vs blocked by phase restrictions.
        # Defense-in-depth: reuses check_phase_file_restrictions() from phase_filter
        # (same function used by push-time validation in gateway.py) to ensure
        # consistent enforcement.  If the import fails, all files are allowed
        # (fail-open) since push-time validation remains the authoritative gate.
        allowed_files = list(changed_files)
        blocked_files: list[str] = []

        if phase and changed_files:
            try:
                from phase_filter import check_phase_file_restrictions  # type: ignore[import-not-found]  # noqa: I001
            except ImportError:
                try:
                    from gateway.phase_filter import check_phase_file_restrictions
                except ImportError:
                    check_phase_file_restrictions = None  # type: ignore[assignment, unused-ignore]

            if check_phase_file_restrictions is not None:
                result = check_phase_file_restrictions(phase, changed_files)
                if not result.allowed:
                    blocked_files = result.blocked_files or []
                    allowed_files = [f for f in changed_files if f not in blocked_files]

                    logger.info(
                        "Phase restrictions filter auto-commit files",
                        event_type="post_agent_phase_filter",
                        phase=phase,
                        blocked_files=blocked_files,
                        allowed_count=len(allowed_files),
                        container_id=container_id,
                    )

        # Restore blocked files to their committed state.
        if blocked_files:
            for bf in blocked_files:
                restore = _git("checkout", "--", bf, cwd=worktree_path)
                if restore.returncode != 0:
                    logger.warning(
                        "Failed to restore blocked file",
                        file=bf,
                        stderr=restore.stderr,
                        container_id=container_id,
                    )

        # If no allowed files remain after filtering, nothing to commit.
        if not allowed_files:
            logger.info(
                "All changed files blocked by phase restrictions, skipping auto-commit",
                event_type="post_agent_auto_commit_skipped",
                container_id=container_id,
                phase=phase,
                blocked_files=blocked_files,
            )
            return None

        # Stage only allowed files (not git add -A which stages everything).
        add_result = _git("add", "--", *allowed_files, cwd=worktree_path)
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
            "-m",
            message,
            "--author",
            "egg <egg@localhost>",
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
        sha = rev.stdout.strip() if rev.returncode == 0 else None

        logger.info(
            "Auto-committed uncommitted work",
            event_type="post_agent_auto_commit",
            worktree_path=worktree_path,
            container_id=container_id,
            agent_role=agent_role,
            pipeline_id=pipeline_id,
            phase=phase,
            commit_sha=sha,
            allowed_files=allowed_files,
            blocked_files=blocked_files,
        )

        # Push via gateway if session credentials are available.
        if session_token and gateway_url:
            # Determine branch name for push refspec.
            branch_result = _git(
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                cwd=worktree_path,
            )
            if branch_result.returncode == 0 and branch_result.stdout.strip():
                branch = branch_result.stdout.strip()
                pushed = _push_via_gateway(
                    worktree_path,
                    session_token,
                    gateway_url,
                    branch,
                )
                if pushed:
                    logger.info(
                        "Auto-commit pushed via gateway",
                        event_type="post_agent_auto_push",
                        commit_sha=sha,
                        branch=branch,
                        container_id=container_id,
                    )
                else:
                    logger.warning(
                        "Auto-commit push failed, commit is local only",
                        commit_sha=sha,
                        container_id=container_id,
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
