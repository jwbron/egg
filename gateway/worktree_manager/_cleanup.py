"""WorktreeManager orphan/stale-sweep method bodies (#3312 slice-12).

Orphaned-worktree cleanup, ``git worktree prune`` variants, orphan-dir enumeration,
orphaned pack-file cleanup, and age-based stale-pipeline sweeps. Extracted verbatim and
bound onto ``WorktreeManager`` in the barrel; ``_is_pipeline_anchored`` becomes a
module-level function (re-bound as a ``staticmethod``). The Docker-container seam is read
off the barrel via ``_barrel()`` so ``patch("worktree_manager.get_active_docker_containers")``
resolves.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ._common import (
    _format_bytes,
    logger,
)

try:
    from ..git_client import git_cmd
except ImportError:  # pragma: no cover - flat (container) import path
    from git_client import git_cmd  # type: ignore[no-redef, import-untyped]


def _barrel():
    """Return the package barrel so patched seams resolve at call time.

    Method bodies extracted here reference module-level symbols that tests
    rebind via ``patch("worktree_manager.<symbol>")`` (e.g. ``get_token_for_repo``,
    ``get_active_docker_containers``). Reading them off the barrel module at call
    time preserves those patch points across the split.
    """
    return sys.modules[__package__]


def _is_pipeline_anchored(container_id: str, active_pipeline_ids: set[str]) -> bool:
    """True when *container_id* belongs to an active pipeline.

    Worktree dir names are either the pipeline ID itself (the
    pipeline-level worktree) or ``{pipeline_id}-{suffix}`` (per-agent
    worktrees, e.g. ``pipeline-c978dac3-refiner``,
    ``issue-3023-slice-1-coder``). The delimiter anchor prevents
    ``issue-302`` from matching ``issue-3023-*``.

    Intentionally looser than ``list_worktrees_for_pipeline``'s
    ``{pid}-[a-z_]+`` regex (#1865): slice-scoped suffixes like
    ``issue-3023-slice-1-coder`` contain digits and would be
    falsely orphaned by the stricter pattern. Over-preserving on
    one-active-pipeline-ID-prefixes-another is the right side of
    the fail-safe principle here (#3070).
    """
    return any(
        container_id == pid or container_id.startswith(f"{pid}-") for pid in active_pipeline_ids
    )


def cleanup_orphaned_worktrees(
    self,
    active_containers: set[str],
    session_manager: Any | None = None,
    active_pipeline_ids: set[str] | None = None,
) -> int:
    """
    Remove worktrees for containers that no longer exist.

    Called on gateway startup and periodically to clean up orphaned worktrees
    from crashed containers.

    For orphaned containers with active sessions, auto-commits the agent's
    uncommitted work before cleaning up.

    Args:
        active_containers: Set of currently active container IDs
        session_manager: Optional SessionManager for session auto-commit on cleanup
        active_pipeline_ids: IDs of non-terminal pipelines per the
            orchestrator. Worktrees anchored to them are preserved even
            with no live container — a pipeline parked at a HITL gate or
            between phases has no containers and no sessions, but its
            worktree holds the contract and any un-pushed work (#3070).
            ``None`` means liveness could not be verified; callers must
            not invoke this sweep in that case (see ``startup_cleanup``).

    Returns:
        Number of worktrees removed
    """
    removed = 0

    if not self.worktree_base.exists():
        return removed

    for container_dir in list(self.worktree_base.iterdir()):
        if not container_dir.is_dir():
            continue

        container_id = container_dir.name

        # Skip active containers
        if container_id in active_containers:
            continue

        # Skip worktrees belonging to a live pipeline, container or not.
        if active_pipeline_ids and self._is_pipeline_anchored(container_id, active_pipeline_ids):
            continue

        # Skip worktrees that this process just created.  create_worktree
        # populates ``_active_worktrees[container_id]`` before returning,
        # so any per-agent worktree made during this gateway's lifetime
        # is protected even if its session was not yet registered when
        # ``active_containers`` was captured — closing the spawn-vs-prune
        # race that motivated #1874.  Re-checked per iteration so a
        # worktree created mid-sweep is still shielded.
        with self._lock:
            if container_id in self._active_worktrees:
                continue

        logger.info(
            "Cleaning up orphaned worktrees",
            container_id=container_id,
        )

        # Auto-commit the crashed container's uncommitted work before
        # removing worktrees — auto-commit runs synchronously and uses the
        # repo dir as cwd, so it must finish before the dir is deleted.
        if session_manager is not None:
            try:
                session = session_manager.get_session_by_container(container_id)
                if session:
                    from session_manager import _capture_and_cleanup_session  # type: ignore[import-untyped]  # noqa: I001

                    _capture_and_cleanup_session(session, "failed")
            except Exception as e:
                logger.warning(
                    "Failed to auto-commit orphaned container",
                    container_id=container_id,
                    error=str(e),
                )

        # Remove each worktree. Never delete branches here: an orphan
        # sweep cannot know whether the work they point at was pushed,
        # and deleting them turns a recoverable mistake into data loss —
        # in #3070 this left an operator-approved analysis reachable only
        # as a dangling commit. Branch deletion stays with the explicit
        # per-container teardown paths.
        for worktree in list(container_dir.iterdir()):
            if worktree.is_dir():
                result = self.remove_worktree(
                    container_id, worktree.name, force=True, delete_branch=False
                )
                if result.success:
                    removed += 1
                else:
                    logger.warning(
                        "Failed to remove orphaned worktree",
                        container_id=container_id,
                        repo=worktree.name,
                        error=result.error,
                    )

        # Remove container directory
        try:
            if container_dir.exists():
                shutil.rmtree(container_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(
                "Failed to remove container worktree dir",
                container_id=container_id,
                error=str(e),
            )

    return removed


def prune_stale_worktrees(self) -> int:
    """
    Run ``git worktree prune`` on every repo to remove registrations
    whose working directories no longer exist.

    This is a defense-in-depth measure intended to be called at gateway
    startup (after orphan cleanup), when the set of active containers is
    known and Docker mount races are not a concern.  It catches any stale
    entries that slipped past ``remove_worktree`` — for example if the
    gateway crashed before cleanup could run.

    Note: ``git worktree prune`` respects locks.  Because
    ``create_worktree`` locks every worktree to protect it from
    ``git gc --auto``, a stale locked admin dir (e.g., gateway crash +
    Docker cleanup) will **not** be pruned by this method.  The
    ``cleanup_orphaned_worktrees`` → ``remove_worktree`` path handles
    this case via its manual ``shutil.rmtree`` fallback.

    Returns:
        Number of repos where ``git worktree prune`` ran successfully.
    """
    repos_checked = 0
    if not self.repos_base.exists():
        return repos_checked

    for repo_dir in self.repos_base.iterdir():
        if not repo_dir.is_dir():
            continue
        git_dir = repo_dir / ".git"
        # Only prune actual repos (with a .git directory, not a .git file
        # which would indicate a worktree itself)
        if not git_dir.is_dir():
            continue

        repo_name = repo_dir.name
        with self._get_repo_lock(repo_name):
            try:
                result = subprocess.run(
                    git_cmd("worktree", "prune"),
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "git worktree prune timed out",
                    repo=repo_name,
                )
                continue
            if result.returncode == 0:
                repos_checked += 1
            else:
                logger.warning(
                    "git worktree prune failed",
                    repo=repo_name,
                    stderr=result.stderr.strip(),
                )

    if repos_checked > 0:
        logger.info(
            "Ran git worktree prune on repos",
            repos_checked=repos_checked,
        )

    return repos_checked


def git_worktree_prune_all(self) -> dict[str, list[str]]:
    """Run ``git worktree prune -v`` on each repo, returning pruned paths.

    Unlike :meth:`prune_stale_worktrees` (which returns the *count* of
    repos it ran against), this variant captures the porcelain output
    so callers can display the specific paths git reported as stale.
    Intended for operator-facing tooling where the set of removed
    registrations is interesting on its own.

    Returns:
        Dict keyed by repo name, each value a list of paths git
        reported as pruned (may be empty). Repos git skipped due to
        locks are not present.
    """
    result: dict[str, list[str]] = {}
    if not self.repos_base.exists():
        return result

    for repo_dir in self.repos_base.iterdir():
        if not repo_dir.is_dir():
            continue
        git_dir = repo_dir / ".git"
        if not git_dir.is_dir():
            continue
        repo_name = repo_dir.name
        with self._get_repo_lock(repo_name):
            try:
                proc = subprocess.run(
                    git_cmd("worktree", "prune", "--verbose"),
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "git worktree prune timed out",
                    repo=repo_name,
                )
                continue
            if proc.returncode != 0:
                logger.warning(
                    "git worktree prune failed",
                    repo=repo_name,
                    stderr=proc.stderr.strip(),
                )
                continue
            # -v output format: "Removing worktrees/<name>: <reason>"
            paths: list[str] = []
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.lower().startswith("removing "):
                    tail = line[len("Removing ") :]
                    name = tail.split(":", 1)[0].strip()
                    if name:
                        paths.append(name)
            result[repo_name] = paths
    return result


def list_orphan_worktree_dirs(
    self,
    active_containers: set[str],
    active_pipeline_ids: set[str] | None = None,
) -> list[str]:
    """Return absolute paths of container dirs considered orphaned.

    A container dir under ``worktree_base`` is considered orphaned
    when its name is not in *active_containers* and it is not anchored
    to an active pipeline (mirrors ``cleanup_orphaned_worktrees`` so
    dry-run output matches what the sweep would do).  Each returned
    path is first validated via :func:`Path.resolve` +
    ``is_relative_to(self.worktree_base)`` to protect against
    symlink-based traversal.
    """
    orphans: list[str] = []
    if not self.worktree_base.exists():
        return orphans

    base_resolved = self.worktree_base.resolve()
    for child in self.worktree_base.iterdir():
        if not child.is_dir():
            continue
        try:
            resolved = child.resolve()
        except OSError:
            continue
        # Path-traversal guard: every candidate must live under the
        # worktree base even after symlink resolution.
        try:
            if not resolved.is_relative_to(base_resolved):
                logger.warning(
                    "worktree dir resolves outside base; skipping",
                    child=str(child),
                    resolved=str(resolved),
                )
                continue
        except AttributeError:
            # Python <3.9 fallback (should never hit on our runtime)
            if not str(resolved).startswith(str(base_resolved) + os.sep):
                continue
        if child.name in active_containers:
            continue
        if active_pipeline_ids and self._is_pipeline_anchored(child.name, active_pipeline_ids):
            continue
        # Mirror the _active_worktrees guard from cleanup_orphaned_worktrees
        # so dry-run output accurately reflects what cleanup would skip.
        with self._lock:
            if child.name in self._active_worktrees:
                continue
        orphans.append(str(child))
    return orphans


def cleanup_orphaned_pack_files(
    self,
    repo_name: str | None = None,
    max_age_seconds: float | None = None,
) -> tuple[int, int]:
    """
    Remove orphaned temporary pack files from git repositories.

    Git creates ``tmp_pack_*``, ``tmp_obj_*``, and ``tmp_idx_*`` files in
    ``.git/objects/pack/`` during fetch/clone/repack operations.  When the
    operation completes, git renames them to their final names.  When the
    operation is **interrupted** (SIGKILL, OOM, container stop, timeout),
    the temp files are left behind as orphans — git never garbage-collects
    them automatically, especially with ``gc.auto=0``.

    These files are always safe to delete when no git operation is actively
    writing them.  Use ``max_age_seconds`` to avoid racing with concurrent
    operations at runtime.  At startup (no concurrent ops), pass
    ``max_age_seconds=None`` to clean everything.

    Args:
        repo_name: If set, only clean this specific repo.  Otherwise scan
            all repos in ``repos_base``.
        max_age_seconds: If set, only remove files whose mtime is older
            than this many seconds ago.  None means remove all.

    Returns:
        Tuple of (files_removed, bytes_reclaimed).
    """
    files_removed = 0
    bytes_reclaimed = 0

    if not self.repos_base.exists():
        return files_removed, bytes_reclaimed

    if repo_name is not None:
        repo_dirs = [self.repos_base / repo_name]
    else:
        try:
            repo_dirs = [d for d in self.repos_base.iterdir() if d.is_dir()]
        except OSError as e:
            logger.warning("Failed to list repos_base", error=str(e))
            return files_removed, bytes_reclaimed

    now = time.time()

    for repo_dir in repo_dirs:
        git_dir = repo_dir / ".git"
        # Only process actual repos (with a .git directory, not a .git file
        # which would indicate a worktree itself)
        if not git_dir.is_dir():
            continue

        pack_dir = git_dir / "objects" / "pack"
        if not pack_dir.is_dir():
            continue

        safe_name = repo_dir.name
        with self._get_repo_lock(safe_name):
            try:
                for entry in pack_dir.iterdir():
                    name = entry.name
                    if not (
                        name.startswith("tmp_pack_")
                        or name.startswith("tmp_obj_")
                        or name.startswith("tmp_idx_")
                    ):
                        continue

                    if not entry.is_file():
                        continue

                    try:
                        st = entry.stat()
                    except OSError:
                        continue

                    if max_age_seconds is not None:
                        if (now - st.st_mtime) < max_age_seconds:
                            continue

                    try:
                        entry.unlink()
                        files_removed += 1
                        bytes_reclaimed += st.st_size
                    except OSError as e:
                        logger.warning(
                            "Failed to remove orphaned pack file",
                            file=str(entry),
                            error=str(e),
                        )
            except OSError as e:
                logger.warning(
                    "Failed to scan pack directory",
                    repo=safe_name,
                    pack_dir=str(pack_dir),
                    error=str(e),
                )

    if files_removed > 0:
        logger.info(
            "Cleaned up orphaned pack files",
            files_removed=files_removed,
            bytes_reclaimed=bytes_reclaimed,
            bytes_reclaimed_human=_format_bytes(bytes_reclaimed),
        )

    return files_removed, bytes_reclaimed


def cleanup_stale_pipeline_worktrees(
    self,
    max_age_hours: int = 48,
    active_containers: set[str] | None = None,
    active_pipeline_ids: set[str] | None = None,
) -> int:
    """Remove worktrees older than max_age_hours regardless of state.

    Periodic cleanup to prevent disk space exhaustion from abandoned
    worktrees.

    TODO: Wire this into the orchestrator's maintenance loop. Currently
    only called from tests — not yet connected to production scheduling.
    Whoever wires this up must pass ``active_pipeline_ids`` from
    ``orchestrator_pipelines.fetch_active_pipeline_ids`` — otherwise a
    long-parked HITL pipeline whose mtimes have aged past ``max_age_hours``
    becomes the next #3070 (an idle parked pipeline has no container and
    no session activity; only the orchestrator-derived set distinguishes
    it from a crashed leftover).

    Args:
        max_age_hours: Worktrees inactive for longer than this are removed.
        active_containers: Set of running container IDs. Worktrees with
            active containers are never deleted. If None, fetched via
            ``get_active_docker_containers()``.
        active_pipeline_ids: IDs of non-terminal pipelines per the
            orchestrator. Worktrees anchored to them are preserved even
            when their mtimes look stale — mirrors
            ``cleanup_orphaned_worktrees`` (#3070). ``None`` means
            "skip the anchor check"; pass a verified set when wiring this
            into production scheduling.

    Returns:
        Number of worktrees removed.
    """
    removed = 0
    if not self.worktree_base.exists():
        return removed

    if active_containers is None:
        active_containers = _barrel().get_active_docker_containers()

    cutoff = time.time() - (max_age_hours * 3600)

    for entry in self.worktree_base.iterdir():
        if not entry.is_dir():
            continue
        # Skip worktrees whose containers are still running.
        if entry.name in active_containers:
            continue
        # Skip worktrees anchored to an active pipeline — a parked HITL
        # pipeline has no container, so age-based deletion would strand
        # its contract and un-pushed work (#3070).
        if active_pipeline_ids and self._is_pipeline_anchored(entry.name, active_pipeline_ids):
            continue
        try:
            # Use .git/index (updated on every commit/checkout) as the
            # staleness signal instead of walking the entire tree, which
            # would cause an I/O storm on large worktrees containing
            # .git/objects, node_modules, build artifacts, etc.
            newest_mtime = entry.stat().st_mtime
            for repo_subdir in entry.iterdir():
                if not repo_subdir.is_dir():
                    continue
                dot_git = repo_subdir / ".git"
                try:
                    # Worktrees have a .git *file* (not directory)
                    # containing "gitdir: /path/to/admin/dir".
                    # Resolve to the actual git admin dir to check
                    # index/HEAD mtime.
                    if dot_git.is_file():
                        gitdir_line = dot_git.read_text().strip()
                        if gitdir_line.startswith("gitdir:"):
                            git_admin = Path(gitdir_line.split(":", 1)[1].strip())
                            if not git_admin.is_absolute():
                                git_admin = (repo_subdir / git_admin).resolve()
                            for git_file in ("index", "HEAD"):
                                try:
                                    fmt = os.stat(git_admin / git_file).st_mtime
                                    if fmt > newest_mtime:
                                        newest_mtime = fmt
                                except OSError:
                                    continue
                    elif dot_git.is_dir():
                        # Full clone (shouldn't happen but handle it)
                        for git_file in ("index", "HEAD"):
                            try:
                                fmt = os.stat(dot_git / git_file).st_mtime
                                if fmt > newest_mtime:
                                    newest_mtime = fmt
                            except OSError:
                                continue
                except OSError:
                    continue
            if newest_mtime < cutoff:
                for repo_dir in entry.iterdir():
                    if repo_dir.is_dir():
                        # Never delete branches in a periodic age sweep:
                        # the sweep can't know whether the work they
                        # point at was pushed, and deleting them turns a
                        # recoverable mistake into data loss. Branch
                        # deletion stays with explicit per-container
                        # teardown paths (mirrors
                        # ``cleanup_orphaned_worktrees``; #3070).
                        removal_result = self.remove_worktree(
                            entry.name, repo_dir.name, force=True, delete_branch=False
                        )
                        if removal_result.success:
                            removed += 1
                        else:
                            logger.warning(
                                "Failed to remove stale worktree",
                                container_id=entry.name,
                                repo=repo_dir.name,
                                error=removal_result.error,
                            )
                # Clean up empty container directory
                if entry.exists() and not any(entry.iterdir()):
                    entry.rmdir()
        except Exception as e:
            logger.warning(
                "Error during stale worktree cleanup",
                container_id=entry.name,
                error=str(e),
            )

    logger.info(
        "Stale worktree cleanup complete",
        removed=removed,
        max_age_hours=max_age_hours,
    )
    return removed
