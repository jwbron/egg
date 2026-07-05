"""worktree sync helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim; patched/barrel-resident globals reached via _pkg so
patch("routes.pipelines.<name>") keeps intercepting.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Literal,
    NamedTuple,  # noqa: F401
)

import routes.pipelines as _pkg  # noqa: E402,F401

if TYPE_CHECKING:
    try:
        from ..container_spawner import ContainerSpawner  # noqa: F401
    except ImportError:  # pragma: no cover
        from container_spawner import ContainerSpawner  # type: ignore  # noqa: F401


class WorktreeSyncOutcome(NamedTuple):
    """Structured outcome from :func:`_sync_worktree_with_remote` (#2792, #2979).

    Phase-boundary callers inspect ``diverged_unreconciled`` to decide
    whether to pause the pipeline for a manual reconcile.  Best-effort
    callers can ignore the return value entirely — every field has a
    safe default and the sync still does the same in-band work whether
    or not the outcome is consumed.

    ``case`` is the same discriminator the function emits to its
    ``worktree_sync_outcome`` log line, so the field can be cross-
    referenced against operator-grep patterns.

    ``diverged_unreconciled`` is True when local and remote had truly
    diverged (ahead AND behind) and the rebase autoresolve could not
    reconcile them.  Since #2979 the helper does **not** hard-reset in
    that case — the rebase autoresolve already aborted (restoring the
    worktree to the clean local HEAD with the orchestrator's committed
    work intact), so the helper leaves the worktree there and reports
    the unreconciled divergence so the caller can pause for a manual
    reconcile rather than discarding committed work.

    ``backup_ref`` is the full ref name (``refs/egg-backup/sync-recovery/
    <pipeline_id>/<unix_ts>``) pinning the local HEAD when divergence is
    unreconciled — a stable handle the operator can inspect/reset to.
    ``None`` means the (best-effort) backup write failed; the commits are
    still on the live HEAD, and the local-only SHAs go into the WARN log
    inline so they're at least in the audit trail (see the helper body).

    ``local_only_commit_shas`` is the list of local-only short SHAs (with
    summaries) that are on HEAD but not yet on origin.  Empty when the
    rev-list itself failed; the divergence is still reported, but the
    operator can't be given the exact commit list inline.

    ``rebase_category`` / ``rebase_detail`` carry the failing rebase's
    ``PushResult.category`` / ``detail`` (conflicting paths, the rebase
    argv, and a git-output excerpt) when ``diverged_unreconciled`` is
    True.  They exist so the reconcile HITL can show the operator *what*
    failed instead of an unfalsifiable generic claim (#3416) — the log
    lines carry the same data but roll; the decision persists.
    """

    case: str
    diverged_unreconciled: bool = False
    backup_ref: str | None = None
    local_only_commit_shas: tuple[str, ...] = ()
    rebase_category: str | None = None
    rebase_detail: str | None = None


def _build_sync_recovery_backup_ref(pipeline_id: str, unix_ts: int) -> str:
    """Return the canonical ``refs/egg-backup/sync-recovery/<pid>/<ts>`` name (#2792).

    Pulled out so the test, the writer, and any future opportunistic
    pruner share a single ref-name convention.  The slash-segment
    layout lets ``git for-each-ref refs/egg-backup/sync-recovery/<pid>``
    enumerate just this pipeline's backups.
    """
    return f"refs/egg-backup/sync-recovery/{pipeline_id}/{unix_ts}"


def _collect_local_only_commits(
    git_base: list[str],
    *,
    pipeline_id: str,
    branch: str,
    remote_branch: str,
) -> tuple[str, ...]:
    """Enumerate local-only commits between HEAD and ``origin/<remote_branch>``.

    Returns a tuple of ``"<short-sha> <summary>"`` strings, oldest first.
    A failure (subprocess error, nonzero rc, parse error) returns an
    empty tuple and emits a WARN — the hard-reset fallback proceeds
    with an unknown discard list rather than blocking on best-effort
    forensic enumeration (#2792 section 5).
    """
    try:
        result = subprocess.run(
            [
                *git_base,
                "rev-list",
                "--reverse",
                "--pretty=format:%h %s",
                "--no-commit-header",
                f"origin/{remote_branch}..HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            _pkg.logger.warning(
                "Failed to enumerate local-only commits before hard reset",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                rc=result.returncode,
                stderr=result.stderr.strip()[:200],
            )
            return ()
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        return tuple(lines)
    except Exception as exc:
        _pkg.logger.warning(
            "Local-only commit enumeration raised before hard reset",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            error=str(exc),
        )
        return ()


def _create_sync_recovery_backup_ref(
    git_base: list[str],
    *,
    pipeline_id: str,
    ref_name: str,
) -> bool:
    """Pin current HEAD under ``ref_name`` via ``git update-ref`` (#2792).

    Returns True on success.  On failure logs WARN and returns False;
    the caller proceeds with the destructive reset regardless — the
    backup is best-effort, the reset is the reconcile primitive.
    """
    try:
        result = subprocess.run(
            [*git_base, "update-ref", ref_name, "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            _pkg.logger.warning(
                "Failed to create sync-recovery backup ref",
                pipeline_id=pipeline_id,
                ref_name=ref_name,
                rc=result.returncode,
                stderr=result.stderr.strip()[:200],
            )
            return False
        return True
    except Exception as exc:
        _pkg.logger.warning(
            "Sync-recovery backup-ref write raised",
            pipeline_id=pipeline_id,
            ref_name=ref_name,
            error=str(exc),
        )
        return False


def _sync_worktree_with_remote(
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    worktree_repo_path: Path,
    prior_phase_succeeded: bool = True,
    gateway_mode: Literal["public", "private"] = "public",
    base_branch: str | None = None,
    *,
    pipeline_branch: str | None = None,
) -> WorktreeSyncOutcome:
    """Sync a worktree with its remote branch (best-effort).

    After an orchestrator restart or a phase boundary, the local worktree
    branch may be behind the remote: commits pushed during previous phases
    (contracts, drafts, statefiles) exist on origin but not in the local
    checkout.  This function fetches those commits and reconciles the
    worktree so that all downstream code (contract loading, draft reading,
    populator, etc.) sees the full pipeline state.

    ``pipeline_branch`` is the **remote** branch name to reconcile against.
    Since #2399 the pipeline tip lives at ``egg/<pid>/work`` on origin so
    slice integration branches at ``egg/<pid>/slice-N`` can coexist as
    siblings; ``pipeline.branch`` already carries that ``/work`` suffix
    (set by :func:`_ensure_pipeline_work_ref` at submission time), so
    callers should pass ``pipeline_branch=pipeline.branch`` directly —
    the local worktree branch and the remote ref now match.  Without an
    explicit ``pipeline_branch``, the function reads
    ``git branch --show-current`` and looks up ``origin/<that-name>``,
    which always misses on real pipelines and exits at
    ``case=no_remote_tracking`` (#2367).  Callers with a pipeline in
    scope MUST pass ``pipeline_branch=pipeline.branch``.  When omitted,
    the function falls back to the local branch name for backward
    compatibility with non-pipeline scripts.

    When local is ahead of remote:
    - If the prior phase succeeded, push local commits to remote first.
      On a successful push, reset to origin (a no-op fast-forward that
      keeps the worktree clean).  If the push FAILS, the local commits
      are preserved as-is and the function returns without resetting —
      ``remote_ahead == 0`` means origin holds nothing to incorporate, so
      a ``reset --hard origin`` would only discard completed, committed
      work (e.g. agent-registered HITL contract decisions) before the
      phase_gate decision bridge could surface them (#2972).
    - If the prior phase failed or was killed, discard local commits and
      reset to remote (discards incomplete work).

    When local has diverged (ahead AND behind), rebase local commits onto
    ``origin/{pipeline_branch}`` via the same helper used by the
    gateway-side push-reject reconcile path.  ``--ff-only`` cannot
    reconcile real divergence by definition, so the pre-#2337
    implementation silently left the worktree stale and downstream
    populator/decision-sync paths consumed the stale state.

    When the rebase itself fails (#2792, made non-destructive in #2979),
    the autoresolve has already run ``git rebase --abort`` — which
    restores the worktree to the clean local HEAD and reapplies the
    autostash, so the orchestrator's committed work is intact on HEAD.
    The helper does **not** hard-reset (the pre-#2979 behaviour, which
    discarded that committed work to a backup ref and FAILed the
    pipeline).  It pins HEAD under ``refs/egg-backup/sync-recovery/
    <pipeline_id>/<unix_ts>`` as a stable operator handle and returns
    ``diverged_unreconciled=True`` so phase-boundary callers pause the
    pipeline for a manual reconcile (AWAITING_HUMAN) rather than
    consuming the un-reconciled state or discarding work.

    Every return path emits at least one ``worktree_sync_outcome`` log
    line with a ``case`` discriminator so production logs name which
    path fired.  The ``rev_list_failed`` and ``divergence_unreconciled``
    cases bail non-destructively (no ``reset --hard``); only the
    local-behind and prior-phase-failed-discard cases reach the step-4
    reset, neither of which can lose committed work that isn't already
    on origin.

    Safe to call on every pipeline start because it is idempotent when the
    local branch is already up to date.

    Returns a :class:`WorktreeSyncOutcome` describing what the helper
    did.  Most callers can ignore the return value; phase-boundary
    callers inspect ``diverged_unreconciled`` to decide whether to pause
    the pipeline for a manual reconcile (#2979).
    """
    base_branch_for_reconcile = base_branch
    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    # Step 1: Authenticated fetch via gateway (gateway holds GitHub credentials)
    fetch_ok = spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    if not fetch_ok:
        _pkg.logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            case="fetch_failed",
        )
        return WorktreeSyncOutcome(case="fetch_failed")

    # Step 2: Determine current branch
    try:
        result = subprocess.run(
            [*git_base, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        branch = result.stdout.strip()
        if not branch:
            _pkg.logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                case="detached_head",
            )
            return WorktreeSyncOutcome(case="detached_head")
    except Exception as branch_err:
        _pkg.logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            case="branch_detect_failed",
            error=str(branch_err),
        )
        return WorktreeSyncOutcome(case="branch_detect_failed")

    # ``branch`` is the **local** branch name (e.g. ``egg/<pid>/work`` on
    # orchestrator worktrees).  ``remote_branch`` is the remote-side name
    # we look up on origin and push/reset against.  When the caller
    # passes ``pipeline_branch`` (the canonical, agent-facing branch),
    # use it for every remote-side ref so the ``/work`` suffix mismatch
    # in #2367 cannot strand a pipeline in ``no_remote_tracking``.
    remote_branch = pipeline_branch or branch

    # Step 3: Verify remote tracking branch exists
    try:
        result = subprocess.run(
            [*git_base, "rev-parse", "--verify", f"origin/{remote_branch}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            _pkg.logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="no_remote_tracking",
            )
            return WorktreeSyncOutcome(case="no_remote_tracking")
    except Exception as rev_parse_err:
        _pkg.logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="rev_parse_failed",
            error=str(rev_parse_err),
        )
        return WorktreeSyncOutcome(case="rev_parse_failed")

    # Step 3b: Check divergence between local and remote.
    local_ahead = 0
    remote_ahead = 0
    rev_list_ok = False
    try:
        result = subprocess.run(
            [
                *git_base,
                "rev-list",
                "--left-right",
                "--count",
                f"HEAD...origin/{remote_branch}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        parts = result.stdout.strip().split()
        if result.returncode == 0 and len(parts) == 2:
            local_ahead = int(parts[0])
            remote_ahead = int(parts[1])
            rev_list_ok = True
        else:
            _pkg.logger.warning(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="rev_list_failed",
                rc=result.returncode,
                stdout=result.stdout.strip()[:200],
            )
            # #2979: the ahead/behind counts are unknown, so a Step-4
            # ``reset --hard origin`` here could discard local-only
            # commits that are NOT on origin — a destructive reset over
            # un-provably-pushed work with no backup ref.  Bail
            # non-destructively instead, leaving the worktree untouched.
            return WorktreeSyncOutcome(case="rev_list_failed")
    except Exception as rev_list_err:
        _pkg.logger.warning(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="rev_list_failed",
            error=str(rev_list_err),
        )
        # #2979: unknown ahead/behind counts — bail non-destructively
        # rather than fall through to the Step-4 ``reset --hard`` (which
        # would risk discarding un-pushed local work without a backup).
        return WorktreeSyncOutcome(case="rev_list_failed")

    # Step 3c: Handle local-ahead commits.
    if local_ahead == 0 and remote_ahead == 0 and rev_list_ok:
        # Local and remote are already in sync — skip the no-op reset entirely
        # so the outcome is distinguishable from a true behind-remote sync.
        _pkg.logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="already_in_sync",
            local_ahead=0,
            remote_ahead=0,
        )
        return WorktreeSyncOutcome(case="already_in_sync")

    if local_ahead > 0 and remote_ahead == 0:
        # Local is strictly ahead of remote (no divergence).
        if prior_phase_succeeded:
            # Prior phase completed successfully — push local work to remote
            # before resetting, so it's not lost.  Pushing to ``remote_branch``
            # (not the local ``/work`` name) so the agent-facing branch
            # receives the commits — the gateway builds
            # ``HEAD:refs/heads/{branch}`` from this argument.
            push_result = spawner.gateway.push_worktree_branch(
                pipeline_id=pipeline_id,
                repo_path=str(worktree_repo_path),
                branch=remote_branch,
                mode=gateway_mode,
                base_branch=base_branch_for_reconcile,
            )
            if push_result:
                # Push succeeded — local and remote are now in sync.
                # Re-fetch to update the remote tracking ref so that
                # origin/{remote_branch} reflects the pushed commits.
                spawner.gateway.fetch_worktree_branch(
                    pipeline_id=pipeline_id,
                    repo_path=str(worktree_repo_path),
                    mode=gateway_mode,
                )
                _pkg.logger.info(
                    "worktree_sync_outcome",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    remote_branch=remote_branch,
                    case="local_ahead_pushed",
                    local_ahead=local_ahead,
                    remote_ahead=remote_ahead,
                )
                return WorktreeSyncOutcome(case="local_ahead_pushed")
            else:
                # Push failed.  ``remote_ahead == 0`` in this branch, so
                # origin holds nothing the worktree lacks — resetting to
                # origin here would discard the completed, committed local
                # work (e.g. the agent-registered HITL contract decisions
                # the pre-sync ``_commit_statefiles_to_worktree`` just
                # committed) for ZERO reconcile benefit, then advance
                # silently.  That is exactly how #2972 dropped a refiner's
                # ``register_open_question`` / ``request_feedback`` items
                # before the phase_gate decision bridge could surface them:
                # the prior code fell through to the Step-4 ``reset --hard``
                # and returned ``reset_succeeded`` (``hard_reset_performed``
                # False), so no operator signal fired.  Preserve the local
                # commits instead — they remain in the worktree for
                # downstream reads (the decision bridge, populator) and for
                # the next push attempt.  The WARNING below is the loud
                # breadcrumb that the tip is unpushed; unlike the divergence
                # path (``remote_ahead > 0``) there is no remote work to
                # rebase onto, so non-destructive preservation is correct.
                _pkg.logger.warning(
                    "worktree_sync_outcome",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    remote_branch=remote_branch,
                    case="local_ahead_push_failed",
                    local_ahead=local_ahead,
                    remote_ahead=remote_ahead,
                    category=push_result.category,
                    error=push_result.detail,
                )
                return WorktreeSyncOutcome(case="local_ahead_push_failed")
        else:
            # Prior phase failed — incomplete local work will be discarded by
            # the step-4 reset. Emit a distinct case so operators can grep
            # this branch of the taxonomy without inferring it from
            # reset_succeeded with local_ahead > 0.
            _pkg.logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="local_ahead_discarded",
                local_ahead=local_ahead,
                remote_ahead=remote_ahead,
            )
        # Fall through to reset (Step 4) — discards incomplete local work
        # from a failed/killed prior phase.  (The successful-phase
        # push-failure case returns above without resetting so completed
        # work is never silently dropped — #2972.)

    elif local_ahead > 0 and remote_ahead > 0:
        # True divergence.  Reconcile by rebasing local commits onto
        # origin/{branch} via the same helper used by the gateway-side
        # push-reject reconcile path (#2337).  --ff-only cannot reconcile
        # real divergence by definition, so the pre-#2337 implementation
        # silently left the worktree stale.
        #
        # ⚠️ When ``base_branch_for_reconcile`` is None,
        # ``_build_rebase_cmd`` falls back to the plain
        # ``git rebase origin/{branch}`` form — the same form that
        # triggered #2222 main-contamination on the gateway-side
        # push-reject path.  That fallback is the contamination vector:
        # with HEAD at current main and origin/{branch} on a stale
        # snapshot, the plain form replays merge-base..HEAD on the
        # stale tip, producing a PR full of duplicate-by-content
        # commits.  Callers should always thread ``pipeline.base_branch``
        # so the helper emits the safer
        # ``--onto origin/{branch} origin/{base_branch}`` form.  Logging
        # the None case so the next person debugging contamination has a
        # breadcrumb.
        if base_branch_for_reconcile is None:
            _pkg.logger.warning(
                "worktree_sync divergence_rebase with base_branch=None — "
                "falling back to bare-rebase form (#2222 contamination risk)",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
            )
        _pkg.logger.info(
            "Local and remote have diverged — rebasing local onto origin",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
        )
        rebase_outcome = _pkg._rebase_with_agent_output_autoresolve(
            git_base=git_base,
            pipeline_id=pipeline_id,
            branch=remote_branch,
            base_branch=base_branch_for_reconcile,
        )
        if rebase_outcome.ok:
            _pkg.logger.info(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="divergence_rebased",
                local_ahead=local_ahead,
                remote_ahead=remote_ahead,
            )
            return WorktreeSyncOutcome(case="divergence_rebased")
        _pkg.logger.error(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="divergence_rebase_failed",
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
            category=rebase_outcome.category,
            detail=rebase_outcome.detail,
        )

        # #2979: non-destructive divergence reconcile.  The rebase
        # autoresolve could not reconcile the divergence — a conflict on
        # a path outside ``.egg-state/agent-outputs/``.  In normal
        # operation this is now unreachable: #2979 stopped agents from
        # git-pushing ``.egg-state/contracts/`` (they mutate contracts
        # through the contract API), so the orchestrator is the sole
        # writer of the only non-agent-outputs path both sides touched on
        # the work branch, and the rebase only ever replays disjoint
        # paths.  When it *does* fire (an unexpected residual conflict, a
        # restart mid-flight), the autoresolve has already run
        # ``git rebase --abort``, which restored the worktree to the
        # clean local HEAD and reapplied the autostash — the
        # orchestrator's committed work is intact on HEAD.
        #
        # #2792/#2797 used to ``git reset --hard origin`` here, discarding
        # that committed work (operator-bound contract decisions included)
        # to a backup ref the operator had to spelunk, then FAIL the
        # pipeline.  Instead, leave the worktree at local HEAD and report
        # the unreconciled divergence; the caller pauses the pipeline for
        # a manual reconcile (AWAITING_HUMAN, not FAILED).  Downstream
        # consumers — populator, decision-sync, plan-complete — never run
        # against the un-reconciled state because the pause halts the
        # phase before them, which is the silent-stale-read failure #2337
        # raised an error for, addressed without discarding work.
        #
        # Pin HEAD under a backup ref anyway: a stable, enumerable handle
        # the operator can ``git log`` / ``git reset`` against, and a
        # guard against any later worktree mutation.  Best-effort — a
        # failed write inlines the SHAs into the WARN log for the audit
        # trail (the commits remain on the live HEAD regardless).
        # Nanosecond precision so two reconcile attempts within the same
        # second on the same pipeline cannot collide on the ref name.
        local_only = _collect_local_only_commits(
            git_base,
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
        )
        unix_ts = time.time_ns()
        backup_ref = _build_sync_recovery_backup_ref(pipeline_id, unix_ts)
        backup_ok = _create_sync_recovery_backup_ref(
            git_base,
            pipeline_id=pipeline_id,
            ref_name=backup_ref,
        )
        if not backup_ok and local_only:
            _pkg.logger.warning(
                "Divergence-reconcile backup ref write failed; local-only "
                "SHAs inlined for audit (commits remain on the live HEAD)",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                local_only_commit_shas=list(local_only),
            )
        _pkg.logger.warning(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="divergence_unreconciled",
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
            backup_ref=backup_ref if backup_ok else None,
            local_only_commit_count=len(local_only),
            rebase_category=rebase_outcome.category,
        )
        return WorktreeSyncOutcome(
            case="divergence_unreconciled",
            diverged_unreconciled=True,
            backup_ref=backup_ref if backup_ok else None,
            local_only_commit_shas=local_only,
            rebase_category=rebase_outcome.category,
            rebase_detail=rebase_outcome.detail,
        )

    # Step 4: Reset local branch to remote.
    # This handles: local behind remote (origin strictly ahead — nothing
    # local to lose) and the prior-phase-failed local-ahead discard (the
    # incomplete work is intentionally dropped).  The already-in-sync case
    # returns early above; the rev-list-failed and unreconciled-divergence
    # cases now bail non-destructively before reaching here (#2979), so
    # this reset never runs over un-provably-pushed committed work.
    try:
        result = subprocess.run(
            [*git_base, "reset", "--hard", f"origin/{remote_branch}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            _pkg.logger.warning(
                "worktree_sync_outcome",
                pipeline_id=pipeline_id,
                branch=branch,
                remote_branch=remote_branch,
                case="reset_failed",
                local_ahead=local_ahead,
                remote_ahead=remote_ahead,
                error=result.stderr.strip(),
            )
            return WorktreeSyncOutcome(case="reset_failed")
        _pkg.logger.info(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="reset_succeeded",
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
        )
        return WorktreeSyncOutcome(case="reset_succeeded")
    except Exception as sync_err:
        _pkg.logger.warning(
            "worktree_sync_outcome",
            pipeline_id=pipeline_id,
            branch=branch,
            remote_branch=remote_branch,
            case="reset_failed",
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
            error=str(sync_err),
        )
        return WorktreeSyncOutcome(case="reset_failed")


class StalePipelineBranchError(RuntimeError):
    """Raised when ``origin/<pipeline.branch>`` is behind base and the
    rebase to bring it up to date hit a conflict.

    Phase-startup callers convert this into a FAILED pipeline with a
    clear ``error`` so the operator knows to manually rebase or start
    fresh — vastly preferable to silently producing a PR with 70+
    cherry-picked-variant commits buried in it (#2098).
    """


def _rebase_pipeline_branch_onto_base(
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    worktree_repo_path: Path,
    pipeline_branch: str,
    base_branch: str,
    gateway_mode: Literal["public", "private"] = "public",
) -> None:
    """Rebase a stale ``origin/<pipeline_branch>`` onto ``origin/<base_branch>``.

    When ``submit_task`` resumes a pipeline whose branch has been sitting
    on the remote for days/weeks while ``main`` advanced, the existing
    pipeline branch tip carries old-SHA copies of commits that have since
    been rebased onto main.  Without this helper, the first orchestrator
    push hits non-fast-forward, the reconcile path rebases ``HEAD`` onto
    the stale tip, and every downstream commit inherits 70+ stale-from-
    main commits as ancestors — producing a final PR diff that buries
    the actual feature work under contamination (#2098).

    This helper runs on the orchestrator-side worktree and treats it as
    scratch space for the rebase:

    1. Skip when ``pipeline_branch`` doesn't exist on the remote (fresh
       run — there's nothing to rebase).
    2. Skip when ``origin/<pipeline_branch>`` is not behind
       ``origin/<base_branch>`` (already up to date).
    3. Skip when ``HEAD`` is an ancestor of *neither*
       ``origin/<pipeline_branch>`` *nor* ``origin/<base_branch>``.  Two
       real resume paths satisfy the ancestry check:

       (a) **Preserved worktree** (canonical #2098 case): the
           orchestrator-side worktree was kept across a cancel/resubmit,
           so ``HEAD`` carries state-file commits that were already
           pushed to ``origin/<branch>``.  ``HEAD`` is a strict ancestor
           of ``origin/<branch>``.
       (b) **Fresh worktree**: the worktree volume was wiped between
           cancel and resubmit (e.g. orchestrator redeploy onto a fresh
           PVC), so the gateway recreated it from ``origin/<base>``.
           ``HEAD == origin/<base>`` is a (trivial) ancestor of
           ``origin/<base>``; resetting to ``origin/<branch>`` discards
           no unique commits because every base commit is preserved as
           the rebase target.

       If neither ancestry holds, ``HEAD`` carries truly unpublished
       work and we defer rather than overwrite it.
    4. Reset the worktree to ``origin/<pipeline_branch>``, ``git rebase
       origin/<base_branch>``, and force-push the rebased tip.  Git's
       built-in cherry-pick-skip drops commits already content-equivalent
       to ones on the new base.
    5. On conflict: abort the rebase, restore the worktree to
       ``origin/<base_branch>``, and raise ``StalePipelineBranchError``
       so phase startup fails fast with an actionable error.

    Best-effort fetch+rev-list errors are logged and swallowed so a
    transient gateway hiccup doesn't block pipeline startup; only a
    rebase that *started* but couldn't finish raises.
    """
    if not pipeline_branch or not base_branch or pipeline_branch == base_branch:
        return

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    def _run_git(
        args: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str] | None:
        """Run a git command and convert ``TimeoutExpired`` / ``OSError``
        into a ``None`` return so callers can decide what to do.

        Mirrors the defensive pattern in ``_sync_worktree_with_remote``.
        """
        try:
            return subprocess.run(
                [*git_base, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _pkg.logger.warning(
                "rebase-on-resume: git command failed to run",
                pipeline_id=pipeline_id,
                branch=pipeline_branch,
                git_args=args,
                error=str(exc),
            )
            return None

    # Step 1: Fetch both refs through the gateway so we have current
    # origin/<branch> and origin/<base> tips locally.  fetch_worktree_branch
    # already runs `git fetch origin` (no refspec) which updates all
    # remote-tracking refs in one call.
    fetch_ok = spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    if not fetch_ok:
        _pkg.logger.warning(
            "rebase-on-resume: fetch failed, skipping rebase check",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
        )
        return

    # Step 2: Verify origin/<pipeline_branch> exists.  Fresh pipelines
    # haven't pushed yet, so there's nothing to rebase.
    verify_branch = _run_git(["rev-parse", "--verify", f"origin/{pipeline_branch}"], timeout=10)
    if verify_branch is None or verify_branch.returncode != 0:
        return

    verify_base = _run_git(["rev-parse", "--verify", f"origin/{base_branch}"], timeout=10)
    if verify_base is None or verify_base.returncode != 0:
        _pkg.logger.warning(
            "rebase-on-resume: origin/<base_branch> not resolvable, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
        )
        return

    # Step 3: Is the pipeline branch actually behind base?  If not, no-op.
    behind = _run_git(
        [
            "rev-list",
            "--count",
            f"origin/{pipeline_branch}..origin/{base_branch}",
        ],
        timeout=10,
    )
    if behind is None or behind.returncode != 0:
        _pkg.logger.warning(
            "rebase-on-resume: rev-list failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            stderr=(behind.stderr.strip() if behind is not None else None),
        )
        return
    try:
        behind_count = int(behind.stdout.strip() or "0")
    except ValueError:
        behind_count = 0
    if behind_count == 0:
        return

    # Step 4: Confirm reset-to-origin/<branch> is lossless before we
    # overwrite HEAD.  Three worktree states are handled:
    #
    #   (a) Preserved-worktree resume (#2098 canonical): the orchestrator-
    #       side worktree was kept across a cancel/resubmit, so HEAD
    #       carries state-file commits that were already pushed to
    #       origin/<branch>.  HEAD is a strict ancestor of
    #       origin/<branch> — resetting drops nothing.
    #   (b) Fresh-worktree resume: the worktree volume was wiped between
    #       cancel and resubmit (e.g. orchestrator redeploy onto a fresh
    #       PVC, manual cleanup), so the gateway recreated the worktree
    #       from origin/<base>.  HEAD == origin/<base>; resetting to
    #       origin/<branch> discards no unique commits because every
    #       commit on origin/<base> is preserved as the rebase target.
    #   (c) Confused-HEAD resume (#2222): the worktree carries a local-
    #       only commit (e.g. a half-pushed statefiles commit) on top of
    #       a stale origin/<branch> tip — HEAD is on neither ref.  The
    #       previous behaviour was to "defer to push-reconcile", but the
    #       reconcile path's _build_rebase_cmd fallback is the
    #       contamination producer in #2222.  Recover by hard-resetting
    #       to origin/<base>: any local-only work is dropped (it would
    #       be re-created by agents on the next phase, vastly preferable
    #       to a contaminated PR).
    def _head_on(ref: str) -> bool:
        result = _run_git(["merge-base", "--is-ancestor", "HEAD", ref], timeout=10)
        return result is not None and result.returncode == 0

    if not (_head_on(f"origin/{pipeline_branch}") or _head_on(f"origin/{base_branch}")):
        _pkg.logger.warning(
            "rebase-on-resume: HEAD on neither origin/<branch> nor origin/<base> — "
            "resetting to origin/<base> to avoid push-reconcile contamination (#2222)",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
            behind_base=behind_count,
        )
        # Step 4a: hard-reset to ``origin/<base>`` first.  Note that step 5
        # immediately overwrites HEAD again with ``reset --hard
        # origin/<branch>`` in the success path, so this reset's effect on
        # HEAD is short-lived — its purpose is to act as a safe-state floor:
        # if step 5 itself fails (network blip, ref vanishes), we leave the
        # worktree on a known-good ref (``origin/<base>``) instead of the
        # ambiguous pre-recovery state that prompted the rescue.  Don't
        # "simplify" by dropping this — the back-to-back hard resets are
        # intentional.
        recovery_reset = _run_git(["reset", "--hard", f"origin/{base_branch}"], timeout=30)
        if recovery_reset is None or recovery_reset.returncode != 0:
            _pkg.logger.warning(
                "rebase-on-resume: recovery reset to origin/<base> failed, skipping",
                pipeline_id=pipeline_id,
                branch=pipeline_branch,
                base_branch=base_branch,
                stderr=(recovery_reset.stderr.strip() if recovery_reset is not None else None),
            )
            return

    _pkg.logger.info(
        "rebase-on-resume: pipeline branch is behind base, attempting rebase",
        pipeline_id=pipeline_id,
        branch=pipeline_branch,
        base_branch=base_branch,
        behind_base=behind_count,
    )

    # Step 5: Reset the worktree to the stale pipeline branch tip so we
    # can rebase it onto current base.
    reset_to_branch = _run_git(["reset", "--hard", f"origin/{pipeline_branch}"], timeout=30)
    if reset_to_branch is None or reset_to_branch.returncode != 0:
        _pkg.logger.warning(
            "rebase-on-resume: reset to pipeline branch failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            stderr=(reset_to_branch.stderr.strip() if reset_to_branch is not None else None),
        )
        return

    # Step 6: Rebase onto current base.  Plain ``git rebase
    # origin/<base>`` — git's cherry-pick-skip drops content-equivalent
    # commits already on base (the 70+ stale-variant commits in #2098).
    rebase = _run_git(["rebase", f"origin/{base_branch}"], timeout=120)
    if rebase is None or rebase.returncode != 0:
        # Conflict, timeout, or other rebase failure.  Abort the rebase,
        # restore the worktree to origin/<base> so it isn't left mid-
        # rebase for downstream callers, and raise so the operator gets
        # an actionable error rather than a contaminated PR.  ``rebase
        # is None`` covers the timeout case where ``_run_git`` already
        # logged the underlying exception.
        _run_git(["rebase", "--abort"], timeout=30)
        _run_git(["reset", "--hard", f"origin/{base_branch}"], timeout=30)
        stderr_text = rebase.stderr.strip() if rebase is not None else "rebase command timed out"
        _pkg.logger.error(
            "rebase-on-resume: rebase failed — aborting pipeline start",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
            stderr=stderr_text,
            timed_out=rebase is None,
        )
        raise StalePipelineBranchError(
            f"origin/{pipeline_branch} is {behind_count} commits behind "
            f"origin/{base_branch} and rebasing it failed. "
            f"Manually rebase the branch (or delete it to start fresh) "
            f"and resubmit. Stderr: {stderr_text}"
        )

    # Git emits ``warning: skipped previously applied commit <sha>`` on
    # stderr for every cherry-pick-equivalent it dropped.  Counting them
    # gives operators a quick sanity check that the helper actually
    # discarded the stale-from-main commits (vs. e.g. silently no-op'd).
    skipped_via_rebase = sum(
        1 for line in rebase.stderr.splitlines() if "skipped previously applied commit" in line
    )

    # Step 7: Force-push the rebased branch.  ``force=True`` is required
    # because the rebased tip has different SHAs from origin/<branch>;
    # this is exactly the contamination we just removed, so overwriting
    # is the desired behavior.
    push_result = spawner.gateway.push_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        branch=pipeline_branch,
        mode=gateway_mode,
        base_branch=base_branch,
        force=True,
    )
    if not push_result.ok:
        # Restore HEAD to origin/<base> so the worktree is in a known
        # state for downstream callers (the rebased commits stay in the
        # local reflog if needed for recovery).
        _run_git(["reset", "--hard", f"origin/{base_branch}"], timeout=30)
        _pkg.logger.error(
            "rebase-on-resume: force-push of rebased branch failed",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            category=push_result.category,
            detail=push_result.detail,
        )
        raise StalePipelineBranchError(
            f"Rebased {pipeline_branch} onto origin/{base_branch} but "
            f"force-push to remote failed ({push_result.category}): "
            f"{push_result.detail}"
        )

    # Re-fetch so origin/<pipeline_branch> reflects the rebased tip for
    # any subsequent rev-parse in the same pipeline-start path.
    spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    _pkg.logger.info(
        "rebase-on-resume: rebased and force-pushed pipeline branch",
        pipeline_id=pipeline_id,
        branch=pipeline_branch,
        base_branch=base_branch,
        dropped_stale_commits=behind_count,
        skipped_via_rebase=skipped_via_rebase,
    )


def _refresh_pipeline_branch_against_current_base(
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    worktree_repo_path: Path,
    pipeline_branch: str,
    base_branch: str,
    gateway_mode: Literal["public", "private"] = "public",
) -> bool:
    """Rebase ``origin/<pipeline_branch>`` onto current ``origin/<base_branch>``
    immediately before opening the PR (#2224 PR 2).

    ``_rebase_pipeline_branch_onto_base`` runs at the start of each
    phase iteration to clean up stale branch state on resume.  Nothing
    between branch-cut and PR-open refreshes against
    ``origin/<base_branch>``; if ``base_branch`` advances *during* the
    PR phase's own work, the resulting PR is behind.  This helper
    closes that gap.

    The pipeline branch is the only ref this helper writes to: the
    rebase replays pipeline-branch commits onto current
    ``origin/<base_branch>``, and the force-push targets
    ``pipeline_branch``.  ``base_branch`` is read-only here — no
    commits are ever pushed to it, even when it happens to be
    ``main``.

    On success, force-pushes the rebased branch so the open PR's head
    SHA reflects the rebase.

    On *any* failure (rebase conflict, push rejection, transient gateway
    error), restores the worktree to ``origin/<pipeline_branch>``,
    logs at WARNING, and returns ``False`` — the caller still opens the
    PR against the un-rebased tip.  This is intentional: a merge conflict
    at PR-open time is better surfaced to the human reviewer than
    swallowed by failing the whole pipeline.

    Returns ``True`` when a rebase was performed and pushed; ``False``
    when no rebase was needed or any step failed (in which case the
    caller proceeds with the un-rebased tip).
    """
    if not pipeline_branch or not base_branch or pipeline_branch == base_branch:
        return False

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    def _run_git(
        args: list[str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [*git_base, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _pkg.logger.warning(
                "pr-open rebase: git command failed",
                pipeline_id=pipeline_id,
                branch=pipeline_branch,
                git_args=args,
                error=str(exc),
            )
            return None

    # Step 1: Fetch fresh refs.  Without this we'd rebase against the
    # base tip we saw at branch-cut, defeating the whole point.
    fetch_ok = spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    if not fetch_ok:
        _pkg.logger.warning(
            "pr-open rebase: fetch failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
        )
        return False

    # Step 2: Verify both refs resolve.
    verify_branch = _run_git(["rev-parse", "--verify", f"origin/{pipeline_branch}"], timeout=10)
    if verify_branch is None or verify_branch.returncode != 0:
        return False
    verify_base = _run_git(["rev-parse", "--verify", f"origin/{base_branch}"], timeout=10)
    if verify_base is None or verify_base.returncode != 0:
        return False

    # Step 3: No-op when the branch is already up-to-date with current base
    # (no commits behind).  Saves a force-push when none is needed.
    behind = _run_git(
        [
            "rev-list",
            "--count",
            f"origin/{pipeline_branch}..origin/{base_branch}",
        ],
        timeout=10,
    )
    if behind is None or behind.returncode != 0:
        return False
    try:
        behind_count = int((behind.stdout or "0").strip() or "0")
    except ValueError:
        behind_count = 0
    if behind_count == 0:
        return False

    # Step 4: Compute the merge-base so we can use the safe
    # ``--onto <new_base> <upstream>`` form (HEAD is the implicit branch
    # being rebased after the step-5 reset).  The merge-base is the
    # commit where the branch diverged from base_branch; using it as
    # ``<upstream>`` tells git "replay only the commits unique to HEAD
    # onto <new_base>" — no base-branch commits get absorbed into the
    # branch's linear history, which is the contamination shape #2222
    # hardened against in the push-reconcile path.
    merge_base_proc = _run_git(
        ["merge-base", f"origin/{pipeline_branch}", f"origin/{base_branch}"],
        timeout=15,
    )
    if merge_base_proc is None or merge_base_proc.returncode != 0:
        _pkg.logger.warning(
            "pr-open rebase: merge-base resolution failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
            stderr=(merge_base_proc.stderr.strip() if merge_base_proc is not None else None),
        )
        return False
    merge_base = (merge_base_proc.stdout or "").strip()
    if not merge_base:
        return False

    # Step 5: Reset the worktree to the current branch tip so the
    # rebase operates on the right starting state.  The reset target
    # is ``origin/<pipeline_branch>`` — fresh from fetch in step 1 —
    # so we are not rebasing on top of stale local state.
    #
    # Unlike ``_rebase_pipeline_branch_onto_base`` (resume-time helper),
    # there is no ``_head_on(...)`` ancestry guard before this reset.
    # That is intentional at this PR-open call site: any local-ahead
    # commits at this point are orchestrator housekeeping commits that
    # are orphan-by-design (the agents' work is already on
    # ``origin/<branch>`` via the per-cycle push) so nothing needs to
    # be preserved here.
    reset = _run_git(["reset", "--hard", f"origin/{pipeline_branch}"], timeout=30)
    if reset is None or reset.returncode != 0:
        _pkg.logger.warning(
            "pr-open rebase: reset to origin/<branch> failed, skipping",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            stderr=(reset.stderr.strip() if reset is not None else None),
        )
        return False

    # Step 6: Rebase using the safe ``--onto <new_base> <upstream>``
    # form.  HEAD is the implicit branch being rebased (set by the
    # step-5 reset above).  The closest argv-shape prior art is
    # ``gateway_client._build_rebase_cmd`` — that one rebases in the
    # opposite direction (replay HEAD onto a stale branch tip) but uses
    # the same explicit-upstream pattern that pins the replay range to
    # ``<upstream>..HEAD`` and so sidesteps the bare-form contamination
    # shape behind #2222.
    rebase = _run_git(
        [
            "rebase",
            "--onto",
            f"origin/{base_branch}",
            merge_base,
        ],
        timeout=120,
    )
    if rebase is None or rebase.returncode != 0:
        # Conflict, timeout, or any failure: abort cleanly and restore
        # to origin/<branch> so the caller can still open the PR
        # against the un-rebased tip.  Unlike the resume-time helper,
        # we *don't* raise here — pipeline failure for a merge conflict
        # at PR-open time is worse than a slightly-behind PR.
        _run_git(["rebase", "--abort"], timeout=30)
        _run_git(["reset", "--hard", f"origin/{pipeline_branch}"], timeout=30)
        stderr_text = rebase.stderr.strip() if rebase is not None else "rebase command timed out"
        _pkg.logger.warning(
            "pr-open rebase: rebase failed, opening PR against un-rebased tip",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            base_branch=base_branch,
            behind_base=behind_count,
            stderr=stderr_text,
            timed_out=rebase is None,
        )
        return False

    # Step 7: Force-push the rebased tip so origin/<branch> matches the
    # SHAs the PR will be opened against.
    push_result = spawner.gateway.push_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        branch=pipeline_branch,
        mode=gateway_mode,
        base_branch=base_branch,
        force=True,
    )
    if not push_result.ok:
        # Best-effort restore so the worktree state is predictable for
        # downstream callers; the PR still opens against the pre-rebase
        # remote tip (which is what origin/<branch> still reflects).
        _run_git(["reset", "--hard", f"origin/{pipeline_branch}"], timeout=30)
        _pkg.logger.warning(
            "pr-open rebase: force-push of rebased branch failed, "
            "opening PR against un-rebased remote tip",
            pipeline_id=pipeline_id,
            branch=pipeline_branch,
            category=push_result.category,
            detail=push_result.detail,
        )
        return False

    # Re-fetch so origin/<branch> reflects the pushed tip locally.
    spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
        mode=gateway_mode,
    )
    _pkg.logger.info(
        "pr-open rebase: rebased and force-pushed pipeline branch",
        pipeline_id=pipeline_id,
        branch=pipeline_branch,
        base_branch=base_branch,
        behind_base_at_start=behind_count,
    )
    return True


def _read_tree_head(git_base: list[str]) -> None:
    """Refresh the index from HEAD without touching the working tree.

    Defends ``_commit_statefiles_to_worktree`` against a cross-worktree
    branch-ref advance.  When an agent runs the gateway-allowed recovery
    primitive ``git update-ref refs/heads/<pipeline-branch> <sha>`` from
    a sibling worktree (see ``sandbox/agent-config/rules/branch-recovery.md``
    and the detached-HEAD hint in ``gateway/gateway.py``), the shared local
    branch ref advances out from under this worktree.  ``update-ref`` does
    not honour per-worktree locks, so this worktree's HEAD symref
    silently jumps to the agent's commit while the index and working tree
    stay at the prior state.  Without this refresh, the stale index
    reports every agent-pushed file as a *staged deletion* against HEAD,
    and the subsequent ``git commit`` lands them as a real delete commit
    (the symptom in #2626).  ``read-tree HEAD`` repoints the index to
    the new HEAD without touching the working tree; the immediately
    following ``git add --force`` then stages only the orchestrator's
    on-disk writes.
    """
    subprocess.run(
        [*git_base, "read-tree", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _restore_missing_state_files_from_head(
    git_base: list[str],
    worktree_path: Path,
    pipeline_id: str | None = None,
) -> None:
    """Materialize tracked ``.egg-state/`` files that HEAD has but disk doesn't.

    Companion to :func:`_read_tree_head`: the same cross-worktree
    ``update-ref`` advance behind #2626 leaves the working tree stale
    relative to the just-advanced HEAD.  The #2626 fix protected the
    *commit* (no delete-commit lands), but downstream readers go through
    the working tree — :func:`_populate_contract_from_plan` reads the
    plan draft via ``Path(...).read_text()``, which fails with the
    natural ``PlanDraftMissingOnLocalError`` even though HEAD itself
    carries the agent-pushed draft (the #2721 symptom; recovery in the
    field was ``git checkout HEAD -- .egg-state/drafts/
    .egg-state/agent-outputs/``).

    ``git ls-files -z --deleted -- .egg-state/`` lists tracked files
    that are missing on disk.  ``-z`` switches the output to
    NUL-separated raw bytes so paths with non-ASCII chars, newlines, or
    quote chars survive parsing intact (with the default
    ``core.quotePath=true`` the non-``-z`` form C-quote-encodes those
    paths and ``splitlines()`` would misparse them).  Must be called
    AFTER :func:`_read_tree_head` so the index reflects HEAD; otherwise
    a stale index can leave the delete-list incomplete.  ``git checkout
    HEAD --pathspec-from-file=- --pathspec-file-nul`` then restores each
    missing path in both the index and the working tree (the index
    reset is a no-op because read-tree HEAD already aligned it); piping
    the NUL-separated list via stdin sidesteps any ARG_MAX limit on the
    argv path even for pathological ``.egg-state/`` populations.
    Confined to ``.egg-state/`` so the restoration cannot resurrect a
    sibling-pipeline file the orchestrator deliberately removed
    elsewhere in the tree.

    Fail-open: any subprocess error logs and returns silently — the
    downstream populator still has its own missing-draft guard, so a
    failure here cannot silently hide a true draft-missing case.
    """
    try:
        deleted = subprocess.run(
            [*git_base, "ls-files", "-z", "--deleted", "--", ".egg-state/"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as ls_err:
        _pkg.logger.warning(
            "_restore_missing_state_files_from_head: ls-files probe failed",
            worktree_path=str(worktree_path),
            pipeline_id=pipeline_id,
            error=str(ls_err),
        )
        return
    if deleted.returncode != 0:
        _pkg.logger.warning(
            "_restore_missing_state_files_from_head: ls-files probe failed",
            worktree_path=str(worktree_path),
            pipeline_id=pipeline_id,
            returncode=deleted.returncode,
            stderr=deleted.stderr.decode("utf-8", errors="replace").strip()[:200],
        )
        return
    missing_paths = [p for p in deleted.stdout.split(b"\0") if p]
    if not missing_paths:
        return
    pathspec_stdin = b"\0".join(missing_paths) + b"\0"
    try:
        restore = subprocess.run(
            [
                *git_base,
                "checkout",
                "HEAD",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            input=pathspec_stdin,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as checkout_err:
        _pkg.logger.warning(
            "_restore_missing_state_files_from_head: checkout failed",
            worktree_path=str(worktree_path),
            pipeline_id=pipeline_id,
            missing_count=len(missing_paths),
            error=str(checkout_err),
        )
        return
    if restore.returncode != 0:
        _pkg.logger.warning(
            "_restore_missing_state_files_from_head: checkout failed",
            worktree_path=str(worktree_path),
            pipeline_id=pipeline_id,
            missing_count=len(missing_paths),
            returncode=restore.returncode,
            stderr=restore.stderr.decode("utf-8", errors="replace").strip()[:200],
        )
        return
    _pkg.logger.info(
        "_restore_missing_state_files_from_head: restored tracked-but-missing files",
        worktree_path=str(worktree_path),
        pipeline_id=pipeline_id,
        restored_count=len(missing_paths),
        restored_sample=[p.decode("utf-8", errors="replace") for p in missing_paths[:5]],
    )
