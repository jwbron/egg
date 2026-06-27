"""WorktreeManager removal / teardown method bodies (#3312 slice-12).

Worktree removal (with stale-admin-dir cleanup), phase-worktree cleanup, branch
deletion, and clean-worktree teardown. Extracted verbatim and bound onto
``WorktreeManager`` in the barrel; they take ``self`` explicitly.
"""

import contextlib
import re
import shutil
import subprocess
from pathlib import Path

from ._common import (
    WorktreeRemovalResult,
    logger,
    validate_identifier,
)

try:
    from ..git_client import git_cmd
except ImportError:  # pragma: no cover - flat (container) import path
    from git_client import git_cmd  # type: ignore[no-redef, import-untyped]


def cleanup_phase_worktrees(
    self,
    container_id: str,
    repo_name: str,
    phase_ids: list[str] | None = None,
) -> list[WorktreeRemovalResult]:
    """Remove phase worktrees after integration.

    Cleans up sub-worktrees created by create_phase_worktree() after
    sub-branches have been merged.

    Args:
        container_id: Container identifier
        repo_name: Repository name
        phase_ids: Specific phase IDs to clean up. If None, cleans all
            phase worktrees for this container.

    Returns:
        List of WorktreeRemovalResult for each cleaned worktree
    """
    results: list[WorktreeRemovalResult] = []

    if phase_ids:
        for phase_id in phase_ids:
            safe_phase_id = re.sub(r"[^a-zA-Z0-9-]", "-", phase_id)
            phase_container_id = f"{container_id}-{safe_phase_id}"
            result = self.remove_worktree(
                container_id=phase_container_id,
                repo_name=repo_name,
                force=True,
                delete_branch=True,
            )
            results.append(result)
    else:
        # Clean all phase worktrees for this container by scanning.
        # Match "{container_id}-{phase}" where {phase} has the shape
        # of a sanitized phase ID ([a-zA-Z0-9-]+).  A naive
        # `startswith(prefix)` could collide if another container_id
        # extends this one (same bug class as #1865).  In practice
        # the risk is low because container_id already includes the
        # agent role suffix (e.g. "issue-1758-coder"), but
        # constraining the suffix shape adds a layer of safety.
        worktree_dir = self.worktree_base / repo_name
        if worktree_dir.exists():
            phase_pattern = re.compile(rf"{re.escape(container_id)}-[a-zA-Z0-9-]+")
            for entry in worktree_dir.iterdir():
                if (
                    entry.is_dir()
                    and entry.name != container_id
                    and phase_pattern.fullmatch(entry.name)
                ):
                    # Extract the phase container ID from dir name
                    phase_container_id = entry.name
                    result = self.remove_worktree(
                        container_id=phase_container_id,
                        repo_name=repo_name,
                        force=True,
                        delete_branch=True,
                    )
                    results.append(result)

    return results


def remove_worktree(
    self,
    container_id: str,
    repo_name: str,
    force: bool = False,
    delete_branch: bool = True,
) -> WorktreeRemovalResult:
    """
    Remove a container's worktree.

    Args:
        container_id: Container identifier
        repo_name: Repository name
        force: If True, remove even with uncommitted changes
        delete_branch: If True, delete the egg/{container_id}/work branch

    Returns:
        WorktreeRemovalResult with operation status
    """
    result = WorktreeRemovalResult(success=False)

    try:
        validate_identifier(container_id, "container_id")
        validate_identifier(repo_name, "repo_name")
    except ValueError as e:
        result.error = str(e)
        return result

    worktree_path = self.worktree_base / container_id / repo_name
    main_repo = self.repos_base / repo_name
    branch_name = f"egg/{container_id}/work"

    if not worktree_path.exists():
        # Directory already gone (e.g., Docker cleanup), but git may still
        # have a stale worktree registration in .git/worktrees/.  Clean it
        # up so `git worktree list` doesn't show the dead entry and branch
        # checkout isn't blocked.  See: https://github.com/jwbron/egg/issues/929
        if main_repo.exists():
            with self._get_repo_lock(repo_name):
                admin_dir = self._find_worktree_git_dir(main_repo, worktree_path)
                if admin_dir is not None and admin_dir.exists():
                    shutil.rmtree(admin_dir, ignore_errors=True)
                    logger.info(
                        "Removed stale worktree admin dir (directory already gone)",
                        admin_dir=str(admin_dir),
                        container_id=container_id,
                        repo=repo_name,
                    )
                elif admin_dir is None:
                    logger.info(
                        "No matching admin dir found for stale worktree, skipping cleanup",
                        container_id=container_id,
                        repo=repo_name,
                    )

                if delete_branch:
                    result.branch_deleted = self._delete_worktree_branch(
                        main_repo, branch_name, force=True
                    )

        # Clean up container directory if empty
        container_dir = self.worktree_base / container_id
        if container_dir.exists() and not any(container_dir.iterdir()):
            with contextlib.suppress(OSError):
                container_dir.rmdir()

        # Remove from memory tracking
        with self._lock:
            if container_id in self._active_worktrees:
                self._active_worktrees[container_id] = [
                    wt for wt in self._active_worktrees[container_id] if wt.repo_name != repo_name
                ]
                if not self._active_worktrees[container_id]:
                    del self._active_worktrees[container_id]

        logger.info(
            "Stale worktree cleaned up (directory already removed)",
            container_id=container_id,
            repo=repo_name,
            branch_deleted=result.branch_deleted,
        )
        result.success = True
        return result

    # Check for uncommitted changes and remove worktree under per-repo lock
    if main_repo.exists():
        with self._get_repo_lock(repo_name):
            status = subprocess.run(
                git_cmd("status", "--porcelain"),
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
            )
            has_changes = bool(status.stdout.strip())

            if has_changes and not force:
                result.uncommitted_changes = True
                result.warning = (
                    "Worktree has uncommitted changes. "
                    "Use force=True to remove anyway, or commit/stash changes first."
                )
                return result

            if has_changes:
                logger.warning(
                    "Removing worktree with uncommitted changes",
                    container_id=container_id,
                    repo=repo_name,
                )
                result.warning = "Worktree removed with uncommitted changes"

            # Find the admin dir BEFORE removal so we can clean it up
            # manually if `git worktree remove` fails.
            admin_dir = self._find_worktree_git_dir(main_repo, worktree_path)

            remove_result = subprocess.run(
                git_cmd("worktree", "remove", str(worktree_path), "--force", "--force"),
                cwd=main_repo,
                capture_output=True,
                text=True,
                check=False,
            )

            if remove_result.returncode != 0:
                # `git worktree remove` failed — clean up manually.
                # Remove the worktree directory first, then surgically
                # remove only this worktree's admin dir.  Avoids calling
                # `git worktree prune` which can accidentally remove
                # admin dirs for OTHER containers' worktrees if their
                # paths are temporarily inaccessible (e.g., Docker mount
                # race conditions during container lifecycle changes).
                logger.info(
                    "Git worktree remove failed, cleaning up manually",
                    container_id=container_id,
                    repo=repo_name,
                    stderr=remove_result.stderr,
                )
                shutil.rmtree(worktree_path, ignore_errors=True)

                # Remove the specific admin dir for this worktree.
                # admin_dir is None when no admin dir matched — skip
                # deletion to avoid destroying another container's state.
                # See: https://github.com/jwbron/egg/issues/1245
                if admin_dir is not None and admin_dir.exists():
                    shutil.rmtree(admin_dir, ignore_errors=True)
                    logger.info(
                        "Removed worktree admin dir",
                        admin_dir=str(admin_dir),
                        container_id=container_id,
                        repo=repo_name,
                    )
                elif admin_dir is None:
                    logger.warning(
                        "No matching admin dir found during manual cleanup, "
                        "skipping admin dir deletion to avoid cross-container damage",
                        container_id=container_id,
                        repo=repo_name,
                    )

            # Delete the branch if requested
            if delete_branch:
                result.branch_deleted = self._delete_worktree_branch(main_repo, branch_name, force)
                if not result.branch_deleted and not force:
                    result.warning = (
                        (result.warning or "")
                        + f" Branch {branch_name} has unmerged commits and was not deleted."
                    ).strip()
    else:
        # Main repo not found, just remove the directory
        shutil.rmtree(worktree_path, ignore_errors=True)

    # Clean up container directory if empty
    container_dir = self.worktree_base / container_id
    if container_dir.exists() and not any(container_dir.iterdir()):
        with contextlib.suppress(OSError):
            container_dir.rmdir()

    # Remove from memory tracking
    with self._lock:
        if container_id in self._active_worktrees:
            self._active_worktrees[container_id] = [
                wt for wt in self._active_worktrees[container_id] if wt.repo_name != repo_name
            ]
            if not self._active_worktrees[container_id]:
                del self._active_worktrees[container_id]

    logger.info(
        "Worktree removed",
        container_id=container_id,
        repo=repo_name,
        force=force,
        branch_deleted=result.branch_deleted,
    )

    result.success = True
    return result


def _delete_worktree_branch(self, main_repo: Path, branch_name: str, force: bool) -> bool:
    """
    Delete a worktree branch if it's safe to do so.

    Args:
        main_repo: Path to main repository
        branch_name: Name of branch to delete
        force: If True, force delete even if unmerged

    Returns:
        True if branch was deleted, False otherwise
    """
    # Check if branch is fully merged
    merge_check = subprocess.run(
        git_cmd("branch", "--merged", "HEAD", "--list", branch_name),
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    is_merged = branch_name in merge_check.stdout

    if is_merged or force:
        delete_result = subprocess.run(
            git_cmd("branch", "-D" if force else "-d", branch_name),
            cwd=main_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return delete_result.returncode == 0

    return False


def cleanup_clean_worktree(self, container_id: str, repo_name: str) -> bool:
    """Remove a worktree that has no uncommitted changes.

    Called after container exit for worktrees without uncommitted work.

    Returns:
        True if cleaned up, False if has uncommitted changes or error.
    """
    worktree_path = self.worktree_base / container_id / repo_name
    if not worktree_path.exists():
        return True  # Already cleaned

    # Check for uncommitted changes
    try:
        result = subprocess.run(
            git_cmd("status", "--porcelain"),
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info(
                "Worktree has uncommitted changes, preserving for HITL",
                container_id=container_id,
                repo_name=repo_name,
            )
            return False
    except Exception as e:
        logger.warning(
            "Failed to check worktree status for cleanup",
            container_id=container_id,
            error=str(e),
        )
        return False

    # Clean worktree -- remove it.  Use force=False as a TOCTOU safety net:
    # if changes were made between the check and removal, git will refuse
    # rather than silently discarding.  (#1494 review)
    removal = self.remove_worktree(container_id, repo_name, force=False, delete_branch=True)
    return removal.success
