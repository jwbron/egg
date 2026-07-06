"""alerts helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim; patched/barrel-resident globals reached via _pkg so
patch("routes.pipelines.<name>") keeps intercepting.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import routes.pipelines as _pkg  # noqa: E402,F401

if TYPE_CHECKING:
    try:
        from ..container_spawner import ContainerSpawner  # noqa: F401
    except ImportError:  # pragma: no cover
        from container_spawner import ContainerSpawner  # type: ignore  # noqa: F401

from events import EventType
from models import Pipeline, PipelinePhase, PipelineStatus
from state_store import StateStore

from ._worktree_sync import WorktreeSyncOutcome


def _emit_divergence_reconcile_hitl(
    pipeline_id: str,
    store,  # noqa: ANN001 — StateStore (avoid import cycle)
    *,
    phase: PipelinePhase | None,
    backup_ref: str | None,
    local_only_commit_shas: tuple[str, ...] | list[str],
    rebase_category: str | None = None,
    rebase_detail: str | None = None,
):
    """Pin pipeline+phase to AWAITING_HUMAN and persist the reconcile HITL (#2979).

    Used by the non-blocking ``populate_contract`` route, which cannot
    block on the operator the way the in-loop phase-boundary callers do.
    Sets the pipeline + phase to ``AWAITING_HUMAN`` (NOT ``FAILED`` — the
    divergence is recoverable and nothing was discarded) and persists the
    reconcile HITL under the same lock so a reader never observes
    ``AWAITING_HUMAN`` without the pending decision, then broadcasts a
    ``decision.created`` event.

    Returns the persisted decision (or None on persistence failure).  The
    operator reconciles the worktree, resolves this decision, and re-runs
    ``populate_contract`` against the now-reconciled worktree.
    """
    with _pkg.get_pipeline_state_lock(pipeline_id):
        pipeline = store.load_pipeline(pipeline_id)
        if phase is not None:
            phase_execution = pipeline.get_phase_execution(phase)
            if phase_execution is not None:
                phase_execution.status = PipelineStatus.AWAITING_HUMAN
        pipeline.status = PipelineStatus.AWAITING_HUMAN
        store.save_pipeline(pipeline)
        decision = _pkg._persist_hitl_decision(
            pipeline_id,
            pipeline,
            store,
            question=_pkg._divergence_reconcile_hitl_question(
                pipeline_id=pipeline_id,
                phase=phase,
                backup_ref=backup_ref,
                local_only_commit_shas=tuple(local_only_commit_shas),
                rebase_category=rebase_category,
                rebase_detail=rebase_detail,
            ),
            options=list(_pkg._DIVERGENCE_RECONCILE_HITL_OPTIONS),
            phase=phase,
            context=_pkg._DIVERGENCE_RECONCILE_HITL_CONTEXT,
        )
    _pkg.report_pipeline_status(
        pipeline,
        event_type="decision.created",
        message=(
            f"Awaiting manual worktree reconcile for "
            f"{phase.value if phase else 'current phase'} phase"
        ),
    )
    _pkg._emit_pipeline_event(pipeline, "decision.created")
    return decision


def _fail_pipeline_after_divergence_abort(
    pipeline_id: str,
    store,  # noqa: ANN001 — StateStore (avoid import cycle)
    *,
    phase: PipelinePhase | None,
    backup_ref: str | None,
    local_only_commit_shas: tuple[str, ...] | list[str],
    budget_exhausted: bool = False,
    pre_event_hook: Callable[[], None] | None = None,
) -> None:
    """Pin pipeline+phase to FAILED after an aborted divergence reconcile (#2979).

    Reached when the operator resolved the reconcile HITL with
    ``Abort pipeline`` (or the reconcile pause budget was exhausted).  No
    HITL is emitted here — the reconcile decision was already surfaced and
    resolved.  Mirrors the FAILED-write + ``pipeline.failed`` broadcast of
    the old destructive-recovery helper, minus the discard: the committed
    work is still on HEAD and pinned under ``backup_ref`` for offline
    recovery.

    ``pre_event_hook`` runs after the FAILED-write but before the public
    ``pipeline.failed`` broadcast (the post-phase site uses it to tear down
    the per-phase overseer container).
    """
    phase_label = phase.value if phase is not None else "current phase"
    reason = (
        "the reconcile pause budget was exhausted"
        if budget_exhausted
        else "the operator chose to abort"
    )
    error_message = (
        f"Worktree diverged from origin at {phase_label} and could not be "
        f"auto-reconciled; {reason} (#2979). Local-only commits are "
        f"preserved under {backup_ref or '(backup ref write failed)'} "
        f"({len(local_only_commit_shas)} commit(s))."
    )
    with _pkg.get_pipeline_state_lock(pipeline_id):
        pipeline = store.load_pipeline(pipeline_id)
        if phase is not None:
            phase_execution = pipeline.get_phase_execution(phase)
            if phase_execution is not None:
                phase_execution.status = PipelineStatus.FAILED
                phase_execution.error = error_message
                phase_execution.completed_at = datetime.now(UTC)
        pipeline.status = PipelineStatus.FAILED
        pipeline.error = error_message
        store.save_pipeline(pipeline)
    if pre_event_hook is not None:
        pre_event_hook()
    _pkg.report_pipeline_status(
        pipeline,
        event_type="pipeline.failed",
        message=f"Pipeline failed: {error_message[:100]}",
    )
    _pkg._emit_pipeline_event(pipeline, "pipeline.failed")


def _sync_worktree_reconciling_divergence(
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    store,  # noqa: ANN001 — StateStore (avoid import cycle)
    repo_path: Path,
    *,
    worktree_repo_path: Path,
    phase: PipelinePhase | None,
    gateway_mode: Literal["public", "private"] = "public",
    base_branch: str | None = None,
    pipeline_branch: str | None = None,
    prior_phase_succeeded: bool = True,
    max_reconcile_pauses: int = _pkg._MAX_DIVERGENCE_RECONCILE_PAUSES,
) -> tuple[WorktreeSyncOutcome, bool]:
    """Sync the worktree, pausing for a manual reconcile on divergence (#2979).

    Runs :func:`_sync_worktree_with_remote`.  When the helper reports an
    unreconciled divergence (``diverged_unreconciled``), the worktree is
    left non-destructively at the local HEAD; this function pauses the
    pipeline (``AWAITING_HUMAN``) on a reconcile HITL and **blocks** the
    ``_run_pipeline`` thread on ``wait_for_decision`` — the same proven
    pause primitive the phase-approval gate uses.  When the operator
    resolves the HITL with "Reconciled — resume", the pipeline returns to
    ``RUNNING`` and the sync re-runs; the caller then continues the same
    phase's post-processing from where it paused, with no full re-run and
    nothing discarded.

    Returns ``(outcome, aborted)``.  ``aborted`` is True when the operator
    chose "Abort pipeline" or the reconcile-pause budget was exhausted; the
    caller should fail the pipeline via
    :func:`_fail_pipeline_after_divergence_abort`.  When ``aborted`` is
    False the worktree is reconciled (or never diverged) and the caller
    proceeds normally.

    Only call this from inside the ``_run_pipeline`` loop thread, which is
    allowed to block; route handlers that cannot block use
    :func:`_emit_divergence_reconcile_hitl` instead.
    """
    dq = _pkg.get_decision_queue(pipeline_id, repo_path)
    phase_label = phase.value if phase is not None else "current phase"

    outcome = _pkg._sync_worktree_with_remote(
        spawner,
        pipeline_id,
        worktree_repo_path,
        prior_phase_succeeded=prior_phase_succeeded,
        gateway_mode=gateway_mode,
        base_branch=base_branch,
        pipeline_branch=pipeline_branch,
    )

    pauses = 0
    while outcome.diverged_unreconciled:
        if pauses >= max_reconcile_pauses:
            _pkg.logger.error(
                "OVERSEER_ALERT worktree_divergence_reconcile_budget_exhausted",
                pipeline_id=pipeline_id,
                phase=phase_label,
                pauses=pauses,
                backup_ref=outcome.backup_ref,
            )
            return outcome, True
        pauses += 1

        # Persist the reconcile HITL and flip to AWAITING_HUMAN under the
        # (reentrant) state lock so a reader never sees AWAITING_HUMAN
        # without the pending decision.
        with _pkg.get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)
            pipeline.status = PipelineStatus.AWAITING_HUMAN
            if phase is not None:
                phase_execution = pipeline.get_phase_execution(phase)
                if phase_execution is not None:
                    phase_execution.status = PipelineStatus.AWAITING_HUMAN
            store.save_pipeline(pipeline)
            decision = _pkg._persist_hitl_decision(
                pipeline_id,
                pipeline,
                store,
                question=_pkg._divergence_reconcile_hitl_question(
                    pipeline_id=pipeline_id,
                    phase=phase,
                    backup_ref=outcome.backup_ref,
                    local_only_commit_shas=outcome.local_only_commit_shas,
                    rebase_category=outcome.rebase_category,
                    rebase_detail=outcome.rebase_detail,
                ),
                options=list(_pkg._DIVERGENCE_RECONCILE_HITL_OPTIONS),
                phase=phase,
                context=_pkg._DIVERGENCE_RECONCILE_HITL_CONTEXT,
            )
        if decision is None:
            # Could not persist the HITL — fail closed rather than spin on
            # a pause the operator can never see.
            _pkg.logger.error(
                "worktree_divergence_reconcile_hitl_persist_failed",
                pipeline_id=pipeline_id,
                phase=phase_label,
            )
            return outcome, True

        _pkg.logger.error(
            "OVERSEER_ALERT worktree_divergence_reconcile_pause",
            pipeline_id=pipeline_id,
            phase=phase_label,
            backup_ref=outcome.backup_ref,
            local_only_commit_count=len(outcome.local_only_commit_shas),
            rebase_category=outcome.rebase_category,
            rebase_detail=outcome.rebase_detail,
            pause_attempt=pauses,
        )

        # Once AWAITING_HUMAN is persisted, an unexpected exception
        # between here and the resume-write (e.g. a broadcast IO error,
        # a decision-queue runtime error, a transient store failure on
        # ``get_decision``) would leave the pipeline pinned to
        # AWAITING_HUMAN on disk while ``_run_pipeline``'s outer
        # ``try/except`` catches the error and moves on — stranding the
        # operator with no waiter ever returning.  Guard the
        # wait-and-resolve span: on unexpected error revert to RUNNING
        # before re-raising so the caller still observes the failure
        # but the pipeline is not left in an unrecoverable paused state.
        # The abort path (operator chose ``Abort pipeline``) returns
        # normally with ``aborted=True`` so the caller can flip to
        # FAILED — that's not an exception and skips the revert.
        try:
            _pkg.report_pipeline_status(
                pipeline,
                event_type="decision.created",
                message=f"Awaiting manual worktree reconcile for {phase_label} phase",
            )
            _pkg._emit_pipeline_event(pipeline, "decision.created")

            dq.wait_for_decision(decision.id)

            resolved = dq.get_decision(decision.id)
            resolution = (resolved.resolution or "") if resolved is not None else ""
            if _pkg._divergence_reconcile_is_abort(resolution):
                _pkg.logger.warning(
                    "worktree_divergence_reconcile_aborted_by_operator",
                    pipeline_id=pipeline_id,
                    phase=phase_label,
                )
                return outcome, True

            # Operator reconciled the worktree — return to RUNNING and re-run
            # the sync.  If it still diverges, loop and re-pause (bounded).
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.status = PipelineStatus.RUNNING
                if phase is not None:
                    phase_execution = pipeline.get_phase_execution(phase)
                    if phase_execution is not None:
                        phase_execution.status = PipelineStatus.RUNNING
                store.save_pipeline(pipeline)
        except Exception:
            # Best-effort revert: load fresh, flip AWAITING_HUMAN→RUNNING
            # only if still pinned, then re-raise.  Swallow secondary
            # errors from the revert itself — losing the revert is bad,
            # but masking the original failure with a save error is
            # worse.  The operator can still recover via the pending
            # decision (the decision queue may have replayed it on
            # restart) or via the backup ref.
            try:
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    if pipeline.status == PipelineStatus.AWAITING_HUMAN:
                        pipeline.status = PipelineStatus.RUNNING
                    if phase is not None:
                        phase_execution = pipeline.get_phase_execution(phase)
                        if (
                            phase_execution is not None
                            and phase_execution.status == PipelineStatus.AWAITING_HUMAN
                        ):
                            phase_execution.status = PipelineStatus.RUNNING
                    store.save_pipeline(pipeline)
            except Exception:
                _pkg.logger.warning(
                    "worktree_divergence_reconcile_revert_failed",
                    pipeline_id=pipeline_id,
                    phase=phase_label,
                    exc_info=True,
                )
            raise
        _pkg.logger.info(
            "worktree_divergence_reconcile_resume",
            pipeline_id=pipeline_id,
            phase=phase_label,
            pause_attempt=pauses,
        )
        outcome = _pkg._sync_worktree_with_remote(
            spawner,
            pipeline_id,
            worktree_repo_path,
            prior_phase_succeeded=prior_phase_succeeded,
            gateway_mode=gateway_mode,
            base_branch=base_branch,
            pipeline_branch=pipeline_branch,
        )

    return outcome, False


def _emit_empty_contract_hitl(
    pipeline_id: str,
    pipeline: Pipeline,
    store: StateStore,
    *,
    reason: str,
    draft_slice_count: int | None,
    gate: Literal[
        "slice_gate",
        "start_phase_implement_safety_net",
        "plan_complete",
    ],
    phase: PipelinePhase | None = None,
):
    """Persist a dedicated HITL naming the empty-contract divergence (#2627).

    Built on top of :func:`_persist_hitl_decision` so it inherits the
    "load → mutate → save under lock" persistence semantics that make
    the decision survive the FAILED-write the calling block does next.
    Best-effort: a persistence failure logs and returns None so the
    surrounding FAILED-cleanup is not blocked.

    Returns the persisted decision (or None on persistence failure).

    Plain "Retry phase" against this HITL would respawn the implement
    phase into the same empty-contract state, so the option set is
    distinct from the generic phase-failure decision: callers are
    expected to wire each option to its concrete recovery action
    (see :data:`_EMPTY_CONTRACT_HITL_OPTIONS` for the mapping).
    """
    # ``_empty_contract_hitl_question`` is defined further down the
    # module alongside the other #2627 follow-up helpers; importing
    # the symbol here keeps the call-site test isolated from module
    # top-level ordering.
    return _pkg._persist_hitl_decision(
        pipeline_id,
        pipeline,
        store,
        question=_pkg._empty_contract_hitl_question(
            pipeline_id=pipeline_id,
            reason=reason,
            draft_slice_count=draft_slice_count,
            gate=gate,
        ),
        options=list(_pkg._EMPTY_CONTRACT_HITL_OPTIONS),
        phase=phase,
    )


def _check_brc_progress_gate(
    pipeline_id: str,
    slice_id: str | None,
    active_role_names: list[str],
    gate_seconds: float,
) -> tuple[bool, str | None]:
    """Return (defer, reason) for the BRC consensus-timeout progress gate (#2243).

    Defers the consensus-timeout ``OVERSEER_ALERT`` (#2264; previously
    an auto-``choice`` HITL decision) when *any* of the following has
    fired within ``gate_seconds``:

    * The BRC tracker's most recent ``CONSENSUS_PROPOSE`` (producer
      proposal) timestamp.
    * The most recent ACK/NACK timestamp on the approval matrix.
    * The most recent container heartbeat for any role in
      ``active_role_names`` (filters out cross-phase pollution in the
      shared :class:`HealthMonitor` singleton).

    The gate is the operator-friendly half of the issue-2243 fix: at
    :data:`consensus_timeout_minutes` we previously opened a `choice`
    decision unconditionally, even when producers were minutes from
    their first commit. With the gate, the polling loop keeps polling
    while signals are alive; the alert is only published once the bus
    and containers have both gone quiet for ``gate_seconds``.

    ``gate_seconds <= 0`` disables the gate (returns ``(False, None)``).
    Failures in any signal source are logged at WARNING and treated as
    "no signal from that source" — never as a gate defer, since a
    crashed signal collector must not silently keep us off the alert
    surface.

    Heartbeat-cadence contract: the coder-mid-merge-conflict path
    (no ``CONSENSUS_PROPOSE`` yet, only container heartbeats — the
    original incident's ``decision-17`` flavour, pre-#2264) relies on
    container heartbeats firing at least every ``gate_seconds``. Sandbox
    heartbeats (see ``shared/egg_agent`` heartbeat scheduler and
    ``orchestrator/health_monitor.py``) cadence today is well under
    300s, but a long uninterruptible subprocess (e.g. ``git rebase``
    blocked on a merge driver) could starve them; once that happens
    the gate falls open and the pre-fix behaviour returns. Tracked as
    a follow-up under #2243.

    TODO(#2243 step 2): same-role cross-phase pollution. The role-name
    filter handles different-role ghosts (refiner heartbeat lingering
    during a coder phase) but not same-role ghosts: ``coder`` reappears
    across implement / implement-fix / fix-on-PR phases and
    ``HealthMonitor._last_heartbeat['coder']`` is only popped on
    ``clear_agent_state``. A phase boundary clear (or stamping the
    heartbeat key with the phase) would close it; per-phase timeouts
    in step 2 of the issue plan will likely subsume it.
    """
    if gate_seconds <= 0:
        return False, None

    # Two clocks, deliberately. ``now_dt`` is used for tracker
    # timestamps (datetime in UTC). ``now_wallclock`` is the float
    # epoch ``time.time()`` returns, matching the wall-clock values
    # ``HealthMonitor._last_heartbeat`` is populated with. Despite the
    # earlier name ``now_mono``, these are NOT monotonic — an NTP step
    # on the orchestrator host can make ``(now - latest_hb)`` negative
    # or skip the gate window. Acceptable today; revisit alongside the
    # per-phase-timeout follow-up.
    now_dt = datetime.now(UTC)
    now_wallclock = time.time()

    # 1. BRC bus signals (proposal + ACK/NACK timestamps).
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (
                get_peer_consensus_tracker,  # type: ignore[no-redef]
            )
        tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
        if tracker is not None:
            ts = tracker.get_latest_progress_timestamp()
            if ts is not None and (now_dt - ts).total_seconds() < gate_seconds:
                age = (now_dt - ts).total_seconds()
                return True, f"BRC bus active {age:.0f}s ago"
    except Exception as e:
        _pkg.logger.warning(
            "BRC progress-gate tracker check failed",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    # 2. Container heartbeats. Filter by active roles so a stale
    # heartbeat from a prior phase in the singleton HealthMonitor
    # doesn't keep us out of the HITL surface forever. An empty
    # ``active_role_names`` means the caller has no live containers
    # to gate on, so match nothing rather than every stale heartbeat.
    if not active_role_names:
        return False, None
    try:
        from health_monitor import get_health_monitor

        hm = get_health_monitor()
        if hm is not None:
            active_set = set(active_role_names)
            latest_hb: float | None = None
            with hm._lock:  # noqa: SLF001 — read-only snapshot
                hb_snapshot = dict(hm._last_heartbeat)  # noqa: SLF001
            for agent_id, hb_time in hb_snapshot.items():
                if agent_id not in active_set:
                    continue
                if latest_hb is None or hb_time > latest_hb:
                    latest_hb = hb_time
            if latest_hb is not None and (now_wallclock - latest_hb) < gate_seconds:
                age = now_wallclock - latest_hb
                return True, f"container heartbeat {age:.0f}s ago"
    except Exception as e:
        _pkg.logger.warning(
            "BRC progress-gate heartbeat check failed",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    return False, None


def _latest_active_role_heartbeat(active_role_names: list[str]) -> datetime | None:
    """Return the most recent heartbeat timestamp across ``active_role_names``.

    Mirrors the heartbeat half of :func:`_check_brc_progress_gate` so the
    consensus-timeout ``OVERSEER_ALERT`` carries a meaningful
    ``latest_heartbeat_at`` value. Filters by active role to avoid
    pollution from stale entries in the singleton ``HealthMonitor``.

    Returns ``None`` when no live heartbeat is available (no roles
    given, no health monitor, or any failure in the lookup — failures
    are logged at WARNING and treated as "no signal", consistent with
    the gate).
    """
    if not active_role_names:
        return None
    try:
        from health_monitor import get_health_monitor

        hm = get_health_monitor()
        if hm is None:
            return None
        active_set = set(active_role_names)
        latest_hb: float | None = None
        with hm._lock:  # noqa: SLF001 — read-only snapshot
            hb_snapshot = dict(hm._last_heartbeat)  # noqa: SLF001
        for agent_id, hb_time in hb_snapshot.items():
            if agent_id not in active_set:
                continue
            if latest_hb is None or hb_time > latest_hb:
                latest_hb = hb_time
        if latest_hb is None:
            return None
        return datetime.fromtimestamp(latest_hb, tz=UTC)
    except Exception as e:
        _pkg.logger.warning(
            "Consensus-timeout alert heartbeat lookup failed",
            error=str(e),
            exc_info=True,
        )
        return None


def _unresolved_contract_hitl_ids(
    pipeline_id: str,
    pipeline: Pipeline,
    phase_str: str,
) -> list[str]:
    """Return ids of unresolved contract HITL (``cq-N``) decisions gating ``phase_str``.

    Feeds the consensus-timeout HITL gate (#3426): while an agent-registered
    contract question (``register_open_question`` / impasse escalation) for
    the running phase awaits an operator answer, the slice is *operator-gated*
    — a reviewer correctly withholding its ACK pending the ruling is not a
    convergence failure, so the consensus-timeout clock must not expire the
    phase. Scoped to decisions whose ``phase`` matches the running phase;
    phase-less decisions are skipped, mirroring
    ``_collect_unresolved_phase_decisions`` (we cannot prove they gate this
    phase, and an eternally-unanswered legacy entry must not suspend the
    timeout forever).

    Contract decisions have no slice tag, so during a sliced implement phase
    any unresolved implement-tagged question suspends every slice's timeout.
    That errs toward parking rather than failing — acceptable, since the
    overseer's "wedged on HITL" alert stays sticky and the operator's answer
    releases the gate.

    The gate keys on the *existence* of an operator-facing HITL decision
    tagged to this phase, not on causal proof that decision is what a
    reviewer is withholding an ACK for — decisions carry no link to the
    ACK they block. An unrelated implement-tagged HITL therefore suspends
    the timeout too; that is the conservative "park rather than fail"
    direction, self-corrected by the clock reset on release (a genuine
    stall times out on the fresh window) and by the overseer's other
    health checks.

    Fail-open: any failure (missing worktree, unloadable contract) returns
    ``[]`` so a broken scan degrades to the pre-#3426 timeout behaviour
    rather than suspending the clock indefinitely. Matching the sibling
    ``_collect_unresolved_phase_decisions``, the except set is narrowed to
    the IO/validation failures a real scan can hit and logged at
    ``warning`` (so a broken scan is observable, not a silent no-op),
    while programming errors (``AttributeError``/``TypeError``/``NameError``)
    are left to propagate so they surface during development.
    """
    try:
        import contract_store
        from egg_contracts import load_contract
        from egg_contracts.loader import (
            ContractNotFoundError,
            ContractValidationError,
        )
    except ImportError:
        _pkg.logger.warning(
            "Consensus-timeout HITL gate: egg_contracts unavailable, cannot scan",
            pipeline_id=pipeline_id,
            exc_info=True,
        )
        return []

    try:
        worktree = contract_store.resolve_pipeline_worktree(pipeline_id)
        if worktree is None:
            return []
        identifier = _pkg._pipeline_identifier(getattr(pipeline, "issue_number", None), pipeline_id)
        contract = load_contract(identifier, worktree)
    except OSError, ValueError, ContractNotFoundError, ContractValidationError:
        # OSError: filesystem failures resolving the worktree / reading the
        # contract. ValueError: identifier / path-resolution failures from
        # ``_pipeline_identifier`` (``load_contract`` wraps pydantic-V2
        # validation errors as ContractValidationError, so a raw ValueError
        # here does not come from schema validation). Contract*: missing or
        # corrupt contract JSON. All fail open to ``[]``.
        _pkg.logger.warning(
            "Consensus-timeout HITL gate contract scan failed",
            pipeline_id=pipeline_id,
            exc_info=True,
        )
        return []

    ids: list[str] = []
    for d in contract.decisions or []:
        if d.resolved:
            continue
        if getattr(d.type, "value", d.type) != "hitl":
            continue
        if getattr(d.phase, "value", d.phase) != phase_str:
            continue
        ids.append(d.id)
    return ids


def _publish_consensus_timeout_alert(
    pipeline: Pipeline,
    pipeline_id: str,
    consensus_timeout: float,
    blocking_agents: list[str],
    *,
    priority: str,
    latest_proposal_at: datetime | None,
    latest_heartbeat_at: datetime | None,
    slice_id: str | None,
) -> None:
    """Publish a consensus-timeout ``OVERSEER_ALERT`` (#2264).

    Replaces the old auto-``choice`` HITL decision the orchestrator
    used to open at ``consensus_timeout_minutes``. The SDLC skill's
    existing ``OVERSEER_ALERT`` flow surfaces this as a non-blocking
    notification (Check agent logs / Acknowledge / Cancel pipeline)
    rather than gating the pipeline on a binary choice.

    Best-effort: if the message store import or write fails, log at
    WARNING and return — the orchestrator log is the always-on
    fallback (mirrors the slice-cascade alert path).
    """
    timeout_minutes = int(consensus_timeout / 60)
    phase_value = (
        pipeline.current_phase.value
        if hasattr(pipeline.current_phase, "value")
        else str(pipeline.current_phase)
    )
    # Subject role slot follows the SDLC skill convention
    # ``<anomaly_type>: <agent_role> [<priority>]`` (skills/sdlc/SKILL.md
    # §"Overseer Alert Detection") so "Check agent logs" extracts a role
    # the host can pass to ``get_container_logs``. Fall back to the phase
    # only when no blocking role is reported — the phase still appears in
    # ``metadata.phase`` regardless.
    subject_role = blocking_agents[0] if blocking_agents else phase_value
    subject = f"consensus-timeout: {subject_role} [{priority}]"
    blockers_render = ", ".join(blocking_agents) if blocking_agents else "(none reported)"
    proposal_render = (
        latest_proposal_at.isoformat() if latest_proposal_at is not None else "no proposals seen"
    )
    heartbeat_render = (
        latest_heartbeat_at.isoformat()
        if latest_heartbeat_at is not None
        else "no recent heartbeat"
    )
    body = (
        f"BRC consensus has not converged after {timeout_minutes} minutes "
        f"in phase '{phase_value}'.\n"
        f"Blocking agents: {blockers_render}\n"
        f"Latest proposal: {proposal_render}\n"
        f"Latest heartbeat (active roles): {heartbeat_render}\n\n"
        "The pipeline continues to poll for convergence (up to ~60 min "
        "before still-running containers are force-killed). If you want "
        "to intervene, use `cancel_task` to stop the pipeline or "
        "`restart_phase` to retry."
    )
    metadata: dict[str, Any] = {
        "anomaly_type": "consensus-timeout",
        "phase": phase_value,
        "blocking_agents": list(blocking_agents),
        "latest_proposal_at": (
            latest_proposal_at.isoformat() if latest_proposal_at is not None else None
        ),
        "latest_heartbeat_at": (
            latest_heartbeat_at.isoformat() if latest_heartbeat_at is not None else None
        ),
        "consensus_timeout_minutes": timeout_minutes,
        "priority": priority,
    }
    if slice_id is not None:
        metadata["slice_id"] = slice_id

    try:
        try:
            from message_store import Message, MessageType
        except ImportError:
            from ..message_store import (  # type: ignore[no-redef]
                Message,
                MessageType,
            )
        store_fn = _pkg._get_message_store()
        if store_fn is None:
            _pkg.logger.warning(
                "Consensus-timeout alert: message store unavailable",
                pipeline_id=pipeline_id,
            )
            return
        msg_store = store_fn()
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject=subject,
                body=body,
                metadata=metadata,
                phase=phase_value,
            )
        )
    except Exception as e:
        _pkg.logger.warning(
            "Failed to publish consensus-timeout OVERSEER_ALERT",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )


def _emit_producer_death_alert(
    *,
    pipeline_id: str,
    role: str,
    phase: str,
    slice_id: str | None,
    exit_code: int,
) -> None:
    """Publish a high-priority ``OVERSEER_ALERT`` for permanent producer death (#2806).

    Fires from ``_run_concurrent_phase`` when a producer's
    consensus-wrapper container exits with a non-clean code after
    exhausting its retry budget. The pipeline (or slice) is about to
    transition to FAILED — the alert is what makes the operator notice
    rather than waiting for the consensus-timeout / overseer
    ``stuck-phase-transition`` alert to fire 30+ minutes later.

    Best-effort: failures to write to the message store degrade to a
    WARNING log, mirroring ``_publish_consensus_timeout_alert``.
    """
    phase_value = phase if isinstance(phase, str) else getattr(phase, "value", str(phase))
    # ``is not None`` (not truthy) so subject and metadata agree on edge
    # values like ``slice_id == ""``: metadata at 15349 also uses ``is
    # not None`` (#2811 round 3 item 1). In practice ``slice_id`` is
    # validated to ``slice-<N>`` upstream, so the asymmetry can't fire
    # today — keeping the two checks aligned avoids a future footgun.
    subject_slice = f" slice={slice_id}" if slice_id is not None else ""
    subject = f"producer-permanent-death: {role} exit={exit_code}{subject_slice} [high]"
    slice_render = f" (slice {slice_id})" if slice_id is not None else ""
    body = (
        f"Producer '{role}'{slice_render} died permanently in phase "
        f"'{phase_value}': container exited with code {exit_code} after the "
        f"consensus-wrapper exhausted its retry budget.\n\n"
        "The slice/pipeline state machine cannot replace a permanently "
        "dead producer, so the pipeline is being transitioned to FAILED "
        "(Option A, issue #2806). The agent's committed work — if any — "
        "is still on the per-role branch; use `restart_phase` to resume "
        "from the prior known-good state, or `cancel_task` to abort."
    )
    metadata: dict[str, Any] = {
        "anomaly_type": "producer-permanent-death",
        "phase": phase_value,
        "role": role,
        "exit_code": exit_code,
        "priority": "high",
    }
    if slice_id is not None:
        metadata["slice_id"] = slice_id

    try:
        try:
            from message_store import Message, MessageType
        except ImportError:
            from ..message_store import (  # type: ignore[no-redef]
                Message,
                MessageType,
            )
        store_fn = _pkg._get_message_store()
        if store_fn is None:
            _pkg.logger.warning(
                "Producer-death alert: message store unavailable",
                pipeline_id=pipeline_id,
                role=role,
            )
            return
        msg_store = store_fn()
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject=subject,
                body=body,
                metadata=metadata,
                phase=phase_value,
            )
        )
    except Exception as e:
        _pkg.logger.warning(
            "Failed to publish producer-permanent-death OVERSEER_ALERT",
            pipeline_id=pipeline_id,
            role=role,
            error=str(e),
            exc_info=True,
        )


def detect_branch_divergence(snapshot: Any) -> Any | None:
    """Calibration detector for the ``branch_divergence`` corpus rows (#2222/#2224).

    Keys on the git-history signal in ``snapshot.git_state`` rather than the
    brittle PR-subject regex: the branch is genuinely diverged only when it is
    **neither** an ancestor of base **nor** patch-id-equivalent to the merged
    commit. A branch that is an ancestor of base, or whose patch-id matches the
    merged commit, is NOT diverged — even if its PR-style subject would have
    tripped the old regex. Deterministic and cheap → ``requires_adjudication=
    False``.
    """
    from health_checks.types import Finding, FindingClass, Severity

    git_state = getattr(snapshot, "git_state", {}) or {}
    if not isinstance(git_state, dict):
        return None

    is_ancestor = bool(git_state.get("is_ancestor_of_base"))
    patch_id_matches = bool(git_state.get("patch_id_matches"))
    # An ancestor-of-base branch (or a patch-id match against the merged commit)
    # is fully accounted for in main — not divergence.
    if is_ancestor or patch_id_matches:
        return None

    return Finding(
        finding_class=FindingClass.BRANCH_DIVERGENCE,
        severity=Severity.MEDIUM,
        evidence={
            "branch": git_state.get("branch"),
            "is_ancestor_of_base": is_ancestor,
            "patch_id_matches": patch_id_matches,
            "pr_subject_divergence": bool(git_state.get("pr_subject_divergence")),
        },
        recommended_action=(
            "Pipeline branch is neither an ancestor of base nor patch-id-"
            "equivalent to the merged commit — it has genuinely diverged "
            "(see #2222 recovery: rebase --onto the correct base)."
        ),
        requires_adjudication=False,
        detector_key="branch_divergence",
    )


def _check_branch_divergence_for_alert(
    pipeline_id: str,
    worktree_repo_path: Path,
    pipeline_branch: str,
    base_branch: str,
    threshold: int = _pkg.BRANCH_DIVERGENCE_THRESHOLD,
    scan_cap: int = _pkg._BRANCH_DIVERGENCE_SCAN_CAP,
) -> tuple[int, list[tuple[str, str]]]:
    """Return ``(ahead_count, offenders)``.

    ``offenders`` is the list of ahead-commits that are **reabsorbed merged-main
    commits** — an ahead-commit whose patch-id matches a commit already present
    in ``origin/<base>`` (within the capped scan window) — when the pipeline
    branch is more than ``threshold`` commits ahead of base.  This replaces the
    old ``(#NNNN)`` subject regex with a patch-id signal that neither
    false-positives on legitimate PR references nor false-negatives on rewritten
    subjects.  Returns ``(ahead, [])`` when the branch is not far enough ahead,
    nothing reabsorbed matches, or any git invocation fails (best-effort —
    observability must never block the pipeline).
    """
    if not pipeline_branch or not base_branch or pipeline_branch == base_branch:
        return 0, []

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [*git_base, *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _pkg.logger.debug(
                "branch-divergence: git command failed",
                pipeline_id=pipeline_id,
                git_args=args,
                error=str(exc),
            )
            return None

    def _patch_id_to_sha(rev_range: str) -> dict[str, str]:
        """Map ``patch_id -> sha`` for up to ``scan_cap`` commits in ``rev_range``.

        Runs ``git log -p | git patch-id --stable``. Best-effort: any failure
        yields an empty map (the caller degrades to "no offenders").
        """
        log_p = _run(
            [
                "log",
                "-p",
                "--no-merges",
                f"--max-count={scan_cap}",
                rev_range,
            ]
        )
        if log_p is None or log_p.returncode != 0 or not log_p.stdout:
            return {}
        try:
            pid = subprocess.run(
                [*git_base, "patch-id", "--stable"],
                input=log_p.stdout,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired, OSError:
            return {}
        if pid.returncode != 0:
            return {}
        mapping: dict[str, str] = {}
        for line in (pid.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                mapping[parts[0]] = parts[1]
        return mapping

    count = _run(
        [
            "rev-list",
            "--count",
            f"origin/{base_branch}..origin/{pipeline_branch}",
        ]
    )
    if count is None or count.returncode != 0:
        return 0, []
    try:
        ahead = int((count.stdout or "0").strip() or "0")
    except ValueError:
        return 0, []
    if ahead <= threshold:
        return ahead, []

    # Patch-ids present in recent base history — the set an ahead-commit must
    # collide with to count as a reabsorbed merged-main commit.
    base_patch_ids = set(_patch_id_to_sha(f"origin/{base_branch}").keys())
    if not base_patch_ids:
        return ahead, []
    ahead_sha_by_patch_id = _patch_id_to_sha(f"origin/{base_branch}..origin/{pipeline_branch}")
    contaminated_shas = {sha for pid, sha in ahead_sha_by_patch_id.items() if pid in base_patch_ids}
    if not contaminated_shas:
        return ahead, []

    # Re-read subjects (capped, ordered newest-first) for the alert body.
    log = _run(
        [
            "log",
            "--no-merges",
            "--pretty=format:%H%x09%s",
            f"--max-count={scan_cap}",
            f"origin/{base_branch}..origin/{pipeline_branch}",
        ]
    )
    if log is None or log.returncode != 0:
        return ahead, []

    offenders: list[tuple[str, str]] = []
    for line in (log.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, subject = line.partition("\t")
        if not sha:
            continue
        if sha in contaminated_shas:
            offenders.append((sha, subject or "(no subject)"))
    return ahead, offenders


def _publish_branch_divergence_alert(
    pipeline: Pipeline,
    pipeline_id: str,
    *,
    pipeline_branch: str,
    base_branch: str,
    ahead_count: int,
    offenders: list[tuple[str, str]],
) -> None:
    """Publish an ``OVERSEER_ALERT`` for branch-divergence contamination.

    Best-effort: import or write failures are logged at WARNING and
    swallowed — the orchestrator log is the always-on fallback.
    """
    phase_value = (
        pipeline.current_phase.value
        if hasattr(pipeline.current_phase, "value")
        else str(pipeline.current_phase)
    )
    subject = f"branch-divergence: {pipeline_branch} contains merged-main commits"
    offender_render = "\n".join(f"  {sha[:12]} {subj}" for sha, subj in offenders[:10])
    if len(offenders) > 10:
        offender_render += f"\n  ... and {len(offenders) - 10} more"
    body = (
        f"Pipeline branch ``origin/{pipeline_branch}`` is {ahead_count} commits "
        f"ahead of ``origin/{base_branch}`` and contains {len(offenders)} "
        f"commit(s) whose **patch-id matches a commit already merged into "
        f"base** — i.e. reabsorbed merged-main commits.  This is the "
        f"contamination shape investigated in #2222 (Phase 4 / #2224 "
        f"detector; #2270 §2 patch-id calibration).\n\n"
        f"Offending commits:\n{offender_render}\n\n"
        f"If this is real contamination, the resulting PR will show a "
        f"borked diff against current main — see #2222 recovery procedure "
        f"(rebase ``--onto`` the right base)."
    )
    metadata: dict[str, Any] = {
        "anomaly_type": "branch-divergence",
        "phase": phase_value,
        "pipeline_branch": pipeline_branch,
        "base_branch": base_branch,
        "ahead_count": ahead_count,
        "offending_shas": [sha for sha, _ in offenders],
    }

    try:
        try:
            from message_store import Message, MessageType
        except ImportError:
            from ..message_store import (  # type: ignore[no-redef]
                Message,
                MessageType,
            )
        store_fn = _pkg._get_message_store()
        if store_fn is None:
            _pkg.logger.warning(
                "Branch-divergence alert: message store unavailable",
                pipeline_id=pipeline_id,
            )
            return
        msg_store = store_fn()
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.OVERSEER_ALERT,
                subject=subject,
                body=body,
                metadata=metadata,
                phase=phase_value,
            )
        )
    except Exception as e:
        _pkg.logger.warning(
            "Failed to publish branch-divergence OVERSEER_ALERT",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )


def _branch_divergence_tick(
    pipeline_id: str,
    worktree_repo_path: Path,
    store: StateStore,
    alerted_shas: set[str],
) -> None:
    """One iteration of the branch-divergence detector.

    Extracted from the ``_health_monitor_poll`` closure so the
    dedupe + reset behavior is unit-testable.  Mutates ``alerted_shas``
    in place: adds newly-fired SHAs, and clears the set when the
    contamination window goes empty so re-introduction (same SHA,
    e.g. agent re-runs a bad rebase) re-fires per the issue's
    "rather over-alert than miss" stance.

    All errors are logged-and-swallowed — observability must never
    block the pipeline.
    """
    try:
        pipeline = store.load_pipeline(pipeline_id)
        branch = pipeline.branch
        base = pipeline.base_branch
        if not branch or not base:
            return
        ahead, offenders = _pkg._check_branch_divergence_for_alert(
            pipeline_id=pipeline_id,
            worktree_repo_path=worktree_repo_path,
            pipeline_branch=branch,
            base_branch=base,
        )
        if not offenders and alerted_shas:
            # Note: transient git errors in ``_check_branch_divergence_for_alert``
            # also surface as ``offenders == []`` and therefore flush the dedupe
            # set; this is intentional per #2224's "rather over-alert than miss"
            # posture — a flaky git tick will re-fire on the next clean tick.
            alerted_shas.clear()
        new_offenders = [(sha, subj) for sha, subj in offenders if sha not in alerted_shas]
        if new_offenders:
            _pkg._publish_branch_divergence_alert(
                pipeline,
                pipeline_id,
                pipeline_branch=branch,
                base_branch=base,
                ahead_count=ahead,
                offenders=new_offenders,
            )
            alerted_shas.update(sha for sha, _ in new_offenders)
    except Exception as div_err:
        _pkg.logger.debug(
            "Branch-divergence check failed",
            pipeline_id=pipeline_id,
            error=str(div_err),
        )


def _handle_brc_consensus_timeout(
    pipeline: Pipeline,
    pipeline_id: str,
    consensus_timeout: float,
    blocking_agents: list[str],
    store: StateStore,  # noqa: ARG001 — kept for call-site compatibility (#2264)
    slice_id: str | None = None,
    active_role_names: list[str] | None = None,
) -> None:
    # Extracted from _run_concurrent_phase so k3s-style top-level-module
    # layouts (and tests) can exercise this path in isolation — issue #1783.
    # ``slice_id`` is propagated so per-slice trackers (#2137) are looked
    # up under the nested ``{pipeline_id}/{slice_id}`` key.
    #
    # Issue #2264: the auto-``choice`` HITL decision this used to open
    # was the wrong protocol shape — the platform should not gate the
    # pipeline on a binary choice when the operator already has the
    # levers (`cancel_task`, `restart_phase`, `provide_input`).  The
    # two former decision paths now publish ``OVERSEER_ALERT`` messages
    # so the SDLC skill's existing alert flow surfaces them as
    # notifications rather than a blocking decision.
    _brc_handled = False
    _brc_timeout_result: dict | None = None
    _brc_tracker = None
    try:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            from ..peer_consensus import (
                get_peer_consensus_tracker,  # type: ignore[no-redef]
            )

        _brc_tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
        if _brc_tracker is not None:
            _brc_timeout_result = _brc_tracker.handle_timeout()
            _brc_handled = _brc_tracker.is_timeout_handled()
            _pkg.logger.info(
                "BRC timeout handler result",
                pipeline_id=pipeline_id,
                action=(_brc_timeout_result.get("action") if _brc_timeout_result else None),
                brc_handled=_brc_handled,
            )
    except Exception as e:
        _pkg.logger.warning(
            "BRC timeout check failed, falling back to OVERSEER_ALERT",
            pipeline_id=pipeline_id,
            error=str(e),
        )

    latest_proposal_at: datetime | None = None
    if _brc_tracker is not None:
        try:
            latest_proposal_at = _brc_tracker.get_latest_proposal_timestamp()
        except Exception as e:
            _pkg.logger.warning(
                "Consensus-timeout alert proposal lookup failed",
                pipeline_id=pipeline_id,
                error=str(e),
                exc_info=True,
            )
    latest_heartbeat_at = _latest_active_role_heartbeat(active_role_names or [])

    if (
        _brc_handled
        and _brc_timeout_result is not None
        and _brc_timeout_result.get("action") == "escalate"
    ):
        # Narrow the alert's blocking_agents to the *critical* blockers
        # the tracker just escalated on. The caller-supplied
        # ``blocking_agents`` is the full unconfirmed-roles set
        # (advisory + critical) from ``evaluate()`` — surfacing
        # advisory roles on a high-priority alert dilutes the signal.
        critical_entries = _brc_timeout_result.get("critical_blockers") or []
        critical_role_names: list[str] = []
        for entry in critical_entries:
            for role in (entry.get("reviewer_role"), entry.get("producer_role")):
                if role and role not in critical_role_names:
                    critical_role_names.append(role)
        escalate_blocking = critical_role_names or blocking_agents
        _publish_consensus_timeout_alert(
            pipeline,
            pipeline_id,
            consensus_timeout,
            escalate_blocking,
            priority="high",
            latest_proposal_at=latest_proposal_at,
            latest_heartbeat_at=latest_heartbeat_at,
            slice_id=slice_id,
        )
    elif not _brc_handled:
        if _pkg._emit_event is not None:
            _pkg._emit_event(
                EventType.CONSENSUS_TIMEOUT,
                pipeline_id,
                data={
                    "timeout_minutes": consensus_timeout / 60,
                    "blocking_agents": blocking_agents,
                },
            )
        _publish_consensus_timeout_alert(
            pipeline,
            pipeline_id,
            consensus_timeout,
            blocking_agents,
            priority="medium",
            latest_proposal_at=latest_proposal_at,
            latest_heartbeat_at=latest_heartbeat_at,
            slice_id=slice_id,
        )
