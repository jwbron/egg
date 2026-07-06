"""run_pipeline per-phase execution loop block helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _run_phase_execution(
    pipeline,
    phase_execution,
    phase_failed,
    *,
    certs_volume,
    current_phase,
    gateway_mode,
    pipeline_id,
    pipeline_mode,
    repo_volumes,
    repos,
    run_epoch,
    sandbox_env,
    spawner,
    store,
    worktree_repo_path,
):
    """Run one phase (spawn agents / BRC) — the while-loop's phase-execution
    block, extracted verbatim from _run_pipeline. Returns (pipeline,
    phase_execution, phase_failed, action); action in {None, 'return',
    'break'} tells the thin loop whether to return / break / fall through."""
    if True:
        while True:
            # Reset tester gaps each cycle so stale findings don't accumulate
            tester_gap_summary = None

            # Reload to get latest review_cycles count
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                review_cycle = phase_execution.review_cycles

                # Reset status to RUNNING at cycle start so that a
                # previous cycle's FAILED status doesn't persist and
                # cause _derive_subphase_status() to misreport (see
                # issue #1178).
                phase_execution.status = _pkg.PipelineStatus.RUNNING
                pipeline.status = _pkg.PipelineStatus.RUNNING

                # Record when actual agent work begins (excludes sandbox setup
                # and HITL waiting time from the phase duration).
                phase_execution.work_started_at = _pkg.datetime.now(_pkg.UTC)

                # Capture HEAD commit for delta reviews: reviewers in
                # subsequent cycles can diff against this to see only
                # the changes made since the last review.
                cycle_commit_sha: str | None = None
                try:
                    _git_result = _pkg.subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        capture_output=True,
                        text=True,
                        cwd=str(worktree_repo_path),
                        timeout=10,
                    )
                    if _git_result.returncode == 0:
                        cycle_commit_sha = _git_result.stdout.strip()
                except Exception:
                    pass  # Non-fatal — delta review is best-effort

                phase_execution.cycle_timings.append(
                    _pkg.CycleTiming(
                        cycle=review_cycle,
                        started_at=phase_execution.work_started_at,
                        commit_sha=cycle_commit_sha,
                    )
                )
                store.save_pipeline(pipeline)

            # 1. Spawn workers — always use concurrent BRC execution.
            _pkg.logger.info(
                "Spawning concurrent phase execution",
                pipeline_id=pipeline_id,
                phase=current_phase,
                review_cycle=review_cycle,
                mode=gateway_mode,
            )

            # Read structured operator directives + prior iteration
            # history off the phase so iteration N+1 prompts can render
            # them with precedence prose (#2795). These lists accumulate
            # across kickbacks and are never cleared, so no read-and-
            # clear stash is needed.
            _phase_operator_directives: list[_pkg.OperatorDirective] = []
            _phase_iteration_history: list[_pkg.IterationSummary] = []
            try:
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    _fb_pipeline = store.load_pipeline(pipeline_id)
                    _fb_phase = _fb_pipeline.get_phase_execution(current_phase)
                    _phase_operator_directives = list(_fb_phase.operator_directives)
                    _phase_iteration_history = list(_fb_phase.iteration_history)
            except Exception as e:
                _pkg.logger.debug("Failed to read operator directives for phase", error=str(e))

            # #2137: route the implement phase through the slice
            # DAG iterator when the contract has more than one
            # slice. Single-slice and no-slice contracts continue
            # to use the legacy monolithic path so existing
            # pipelines are unaffected.
            _use_slice_loop = False
            _slice_gate_failure: _pkg.SliceGateMonolithicBlock | None = None
            if current_phase.value == "implement":
                try:
                    from egg_contracts.loader import (
                        load_contract as _load_contract_for_slice_check,
                    )

                    _check_contract = _load_contract_for_slice_check(
                        pipeline_id, worktree_repo_path
                    )
                    _slice_count = len(getattr(_check_contract, "slices", []) or [])
                    # #2777 cq-10 — route through ``_is_slice_dag_mode``
                    # so the "what counts as slice-DAG" definition has
                    # a single source of truth. Local ``_slice_count``
                    # is still used by the defensive recheck below for
                    # the structured log when the populator dropped
                    # slices (#2337).
                    _use_slice_loop = _pkg._is_slice_dag_mode(_check_contract)

                    # #2915: Auto-populate contract if empty at implement start
                    # This fills the gap where start_phase=implement doesn't trigger
                    # the plan-completion populate path, leaving agents with nothing to do.
                    if _slice_count == 0:
                        _slice_count = _pkg._auto_populate_contract_at_implement_start(
                            worktree_repo_path,
                            pipeline_id,
                            pipeline_mode,
                            pipeline.issue_number,
                            pipeline.current_phase,
                            pipeline.branch,
                            gateway=spawner.gateway,
                            gateway_mode=gateway_mode,
                            base_branch=pipeline.base_branch,
                        )
                        if _slice_count > 0:
                            # Reload contract after successful populate
                            _check_contract = _load_contract_for_slice_check(
                                pipeline_id, worktree_repo_path
                            )
                            _use_slice_loop = _pkg._is_slice_dag_mode(_check_contract)

                    # #2337 defensive recheck: if the contract has no
                    # slices but the on-disk plan draft parses to N>1
                    # slices, the populator silently failed earlier.
                    # Refuse to demote to monolithic.
                    if _slice_count == 0:
                        _slice_gate_failure = _pkg._slice_gate_block_monolithic_demotion(
                            worktree_repo_path,
                            pipeline_id,
                            pipeline.issue_number,
                        )
                except Exception as _slice_check_err:  # noqa: BLE001
                    _pkg.logger.debug(
                        "Slice-loop gate: contract load failed, falling back to monolithic",
                        pipeline_id=pipeline_id,
                        error=str(_slice_check_err),
                    )

            if _slice_gate_failure is not None:
                _slice_gate_msg = _slice_gate_failure.message
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    if phase_execution.cycle_timings:
                        phase_execution.cycle_timings[-1].completed_at = _pkg.datetime.now(_pkg.UTC)
                    phase_execution.status = _pkg.PipelineStatus.FAILED
                    phase_execution.error = _slice_gate_msg
                    phase_execution.completed_at = _pkg.datetime.now(_pkg.UTC)
                    pipeline.status = _pkg.PipelineStatus.FAILED
                    pipeline.error = _slice_gate_msg
                    store.save_pipeline(pipeline)
                # #2627 follow-up: emit a dedicated HITL naming the
                # empty-contract root cause inline.  The generic
                # post-failure Retry/Accept/Abort decision respawns
                # implement into the same empty-contract state; this
                # HITL's options map to repopulate / restart-plan /
                # abort so the operator has a recovery path that
                # actually changes state.
                _pkg._emit_empty_contract_hitl(
                    pipeline_id,
                    pipeline,
                    store,
                    reason="slice_gate_blocked_monolithic_demotion",
                    draft_slice_count=_slice_gate_failure.draft_slice_count,
                    gate="slice_gate",
                    phase=current_phase,
                )
                _pkg.logger.error(
                    "OVERSEER_ALERT slice_gate_blocked_monolithic_demotion",
                    pipeline_id=pipeline_id,
                    error=_slice_gate_msg,
                    draft_slice_count=_slice_gate_failure.draft_slice_count,
                )
                phase_failed = True
                break

            try:
                if _use_slice_loop:
                    exit_code, container_logs = _pkg._run_implement_phase_slices(
                        pipeline_id=pipeline_id,
                        pipeline=pipeline,
                        spawner=spawner,
                        repo_volumes=repo_volumes,
                        gateway_mode=gateway_mode,
                        repos=repos,
                        sandbox_env=sandbox_env,
                        store=store,
                        certs_volume=certs_volume,
                        worktree_repo_path=worktree_repo_path,
                        run_epoch=run_epoch,
                    )
                else:
                    # Pre-#2137 monolithic-implement fallback. The
                    # impasse-retry wrapper deliberately wraps only
                    # the slice-loop call site (#2529): impasse
                    # delegation rewires a *task* between producer
                    # roles, which only makes sense per-slice.
                    # Pipelines that don't use the slice loop are
                    # legacy / single-PR-shape, so an impasse here
                    # surfaces as a normal slice failure and the
                    # operator handles it via the existing
                    # phase-failure HITL path.
                    exit_code, container_logs = _pkg._run_concurrent_phase(
                        pipeline_id=pipeline_id,
                        pipeline=pipeline,
                        phase=current_phase,
                        spawner=spawner,
                        repo_volumes=repo_volumes,
                        gateway_mode=gateway_mode,
                        repos=repos,
                        sandbox_env=sandbox_env,
                        store=store,
                        certs_volume=certs_volume,
                        worktree_repo_path=worktree_repo_path,
                        operator_directives=_phase_operator_directives,
                        iteration_history=_phase_iteration_history,
                        run_epoch=run_epoch,
                    )
            except (_pkg.ContainerSpawnError, _pkg.KubernetesSpawnError) as e:
                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    if phase_execution.cycle_timings:
                        phase_execution.cycle_timings[-1].completed_at = _pkg.datetime.now(_pkg.UTC)
                    phase_execution.status = _pkg.PipelineStatus.FAILED
                    phase_execution.error = str(e)
                    phase_execution.completed_at = _pkg.datetime.now(_pkg.UTC)
                    pipeline.status = _pkg.PipelineStatus.FAILED
                    pipeline.error = str(e)
                    store.save_pipeline(pipeline)
                _pkg.logger.error(
                    "Failed to spawn concurrent containers",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
                phase_failed = True
                break

            if exit_code != 0:
                # Check if pipeline was restarted while this thread
                # was running (e.g. restart_phase bumped run_epoch).
                # If so, a new _run_pipeline thread owns this pipeline
                # — exit without marking the phase FAILED.  See #1638.
                _check_pip = store.load_pipeline(pipeline_id)
                _check_epoch = _check_pip.run_epoch or _check_pip.created_at
                if _check_epoch != run_epoch:
                    _pkg.logger.info(
                        "Pipeline was restarted during phase execution, exiting old thread",
                        pipeline_id=pipeline_id,
                    )
                    return pipeline, phase_execution, phase_failed, "return"

                error_msg = f"Container exited with code {exit_code}"
                if container_logs:
                    log_lines = container_logs.strip().splitlines()
                    tail = "\n".join(log_lines[-10:])
                    error_msg += f"\n--- container logs (last 10 lines) ---\n{tail}"

                with _pkg.get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    if phase_execution.cycle_timings:
                        phase_execution.cycle_timings[-1].completed_at = _pkg.datetime.now(_pkg.UTC)
                    phase_execution.status = _pkg.PipelineStatus.FAILED
                    phase_execution.error = error_msg
                    phase_execution.completed_at = _pkg.datetime.now(_pkg.UTC)
                    pipeline.status = _pkg.PipelineStatus.FAILED
                    pipeline.error = error_msg
                    store.save_pipeline(pipeline)
                _pkg.logger.error(
                    "Phase failed",
                    pipeline_id=pipeline_id,
                    phase=current_phase,
                    exit_code=exit_code,
                    container_logs=container_logs[-2000:] if container_logs else "",
                )
                phase_failed = True
                break

            # 2. Read tester gap findings (concurrent phases include a tester).
            # Only read when the phase succeeded — a failed phase may
            # have left stale output from a previous cycle on disk.
            if not phase_failed:
                tester_gap_summary = _pkg._read_tester_gaps(
                    worktree_repo_path,
                    identifier=_pkg._pipeline_identifier(pipeline.issue_number, pipeline_id),
                )
                if tester_gap_summary:
                    _pkg.logger.info(
                        "Tester found gaps",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                    )

            # Reviewers are handled within the BRC consensus protocol
            # (see issue #1178) — advance to next phase.
            break
    return pipeline, phase_execution, phase_failed, None
