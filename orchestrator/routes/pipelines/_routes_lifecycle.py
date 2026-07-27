"""lifecycle-route bodies helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _list_pipeline_local_commits_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """List unpushed commits across this pipeline's per-agent worktrees.

    Inspects every per-agent worktree on disk
    (``{pipeline_id}``, ``{pipeline_id}-{role}``,
    ``{pipeline_id}-slice-{N}-{role}``) and reports the commits on its
    local ``egg/{worktree_id}/work`` branch that are not reachable from
    ``origin/<assigned_branch>`` (or ``origin/<base_branch>`` as a
    fallback). Read-only — no fetch, no push.

    Query string (optional):
        agent_role: Filter to a single agent role (e.g. ``coder``).
        slice_id: Filter to a single slice scope (e.g. ``slice-2``).

    Response:
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-2261-v9",
                "worktrees": [
                    {
                        "worktree_id": "issue-2261-v9-slice-2-coder",
                        "agent_role": "coder",
                        "slice_id": "slice-2",
                        "local_branch": "egg/issue-2261-v9-slice-2-coder/work",
                        "assigned_branch": "egg/issue-2261-v9/slice-2",
                        "anchor_ref": "refs/remotes/origin/egg/issue-2261-v9/slice-2",
                        "commits": [
                            {"sha": "...", "summary": "...", "author": "...",
                             "authored_at": "...", "files_changed": 3}
                        ],
                        "error": null
                    }
                ]
            }
        }
    """
    repo_path = _pkg.get_repo_path()

    try:
        _store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}", status_code=400
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(f"Pipeline {pipeline_id} not found", status_code=404)

    agent_role = _pkg.request.args.get("agent_role") or None
    if agent_role is not None:
        try:
            _pkg.AgentRole(agent_role)
        except ValueError:
            return _pkg.make_error_response(f"Invalid agent role: {agent_role}", status_code=400)

    raw_slice_id = _pkg.request.args.get("slice_id")
    try:
        slice_id = _pkg.extract_slice_id(
            {"slice_id": raw_slice_id} if raw_slice_id is not None else {}
        )
    except ValueError as e:
        return _pkg.make_error_response(str(e), status_code=400)

    worktrees = _pkg._filter_salvage_worktrees(
        _pkg.agent_salvage.enumerate_agent_worktrees(pipeline_id),
        agent_role=agent_role,
        slice_id=slice_id,
    )
    reports = [
        _pkg.agent_salvage.list_unpushed_commits(wt, base_branch=pipeline.base_branch)
        for wt in worktrees
    ]

    return _pkg.make_success_response(
        f"Listed local commits for pipeline {pipeline_id}",
        data={
            "pipeline_id": pipeline_id,
            "worktrees": [_pkg._serialize_commit_report(r) for r in reports],
        },
    )


def _salvage_pipeline_local_commits_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """Push unpushed agent commits to recovery refs (#2429).

    For every matching per-agent worktree, push its HEAD to
    ``egg/recovered/<pipeline_id>/<scope>/<short_sha>`` via the gateway's
    launcher-auth path. Launcher auth bypasses the agent-targeted
    branch-allowlist check so this works even when the agent's own
    pushes were rejected for the wrong-branch reason this verb exists
    to recover from.

    Query string (optional):
        agent_role: Salvage only this role's worktree.
        slice_id: Salvage only this slice scope.

    Response (always ``success: true`` when the request was well-formed
    — per-worktree failures are reported in ``data.results``):
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-2261-v9",
                "results": [
                    {"worktree_id": "...", "agent_role": "coder", "slice_id": "slice-2",
                     "recovery_ref": "egg/recovered/issue-2261-v9/slice-2-coder/9665f37a6...",
                     "head_sha": "9665f37a6...", "n_commits": 14, "ok": true, "error": null}
                ]
            }
        }
    """
    repo_path = _pkg.get_repo_path()

    try:
        _store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}", status_code=400
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(f"Pipeline {pipeline_id} not found", status_code=404)

    agent_role = _pkg.request.args.get("agent_role") or None
    if agent_role is not None:
        try:
            _pkg.AgentRole(agent_role)
        except ValueError:
            return _pkg.make_error_response(f"Invalid agent role: {agent_role}", status_code=400)

    raw_slice_id = _pkg.request.args.get("slice_id")
    try:
        slice_id = _pkg.extract_slice_id(
            {"slice_id": raw_slice_id} if raw_slice_id is not None else {}
        )
    except ValueError as e:
        return _pkg.make_error_response(str(e), status_code=400)

    worktrees = _pkg._filter_salvage_worktrees(
        _pkg.agent_salvage.enumerate_agent_worktrees(pipeline_id),
        agent_role=agent_role,
        slice_id=slice_id,
    )

    gateway_mode, _vis = _pkg._compute_gateway_mode(pipeline)
    gateway = _pkg.get_gateway_client()

    results = []
    for wt in worktrees:
        try:
            result = _pkg.agent_salvage.salvage_worktree(
                gateway,
                wt,
                base_branch=pipeline.base_branch,
                mode=gateway_mode,
            )
        except Exception as e:  # noqa: BLE001 — must always return a result row
            _pkg.logger.warning(
                "Salvage raised unexpectedly",
                pipeline_id=pipeline_id,
                worktree_id=wt.worktree_id,
                error=str(e),
            )
            result = _pkg.agent_salvage.SalvageResult(
                worktree_id=wt.worktree_id,
                agent_role=wt.agent_role,
                slice_id=wt.slice_id,
                recovery_ref=None,
                head_sha=None,
                n_commits=0,
                ok=False,
                error=str(e),
            )
        results.append(result)

    return _pkg.make_success_response(
        f"Salvaged {sum(1 for r in results if r.ok and r.recovery_ref)} of "
        f"{len(results)} per-agent worktrees for pipeline {pipeline_id}",
        data={
            "pipeline_id": pipeline_id,
            "results": [_pkg._serialize_salvage_result(r) for r in results],
        },
    )


def _start_pipeline_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """
    Start pipeline execution.

    Spawns containers for each phase in sequence, advancing through
    the phase DAG until completion or failure. Runs in a background thread.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Pipeline started",
            "data": {
                "pipeline_id": "local-a1b2c3d4",
                "status": "running"
            }
        }
    """
    repo_path = _pkg.get_repo_path()

    # Parse force / force_reason from body. ``force=true`` skips the
    # live-pod orphan guard before the phase reset (#2420). force_reason
    # is recorded in the structured warning log, mirroring the
    # complete_phase audit pattern.
    body = _pkg.request.get_json(silent=True) or {}
    # Strict boolean — `body.get("force") is True` rather than
    # `bool(body.get("force"))` so non-boolean truthy values
    # (`"false"`, `[]`, `{}`, `1`) don't silently flip the predicate.
    force = body.get("force") is True
    force_reason = body.get("force_reason")
    if force_reason is not None and not isinstance(force_reason, str):
        return _pkg.make_error_response(
            "force_reason must be a string",
            status_code=400,
            reason="invalid_force_reason",
        )
    if isinstance(force_reason, str) and not force_reason.strip():
        force_reason = None

    try:
        store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)
        # Use the store's repo_path so _run_pipeline operates on the correct directory
        repo_path = store.repo_path

        # Compute gateway mode for session operations in the recovery path
        _gw_mode, _gw_vis = _pkg._compute_gateway_mode(pipeline)

        if pipeline.status == _pkg.PipelineStatus.RUNNING:
            return _pkg.make_error_response(
                f"Pipeline {pipeline_id} is already running",
                status_code=409,
            )

        if pipeline.status == _pkg.PipelineStatus.AWAITING_HUMAN:
            # No pending decisions — the polling thread died (e.g. restart)
            # but the human already resolved everything.  Recover based on
            # the latest phase_gate decision's resolution.
            #
            # #2593 review issue 1 — initialised before the lock so the
            # post-lock deferred context-PR opener invocation has a
            # stable name to read regardless of which branch inside the
            # lock executes.
            _hitl_open_context_pr_after_lock: bool = False
            _hitl_pr_worktree_path: _pkg.Path | None = None
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)

                # Re-validate status after acquiring the lock — another
                # concurrent start_pipeline call may have already recovered
                # this pipeline.
                if pipeline.status != _pkg.PipelineStatus.AWAITING_HUMAN:
                    return _pkg.make_error_response(
                        f"Pipeline {pipeline_id} status changed to "
                        f"{pipeline.status.value} (concurrent recovery)",
                        status_code=409,
                    )

                pending = pipeline.get_pending_decisions()
                if len(pending) > 0:
                    return _pkg.make_error_response(
                        f"Pipeline {pipeline_id} is awaiting human approval "
                        f"({len(pending)} pending decision(s))",
                        status_code=409,
                    )

                # Find the latest resolved phase_gate decision
                phase_gate_decisions = [
                    d
                    for d in reversed(pipeline.decisions)
                    if d.decision_type == "phase_gate" and d.status.value == "resolved"
                ]
                latest_resolution = (
                    phase_gate_decisions[0].resolution if phase_gate_decisions else None
                )

                # Determine if approved or request_changes using the shared
                # parser (handles approve, select, submit_feedback,
                # request_changes, change_approach, and legacy bare strings).
                is_approved, revision_feedback = _pkg._parse_resolution(latest_resolution)

                # Same observability the live gate writes (#3636). This is
                # the path where a divergence is hardest to reconstruct
                # after the fact — the driver that would have logged it is
                # gone — so record how the stored resolution was read, and
                # log the branch. The field is stamped on the in-memory
                # decision; the ``store.save_pipeline(pipeline)`` that ends
                # this locked block (shared by both arms below) persists it.
                if phase_gate_decisions:
                    phase_gate_decisions[0].resolution_outcome = (
                        "approved" if is_approved else "needs_revision"
                    )
                _pkg.logger.info(
                    "HITL recovery: stored resolution parsed",
                    pipeline_id=pipeline_id,
                    phase=pipeline.current_phase.value,
                    decision_id=(phase_gate_decisions[0].id if phase_gate_decisions else None),
                    outcome="approved" if is_approved else "needs_revision",
                    has_feedback=bool(revision_feedback),
                    resolution_preview=(latest_resolution or "")[:200],
                )

                if is_approved:
                    # Mark current phase COMPLETE and advance
                    phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                    phase_execution.status = _pkg.PipelineStatus.COMPLETE
                    if phase_execution.completed_at is None:
                        phase_execution.completed_at = _pkg.datetime.now(_pkg.UTC)

                    # Persist phase gate resolution so next-phase agents see it.  #1295
                    #
                    # The contract and phase draft both live under the
                    # per-pipeline worktree (``<worktree>/.egg-state/``),
                    # not the orchestrator's main repo. Resolve the
                    # worktree explicitly here — the inline path inside
                    # ``_run_pipeline`` already has ``worktree_repo_path``
                    # in scope, but this recovery branch only has the
                    # main ``repo_path``. Passing ``repo_path`` would
                    # silently no-op the contract write and draft append
                    # (#2357, same shape as #2345).
                    if phase_gate_decisions:
                        worktree_repo_path = _pkg._resolve_pipeline_worktree_path(
                            pipeline, repo_path
                        )
                        if worktree_repo_path == repo_path:
                            # No materialised worktree — recovery degrades to
                            # the pre-fix shape (contract write typically
                            # no-ops via ContractNotFoundError, draft append
                            # skipped). The contract write *may* succeed if
                            # the orchestrator's main repo happens to carry a
                            # contract for this pipeline, but it would land
                            # against the wrong tree. Surface this either way
                            # so operators can correlate missing next-phase
                            # context with worktree-cleanup races.
                            _pkg.logger.warning(
                                "No materialised worktree found for phase gate "
                                "persistence; falling back to main repo path. "
                                "Contract write may silently no-op.",
                                pipeline_id=pipeline_id,
                                phase=pipeline.current_phase.value,
                            )
                        _pkg._persist_phase_gate_resolution(
                            worktree_repo_path,
                            pipeline_id,
                            phase_gate_decisions[0],
                            pipeline.current_phase.value,
                            pipeline.issue_number,
                        )

                        # Commit statefiles so worktrees created by _run_pipeline
                        # include the contract/draft changes.
                        try:
                            _pkg._commit_statefiles_to_worktree(
                                worktree_repo_path,
                                f"Persist HITL resolution after {pipeline.current_phase.value} phase gate",
                                pipeline_identifier=_pkg._pipeline_identifier(
                                    pipeline.issue_number, pipeline_id
                                ),
                                pipeline_id=pipeline_id,
                            )
                        except Exception as git_err:
                            # Catch broadly: see #2219.  The helper raises
                            # ``TimeoutExpired`` and ``OSError`` paths that a
                            # ``CalledProcessError``-only handler did not catch.
                            _pkg.logger.warning(
                                "Failed to commit statefiles after phase gate resolution (continuing)",
                                pipeline_id=pipeline_id,
                                error=str(git_err),
                            )

                        # Push if this repo tracks a remote branch and a
                        # worktree was materialised. Mirrors the inline
                        # path's guard at pipelines.py:16044 — pushing from
                        # the orchestrator's main repo would target the
                        # wrong working tree.
                        if pipeline.branch and worktree_repo_path != repo_path:
                            try:
                                _spawner = _pkg._get_spawner()
                                _spawner.gateway.push_worktree_branch(
                                    pipeline_id=pipeline_id,
                                    repo_path=str(worktree_repo_path),
                                    branch=pipeline.branch,
                                    mode=_gw_mode,
                                    base_branch=pipeline.base_branch,
                                )
                            except Exception as push_err:
                                _pkg.logger.warning(
                                    "Failed to push statefiles after phase gate resolution (continuing)",
                                    pipeline_id=pipeline_id,
                                    error=str(push_err),
                                )

                    from routes.phases import PHASE_TRANSITIONS

                    transitions = PHASE_TRANSITIONS
                    current_phase = pipeline.current_phase
                    # Issue #1557 — route epic pipelines through APPLY
                    # between PLAN and IMPLEMENT.  Non-epic pipelines
                    # see the default transition unchanged.
                    next_phases = _pkg._next_phases_for_epic(
                        pipeline,
                        current_phase,
                        transitions.get(current_phase, []),
                    )
                    # #2593 — populate contract from the plan draft when
                    # the HITL recovery is advancing the pipeline out
                    # of the plan phase.  Without this, contract.pr is
                    # empty (so the PR phase falls back to placeholder
                    # title/body and the context PR hook short-circuits
                    # on "contract has no pr block"), and the slice
                    # stack ends up rooted on ``/work`` with no PR to
                    # ``main`` — exactly the symptom reported on the
                    # in-flight #2474 pipeline.  Mirrors the plan-exit
                    # logic in ``advance_phase`` (routes/phases.py)
                    # and the auto-advance path in ``_run_pipeline``.
                    # Best-effort: failures warn and continue so a
                    # transient infra problem cannot strand the HITL
                    # recovery.  The actual context-PR open is
                    # deferred until after the lock is released
                    # (#2593 review issue 1) so the multi-second
                    # gateway sequence does not extend the
                    # per-pipeline state lock's hold time.
                    _next_phase_peek = next_phases[0] if next_phases else None
                    if (
                        current_phase == _pkg.PipelinePhase.PLAN
                        and _next_phase_peek == _pkg.PipelinePhase.IMPLEMENT
                    ):
                        _hitl_worktree_path = _pkg._resolve_pipeline_worktree_path(
                            pipeline, repo_path
                        )
                        try:
                            _pipeline_mode = pipeline.mode.value if pipeline.mode else "issue"
                            _hitl_populate_result = _pkg._populate_contract_from_plan_safe(
                                _hitl_worktree_path,
                                pipeline_id,
                                _pipeline_mode,
                                pipeline.issue_number,
                                source="hitl_plan_gate_approval",
                            )
                            # #1941: HITL plan-gate approval is a recovery
                            # hammer like force-advance — blocking it on a
                            # populate failure defeats the purpose.  We log
                            # the structured outcome but never raise.
                            if _hitl_populate_result.outcome != _pkg.PopulateOutcome.POPULATED:
                                _pkg.logger.warning(
                                    "HITL plan-gate approval populate produced non-POPULATED outcome",
                                    pipeline_id=pipeline_id,
                                    outcome=_hitl_populate_result.outcome.value,
                                )
                            try:
                                _pkg._commit_statefiles_to_worktree(
                                    _hitl_worktree_path,
                                    "Populate contract from plan on HITL plan-gate approval",
                                    pipeline_identifier=_pkg._pipeline_identifier(
                                        pipeline.issue_number, pipeline_id
                                    ),
                                    pipeline_id=pipeline_id,
                                )
                            except Exception as _hitl_commit_err:  # noqa: BLE001
                                _pkg.logger.warning(
                                    "Failed to commit populated contract on HITL plan-gate approval (continuing) (#2593)",
                                    pipeline_id=pipeline_id,
                                    error=str(_hitl_commit_err),
                                )

                            # #2593 review issue 5 — the earlier
                            # ``push_worktree_branch`` at line ~20598
                            # ran *before* this populate commit, so
                            # the populated ``contract.pr`` only
                            # exists locally until the IMPLEMENT
                            # phase's next phase-boundary sync.  Push
                            # again now so any slice-agent container
                            # that materialises a fresh worktree from
                            # origin before that sync still sees
                            # ``contract.pr``.  Mirrors the
                            # auto-advance flow's pre-context-PR push
                            # in ``_run_pipeline``.
                            if pipeline.branch and _hitl_worktree_path != repo_path:
                                try:
                                    _pkg._get_spawner().gateway.push_worktree_branch(
                                        pipeline_id=pipeline_id,
                                        repo_path=str(_hitl_worktree_path),
                                        branch=pipeline.branch,
                                        mode=_gw_mode,
                                        base_branch=pipeline.base_branch,
                                    )
                                except Exception as _hitl_push_err:  # noqa: BLE001
                                    _pkg.logger.warning(
                                        "Failed to push populated contract on HITL plan-gate approval (continuing) (#2593)",
                                        pipeline_id=pipeline_id,
                                        error=str(_hitl_push_err),
                                    )
                        except Exception as _hitl_pop_err:  # noqa: BLE001
                            _pkg.logger.warning(
                                "Failed to run plan-exit populate on HITL recovery (continuing) (#2593)",
                                pipeline_id=pipeline_id,
                                error=str(_hitl_pop_err),
                            )

                        # Defer the context-PR open until after the
                        # per-pipeline state lock is released — see
                        # ``_open_context_pr_at_implement_start``'s
                        # idempotency docstring on why this multi-
                        # second network sequence (one ``gh pr list``
                        # + maybe one ``gh pr create``) must not run
                        # under the lock (#2593 review issue 1).
                        _hitl_open_context_pr_after_lock = True
                        _hitl_pr_worktree_path = _hitl_worktree_path

                    if not next_phases:
                        # Terminal phase — pipeline complete.
                        # Bump run_epoch so any lingering old _run_pipeline
                        # thread (e.g. stuck in its finally block) detects the
                        # recreation and exits without double-cleaning up.
                        pipeline.status = _pkg.PipelineStatus.COMPLETE
                        pipeline.run_epoch = _pkg.datetime.now(_pkg.UTC)
                        store.save_pipeline(pipeline)
                        return _pkg.make_success_response(
                            "Pipeline recovered and completed",
                            data={
                                "pipeline_id": pipeline_id,
                                "status": "complete",
                                "current_phase": pipeline.current_phase.value,
                            },
                        )

                    # Advance to next phase
                    next_phase = next_phases[0]
                    pipeline.current_phase = next_phase

                    # Issue #1557: PLAN → APPLY transition on epic
                    # pipelines (mirrors auto-advance path).  Write the
                    # applier handoff JSON before the next _run_pipeline
                    # thread is respawned so the APPLIER container's
                    # first read finds it on disk.
                    if (
                        getattr(pipeline, "is_epic", False)
                        and current_phase == _pkg.PipelinePhase.PLAN
                        and next_phase == _pkg.PipelinePhase.APPLY
                    ):
                        _hitl_apply_worktree = _pkg._resolve_pipeline_worktree_path(
                            pipeline, repo_path
                        )
                        _pkg._write_apply_phase_handoff(
                            pipeline,
                            _hitl_apply_worktree,
                            approved_phase="plan",
                        )

                    # Issue #1557 task-2-7: when the resolved phase was
                    # APPLY (BRC consensus confirmed via HITL recovery
                    # path), drain the Won't-Do handoff before advancing.
                    if current_phase == _pkg.PipelinePhase.APPLY:
                        _hitl_drain_worktree = _pkg._resolve_pipeline_worktree_path(
                            pipeline, repo_path
                        )
                        _pkg._drain_wontdo_batch_after_apply(pipeline, _hitl_drain_worktree)

                    # Update health monitor phase threshold before agents spawn
                    try:
                        from health_monitor import get_health_monitor

                        _hm_instance = get_health_monitor()
                        if _hm_instance is not None:
                            _hm_instance.set_current_phase(next_phase.value)
                    except ImportError:
                        pass

                else:
                    # request_changes/change_approach — reset phase for re-run
                    phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                    # #2795: derive iteration_n monotonically. The
                    # ``max(len(iteration_history), max(directive_idx) + 1)``
                    # form does not depend on ``hitl_review_cycles``, so
                    # this expression is safe to evaluate either before
                    # or after ``_clear_concurrent_state`` resets the
                    # per-phase counter. What *is* order-sensitive is
                    # the tracker snapshot a few lines below: the BRC
                    # tracker is in-memory only and gets wiped by
                    # ``_clear_concurrent_state``, so the snapshot MUST
                    # happen first. On a crash-recovery resolution the
                    # snapshot will typically have empty verdict detail,
                    # but the iteration index + artifacts are still
                    # useful context for iteration N+1's prompts.
                    # The ``max(...) + 1`` floor ensures a legacy-
                    # hitl_feedback migration (which synthesises a
                    # directive but leaves iteration_history empty)
                    # doesn't restart the index at 0.
                    _recovery_iteration_n = max(
                        len(phase_execution.iteration_history),
                        max(
                            (d.iteration_n for d in phase_execution.operator_directives),
                            default=-1,
                        )
                        + 1,
                    )
                    _recovery_tracker = None
                    try:
                        from peer_consensus import (
                            get_peer_consensus_tracker as _gpct_recovery,
                        )

                        _recovery_tracker = _gpct_recovery(pipeline_id)
                    except Exception as tracker_err:  # noqa: BLE001
                        _pkg.logger.debug(
                            "Tracker lookup failed during recovery snapshot",
                            pipeline_id=pipeline_id,
                            error=str(tracker_err),
                        )
                    _recovery_summary = _pkg._build_iteration_summary_from_tracker(
                        _recovery_tracker,
                        iteration_n=_recovery_iteration_n,
                        artifacts=phase_execution.artifacts,
                    )

                    if phase_execution.status in (
                        _pkg.PipelineStatus.COMPLETE,
                        _pkg.PipelineStatus.FAILED,
                        _pkg.PipelineStatus.RUNNING,
                        _pkg.PipelineStatus.AWAITING_HUMAN,
                    ):
                        # Refuse to clear containers/agents/artifacts when
                        # pods labeled to this pipeline are still alive —
                        # the reset would orphan them (#2420).
                        guard = _pkg._guard_live_pods_or_force(pipeline_id, force, force_reason)
                        if guard is not None:
                            return guard
                        phase_execution.status = _pkg.PipelineStatus.PENDING
                        phase_execution.started_at = None
                        phase_execution.work_started_at = None
                        phase_execution.completed_at = None
                        phase_execution.error = None
                        phase_execution.review_cycles = 0
                        phase_execution.hitl_review_cycles = 0
                        phase_execution.containers = []
                        phase_execution.agents = []
                        phase_execution.artifacts = {}

                    # Clear stale consensus state so re-run doesn't
                    # short-circuit (issue #1296).
                    from routes.phases import _clear_concurrent_state

                    _clear_concurrent_state(pipeline_id)

                    # #2795: append the operator directive + iteration
                    # summary so iteration N+1 prompts can render them
                    # with precedence prose. Both lists accumulate
                    # across kickbacks (no clear).
                    if revision_feedback:
                        phase_execution.operator_directives.append(
                            _pkg.OperatorDirective(
                                iteration_n=_recovery_iteration_n,
                                feedback_text=revision_feedback,
                            )
                        )
                        phase_execution.iteration_history.append(_recovery_summary)

                pipeline.error = None
                pipeline.run_epoch = _pkg.datetime.now(_pkg.UTC)
                pipeline.status = _pkg.PipelineStatus.RUNNING
                store.save_pipeline(pipeline)

            # TEST_MARKER: recover_advance_clear (load-bearing: brackets
            # the post-lock clear for TestRecoverPipelineClearsConcurrentState;
            # do not remove without updating that test class).
            # Drop the previous phase's in-memory consensus tracker on
            # cross-phase advance (#2502).  The request_changes /
            # change_approach branch above already cleared inside the
            # lock for same-phase re-runs (#1296); the advance branch
            # needs its own post-lock clear so persisted state lands
            # before the tracker is wiped, matching the persist-then-
            # clear-then-spawn order used by ``advance_phase`` and the
            # auto-advance block.
            if is_approved:
                from routes.phases import _clear_concurrent_state

                _clear_concurrent_state(pipeline_id)

                # #3521: advance contract.current_phase in lockstep with
                # the pipeline record on the HITL-recovery advance (this
                # path does NOT route through advance_phase REST; the
                # runner thread is spawned inline below). Best-effort +
                # forward-only; never raises.
                _pkg._sync_contract_phase_to_pipeline(
                    pipeline,
                    _pkg._resolve_pipeline_worktree_path(pipeline, repo_path),
                    source="hitl_recovery_advance",
                )

            # #2593 review issue 1 — context-PR open moved out of the
            # per-pipeline state lock so the multi-second gateway
            # sequence does not hold the lock and block concurrent
            # ``advance_phase`` / status reads.
            #
            # #2777 (cq-4, TASK-1-2) — HITL-recovery context-PR site
            # calls the new idempotent
            # ``_open_context_pr_at_implement_start`` opener directly.
            # HITL recovery in ``start_pipeline`` does NOT route
            # through ``advance_phase`` REST (the runner thread is
            # spawned inline below), so without this call site an
            # operator-resumed pipeline would silently strand its
            # slice stack on ``egg/<id>/work``. The opener's
            # ``gh pr list`` pre-flight makes a redundant call from a
            # later ``advance_phase`` invocation a one-round-trip
            # no-op (reviewer_code_holistic blocker 1 fix; v1 deleted
            # this site under the incorrect "single canonical site"
            # plan AC).
            if _hitl_open_context_pr_after_lock and _hitl_pr_worktree_path is not None:
                try:
                    _pkg._open_context_pr_at_implement_start(pipeline_id, repo_path=repo_path)
                except _pkg.ContextPrCreationError as ctx_err:
                    _pkg.logger.warning(
                        "Context PR opener: HITL-resume failed "
                        "(continuing — hard-require enforced at "
                        "advance_phase and the implement-start plan "
                        "pre-flight gate) (#2777, #3100)",
                        pipeline_id=pipeline_id,
                        reason=ctx_err.reason,
                        error=str(ctx_err),
                    )
                except Exception as hitl_err:  # noqa: BLE001
                    _pkg.logger.warning(
                        "Context PR opener: HITL-resume outer wrapper raised (continuing) (#2777)",
                        pipeline_id=pipeline_id,
                        error=str(hitl_err),
                    )

            # Launch runner thread
            thread = _pkg.threading.Thread(
                target=_pkg._run_pipeline,
                args=(pipeline_id, repo_path),
                daemon=True,
                name=f"pipeline-{pipeline_id}",
            )
            thread.start()

            _pkg.logger.info(
                "Pipeline recovered from AWAITING_HUMAN",
                pipeline_id=pipeline_id,
                recovery_action="advance" if is_approved else "rerun",
            )

            return _pkg.make_success_response(
                "Pipeline recovered and started",
                data={
                    "pipeline_id": pipeline_id,
                    "status": "running",
                    "current_phase": pipeline.current_phase.value,
                },
            )

        if pipeline.status == _pkg.PipelineStatus.COMPLETE:
            return _pkg.make_error_response(
                f"Pipeline {pipeline_id} is already complete",
                status_code=409,
            )

        if pipeline.status == _pkg.PipelineStatus.CANCELLED:
            return _pkg.make_error_response(
                f"Pipeline {pipeline_id} is cancelled",
                status_code=409,
            )

        with _pkg.get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            if pipeline.status == _pkg.PipelineStatus.FAILED:
                # Reset the failed phase so it can be re-run.
                # Also reset phases stuck in RUNNING — a pipeline-level exception
                # sets the pipeline to FAILED without updating the phase status.
                phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                if phase_execution.status in (
                    _pkg.PipelineStatus.FAILED,
                    _pkg.PipelineStatus.RUNNING,
                ):
                    # Refuse to clear containers/agents/artifacts when pods
                    # labeled to this pipeline are still alive — the reset
                    # would orphan them (#2420).
                    guard = _pkg._guard_live_pods_or_force(pipeline_id, force, force_reason)
                    if guard is not None:
                        return guard
                    prev_status = phase_execution.status.value
                    phase_execution.status = _pkg.PipelineStatus.PENDING
                    phase_execution.started_at = None
                    phase_execution.work_started_at = None
                    phase_execution.completed_at = None
                    phase_execution.error = None
                    phase_execution.review_cycles = 0
                    phase_execution.hitl_review_cycles = 0
                    phase_execution.containers = []
                    phase_execution.agents = []
                    phase_execution.artifacts = {}
                    _pkg.logger.info(
                        "Resetting phase for restart",
                        pipeline_id=pipeline_id,
                        phase=pipeline.current_phase.value,
                        previous_phase_status=prev_status,
                    )
                pipeline.error = None

                # Bump run_epoch so the old _run_pipeline thread's finally block
                # detects the restart and skips worktree cleanup.
                pipeline.run_epoch = _pkg.datetime.now(_pkg.UTC)

            # Mark pipeline as running
            pipeline.status = _pkg.PipelineStatus.RUNNING
            store.save_pipeline(pipeline)

        # Run the pipeline in a background thread
        thread = _pkg.threading.Thread(
            target=_pkg._run_pipeline,
            args=(pipeline_id, repo_path),
            daemon=True,
            name=f"pipeline-{pipeline_id}",
        )
        thread.start()

        _pkg.logger.info("Pipeline started", pipeline_id=pipeline_id)

        return _pkg.make_success_response(
            "Pipeline started",
            data={
                "pipeline_id": pipeline_id,
                "status": "running",
                "current_phase": pipeline.current_phase.value,
            },
        )

    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
