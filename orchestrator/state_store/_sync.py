"""Remote-sync logic for ``StateStore`` (#3312).

Method bodies extracted verbatim from the pre-split ``state_store.py`` as
module-level functions taking ``self`` explicitly. The barrel binds these onto
the ``StateStore`` class (the sync property getters via ``property(...)``).

The per-repo consecutive-failure state (``_sync_failure_state`` /
``_sync_failure_state_lock``) lives here and is re-exported through the barrel,
preserving the ``state_store._sync_failure_state`` test seam.
"""

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from egg_config.constants import PIPELINE_STATE_BRANCH as STATE_BRANCH

from . import logger

if TYPE_CHECKING:
    from gateway_client import GatewayClient


# Per-repo consecutive-failure state for remote-sync escalation (#3088).
# Module-level so the alert threshold tracks the *repo*, not a single
# ``StateStore`` instance — route handlers re-create their store on each
# call (via ``get_state_store_for_pipeline``), and a per-instance counter
# would fragment such that no individual store ever crossed the threshold
# even when the underlying remote was repeatedly failing.
_sync_failure_state: dict[Path, tuple[int, str | None]] = {}
_sync_failure_state_lock = threading.Lock()


def _detect_gateway_mode(self) -> Literal["public", "private"]:
    """Auto-detect gateway session mode from repository visibility.

    Result is cached for the lifetime of this StateStore instance since
    repo visibility does not change during a process run.
    """
    if hasattr(self, "_cached_gateway_mode"):
        return self._cached_gateway_mode

    mode = "public"
    try:
        from gateway_client import get_gateway_client

        client = get_gateway_client()
        # Extract owner/repo from git remote
        result = self._run_git("remote", "get-url", "origin", cwd=self.repo_path, check=False)
        if result.returncode == 0:
            url = result.stdout.strip()
            # Normalize SSH colon syntax: git@github.com:owner/repo → git@github.com/owner/repo
            if ":" in url and not url.startswith(("http://", "https://", "ssh://", "git://")):
                url = url.replace(":", "/", 1)
            # Parse "https://github.com/owner/repo.git" or "owner/repo"
            parts = url.rstrip("/").removesuffix(".git").rsplit("/", 2)
            if len(parts) >= 2:
                repo = f"{parts[-2]}/{parts[-1]}"
                vis = client.get_repo_visibility(repo)
                if vis in ("private", "internal"):
                    mode = "private"
    except Exception:
        pass

    self._cached_gateway_mode = mode
    return mode


def sync_to_remote(self) -> bool:
    """Push the state branch to remote (best-effort).

    Pushes the state branch ref from the main repo's object DB, not
    from the state worktree. The state worktree lives under
    ``/home/egg/.egg-state/`` which is a pod-local ``emptyDir`` —
    the gateway pod cannot ``cd`` into it. The main repo under
    ``/home/egg/repos/`` is a shared hostPath that both pods see,
    and the state branch's commits and ref live in its ``.git/``
    object DB, so the push only needs the main repo path (see #1808).

    A non-fast-forward rejection is reconciled via
    :meth:`_reconcile_diverged_remote` (#3088) instead of failing
    forever — without that, a single out-of-band write to the remote
    state branch permanently kills this backstop.  Persistent failure
    of any kind escalates through :meth:`_record_sync_outcome`.

    Returns:
        True on success, False on failure (logged, never raises)
    """
    try:
        from gateway_client import get_gateway_client

        client = get_gateway_client()
        mode = self._detect_gateway_mode()
        result = client.push_worktree_branch(
            pipeline_id="state-sync",
            repo_path=str(self.repo_path),
            branch=STATE_BRANCH,
            mode=mode,
            ref=STATE_BRANCH,
        )
        if result:
            self._record_sync_outcome(ok=True)
            return True

        if getattr(result, "category", None) == "non_fast_forward":
            ok = self._reconcile_diverged_remote(client, mode)
            self._record_sync_outcome(
                ok=ok, detail=None if ok else "non_fast_forward: reconcile declined or failed"
            )
            return ok

        self._record_sync_outcome(
            ok=False,
            detail=f"{getattr(result, 'category', 'unknown')}: {getattr(result, 'detail', '')}",
        )
        return False
    except Exception as e:
        logger.warning(
            "Failed to sync state branch to remote: %s",
            e,
        )
        self._record_sync_outcome(ok=False, detail=str(e))
        return False


def _reconcile_diverged_remote(
    self, client: GatewayClient, mode: Literal["public", "private"]
) -> bool:
    """Heal a non-fast-forward state-branch push (#3088).

    The state branch is single-writer by design — this orchestrator is
    the only legitimate author — so divergence means an out-of-band
    write landed on the remote (e.g. a manual plan edit pushed from an
    isolated clone, the #3088 incident).  The local line has evolved
    past that write and is authoritative: overwrite the remote with
    ``--force-with-lease``.

    Two shapes are never forced, only escalated:

    * **Unrelated histories** (no merge-base): the local-wipe
      signature (#3070) — the local branch was recreated as a fresh
      orphan and the remote may hold the only surviving backup.
    * **Remote strictly ahead** (local tip is the merge-base): the
      remote is a superset of local — a local rollback.  Forcing
      would delete records from the backup.

    The lease checks the remote tip against the tracking ref the
    explicit-refspec fetch below just updated; a bare-name fetch
    would leave that ref stale on narrow-refspec mirrors (#3072) and
    the lease would reject spuriously.

    The single-writer assumption is what justifies forcing local over
    the remote.  If two orchestrators were ever pointed at the same
    remote state branch, they would ping-pong force-with-leases at
    each other and neither would alert from the shapes above; that
    misconfiguration is out of scope here (it'd show up earlier as
    cross-orchestrator state divergence).

    TOCTOU note: the ``rev-parse`` reads below run inside the same
    process but outside ``_git_op``, so the local tip could in
    principle advance between the rev-parse and the
    ``--force-with-lease`` push.  This is benign: the lease only
    constrains the *remote* tip (against the tracking ref we just
    fetched), and a local tip that has moved forward is still a
    descendant of the rev-parsed sha — the merge-base classification
    and the resulting force are still correct.

    Returns:
        True if the remote was reconciled (force-with-lease push
        succeeded), False otherwise.
    """
    tracking_ref = f"refs/remotes/origin/{STATE_BRANCH}"
    if not client.fetch_branch(
        pipeline_id="state-sync",
        repo_path=str(self.repo_path),
        args=[f"+refs/heads/{STATE_BRANCH}:{tracking_ref}"],
        mode=mode,
    ):
        logger.warning(
            "State-branch reconcile aborted — could not fetch remote tip (repo=%s)",
            self.repo_path,
        )
        return False

    local = self._run_git(
        "rev-parse", f"refs/heads/{STATE_BRANCH}", cwd=self.repo_path, check=False
    )
    remote = self._run_git("rev-parse", tracking_ref, cwd=self.repo_path, check=False)
    if local.returncode != 0 or remote.returncode != 0:
        logger.warning(
            "State-branch reconcile aborted — could not resolve tips (repo=%s)",
            self.repo_path,
        )
        return False
    local_sha = local.stdout.strip()
    remote_sha = remote.stdout.strip()

    merge_base = self._run_git("merge-base", local_sha, remote_sha, cwd=self.repo_path, check=False)
    if merge_base.returncode != 0:
        logger.error(
            "OVERSEER_ALERT state_sync_unrelated_histories: local and remote "
            "state branches share no history (repo=%s local=%s remote=%s) — "
            "local-wipe signature (#3070); refusing to force-push over what "
            "may be the only surviving backup",
            self.repo_path,
            local_sha,
            remote_sha,
        )
        return False
    if merge_base.stdout.strip() == local_sha:
        logger.error(
            "OVERSEER_ALERT state_sync_remote_ahead: remote state branch is "
            "strictly ahead of local (repo=%s local=%s remote=%s) — local "
            "rollback signature; refusing to force-push records away",
            self.repo_path,
            local_sha,
            remote_sha,
        )
        return False

    logger.info(
        "State branch diverged from remote — overwriting out-of-band remote "
        "write with force-with-lease (repo=%s local=%s remote=%s)",
        self.repo_path,
        local_sha,
        remote_sha,
    )
    return bool(
        client.push_worktree_branch(
            pipeline_id="state-sync",
            repo_path=str(self.repo_path),
            branch=STATE_BRANCH,
            mode=mode,
            ref=STATE_BRANCH,
            force_with_lease=True,
        )
    )


def _sync_consecutive_failures(self) -> int:
    """Read the per-repo consecutive-failure counter (see #3088)."""
    with _sync_failure_state_lock:
        failures, _ = _sync_failure_state.get(self.repo_path, (0, None))
        return failures


def _sync_last_error(self) -> str | None:
    """Read the per-repo last-error string (see #3088)."""
    with _sync_failure_state_lock:
        _, last_error = _sync_failure_state.get(self.repo_path, (0, None))
        return last_error


def _record_sync_outcome(self, ok: bool, detail: str | None = None) -> None:
    """Track consecutive sync failures and escalate persistent ones (#3088).

    Fires an ``OVERSEER_ALERT`` at ``_SYNC_ALERT_THRESHOLD`` consecutive
    failures, then re-fires every ``_SYNC_ALERT_RESPAM_PERIOD`` failures
    thereafter so a long outage stays visible without per-attempt spam.
    The #3088 incident ran 8 days / hundreds of failed pushes with only
    per-attempt WARNINGs.

    Counter state is keyed per-repo at module scope rather than per
    instance: route handlers re-create their ``StateStore`` on each
    call, and a per-instance counter would fragment such that no
    individual store ever crossed the threshold even when the repo's
    underlying remote was repeatedly failing.
    """
    recovered_from = 0
    log_recovery = False
    with _sync_failure_state_lock:
        failures, _ = _sync_failure_state.get(self.repo_path, (0, None))
        if ok:
            if failures >= self._SYNC_ALERT_THRESHOLD:
                recovered_from = failures
                log_recovery = True
            _sync_failure_state.pop(self.repo_path, None)
            n = 0
        else:
            n = failures + 1
            _sync_failure_state[self.repo_path] = (n, detail)

    if ok:
        if log_recovery:
            logger.info(
                "State-branch remote sync recovered after %d consecutive failures (repo=%s)",
                recovered_from,
                self.repo_path,
            )
        return

    # Fire at the threshold; thereafter re-fire every Nth additional
    # failure.  The ``n > _SYNC_ALERT_THRESHOLD`` guard keeps the
    # re-fire branch from triggering before the initial alert in the
    # event the respam period is set smaller than the threshold (or
    # the threshold is bumped above the respam period).
    if n == self._SYNC_ALERT_THRESHOLD or (
        n > self._SYNC_ALERT_THRESHOLD and n % self._SYNC_ALERT_RESPAM_PERIOD == 0
    ):
        logger.error(
            "OVERSEER_ALERT state_sync_push_failing: %d consecutive state-branch "
            "push failures (repo=%s last_error=%s) — the remote durability "
            "backstop for this repo is dead until this is resolved",
            n,
            self.repo_path,
            (detail or "")[:300],
        )


def _sync_to_remote_async(self, _retry_depth: int = 0) -> None:
    """Push the state branch to remote in a daemon thread.

    Debounces per instance (per repo, #3088): if this store's push is
    already in flight, marks a pending flag so the in-flight thread
    re-pushes after completing.  This ensures the latest committed
    state always reaches the remote.

    Retries are capped at ``_MAX_PUSH_RETRIES`` to prevent unbounded
    recursion if commits arrive faster than pushes complete.
    """
    with self._push_lock:
        if self._push_in_flight:
            self._push_pending = True
            logger.debug("Push already in flight — marked pending for retry")
            return
        self._push_in_flight = True

    def _push() -> None:
        try:
            self.sync_to_remote()
        finally:
            retry = False
            with self._push_lock:
                self._push_in_flight = False
                if self._push_pending:
                    self._push_pending = False
                    retry = True
            if retry:
                next_depth = _retry_depth + 1
                if next_depth >= self._MAX_PUSH_RETRIES:
                    logger.warning(
                        "Max push retries (%d) reached — skipping retry",
                        self._MAX_PUSH_RETRIES,
                    )
                else:
                    self._sync_to_remote_async(_retry_depth=next_depth)

    t = threading.Thread(target=_push, daemon=True)
    t.start()


def _restore_from_remote(self) -> bool:
    """Restore the state branch from remote if it exists.

    Called during worktree initialization when the local branch
    doesn't exist. Checks remote via ls-remote, then fetches.

    Returns:
        True if the local branch was restored from remote, False otherwise
    """
    try:
        from gateway_client import get_gateway_client

        client = get_gateway_client()
        mode = self._detect_gateway_mode()

        # Check if remote branch exists
        if not client.ls_remote_branch(
            pipeline_id="state-restore",
            repo_path=str(self.repo_path),
            ref=f"refs/heads/{STATE_BRANCH}",
            mode=mode,
        ):
            logger.debug("No remote state branch found — will create fresh")
            return False

        # Fetch the remote branch to create the local tracking ref
        if not client.fetch_branch(
            pipeline_id="state-restore",
            repo_path=str(self.repo_path),
            args=[f"+refs/heads/{STATE_BRANCH}:refs/heads/{STATE_BRANCH}"],
            mode=mode,
        ):
            logger.warning("Failed to fetch state branch from remote")
            return False

        logger.info("Restored state branch from remote")
        return True
    except Exception as e:
        logger.debug(
            "Could not restore state branch from remote (will create fresh): %s",
            e,
        )
        return False
