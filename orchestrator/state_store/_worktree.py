"""State worktree lifecycle for ``StateStore`` (#3312).

Method bodies extracted verbatim from the pre-split ``state_store.py`` as
module-level functions taking ``self`` explicitly (decomposition-pattern.md
§c). The barrel binds these onto the ``StateStore`` class. The
``_BRANCH_IN_USE_PATTERN`` class attribute stays in the barrel and is reached
here via ``self._BRANCH_IN_USE_PATTERN``.
"""

import shutil
import time
from pathlib import Path

from egg_config.constants import PIPELINE_STATE_BRANCH as STATE_BRANCH

from . import logger
from ._errors import GitOperationError, StateStoreError


def _ensure_worktree(self) -> Path:
    """Create or validate the persistent state worktree."""
    # Fail-fast guard: every code path below runs `git` from
    # ``self.repo_path``. If that path is not itself a git repository
    # (e.g., a deployment sets ``EGG_REPO_PATH=/home/egg/repos`` — a
    # parent dir containing several repos — and a caller constructs
    # ``StateStore`` directly without going through
    # :func:`get_state_store`'s multi-repo discovery), git walks up
    # to the mount point and produces an opaque "not a git repository"
    # error from inside `git worktree add`. Catch that case here and
    # raise an actionable error naming the offending env var.
    if not (self.repo_path / ".git").exists():
        raise StateStoreError(
            f"StateStore.repo_path is not a git repository: {self.repo_path}. "
            f"This usually means EGG_REPO_PATH points at a parent directory "
            f"containing multiple repos rather than a single repo. Use "
            f"`get_state_store(repo_path)` (which calls `discover_repo_paths`) "
            f"to resolve the parent to a specific repo before constructing "
            f"the state store."
        )

    # Serialize the entire worktree bring-up sequence under the same
    # reentrant RLock + flock that ``_run_git`` uses.  Without this,
    # concurrent callers (state-store probe, pipeline driver thread,
    # ``/api/v1/health``) race between detect-failure → cleanup →
    # retry inside ``_add_worktree_with_branch_recovery`` and surface
    # misleading one-shot 500s on whichever arrived second (#2177).
    # The same root cause produced #2234's ENOENT race.  ``_git_op``
    # delegates to ``bare_repo_lock``, which is reentrant via a depth
    # counter, so nested ``_run_git`` calls compose without deadlock.
    # Cost: lock window grows from "one git command" to "the
    # worktree-bring-up sequence" — tens of ms in steady state; the
    # cold-start ``_restore_from_remote`` fetch is the longest case,
    # runs at most once per repo per process.
    with self._git_op():
        # Clean up stale admin dir for THIS worktree only (e.g., from crashes).
        # IMPORTANT: Do NOT use `git worktree prune` — the orchestrator cannot
        # see the gateway's worktree paths (different bind mounts), so prune
        # would incorrectly remove admin dirs for active gateway worktrees,
        # breaking all container git operations.
        #
        # This early call covers the wt-gone case: the worktree directory was
        # wiped (e.g., state volume reset) but the admin dir under
        # `<repo>/.git/worktrees/` survived.  The forced call later in this
        # method covers the wt-broken case: the worktree directory
        # is on disk but rev-parse rejects it.
        self._remove_stale_admin_dir()

        wt = self._worktree_dir

        if wt.exists():
            # Validate against `git rev-parse` rather than relying on the
            # presence of `wt/.git`.  The `.git` link can be missing or
            # broken (e.g., the matching admin dir under
            # `<repo>/.git/worktrees/...` was orphaned by a crash) while
            # the worktree directory itself still sits on disk.  In that
            # state, falling through to `git worktree add` below would
            # fail with `'<wt>' already exists` and strand the pipeline
            # on `request_changes` re-runs (#2140).  Probe with rev-parse
            # and only treat the worktree as healthy when git agrees.
            #
            # One retry covers transient git contention (e.g., concurrent
            # _commit_statefiles_to_worktree holding a lock on the shared
            # .git directory).  See #1396.
            healthy = False
            for _attempt in range(2):
                result = self._run_git("rev-parse", "--is-inside-work-tree", cwd=wt, check=False)
                if result.returncode == 0:
                    healthy = True
                    break
                if _attempt == 0:
                    time.sleep(0.1)
            if healthy:
                # Ensure existing worktrees that pre-date the lock-on-create
                # path are locked too (#2324).  ``_lock_worktree`` is a
                # no-op if it is already locked.
                self._lock_worktree(wt)
                return wt

            logger.warning(
                "Worktree validation failed, recreating: worktree=%s returncode=%s stderr=%s",
                str(wt),
                result.returncode,
                (result.stderr or "").strip(),
            )
            try:
                shutil.rmtree(wt)
            except FileNotFoundError:
                # Defensive: with the ``_git_op`` wrap above, the
                # cross-thread/process race that produced #2234 is
                # closed.  Keep tolerating ENOENT anyway — the
                # post-state is what we wanted, and an external
                # actor (operator cleanup, container restart between
                # the ``wt.exists()`` check and this rmtree) can
                # still produce it.
                pass
            except OSError as exc:
                raise GitOperationError(
                    f"Failed to remove stale state worktree at {wt}: {exc}"
                ) from exc
            # Force-remove the admin dir too — the matching wt directory
            # was just removed, but `_remove_stale_admin_dir` checks
            # `wt.exists()` before deleting, so call the forced variant
            # to guarantee `git worktree add` won't refuse.
            self._remove_stale_admin_dir(force=True)
            if wt.exists():
                # Defensive: rmtree returned without raising but the
                # directory persists (e.g., a sub-mount).  Refuse to
                # call `git worktree add` over it.
                raise GitOperationError(
                    f"State worktree at {wt} could not be removed; "
                    "refusing to recreate over existing directory"
                )

        wt.parent.mkdir(parents=True, exist_ok=True)

        # Try to restore from remote if the local branch doesn't exist yet.
        # This enables cross-host recovery when the local state volume is lost.
        if not self._state_branch_exists():
            self._restore_from_remote()

        if self._state_branch_exists():
            self._add_worktree_with_branch_recovery(wt)
        else:
            # First run: create orphan branch
            # Wrap in try/except to clean up on partial failure
            try:
                self._run_git("worktree", "add", "--detach", str(wt))
                self._run_git("checkout", "--orphan", STATE_BRANCH, cwd=wt)
                self._run_git("rm", "-rf", "--cached", ".", cwd=wt, check=False)
                # Remove inherited files from working directory
                for item in wt.iterdir():
                    if item.name == ".git":
                        continue
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
            except GitOperationError, OSError:
                # Clean up partial worktree on failure to avoid broken state.
                # Catch both GitOperationError (git command failures) and OSError
                # (filesystem errors during file cleanup like permission denied).
                shutil.rmtree(wt, ignore_errors=True)
                self._remove_stale_admin_dir()
                raise

        # Lock the worktree so `git worktree prune` (run from any pod
        # sharing the bare repo) cannot remove its admin dir.  The
        # remaining cross-pod prune surfaces are the operator-facing
        # ``/api/v1/worktrees/prune`` endpoint and the gateway's
        # startup ``prune_stale_worktrees`` defense-in-depth sweep;
        # from those callers' filesystem the orchestrator's
        # state-worktree path resolves to a non-existent location
        # (per-pod emptyDir mounts), so without a lock the admin
        # dir is treated as prunable and surgically removed
        # (#2324).  Removal here is still possible because
        # state_store tears down via ``shutil.rmtree`` +
        # ``_remove_stale_admin_dir(force=True)`` rather than
        # ``git worktree remove``, neither of which is blocked by
        # the lock.
        self._lock_worktree(wt)
        return wt


def _remove_stale_admin_dir(self, force: bool = False) -> None:
    """Remove the git admin dir for the state worktree if it's stale.

    When the state worktree directory is gone but its admin dir still
    exists under ``{repo}/.git/worktrees/``, ``git worktree add`` will
    refuse to recreate it.  This method finds and removes only the
    admin dir that belongs to this state worktree — without touching
    admin dirs for other worktrees (e.g., gateway-managed container
    worktrees).

    Args:
        force: When True, remove the admin dir even if the worktree
            directory still exists.  Used by ``_ensure_worktree`` after
            it has just removed a broken worktree directory and is
            about to recreate it; without this, the orphaned admin
            dir would still cause ``git worktree add`` to refuse.
    """
    worktrees_dir = self.repo_path / ".git" / "worktrees"
    if not worktrees_dir.exists():
        return

    wt = self._worktree_dir
    expected_gitdir = str(wt / ".git")

    for entry in worktrees_dir.iterdir():
        if not entry.is_dir():
            continue
        gitdir_file = entry / "gitdir"
        if not gitdir_file.exists():
            continue
        try:
            gitdir_content = gitdir_file.read_text().strip()
            if gitdir_content.rstrip("/") == expected_gitdir.rstrip("/"):
                # This admin dir belongs to our state worktree
                if force or not wt.exists():
                    shutil.rmtree(entry, ignore_errors=True)
                return
        except OSError:
            continue


def _add_worktree_with_branch_recovery(self, wt: Path) -> None:
    """Run ``git worktree add <wt> <STATE_BRANCH>`` with self-heal.

    The state branch can become pinned by an admin dir under
    ``{repo}/.git/worktrees/`` whose worktree directory is gone
    (``prunable``) — for example, after a state-volume reset that
    wiped ``pipeline-worktree`` while the admin dir survived, or
    after :func:`get_state_store` switched the worktree path
    (single-repo → multi-repo deployment) and orphaned the legacy
    admin dir.  Either way, the next ``worktree add`` fails with
    ``'<branch>' is already used by worktree at '<path>'`` and the
    orchestrator wedges every state load (#2167).

    On that specific failure we parse the path from stderr, confirm
    it is gone from disk (never touch a live worktree), remove the
    single admin dir whose ``gitdir`` matches that path, and retry
    the add once.  ``git worktree prune`` is intentionally avoided
    because the orchestrator pod cannot see the gateway's worktree
    paths (different bind mounts) and prune would incorrectly
    remove their admin dirs (see ``_remove_stale_admin_dir``).

    The body runs under ``_git_op`` so the detect-failure → cleanup
    → retry sequence is atomic against concurrent callers.  Without
    this, two callers raced and the loser saw a misleading 500
    because the admin dir was already cleaned by the winner (#2177).
    ``_git_op`` is reentrant; ``_ensure_worktree`` already holds it
    when calling this method, and the inner ``_run_git`` calls
    nest on the same lock.
    """
    with self._git_op():
        try:
            self._run_git("worktree", "add", str(wt), STATE_BRANCH)
            return
        except GitOperationError as exc:
            match = self._BRANCH_IN_USE_PATTERN.search(str(exc))
            if not match:
                raise
            stale_path = Path(match.group(1))
            if stale_path.exists():
                # A live worktree genuinely holds the branch.  Refuse to
                # touch it; surface the original error.
                raise
            removed = self._remove_admin_dir_for_path(stale_path)
            if not removed:
                logger.warning(
                    "Branch %s held by prunable worktree at %s but no admin dir matched",
                    STATE_BRANCH,
                    stale_path,
                )
                raise
            logger.info(
                "Cleared stale admin dir for prunable worktree; retrying add: path=%s",
                stale_path,
            )
            self._run_git("worktree", "add", str(wt), STATE_BRANCH)


def _remove_admin_dir_for_path(self, target_wt_path: Path) -> bool:
    """Remove the admin dir whose ``gitdir`` references ``target_wt_path``.

    Returns True if an admin dir was removed.  Used by
    :meth:`_add_worktree_with_branch_recovery` to clear a single
    stale entry by path — independent of ``self._worktree_dir``,
    which may have changed since the admin dir was written.
    """
    worktrees_dir = self.repo_path / ".git" / "worktrees"
    if not worktrees_dir.exists():
        return False

    expected_gitdir = str(target_wt_path / ".git").rstrip("/")
    for entry in worktrees_dir.iterdir():
        if not entry.is_dir():
            continue
        gitdir_file = entry / "gitdir"
        if not gitdir_file.exists():
            continue
        try:
            if gitdir_file.read_text().strip().rstrip("/") == expected_gitdir:
                shutil.rmtree(entry, ignore_errors=True)
                return True
        except OSError:
            continue
    return False


def _lock_worktree(self, wt: Path) -> None:
    """Mark ``wt`` as locked so ``git worktree prune`` will skip it.

    The state worktree lives on a per-pod ``emptyDir`` mount that the
    gateway pod cannot see, while the bare repo (and its
    ``.git/worktrees/<name>/gitdir`` pointers) is shared via
    ``hostPath``.  The remaining cross-pod prune surfaces are the
    operator-facing ``/api/v1/worktrees/prune`` endpoint and the
    gateway's startup ``prune_stale_worktrees`` defense-in-depth
    sweep — from those callers' filesystem the orchestrator's
    state-worktree path resolves to a non-existent location, so
    without a lock the admin dir is treated as ``prunable`` and
    removed, taking down every subsequent ``rev-parse`` from the
    orchestrator (#2324).

    Locking is best-effort: on failure we log and continue, mirroring
    ``gateway/worktree_manager.py``'s lock-after-add pattern.  An
    already-locked worktree (e.g. on a re-entry after a crash) also
    produces a non-zero return code; treat that as success.
    """
    result = self._run_git("worktree", "lock", str(wt), check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "already locked" in stderr.lower():
            return
        logger.warning(
            "Failed to lock state worktree (continuing): worktree=%s stderr=%s",
            str(wt),
            stderr,
        )


def _state_branch_exists(self) -> bool:
    """Check if the state branch exists locally."""
    result = self._run_git(
        "rev-parse",
        "--verify",
        f"refs/heads/{STATE_BRANCH}",
        check=False,
    )
    return result.returncode == 0
