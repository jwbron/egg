"""implement-phase slice loop, extracted verbatim from the pipelines barrel; barrel-resident/test-patched globals via ``_pkg`` (#3312 slice-4)."""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _run_implement_phase_slices(
    pipeline_id: str,
    pipeline: _pkg.Pipeline,
    spawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store,
    certs_volume: str | None,
    worktree_repo_path: _pkg.Path,
    run_epoch: _pkg.datetime | None = None,
) -> tuple[int, str]:
    """Drive the implement phase as a DAG of independent slices (#2137).

    For each wave produced by :class:`SliceScheduler`, spawns a fresh
    BRC team per slice and waits for that slice's consensus before
    advancing the scheduler. Each slice runs through the existing
    :func:`_run_concurrent_phase` machinery with a slice-scoped tracker
    namespace (``{pipeline_id}/{slice_id}``) and slice-scoped per-role
    branches (``egg/issue-N/{slice_id}/{role}/work``).

    Per-slice PRs are opened via ``GatewayClient.create_slice_pr`` after
    each slice reaches CONSENSUS_CONFIRMED — root slices target the
    pipeline branch; child slices target their parent slice's
    integration branch. The stacked-PR reconciler runs in parallel as a
    daemon thread for the lifetime of this call.

    Returns ``(exit_code, logs)`` where ``exit_code == 0`` means every
    slice reached CONFIRMED; non-zero means at least one slice failed.
    """
    try:
        from orchestrator.slice_scheduler import SliceScheduler
    except ImportError:
        from slice_scheduler import SliceScheduler

    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError as exc:
        _pkg.logger.error(
            "Slice loop: egg_contracts.loader unavailable — falling back",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return 1, "slice loop bootstrap failed"

    contract = load_contract(pipeline_id, worktree_repo_path)
    slices = list(getattr(contract, "slices", []) or [])
    if not slices:
        _pkg.logger.warning(
            "Slice loop: contract has no slices, falling back to monolithic implement",
            pipeline_id=pipeline_id,
        )
        return 1, "no slices in contract"

    pipeline_branch = pipeline.branch or (
        f"egg/issue-{pipeline.issue_number}/work"
        if pipeline.issue_number is not None
        else f"egg/{pipeline_id}/work"
    )
    issue_number = pipeline.issue_number
    # Slice integration branches stack as siblings of the pipeline tip
    # under ``egg/<id>/`` (see :func:`_ensure_pipeline_work_ref` for the
    # ``/work`` namespace decision in #2399). The namespace root drops the
    # trailing ``/work`` so slice paths build to ``<root>/slice-M`` rather
    # than ``<root>/work/slice-M``. The qualifier suffix (``-v3``,
    # ``-backend``) is preserved through ``pipeline.branch`` so two
    # qualified pipelines for the same issue do not collide on
    # ``egg/issue-N/slice-M`` (#2368).
    issue_branch = _pkg._slice_namespace_root(pipeline_branch)

    # Wrap scheduler construction so the run loop doesn't crash if the
    # contract bypassed plan-ingestion validation and reaches the
    # scheduler with a multi-parent / cyclic forest. ``SliceScheduler``
    # raises ``ValueError`` with the structured forest errors; surface
    # them to the operator via the existing return path so the run
    # loop can route to HITL escalation rather than wedge the pipeline.
    # Wire the slice.closed emitter (issue #3364): the scheduler invokes this
    # OUTSIDE its lock from record_complete / record_failure, so a real slice
    # close publishes an allowlisted ``slice.closed`` event to the bus that a
    # long-haul monitor threads on. Guarded on the optional event-bus handle
    # and no-ops when it's unavailable — mirroring the CONSENSUS_TIMEOUT /
    # PIPELINE_FAILED emit sites in _alerts.py / _run_pipeline.py. The
    # ``outcome`` (``complete`` | ``failed``) distinguishes success from
    # failure so a consumer needs no second lookup.
    def _emit_slice_closed(slice_id: str, outcome: str) -> None:
        if _pkg._emit_event is None:
            return
        _pkg._emit_event(
            _pkg.EventType.SLICE_CLOSED,
            pipeline_id,
            data={"slice_id": slice_id, "outcome": outcome},
        )

    try:
        scheduler = SliceScheduler(
            contract,
            max_parallel_slices=pipeline.config.max_parallel_slices,
            slice_closed_emitter=_emit_slice_closed,
        )
    except ValueError as exc:
        _pkg.logger.error(
            "Slice loop: scheduler refused to start (forest validation failed)",
            pipeline_id=pipeline_id,
            error=str(exc),
        )
        return 1, f"slice scheduler validation failed: {exc}"

    # Defensive idempotent context-PR opener (#2777 cq-4). The
    # canonical advance_phase REST path enforces hard-required, but
    # the runner-driven entries (auto-advance, implement-entry,
    # HITL-resume, this slice-loop entry) must also fire it to avoid
    # silent strands on ``egg/<id>/work``. Soft-fail on transient
    # gateway errors here — the canonical site already enforces the
    # 422 contract.
    try:
        # Pass the main repo path (``store.repo_path``) — not
        # ``worktree_repo_path`` — so all four opener call sites of
        # ``_open_context_pr_at_implement_start`` read identically.
        # The opener rederives its own per-pipeline worktree internally
        # via ``resolve_worktree_path(pipeline_id, store.repo_path)``.
        _pkg._open_context_pr_at_implement_start(pipeline_id, repo_path=_pkg.Path(store.repo_path))
    except _pkg.ContextPrCreationError as ctx_err:
        _pkg.logger.warning(
            "Context PR opener: slice-loop entry safety net failed "
            "(continuing — hard-require enforced at advance_phase and "
            "the implement-start plan pre-flight gate) (#2777, #3100)",
            pipeline_id=pipeline_id,
            reason=ctx_err.reason,
            error=str(ctx_err),
        )
    except Exception as safety_err:  # noqa: BLE001
        # Defence in depth: import / lookup failures must not strand
        # the slice loop.
        _pkg.logger.warning(
            "Context PR opener: slice-loop entry safety net outer "
            "wrapper raised (continuing) (#2777)",
            pipeline_id=pipeline_id,
            error=str(safety_err),
        )

    _contract_loader = _pkg.functools.partial(
        _pkg._contract_loader_impl,
        pipeline_id=pipeline_id,
        worktree_repo_path=worktree_repo_path,
    )

    # Stacked-PR reconciler starts after the bootstrap pass below so
    # an unhandled bootstrap exception cannot leak its daemon thread
    # (the ``finally`` at the bottom of the run loop owns teardown).
    aggregate_logs: list[str] = []
    overall_exit = 0
    poll_interval = 5.0

    from egg_contracts.models import SliceStatus

    try:
        from orchestrator import global_slice_admit
    except ImportError:
        import global_slice_admit  # type: ignore[no-redef]
    try:
        from orchestrator.peer_consensus import remove_peer_consensus_tracker
    except ImportError:
        from peer_consensus import remove_peer_consensus_tracker  # type: ignore[no-redef]
    try:
        from orchestrator.state_store import get_pipeline_state_lock
    except ImportError:
        from state_store import get_pipeline_state_lock  # type: ignore[no-redef]

    _commit_and_push_slice_statefiles = _pkg.functools.partial(
        _pkg._commit_and_push_slice_statefiles_impl,
        pipeline_id=pipeline_id,
        worktree_repo_path=worktree_repo_path,
        pipeline=pipeline,
        store=store,
        spawner=spawner,
        gateway_mode=gateway_mode,
        issue_number=issue_number,
    )
    _persist_slice_status_complete = _pkg.functools.partial(
        _pkg._persist_slice_status_complete_impl,
        pipeline_id=pipeline_id,
        worktree_repo_path=worktree_repo_path,
        commit_and_push=_commit_and_push_slice_statefiles,
    )

    # Bootstrap reconciliation pass (#2549). Before the run loop ticks,
    # fold in two sources of "this slice is already done" state that
    # the scheduler (a pure rebuild from ``contract.slices``) cannot
    # see on its own:
    #
    # (A) Slices the contract already records as
    #     ``SliceStatus.COMPLETE`` — trusted directly, no I/O.
    # (B) Slices whose integration branch on origin is reachable from
    #     their parent's tip (PR merged). On a hit, also persist (A)
    #     so subsequent restarts skip the GitHub round-trip.
    #
    # Without this pass the scheduler would re-yield merged slices as
    # READY and ``create_slice_integration_branch`` would
    # non-fast-forward-reject. Best-effort: failure falls through to
    # the run loop.
    bootstrap_complete: list[str] = []
    bootstrap_merged: list[str] = []

    # Layer (A): cheap, no I/O. Trust contract-recorded COMPLETE status —
    # but verify the recorded COMPLETE is not itself a #3214 false-complete
    # (an interior forest node persisted COMPLETE with pending tasks, no
    # PR, no merge). Blindly trusting a corrupt contract here is how the
    # false-complete propagated into the scheduler and wedged the chain.
    # On an invalid record, alert and decline to trust it — route the
    # slice through Layer-B/C so it is re-evaluated and (re-)run rather
    # than silently skipped.
    #
    # Note: a COMPLETE slice that recorded *no* durable evidence (no
    # pr_number, no integration_base_sha — e.g. a legacy pre-#2871
    # contract from before integration_base_sha existed) is distrusted
    # here on every restart, even when it was genuinely merged. That is
    # intentional, not a bug: such a slice falls through to Layer-B,
    # where origin-side merge detection re-confirms it and re-marks it
    # COMPLETE. The outcome stays correct; the only cost is one extra
    # GitHub round-trip per restart. A slice that forked under current
    # code *usually* records integration_base_sha and is trusted here
    # directly — but that write is best-effort (the get_remote_branch_sha
    # call at slice spawn swallows failures and degrades to ancestor-only
    # detection), so a current-code slice whose base-SHA write failed also
    # falls through to Layer-B and self-corrects identically to the legacy
    # case above.
    #
    # Known limitation (#3253): a slice that pre-fix code *already* persisted
    # COMPLETE basis="merged" with a stale ``integration_base_sha`` and no
    # produced commits / PR is still trusted here — Layer-A validates with no
    # ``basis`` (the #3253 merged-empty guard keys on ``basis == "merged"``,
    # which Layer-A never supplies), so the ``forked`` free-pass below accepts
    # the stale fork base. This is deliberately *not* fixed by broadening the
    # guard to the basis-less path: a legitimate ``basis="consensus_complete"``
    # slice can also have no PR and no recorded task commit (best-effort agent
    # recording + ``pr_number`` None on an unparseable PR URL, #3122), so
    # re-running on "no commit + no PR" alone here would re-run genuinely
    # completed work. The #3253 fix prevents the corrupt write going forward;
    # a pipeline already wedged by this exact bug *before* the upgrade needs a
    # manual contract touch-up (clear the slice's COMPLETE status) rather than
    # self-healing on restart.
    layer_b_candidates = []
    for s in slices:
        if s.status == SliceStatus.COMPLETE:
            invalid = _pkg._validate_slice_completion_basis(s, pr_number=s.pr_number)
            if invalid is not None:
                _pkg.logger.error(
                    "Contract records slice COMPLETE but the completion basis is "
                    "invalid — NOT trusting it; re-evaluating the slice (#3214)",
                    pipeline_id=pipeline_id,
                    slice_id=s.id,
                    reason=invalid,
                )
                layer_b_candidates.append(s)
                continue
            scheduler.record_complete(s.id)
            bootstrap_complete.append(s.id)
            continue
        layer_b_candidates.append(s)

    # Layer (B): origin-side detection for slices not yet recorded as
    # COMPLETE on the contract. Each helper call uses its own synthetic
    # gateway session, so we parallelise across slices to keep startup
    # latency bounded as forests grow. Cap workers so a large forest
    # doesn't burst against the gateway.
    if pipeline.repo and layer_b_candidates:

        def _bootstrap_check_one(slice_obj: _pkg.Any) -> tuple[str, bool]:
            # Prefer the parent branch the slice was actually forked
            # off of (recorded by ``_run_one_slice_inner``). Falls back
            # to the dependency-derived parent for slices that never
            # made it through ``_run_one_slice_inner`` (e.g. fresh
            # contract on first run). Both should agree today, but a
            # future re-plan that mutates ``dependencies`` post-creation
            # would diverge — preferring the recorded value future-
            # proofs the check.
            if slice_obj.parent_branch_at_creation:
                parent_branch_for_check = slice_obj.parent_branch_at_creation
            elif slice_obj.dependencies:
                parent_branch_for_check = f"{issue_branch}/{slice_obj.dependencies[0]}"
            else:
                parent_branch_for_check = pipeline_branch
            integration_branch_for_check = f"{issue_branch}/{slice_obj.id}"
            try:
                merged = spawner.gateway.is_slice_branch_merged_into_parent(
                    pipeline_id,
                    str(worktree_repo_path),
                    integration_branch=integration_branch_for_check,
                    parent_branch=parent_branch_for_check,
                    # #2871 — pass the recorded fork base so an empty
                    # (un-started) slice branch whose tip is still at its
                    # creation base is not mistaken for merged work.
                    integration_base_sha=slice_obj.integration_base_sha,
                    # Read-only ancestry check run by the orchestrator's
                    # slice-loop scheduler; attribute to the orchestrator
                    # in the audit log, not a phantom coder (#2919).
                    agent_role="orchestrator",
                    mode=gateway_mode,  # type: ignore[arg-type]
                )
            except Exception as detect_err:  # noqa: BLE001
                # Gateway `is_slice_branch_merged_into_parent` call.
                # Catches gateway HTTP/timeout errors (GatewayError),
                # low-level socket / DNS errors (OSError), and any
                # rare argument-shape errors. Default to "not merged"
                # so the slice can still spawn fresh.
                _pkg.logger.warning(
                    "Bootstrap merged-detection raised; treating slice as not-merged",
                    pipeline_id=pipeline_id,
                    slice_id=slice_obj.id,
                    error=str(detect_err),
                )
                return slice_obj.id, False
            # #3253 — guard against a false-positive merged result. A slice
            # whose producers never committed (no produced task commit) and
            # that has no slice PR has an empty integration branch: its tip
            # is still the fork base, so it is trivially an ancestor of an
            # advanced parent and the origin ancestry check reports it
            # merged. Marking it COMPLETE basis=merged silently drops the
            # slice and lets the pipeline complete with its work missing —
            # the restart-to-retry failure mode (#3138 producer exhaustion →
            # operator restart → false-complete). Override to not-merged so
            # the slice falls through to Layer-C and re-runs. A genuine merge
            # has produced commits or a recorded PR, so this never overrides
            # a real merge.
            if (
                merged
                and not _pkg._slice_produced_commits(slice_obj)
                and getattr(slice_obj, "pr_number", None) is None
            ):
                _pkg.logger.warning(
                    "Bootstrap merged-detection overridden: origin ancestry "
                    "reports merged but the slice has no produced task commit "
                    "and no PR — empty/un-started branch, re-running rather than "
                    "false-completing as merged (#3253)",
                    pipeline_id=pipeline_id,
                    slice_id=slice_obj.id,
                )
                return slice_obj.id, False
            return slice_obj.id, bool(merged)

        max_workers = min(len(layer_b_candidates), 8)
        with _pkg.concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"slice-bootstrap-{pipeline_id}",
        ) as bootstrap_pool:
            results = list(bootstrap_pool.map(_bootstrap_check_one, layer_b_candidates))

        for slice_id, already_merged in results:
            if already_merged:
                scheduler.record_complete(slice_id)
                _persist_slice_status_complete(slice_id, basis="merged", commit_to_branch=False)
                bootstrap_merged.append(slice_id)

    # Layer (C): non-COMPLETE slice classification (slice-4 TASK-4-4).
    # After layers A (contract-recorded COMPLETE) and B (merged on
    # origin), classify the remaining slices per the 5-way matrix
    # so crash recovery does not respawn agents for a slice that is
    # already running, silently advance a slice whose HITL is still
    # pending, or treat a corrupt status enum as a benign default:
    #
    #   (1) IN_PROGRESS, no commits on integration branch → no Layer-C
    #       action; the scheduler will re-yield the slice as READY and
    #       the run loop spawns fresh agents.
    #   (2) IN_PROGRESS, commits on integration branch, consensus
    #       NOT reached → call ``scheduler.mark_spawned`` so the run
    #       loop does NOT respawn. Per-slice tracker reconstruction
    #       is handled at orchestrator boot by
    #       startup_reconciliation.py (slice-4 TASK-4-5); the
    #       producer pods (if alive) or the lazy spawn-on-need path
    #       carry the slice forward.
    #   (3) IN_PROGRESS, commits on integration branch, consensus
    #       REACHED, slice PR NOT opened → mark COMPLETE so the
    #       slice-PR opener path (with TASK-3-2 idempotency
    #       pre-flight) fires on the next loop iteration; do not
    #       respawn agents.
    #   (4) BLOCKED (HITL pending) → preserve the BLOCKED status.
    #       Verify the HITL decision is still on the contract; if
    #       not, surface an OVERSEER_ALERT so a human investigates.
    #   (5) Unknown / corrupt state (impossible status enum value)
    #       → surface an OVERSEER_ALERT instead of silently
    #       re-yielding as READY.
    bootstrap_resumed: list[str] = []
    bootstrap_consensus_complete: list[str] = []
    bootstrap_blocked: list[str] = []
    bootstrap_corrupt: list[str] = []
    bootstrap_reclassified_fresh: list[str] = []  # resume-but-dead → fresh (#2914)
    layer_b_marked_complete = set(bootstrap_merged)
    for s in layer_b_candidates:
        if s.id in layer_b_marked_complete:
            continue
        classification = _pkg._classify_non_complete_slice(
            pipeline_id=pipeline_id,
            slice_obj=s,
            issue_branch=issue_branch,
            pipeline_repo=pipeline.repo,
            worktree_repo_path=worktree_repo_path,
            gateway=spawner.gateway,
            gateway_mode=gateway_mode,
            consensus_tracker_lookup=_pkg._lookup_peer_consensus_tracker_or_none,
        )
        if classification == "consensus_complete":
            # Case 3 — louder than fresh-spawn but quieter than
            # case-4/5 HITL. A warning here makes the non-trivial
            # recovery (consensus reached pre-crash, PR not opened)
            # auditable in operator logs without paging anyone
            # (reviewer_code v1 non-blocking).
            _pkg.logger.warning(
                "Layer-C case 3 — slice consensus reached pre-restart but "
                "slice PR was never opened; marking COMPLETE so the next "
                "loop iteration runs the slice-PR opener (slice-4 TASK-4-4)",
                pipeline_id=pipeline_id,
                slice_id=s.id,
            )
            scheduler.record_complete(s.id)
            _persist_slice_status_complete(s.id, basis="consensus_complete", commit_to_branch=False)
            bootstrap_consensus_complete.append(s.id)
            continue
        if classification == "resume":
            # Verify agents are actually live before marking as spawned (#2914).
            # On restart_phase, agents were torn down but contract still shows
            # IN_PROGRESS with commits — we must not mark_spawned when cohort
            # is absent, or the pipeline wedges with no agents running.
            if _pkg._slice_agents_alive(spawner, pipeline_id, s.id):
                scheduler.mark_spawned(s.id)
                bootstrap_resumed.append(s.id)
            else:
                _pkg.logger.warning(
                    "Layer-C resume classification but no live agents; "
                    "treating as fresh to force re-spawn (#2914)",
                    pipeline_id=pipeline_id,
                    slice_id=s.id,
                )
                bootstrap_reclassified_fresh.append(s.id)
            continue
        if classification == "blocked":
            bootstrap_blocked.append(s.id)
            continue
        if classification == "corrupt":
            bootstrap_corrupt.append(s.id)
            continue
        # "fresh" → no Layer-C action, scheduler re-yields READY.

    # The bootstrap passes above persist with ``commit_to_branch=False``
    # — one batched commit+push here covers every reconciled slice
    # (Layer B merged-detection + Layer-C case 3) instead of a commit
    # per slice (#3117).
    if bootstrap_merged or bootstrap_consensus_complete:
        _commit_and_push_slice_statefiles(
            "Persist slice completion statuses after bootstrap reconciliation (#3117)"
        )

    if bootstrap_complete or bootstrap_merged:
        _pkg.logger.info(
            "Slice bootstrap reconciliation marked slices complete",
            pipeline_id=pipeline_id,
            already_complete_on_contract=bootstrap_complete,
            detected_merged_on_origin=bootstrap_merged,
        )
    if (
        bootstrap_resumed
        or bootstrap_consensus_complete
        or bootstrap_blocked
        or bootstrap_corrupt
        or bootstrap_reclassified_fresh
    ):
        # NOTE: include ``bootstrap_blocked`` in the gate (reviewer_code
        # v3 NACK fix) — a bootstrap pass whose only Layer-C activity is
        # BLOCKED slices was previously suppressing the audit-trail line
        # entirely. Case-4 escalation still fires, but operators need
        # the structured "we saw a blocked slice" log to spot
        # pending-HITL backlogs without grepping for the side-effect.
        #
        # Also include ``bootstrap_reclassified_fresh`` (#2914) — resume-
        # classified slices that were re-verified against k8s and found
        # to have no live agents. Surfacing the reclassification here
        # gives operators a structured audit trail for the
        # ``restart_phase``-recovery path.
        _pkg.logger.info(
            "Slice bootstrap reconciliation classified non-COMPLETE slices (slice-4 TASK-4-4)",
            pipeline_id=pipeline_id,
            resumed=bootstrap_resumed,
            consensus_complete_unrecorded=bootstrap_consensus_complete,
            blocked=bootstrap_blocked,
            corrupt=bootstrap_corrupt,
            reclassified_fresh=bootstrap_reclassified_fresh,
        )
    # Case 5 — escalate via HITL so the pipeline pauses until the
    # operator picks an option (reviewer_contract / reviewer_code v1
    # blocker). OVERSEER_ALERT alone is too weak — it surfaces but
    # does not gate progress. The Decision lands on the contract via
    # ``_escalate_corrupt_slice_to_hitl`` so ``/sdlc`` reads it on
    # the next poll.
    _current_phase = getattr(pipeline, "current_phase", None)
    for _corrupt_slice_id in bootstrap_corrupt:
        try:
            _pkg._escalate_corrupt_slice_to_hitl(
                pipeline_id=pipeline_id,
                slice_id=_corrupt_slice_id,
                worktree_repo_path=worktree_repo_path,
                current_phase=_current_phase,
            )
        except Exception as escalate_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Failed to escalate corrupt-state slice to HITL during "
                "bootstrap (slice-4 TASK-4-4 case 5)",
                pipeline_id=pipeline_id,
                slice_id=_corrupt_slice_id,
                error=str(escalate_err),
            )
    # Case 4 — symmetric HITL escalation for BLOCKED-without-HITL.
    for _blocked_slice_id, _escalate_reason in [
        (sid, "no pending HITL decision found on contract")
        for sid in bootstrap_blocked
        if not _pkg._slice_has_pending_decision(sid, getattr(contract, "decisions", None) or [])
    ]:
        try:
            _pkg._escalate_blocked_slice_to_hitl(
                pipeline_id=pipeline_id,
                slice_id=_blocked_slice_id,
                reason=_escalate_reason,
                worktree_repo_path=worktree_repo_path,
                current_phase=_current_phase,
            )
        except Exception as escalate_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Failed to escalate blocked-without-HITL slice to HITL "
                "during bootstrap (slice-4 TASK-4-4 case 4)",
                pipeline_id=pipeline_id,
                slice_id=_blocked_slice_id,
                error=str(escalate_err),
            )

    reconciler_thread, reconciler_stop = _pkg._start_stacked_pr_reconciler(
        pipeline_id,
        _contract_loader,
        spawner.gateway,
        pipeline,
        worktree_repo_path=worktree_repo_path,
        repo=getattr(pipeline, "repo", None),
    )

    try:
        while not scheduler.all_done():
            # 1. Snapshot ready slices for this tick.
            ready_batch = list(scheduler.iter_ready())
            if not ready_batch:
                # 2. Drain cascades whose grace window expired so the
                #    descendants are visibly BLOCKED in the runtime view
                #    and we don't busy-spin.
                events = scheduler.poll_cascades()
                for event in events:
                    _pkg.logger.warning(
                        "Slice cascade fired",
                        pipeline_id=pipeline_id,
                        failed_slice=event.failed_slice_id,
                        blocked=event.blocked_subtree,
                    )
                    try:
                        from orchestrator.gateway_client import (
                            get_gateway_client as _get_gateway_client,
                        )

                        _ = _get_gateway_client  # noqa: F841 — kept for symmetry
                    except ImportError:
                        # Symmetry-only import; the module not being
                        # available means the cascade alert path can't
                        # call the gateway, but the warning above is
                        # the always-on fallback.
                        pass
                if scheduler.all_done():
                    break
                _pkg.time.sleep(poll_interval)
                continue

            # Run every ready slice in this wave in parallel
            # (#2137 TASK-4-4 + decision-5: unbounded). The
            # ``max_parallel_slices`` cap from ``iter_ready`` already
            # bounds ``ready_batch`` so the executor's worker pool
            # mirrors that cap. Each slice runs through the existing
            # ``_run_concurrent_phase`` machinery in its own thread.
            # Per-slice failure / completion events are recorded back
            # on the scheduler from inside ``_run_one_slice`` so the
            # cascade machinery sees the same wall-clock as the run
            # loop.

            def _run_one_slice(slice_id: str, parent_slice_id: str | None) -> tuple[int, str]:
                # Release the global-admission slot when the slice
                # exits, regardless of how (consensus, failure, raised
                # exception). Idempotent — safe even if a future
                # codepath calls release() somewhere else (#2241 gap 1).
                try:
                    return _run_one_slice_inner(slice_id, parent_slice_id)
                finally:
                    global_slice_admit.release(pipeline_id, slice_id)

            def _run_one_slice_inner(
                slice_id: str,
                parent_slice_id: str | None,  # noqa: ARG001 — kept for caller compat; resolver reads contract
            ) -> tuple[int, str]:
                # Resolve parent branch for stacking via
                # :func:`_resolve_slice_base_branch` (#2777, cq-2 / cq-4 /
                # cq-9 / cq-10). The helper handles both:
                #
                # * eager-persisted ``parent_branch_at_creation`` (the
                #   primary path post-slice-4 TASK-4-2), and
                # * fresh-pipeline derivation from
                #   ``slice.dependencies[0]`` (the path #2777's slice-2
                #   takes before slice-4 lands).
                #
                # The legacy ``egg/<id>/context`` branch was removed in
                # cq-4 so slice-1 (the root) now stacks on
                # ``pipeline_branch`` like every other root slice — the
                # work-branch context PR's diff already encompasses the
                # slice-1 integration branch via ancestry.
                # #2928: wire a parent-branch-existence probe so the
                # resolver can tell a FRESH non-root slice (whose
                # dependency parent branch is still on origin → stack
                # on it) apart from an orphaned one (parent merged
                # into ``work`` and cascade-deleted → base on
                # ``pipeline_branch``). This replaces the pre-#2928
                # merge-base probe, which probed the slice's OWN
                # integration branch — non-existent on a first run —
                # and so mis-routed every fresh non-root slice onto
                # ``work`` whenever ``work`` had advanced ahead of the
                # parent. Repoless test scaffolds short-circuit to
                # ``True`` (no origin to check; the derived parent is
                # the correct DAG target), mirroring the resolver's
                # conservative "assume parent exists" default.
                #
                # IMPORTANT: this wrapper calls the STRICT ls-remote
                # variant (``ls_remote_branch_strict``) so a gateway /
                # network / policy failure RAISES into the resolver's
                # ``try/except`` instead of being collapsed to
                # ``False``. The lenient ``ls_remote_branch`` /
                # ``get_remote_branch_sha`` helpers swallow all
                # exceptions and return ``False`` / ``None`` for both
                # "branch absent" AND "gateway error" — using either
                # here would silently route a real slice onto
                # ``pipeline_branch`` on a flaky gateway, re-creating
                # the #2928 wedge that this PR claims to fix.
                def _probe_parent_branch_exists(parent_branch: str) -> bool:
                    if not pipeline.repo:
                        return True
                    return spawner.gateway.ls_remote_branch_strict(
                        pipeline_id,
                        str(worktree_repo_path),
                        f"refs/heads/{parent_branch}",
                        mode=gateway_mode,  # type: ignore[arg-type]
                    )

                parent_branch = _pkg._resolve_slice_base_branch(
                    contract,
                    slice_id,
                    pipeline_id=pipeline_id,
                    pipeline_branch=pipeline_branch,
                    parent_branch_exists=_probe_parent_branch_exists,
                )
                integration_branch = f"{issue_branch}/{slice_id}"

                # Persist the parent-branch reference on the contract
                # under the per-pipeline state lock so a concurrent
                # tester / documenter contract write doesn't race with
                # ours (reviewer_code v4 #5). While we hold the contract,
                # also read back any integration_base_sha recorded on a
                # prior run (#2871) — on a restart this lets the race
                # check below tell an empty branch apart from a merged
                # one. It is ``None`` on a slice's first run (recorded
                # only after the branch is created, just below).
                recorded_base_sha: str | None = None
                # #3253 — capture whether the slice has any produced task
                # commit / PR while we hold the contract, so the race-merged
                # skip below cannot mistake an empty / un-started branch for
                # a merged one (see the merged-acceptance guard below).
                slice_produced_work = False
                try:
                    with get_pipeline_state_lock(pipeline_id):
                        contract_local = load_contract(pipeline_id, worktree_repo_path)
                        for s in contract_local.slices:
                            if s.id == slice_id:
                                s.parent_branch_at_creation = parent_branch
                                # Slice-4 TASK-4-2: flip PENDING →
                                # IN_PROGRESS in the SAME contract write
                                # that persists parent_branch_at_creation
                                # (cq-9). Crash recovery (TASK-4-4
                                # Layer C) now has a single signal to
                                # distinguish a fresh slice from one
                                # whose run was interrupted between
                                # status flip and branch creation.
                                # Idempotent on re-entry (e.g. orphan
                                # reconciler): only PENDING is flipped;
                                # COMPLETE / BLOCKED / IN_PROGRESS are
                                # left untouched.
                                if s.status == SliceStatus.PENDING:
                                    s.status = SliceStatus.IN_PROGRESS
                                recorded_base_sha = s.integration_base_sha
                                slice_produced_work = (
                                    _pkg._slice_produced_commits(s) or s.pr_number is not None
                                )
                                break
                        save_contract(contract_local, worktree_repo_path)
                except Exception as save_err:  # noqa: BLE001
                    # Contract load/save under per-pipeline state lock.
                    # Same exception surface as the COMPLETE-persist
                    # site above (loader validation, atomic-rename
                    # I/O, pydantic re-serialisation). Best-effort.
                    _pkg.logger.warning(
                        "Failed to persist parent_branch_at_creation",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        error=str(save_err),
                    )

                # Race protection: a slice's PR can be merged between
                # bootstrap reconciliation and this spawn. Detect and
                # skip to COMPLETE so the create-branch push below
                # doesn't non-fast-forward (#2549).
                if pipeline.repo:
                    try:
                        already_merged = spawner.gateway.is_slice_branch_merged_into_parent(
                            pipeline_id,
                            str(worktree_repo_path),
                            integration_branch=integration_branch,
                            parent_branch=parent_branch,
                            integration_base_sha=recorded_base_sha,
                            # Read-only ancestry check run by the
                            # orchestrator's slice-loop scheduler; attribute
                            # to the orchestrator, not a phantom coder (#2919).
                            agent_role="orchestrator",
                            mode=gateway_mode,  # type: ignore[arg-type]
                        )
                    except Exception as detect_err:  # noqa: BLE001
                        # Same `is_slice_branch_merged_into_parent`
                        # surface as the bootstrap pass above
                        # (GatewayError + OSError). Default to "not
                        # merged" so the slice can still spawn.
                        _pkg.logger.warning(
                            "Slice merged-detection raised; treating as not-merged",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(detect_err),
                        )
                        already_merged = False
                    # #3253 — a slice with no produced task commit and no PR
                    # has an empty integration branch (tip still at the fork
                    # base); origin ancestry reports it merged because that
                    # base is trivially an ancestor of an advanced parent.
                    # Don't skip it as merged — spawn so it actually runs.
                    if already_merged and not slice_produced_work:
                        _pkg.logger.info(
                            "Slice merged-detection ignored: no produced task commit "
                            "and no PR — empty/un-started branch, spawning instead of "
                            "skipping as merged (#3253)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            integration_branch=integration_branch,
                            parent_branch=parent_branch,
                        )
                        already_merged = False
                    if already_merged:
                        _pkg.logger.info(
                            "Slice already merged into parent on origin — skipping spawn (#2549)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            integration_branch=integration_branch,
                            parent_branch=parent_branch,
                        )
                        scheduler.record_complete(slice_id)
                        _persist_slice_status_complete(slice_id, basis="merged")
                        try:
                            remove_peer_consensus_tracker(pipeline_id, slice_id)
                        except Exception:  # noqa: BLE001
                            # In-memory dict pop under a lock; only
                            # programming errors (KeyError, AttributeError)
                            # could fire. Bare-except keeps the slice
                            # COMPLETE/return path crash-proof.
                            pass
                        return 0, (
                            f"slice {slice_id}: already merged into "
                            f"{parent_branch} on origin — skipped"
                        )

                # #2137 TASK-4-2: create the slice integration branch
                # on origin BEFORE spawning containers. Push
                # ``parent_branch:refs/heads/integration_branch``
                # through the existing per-agent push allowlist. Agents
                # then push their commits directly to the slice's
                # integration branch (``egg/issue-N/slice-M``) so the
                # slice PR's diff is non-empty when ``gh pr create``
                # runs. On failure, mark the slice failed so the
                # cascade machinery can surface the missing-parent
                # error to the operator instead of silently spawning
                # agents that would push to a missing parent.
                if pipeline.repo:
                    try:
                        # #3185 — the helper now returns the fork-base
                        # SHA it pushed the integration branch at (the
                        # parent tip resolved inside the call), or None
                        # on failure. Recording that SHA directly here
                        # replaces a prior best-effort
                        # ``get_remote_branch_sha`` re-fetch that could
                        # silently fail (no ``retry_transient``) and
                        # leave ``integration_base_sha`` unset — arming
                        # the empty-pre-created-branch trap on the next
                        # restart.
                        created_base_sha = spawner.gateway.create_slice_integration_branch(
                            pipeline_id,
                            str(worktree_repo_path),
                            integration_branch=integration_branch,
                            parent_branch=parent_branch,
                            # #2947 — hand the slice's recorded fork
                            # base to the gateway so a crash/restart
                            # over a branch that already carries this
                            # slice's commits (with an additively
                            # advanced parent) resumes in place
                            # instead of non-fast-forward-failing.
                            integration_base_sha=recorded_base_sha,
                            # Orchestrator pre-creates the slice
                            # integration branch on a synthetic session
                            # before agents spawn; attribute to the
                            # orchestrator, not a phantom coder (#2919).
                            # The push rides the slice-integration
                            # exemption (synthetic + branch shape), not a
                            # role gate.
                            agent_role="orchestrator",
                            mode=gateway_mode,  # type: ignore[arg-type]
                        )
                    except Exception as branch_err:  # noqa: BLE001
                        # Gateway `create_slice_integration_branch`
                        # call. Catches GatewayError (HTTP/timeout)
                        # and OSError (DNS / socket). Treat as failure
                        # so the cascade machinery surfaces a
                        # missing-parent error.
                        _pkg.logger.error(
                            "Slice integration branch creation raised",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(branch_err),
                        )
                        created_base_sha = None
                    if created_base_sha is None:
                        _pkg.logger.error(
                            "Slice integration branch creation failed; "
                            "marking slice failed (agents not spawned)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            parent_branch=parent_branch,
                            integration_branch=integration_branch,
                        )
                        scheduler.record_failure(slice_id)
                        return 1, (
                            f"slice {slice_id}: integration branch "
                            f"{integration_branch} could not be created from "
                            f"{parent_branch}"
                        )

                    # #2871 / #3185 — record the integration branch's fork
                    # base exactly once, on first creation. The branch was
                    # just pushed at the parent's tip and no agent has been
                    # spawned yet, so its origin tip still equals its base.
                    # Persisting it now lets a later restart's bootstrap
                    # reconciliation (and the race check above) tell an
                    # *empty* slice branch — tip still at this base, hence
                    # a trivial ancestor of an advanced parent — apart from
                    # a genuinely *merged* one whose tip moved past it. We
                    # only write it when unset so a restart over a branch
                    # that already carries slice commits (#2512 recovery)
                    # keeps its original base rather than the advanced tip.
                    # ``created_base_sha`` is the SHA the create call
                    # returned (no extra round-trip); it is an empty string
                    # on the unreachable no-op path
                    # (``integration_branch == parent_branch``), which we
                    # skip here.
                    if recorded_base_sha is None and created_base_sha:
                        try:
                            with get_pipeline_state_lock(pipeline_id):
                                contract_local = load_contract(pipeline_id, worktree_repo_path)
                                for s in contract_local.slices:
                                    if s.id == slice_id:
                                        s.integration_base_sha = created_base_sha
                                        break
                                save_contract(contract_local, worktree_repo_path)
                            recorded_base_sha = created_base_sha
                        except Exception as base_err:  # noqa: BLE001
                            # Contract load/save under per-pipeline state
                            # lock. Catches loader validation, atomic-
                            # rename I/O, and pydantic re-serialisation
                            # errors. Best-effort: the fork base is no
                            # longer a round-trip failure (the SHA came
                            # from the create call itself), so this now
                            # only fires on a contract-write failure — a
                            # transient the next run repairs on the same
                            # create path.
                            _pkg.logger.warning(
                                "Failed to persist slice integration_base_sha "
                                "(#2871); a future restart re-records it on the "
                                "create path",
                                pipeline_id=pipeline_id,
                                slice_id=slice_id,
                                integration_branch=integration_branch,
                                error=str(base_err),
                            )

                _pkg.logger.info(
                    "Slice spawn",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    parent_branch=parent_branch,
                    integration_branch=integration_branch,
                )

                exit_code_inner, logs_inner = _pkg._run_concurrent_phase_with_impasse_retry(
                    pipeline_id=pipeline_id,
                    pipeline=pipeline,
                    phase="implement",
                    spawner=spawner,
                    repo_volumes=repo_volumes,
                    gateway_mode=gateway_mode,
                    repos=repos,
                    sandbox_env=sandbox_env,
                    store=store,
                    certs_volume=certs_volume,
                    worktree_repo_path=worktree_repo_path,
                    slice_id=slice_id,
                    run_epoch=run_epoch,
                )

                if exit_code_inner != 0:
                    scheduler.record_failure(slice_id)
                    _pkg.logger.warning(
                        "Slice failed",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        exit_code=exit_code_inner,
                    )
                    return exit_code_inner, logs_inner

                # Slice consensus reached — load the contract ONCE
                # under the per-pipeline state lock and reuse the same
                # snapshot for the #3125 evidence-reachability gate
                # AND the slice's PR data snapshot below. Both readers
                # previously took the lock independently; collapsing
                # them eliminates one file read + lock acquire per
                # slice close (#3125 review).
                #
                # The slice_pr_data block below originally documented
                # the lock as covering only the contract read so the
                # gateway HTTP round-trip wouldn't serialise other
                # writers — the same posture applies here: we release
                # the lock before the gateway call inside the gate.
                contract_post: _pkg.Any | None = None
                try:
                    with get_pipeline_state_lock(pipeline_id):
                        contract_post = load_contract(pipeline_id, worktree_repo_path)
                except Exception as load_err:  # noqa: BLE001
                    _pkg.logger.warning(
                        "Slice close: contract load failed (continuing) (#3125)",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        error=str(load_err),
                    )

                # #3125 — evidence-reachability gate: every commit SHA
                # cited by this slice's contract task records must be
                # an ancestor of the integration branch tip, or the
                # slice PR would ship without a deliverable the task
                # record claims is done (the post-confirmation
                # ``complete-task --commit`` unblock flow, #3124).
                # Fails the slice BEFORE any close side effect so the
                # cascade + HITL machinery surfaces the gap loudly.
                # ``contract_post`` may be None if the load above
                # raised — the gate falls back to its own load in that
                # case (and skips gracefully if that fails too).
                if pipeline.repo:
                    evidence_failure = _pkg._check_slice_evidence_reachability(
                        pipeline_id,
                        spawner,
                        worktree_repo_path,
                        slice_id,
                        integration_branch,
                        gateway_mode=gateway_mode,  # type: ignore[arg-type]
                        contract=contract_post,
                    )
                    if evidence_failure is not None:
                        scheduler.record_failure(slice_id)
                        return 1, evidence_failure

                # #3398 — per-slice green gate: execute the repo's
                # configured checks (repositories.yaml, via
                # get_repo_checks) against the integration-branch tip
                # in a sandboxed one-shot runner, and refuse to open
                # the slice PR while any check is red. Closes the
                # trust-vs-verify gap in the propose-time
                # checks_passed self-report. Same posture as the
                # evidence gate above: fail-open on infra errors,
                # fail-closed only on a definitive red verdict;
                # EGG_SLICE_GREEN_GATE is the operator switch
                # (off during rollout / log / on).
                if pipeline.repo:
                    try:
                        import slice_green_gate as _green_gate
                    except ImportError:
                        from .. import slice_green_gate as _green_gate  # type: ignore[no-redef]

                    green_gate_failure = _green_gate.run_slice_green_gate(
                        pipeline_id,
                        spawner,
                        slice_id,
                        integration_branch,
                        pipeline.repo,
                        gateway_mode=gateway_mode,  # type: ignore[arg-type]
                    )
                    if green_gate_failure is not None:
                        scheduler.record_failure(slice_id)
                        return 1, green_gate_failure

                # Snapshot the slice's PR data from the same loaded
                # contract — no second lock acquire, no second file
                # read.
                slice_pr_data: dict[str, _pkg.Any] | None = None
                try:
                    if contract_post is not None:
                        slice_obj = next(
                            (s for s in contract_post.slices if s.id == slice_id),
                            None,
                        )
                        if slice_obj is not None and pipeline.repo:
                            # #2538: every slice carries the
                            # planner-authored narrative on its PR so
                            # reviewers see context on whichever slice
                            # they open first. Pre-#2777 cq-6 the
                            # terminal slice additionally carried a
                            # program-level rollup (test plan + manual
                            # steps + pre-merge obligations) and a
                            # ``[merge-gate]`` title marker. Under cq-4
                            # the merge gate is the up-front context
                            # PR (``egg/<id>/work → main``) opened by
                            # ``_open_context_pr_at_implement_start``,
                            # so every slice PR — terminal or not —
                            # now uses the same lean shape and the
                            # terminal-slice computation is gone.
                            program_pr = contract_post.pr
                            # #2745: derive 1-based slice position +
                            # total slice count from declared contract
                            # order so the slice PR title can carry
                            # ``[slice-N/M]``.
                            slice_count = len(contract_post.slices)
                            slice_index_lookup = next(
                                (
                                    i + 1
                                    for i, s in enumerate(contract_post.slices)
                                    if s.id == slice_id
                                ),
                                None,
                            )
                            # Union of ``task.files_affected`` across the
                            # slice's tasks; rendered under
                            # ``## This slice`` so reviewers see what
                            # this slice actually touches without
                            # opening the diff (#2745).
                            slice_files_affected_list: list[str] = []
                            seen_paths: set[str] = set()
                            for t in slice_obj.tasks or []:
                                for path in t.files_affected or []:
                                    if path and path not in seen_paths:
                                        seen_paths.add(path)
                                        slice_files_affected_list.append(path)
                            # #3393 slice-4 / task-4-1: route this slice's
                            # PR to its OWN repo (``resolve_slice_repo`` →
                            # ``slice.repo`` else the pipeline primary) and
                            # gather CROSS-repo coordination references for
                            # the PR body. Same-repo relationships are left
                            # to ``## Stack``, so for an N=1 pipeline
                            # ``slice_repo`` is the single repo and both
                            # ref sets are empty — behaviour is unchanged.
                            try:
                                from models import (  # type: ignore[no-redef]
                                    resolve_slice_repo,
                                )
                            except ImportError:
                                from ..models import (  # type: ignore[no-redef]
                                    resolve_slice_repo,
                                )
                            slice_repo = resolve_slice_repo(slice_obj, pipeline) or pipeline.repo
                            sibling_pr_refs: list[dict[str, _pkg.Any]] = []
                            for other in contract_post.slices:
                                if other.id == slice_id:
                                    continue
                                other_repo = resolve_slice_repo(other, pipeline) or pipeline.repo
                                if other_repo and other_repo != slice_repo and other.pr_number:
                                    sibling_pr_refs.append(
                                        {"repo": other_repo, "number": other.pr_number}
                                    )
                            # Dependent-slice upstream PR — surfaced only
                            # when the upstream slice is in a DIFFERENT repo
                            # (a same-repo parent is the stack base already
                            # rendered by ``## Stack``).
                            upstream_pr_ref: dict[str, _pkg.Any] | None = None
                            upstream_ids = slice_obj.dependencies or []
                            if upstream_ids:
                                upstream = next(
                                    (s for s in contract_post.slices if s.id == upstream_ids[0]),
                                    None,
                                )
                                if upstream is not None and upstream.pr_number:
                                    upstream_repo = (
                                        resolve_slice_repo(upstream, pipeline) or pipeline.repo
                                    )
                                    if upstream_repo and upstream_repo != slice_repo:
                                        upstream_pr_ref = {
                                            "repo": upstream_repo,
                                            "number": upstream.pr_number,
                                        }
                            # #3393 slice-5 / task-5-1: a slice with a
                            # CROSS-repo dependency opens its PR as a DRAFT
                            # — cross-repo edges can't stack, so the
                            # dependent slice is developed in parallel and
                            # only its PR *ready* transition waits on the
                            # merge gate (auto draft→ready when the upstream
                            # merges, else a HITL hold). A dep is cross-repo
                            # iff the upstream slice resolves to a DIFFERENT
                            # repo; same-repo-only deps and N=1 pipelines
                            # stay non-draft (behaviour unchanged). Checks
                            # ALL deps so any cross-repo upstream holds it.
                            cross_repo_draft = False
                            for _dep_id in slice_obj.dependencies or []:
                                _dep = next(
                                    (s for s in contract_post.slices if s.id == _dep_id),
                                    None,
                                )
                                if _dep is None:
                                    continue
                                _dep_repo = resolve_slice_repo(_dep, pipeline) or pipeline.repo
                                if _dep_repo and _dep_repo != slice_repo:
                                    cross_repo_draft = True
                                    break
                            slice_pr_data = {
                                # #3393 slice-4: the repo this slice's PR is
                                # opened in + its cross-repo coordination
                                # references (empty for N=1).
                                "slice_repo": slice_repo,
                                # #3393 slice-5: open draft when this slice
                                # has a cross-repo dependency (see above).
                                "cross_repo_draft": cross_repo_draft,
                                "sibling_pr_refs": sibling_pr_refs,
                                "upstream_pr_ref": upstream_pr_ref,
                                "slice_name": slice_obj.name or slice_id,
                                # Planner's reviewer-facing summary —
                                # rendered as the slice PR body's lead
                                # paragraph (#3115). Empty for
                                # pre-#3115 contracts.
                                "slice_goal": getattr(slice_obj, "goal", "") or None,
                                "slice_tasks": [
                                    {
                                        "id": t.id,
                                        "description": t.description,
                                        "acceptance_criteria": t.acceptance_criteria,
                                    }
                                    for t in (slice_obj.tasks or [])
                                ],
                                "slice_index": slice_index_lookup,
                                "slice_count": slice_count,
                                "slice_files_affected": slice_files_affected_list or None,
                                # ``context_pr_number`` is populated by
                                # ``_open_context_pr_at_implement_start``
                                # at the plan→implement boundary (#2777
                                # cq-4). When the contract linkage is
                                # missing (e.g. ``contract.pr`` is None
                                # on an implement-start resume, #3100),
                                # fall back to ``pipeline.pr_number`` —
                                # the pipeline-level mirror written by
                                # ``_persist_context_pr_number`` whose
                                # sole post-#2777 writer is the same
                                # opener — so the slice PR still links
                                # its base PR (#3115). When both are
                                # None — should be unreachable under
                                # the hard-required opener but kept as
                                # defence-in-depth — ``create_slice_pr``
                                # falls back to the pre-#2745 inline-
                                # narrative body so the slice PR stays
                                # reviewable as a standalone diff
                                # against ``/work``.
                                "context_pr_number": (
                                    (program_pr.context_pr_number if program_pr else None)
                                    or pipeline.pr_number
                                ),
                                "program_title": (program_pr.title if program_pr else None),
                                "program_description": (
                                    program_pr.description if program_pr else None
                                ),
                                "program_test_plan": (program_pr.test_plan if program_pr else None),
                                "program_manual_steps": (
                                    program_pr.manual_steps if program_pr else None
                                ),
                            }
                except Exception as attr_err:  # noqa: BLE001
                    # Nested attribute traversal on slice/program PR
                    # objects (the contract load was lifted out to the
                    # block above). Surface is AttributeError /
                    # KeyError on partially-populated PR rollup
                    # fields. Continue without slice_pr_data (the
                    # gateway PR creation just below is gated on it
                    # being non-None).
                    _pkg.logger.warning(
                        "Slice PR pre-load failed (continuing)",
                        pipeline_id=pipeline_id,
                        slice_id=slice_id,
                        error=str(attr_err),
                    )

                # Persist this slice's per-slice BRC consensus history
                # onto its integration branch as the final
                # orchestrator-authored commit before the slice PR is
                # opened, so reviewers see the consensus transcript in
                # the PR diff (#2548). Best-effort + idempotent on
                # retry; per-slice files live ONLY on the integration
                # branch.
                if pipeline.repo:
                    try:
                        _pkg._commit_slice_brc_history_to_integration_branch(
                            pipeline,
                            spawner,
                            worktree_repo_path,
                            slice_id,
                            integration_branch,
                            gateway_mode=gateway_mode,  # type: ignore[arg-type]
                        )
                    except Exception as brc_commit_err:  # noqa: BLE001
                        # Per-slice BRC commit helper calls into the
                        # full git/gateway/message-store machinery
                        # — the exception surface is unbounded
                        # (gateway push failures, git plumbing
                        # errors, message-store reads, file I/O).
                        # Best-effort: the BRC transcript commit is
                        # non-essential to slice consensus.
                        _pkg.logger.warning(
                            "Per-slice BRC commit raised (continuing) (#2548)",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(brc_commit_err),
                        )

                pr_created = True
                slice_pr_url: str | None = None
                slice_pr_number: int | None = None
                if slice_pr_data is not None and pipeline.repo:
                    # Best-effort real-diff summary for the PR body
                    # (#3115) — commit subjects + diffstat from the
                    # pushed integration branch. (None, None) on any
                    # failure; the PR opens without the section.
                    commit_subjects, diffstat = _pkg._build_slice_diff_summary(
                        pipeline,
                        spawner,
                        worktree_repo_path,
                        integration_branch,
                        parent_branch,
                        gateway_mode=gateway_mode,  # type: ignore[arg-type]
                    )
                    try:
                        slice_pr_url = spawner.gateway.create_slice_pr(
                            pipeline_id=pipeline_id,
                            # #3393 slice-4 / task-4-1: route to the slice's
                            # own repo (falls back to the pipeline primary
                            # when ``slice.repo`` is absent — the N=1 case).
                            repo=slice_pr_data["slice_repo"] or pipeline.repo,
                            slice_id=slice_id,
                            slice_name=slice_pr_data["slice_name"],
                            slice_tasks=slice_pr_data["slice_tasks"],
                            head=integration_branch,
                            base=parent_branch,
                            issue_number=issue_number,
                            agent_role="orchestrator",
                            mode=gateway_mode,  # type: ignore[arg-type]
                            # #3393 slice-5 / task-5-1: draft when this
                            # slice has a cross-repo dependency; the merge
                            # gate marks it ready on upstream merge (or a
                            # HITL hold releases it). False for N=1.
                            draft=slice_pr_data["cross_repo_draft"],
                            program_title=slice_pr_data["program_title"],
                            program_description=slice_pr_data["program_description"],
                            program_test_plan=slice_pr_data["program_test_plan"],
                            program_manual_steps=slice_pr_data["program_manual_steps"],
                            slice_index=slice_pr_data["slice_index"],
                            slice_count=slice_pr_data["slice_count"],
                            slice_files_affected=slice_pr_data["slice_files_affected"],
                            context_pr_number=slice_pr_data["context_pr_number"],
                            slice_goal=slice_pr_data["slice_goal"],
                            diffstat=diffstat,
                            commit_subjects=commit_subjects,
                            sibling_pr_refs=slice_pr_data["sibling_pr_refs"],
                            upstream_pr_ref=slice_pr_data["upstream_pr_ref"],
                        )
                    except Exception as pr_err:  # noqa: BLE001
                        # Single `gateway.create_slice_pr` HTTP call.
                        # Catches GatewayError (HTTP) and OSError
                        # (DNS / socket). Mark pr_created=False so
                        # the cascade machinery fires.
                        _pkg.logger.error(
                            "Slice PR creation failed",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id,
                            error=str(pr_err),
                        )
                        pr_created = False

                if not pr_created:
                    scheduler.record_failure(slice_id)
                    return 1, (
                        f"slice {slice_id}: PR creation failed (head={integration_branch}, "
                        f"base={parent_branch})"
                    )

                # Parse the slice PR number from the returned URL
                # (#3122) — same trailing-boundary pattern the context-
                # PR opener uses, narrowed to ``[1-9]\d*`` so a
                # malformed ``/pull/0/...`` URL doesn't make it as far
                # as ``Slice.pr_number``'s ``ge=1`` validator (which
                # would silently downgrade to a warning log via the
                # save try/except in ``_persist_slice_status_complete``).
                # Best-effort: an unparseable URL just means the
                # linkage isn't recorded this pass; the idempotent
                # ``create_slice_pr`` re-yields it on a resume.
                if slice_pr_url:
                    pr_match = _pkg.re.search(r"/pull/([1-9]\d*)(?:[/?#]|$)", slice_pr_url)
                    if pr_match:
                        slice_pr_number = int(pr_match.group(1))

                # Hold the per-pipeline state lock across both the
                # contract-write (``_persist_slice_status_complete``
                # itself reacquires this RLock) and the context-PR
                # body refresh (load + compose + push). Without the
                # outer lock, two slices in the same wave could
                # interleave between persist and push so the slice
                # whose refresh starts earlier but lands later
                # clobbers the body that already included both links
                # — and because no later slice fires a refresh, the
                # final slice's ``— #N`` link would stay missing
                # forever. Serializing here bounds the per-slice tail
                # latency by one gateway PATCH per concurrent slice
                # rather than racing them.
                with get_pipeline_state_lock(pipeline_id):
                    scheduler.record_complete(slice_id)
                    # Reaching here means ``_run_concurrent_phase`` returned
                    # success (BRC consensus) AND ``pr_created`` gated above —
                    # a verified completion independent of whether the PR URL
                    # parsed to a number (#3122 stub URLs leave
                    # ``slice_pr_number`` None). Declare the consensus basis so
                    # the #3214 invariant accepts it; ``pr_number`` is still
                    # passed for the slice-table linkage.
                    _persist_slice_status_complete(
                        slice_id,
                        pr_number=slice_pr_number,
                        pr_url=slice_pr_url if slice_pr_number else None,
                        basis="consensus_complete",
                    )

                    # Refresh the context PR body so its slice table
                    # links the PR that just opened (#3122). Strictly
                    # cosmetic and best-effort: every failure path
                    # inside logs + returns False without raising, and
                    # the slice outcome below never depends on it.
                    if slice_pr_number:
                        _pkg._refresh_context_pr_body(
                            pipeline_id,
                            pipeline=pipeline,
                            spawner=spawner,
                            worktree_repo_path=worktree_repo_path,
                            identifier=_pkg._pipeline_identifier(
                                pipeline.issue_number, pipeline_id
                            ),
                            gateway_mode=gateway_mode,
                        )

                try:
                    remove_peer_consensus_tracker(pipeline_id, slice_id)
                except Exception:  # noqa: BLE001
                    # In-memory dict pop under a lock; same crash-proof
                    # defence-in-depth as the merged-skip branch above.
                    pass
                return exit_code_inner, logs_inner

            # Gate every ready slice through the orchestrator-process-wide
            # admission counter (#2241 gap 1). Slices the global cap
            # rejects stay in READY and re-yield next tick — the per-
            # pipeline ``iter_ready`` accounting is unaffected because
            # we admit BEFORE ``mark_spawned``. If the entire batch is
            # rejected, sleep one poll interval before re-checking so
            # we don't burn CPU spinning on iter_ready.
            admitted_batch: list[tuple[str, str | None]] = [
                (slice_id, parent_slice_id)
                for slice_id, parent_slice_id in ready_batch
                if global_slice_admit.try_admit(pipeline_id, slice_id)
            ]
            if not admitted_batch:
                _pkg.logger.info(
                    "Slice wave deferred behind global cap",
                    pipeline_id=pipeline_id,
                    ready=[s for s, _ in ready_batch],
                    admit=global_slice_admit.snapshot(),
                )
                _pkg.time.sleep(poll_interval)
                continue

            # Mark admitted slices as spawned BEFORE submitting them to
            # the executor so a subsequent ``iter_ready`` from any other
            # thread sees the in-flight count correctly.
            for slice_id, _parent in admitted_batch:
                scheduler.mark_spawned(slice_id)

            max_workers = max(1, len(admitted_batch))
            with _pkg.concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"slice-wave-{pipeline_id}",
            ) as wave_pool:
                futures: dict[_pkg.concurrent.futures.Future, str] = {}
                for slice_id, parent_slice_id in admitted_batch:
                    fut = wave_pool.submit(_run_one_slice, slice_id, parent_slice_id)
                    futures[fut] = slice_id

                for fut in _pkg.concurrent.futures.as_completed(futures):
                    slice_id_done = futures[fut]
                    try:
                        exit_code, logs = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        # fut.result() re-raises whatever the slice
                        # worker raised. Workers call into the full
                        # implement-phase machinery (gateway, contract,
                        # spawner, message store, docker) so the
                        # exception surface is unbounded; mark the
                        # slice failed and continue rather than tearing
                        # down the whole wave.
                        scheduler.record_failure(slice_id_done)
                        exit_code = 1
                        logs = f"slice {slice_id_done} raised: {exc!r}"
                        _pkg.logger.error(
                            "Slice worker raised",
                            pipeline_id=pipeline_id,
                            slice_id=slice_id_done,
                            error=str(exc),
                        )
                    aggregate_logs.append(f"--- slice {slice_id_done} ---\n{logs}")
                    if exit_code != 0:
                        overall_exit = exit_code

            # Drain cascades after each wave so descendants of a
            # failed slice are visibly BLOCKED before the next
            # iteration computes ready slices. Emit an
            # OVERSEER_ALERT per cascade so the human operator sees
            # the blocked subtree (#2137 TASK-3-4 emission path).
            events = scheduler.poll_cascades()
            for event in events:
                _pkg.logger.warning(
                    "Slice cascade fired",
                    pipeline_id=pipeline_id,
                    failed_slice=event.failed_slice_id,
                    blocked=event.blocked_subtree,
                )
                # Emit OVERSEER_ALERT directly through the in-process
                # message store so the human operator's overseer
                # surface picks up the cascade-block event (TASK-3-4
                # emission path).
                try:
                    try:
                        from message_store import Message, get_message_store
                    except ImportError:
                        from ..message_store import (  # type: ignore[no-redef]
                            Message,
                            get_message_store,
                        )

                    msg = Message(
                        pipeline_id=pipeline_id,
                        from_role="orchestrator",
                        to_role="all",
                        message_type="OVERSEER_ALERT",
                        subject=f"slice-cascade-block: {event.failed_slice_id}",
                        body=(
                            f"Slice {event.failed_slice_id} failed; "
                            f"downstream subtree {event.blocked_subtree} marked "
                            "BLOCKED_ON_FAILED_DEPENDENCY (60 s grace expired). "
                            "HITL resolution required to restart the failed slice."
                        ),
                        metadata={
                            "anomaly": "slice-cascade-block",
                            "priority": "high",
                            "failed_slice_id": event.failed_slice_id,
                            "blocked_subtree": list(event.blocked_subtree),
                        },
                        phase="implement",
                    )
                    get_message_store().add_message(msg)
                except Exception:  # noqa: BLE001
                    # Best-effort: the log line above is the
                    # always-on fallback so the operator still sees
                    # the cascade in the orchestrator log.
                    pass
    finally:
        reconciler_stop.set()
        try:
            reconciler_thread.join(timeout=5.0)
        except RuntimeError:
            # Thread.join only raises RuntimeError (e.g. joining the
            # current thread). Other failures are silent timeouts.
            pass

    aggregated = "\n".join(aggregate_logs) if aggregate_logs else "Slice loop completed."
    return overall_exit, aggregated
