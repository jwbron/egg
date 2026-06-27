"""
Git-backed state persistence for pipeline state.

All pipeline state lives on a dedicated ``egg/pipeline-state`` orphan branch,
accessed via a persistent git worktree.  The main checkout is never modified.

Read/write operations go directly to the worktree directory on disk.  Commits
are made in-place inside the worktree and stay on the state branch.

The state branch is synced to the remote after every commit (best-effort,
async push via a daemon thread).  On startup, if the local branch does not
exist, it is restored from the remote so any host can recover the
authoritative pipeline state.

----

Decomposed into a sub-package (#3312, slice-3) following the canonical
method-modules-on-class pattern (docs/guides/decomposition-pattern.md §c). This
barrel is the **stable public API**: ``StateStore`` keeps its identity on the
``state_store`` module path, and every external symbol / ``unittest.mock.patch``
target resolves through here. ``StateStore`` method bodies live in
underscore-prefixed submodules as module-level functions taking ``self``
explicitly; they are bound back onto the class below. Submodule functions reach
barrel-patched module globals (``get_pipeline_state_lock``,
``discover_repo_paths``, ``StateStore``) via ``import state_store as _pkg``, so
those patch seams keep working.
"""

import logging
import os
import re
import shutil  # noqa: F401 — keeps patch("state_store.shutil.rmtree") resolving through the barrel
import threading
import time  # noqa: F401 — keeps patch("state_store.time.sleep") resolving through the barrel
from pathlib import Path
from typing import ClassVar

from egg_config.constants import PIPELINE_STATE_BRANCH as STATE_BRANCH  # noqa: F401 — re-export

logger = logging.getLogger("orchestrator.state_store")

# Relative to the Docker state volume (/home/egg/.egg-state)
_DEFAULT_WORKTREE_DIR = (
    Path(os.environ.get("EGG_STATE_DIR", "/home/egg/.egg-state")) / "pipeline-worktree"
)

# -- public exception + validation surface (bodies in _errors.py) ----------
# -- method-body submodules (bound onto StateStore below) ------------------
# Imported here (not lazily) so the ``import state_store as _pkg`` barrel
# access inside them resolves once the module finishes initialising.
from . import _commit, _crud, _git, _sync, _worktree  # noqa: F401
from ._errors import (  # noqa: F401 — re-export
    PIPELINE_ID_PATTERN,
    GitOperationError,
    InvalidPipelineIdError,
    PipelineNotFoundError,
    StateStoreError,
    StateValidationError,
    VersionConflictError,
    _validate_pipeline_id,
    validate_pipeline_id,
)

# -- re-exported module-level functions / state ----------------------------
from ._factory import discover_repo_paths, get_state_store  # noqa: F401 — re-export / _pkg seam
from ._locks import (  # noqa: F401 — re-export / patch seam
    _pipeline_state_locks,
    _state_locks_lock,
    get_pipeline_state_lock,
    release_pipeline_state_lock,
)
from ._sync import (  # noqa: F401 — re-export (state_store._sync_failure_state test seam)
    _sync_failure_state,
    _sync_failure_state_lock,
)


class StateStore:
    """Git-backed state store for pipeline state.

    All state files live in a persistent git worktree on the
    ``egg/pipeline-state`` orphan branch.  The main repo checkout
    is never modified.
    """

    PIPELINES_DIR = ".egg-state/pipelines"

    # Consecutive sync_to_remote failures before an OVERSEER_ALERT fires
    # (#3088 — a dead remote backstop must not stay invisible).
    _SYNC_ALERT_THRESHOLD: ClassVar[int] = 5
    # After the threshold fires, re-fire every Nth failure so a long outage
    # stays visible without per-attempt spam.  Pinned independently of the
    # threshold so bumping ``_SYNC_ALERT_THRESHOLD`` past this value cannot
    # cause the re-fire branch to trigger before the initial alert (the
    # ``n > _SYNC_ALERT_THRESHOLD`` guard in ``_record_sync_outcome``
    # enforces this).
    _SYNC_ALERT_RESPAM_PERIOD: ClassVar[int] = 50

    # Matches git's "already used by worktree at '<path>'" failure message.
    # Used to parse the prunable worktree path from `git worktree add` stderr
    # so we can drop a single targeted admin dir without scanning all of them.
    _BRANCH_IN_USE_PATTERN = re.compile(r"is already (?:used by worktree|checked out) at '([^']+)'")

    _MAX_PUSH_RETRIES: ClassVar[int] = 3

    def __init__(
        self,
        repo_path: Path,
        worktree_dir: Path | None = None,
    ):
        """Initialize state store for a repository.

        Args:
            repo_path: Path to the main git repository
            worktree_dir: Override the persistent worktree location
                (default: ``/home/egg/.egg-state/pipeline-worktree``)
        """
        self.repo_path = repo_path
        self._worktree_dir = worktree_dir or _DEFAULT_WORKTREE_DIR
        self._worktree: Path | None = None  # lazily initialised

        # -- remote sync state (per instance, i.e. per repo) -----------------
        # These were ClassVars until #3088: with the debounce shared across
        # all stores, a push for repo B arriving while repo A's push was in
        # flight collapsed into A's pending flag — and the retry closure
        # re-pushed A, silently dropping B's sync.  Pushes to different
        # remotes have no reason to serialise.
        #
        # Per-instance debounce coalesces within the lifetime of one store
        # (e.g. the long-lived ``CommitAuthorshipStore`` singleton — the
        # highest-volume caller).  Route handlers go through
        # ``get_state_store_for_pipeline`` which constructs a fresh store
        # per call, so debounce does not coalesce *across* such calls; that
        # is acceptable because their per-repo push rate is far below the
        # singleton path's.  Failure-counter state, in contrast, is kept
        # module-level (see ``_sync_failure_state`` below) so the alert
        # threshold cannot fragment across those short-lived instances.
        self._push_in_flight = False
        self._push_pending = False
        self._push_lock = threading.Lock()

    # -- worktree lifecycle properties -------------------------------------

    @property
    def worktree(self) -> Path:
        """Path to the persistent state worktree (created lazily)."""
        if self._worktree is None:
            self._worktree = self._ensure_worktree()
        return self._worktree

    @property
    def pipelines_dir(self) -> Path:
        return self.worktree / self.PIPELINES_DIR

    # -- cross-process locking + git execution (bodies in _git.py) ---------
    _git_op = _git._git_op
    _get_pipeline_path = _git._get_pipeline_path
    _ensure_dir = _git._ensure_dir
    _run_git = _git._run_git
    _cleanup_stale_locks = _git._cleanup_stale_locks

    # -- worktree lifecycle (bodies in _worktree.py) -----------------------
    _ensure_worktree = _worktree._ensure_worktree
    _remove_stale_admin_dir = _worktree._remove_stale_admin_dir
    _add_worktree_with_branch_recovery = _worktree._add_worktree_with_branch_recovery
    _remove_admin_dir_for_path = _worktree._remove_admin_dir_for_path
    _lock_worktree = _worktree._lock_worktree
    _state_branch_exists = _worktree._state_branch_exists

    # -- git commit helpers (bodies in _commit.py) -------------------------
    _commit_state = _commit._commit_state
    _get_current_commit = _commit._get_current_commit
    _generate_commit_message = _commit._generate_commit_message

    # -- remote sync (bodies in _sync.py) ----------------------------------
    _detect_gateway_mode = _sync._detect_gateway_mode
    sync_to_remote = _sync.sync_to_remote
    _reconcile_diverged_remote = _sync._reconcile_diverged_remote
    _record_sync_outcome = _sync._record_sync_outcome
    _sync_to_remote_async = _sync._sync_to_remote_async
    _restore_from_remote = _sync._restore_from_remote
    _sync_consecutive_failures = property(_sync._sync_consecutive_failures)
    _sync_last_error = property(_sync._sync_last_error)

    # -- CRUD + pipeline lifecycle (bodies in _crud.py) --------------------
    pipeline_exists = _crud.pipeline_exists
    load_pipeline = _crud.load_pipeline
    save_pipeline = _crud.save_pipeline
    create_pipeline = _crud.create_pipeline
    delete_pipeline = _crud.delete_pipeline
    list_pipelines = _crud.list_pipelines
    get_active_pipelines = _crud.get_active_pipelines
    pipelines_for_jira_ticket = _crud.pipelines_for_jira_ticket
    update_pipeline = _crud.update_pipeline
