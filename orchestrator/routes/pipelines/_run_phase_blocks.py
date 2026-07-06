"""run_pipeline per-phase advance loop blocks helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _run_implement_advance(
    pipeline,
    *,
    current_phase,
    gateway_mode,
    pipeline_id,
    repo_path,
    spawner,
    store,
    worktree_repo_path,
):
    """IMPLEMENT-phase advance loop block (extracted verbatim; pure fall-through)."""
    if current_phase == _pkg.PipelinePhase.IMPLEMENT:
        try:
            gap_gated = _pkg._await_unresolved_gap_gate(
                store,
                pipeline_id,
                repo_path,
                worktree_repo_path,
                _pkg._pipeline_identifier(pipeline.issue_number, pipeline_id),
                current_phase,
                pipeline.config.hitl_gates,
            )
            pipeline = store.load_pipeline(pipeline_id)
            # The gate ran after the statefile commit+push above, so
            # when it changed the contract (operator resolved a gap,
            # or the override audit landed) the resolution is still
            # uncommitted in the worktree. Re-commit + push so the
            # work branch tree CI sees reflects the post-gate
            # contract, not the open-gap snapshot pushed earlier.
            if gap_gated:
                gate_committed = False
                try:
                    gate_committed = _pkg._commit_statefiles_to_worktree(
                        worktree_repo_path,
                        f"Persist contract after {current_phase.value} gap gate",
                        pipeline_identifier=_pkg._pipeline_identifier(
                            pipeline.issue_number, pipeline_id
                        ),
                        pipeline_id=pipeline_id,
                    )
                except Exception as git_err:
                    _pkg.logger.warning(
                        "Failed to commit statefiles after gap gate (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        error=str(git_err),
                    )
                # Skip the follow-up push when nothing was committed
                # (e.g. the override path leaves the contract
                # unchanged) — it would be a no-op fast-forward
                # (#2548).
                if gate_committed and pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                            mode=gateway_mode,
                            base_branch=pipeline.base_branch,
                        )
                    except Exception as push_err:
                        _pkg.logger.warning(
                            "Failed to push statefiles after gap gate (continuing)",
                            pipeline_id=pipeline_id,
                            phase=current_phase.value,
                            error=str(push_err),
                        )
        except Exception as gap_gate_err:  # noqa: BLE001
            # Never let a gate bug strand the pipeline — the
            # reactive test_models_gaps.py CI check remains the
            # backstop if this fails open.
            _pkg.logger.warning(
                "Unresolved-gap gate raised (continuing)",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
                error=str(gap_gate_err),
            )
    return pipeline


def _run_plan_advance(
    pipeline,
    phase_overseer_active,
    *,
    current_phase,
    gateway_mode,
    overseer_container_id,
    overseer_lock,
    pipeline_id,
    pipeline_mode,
    repo_path,
    spawner,
    store,
    worktree_repo_path,
):
    """Plan-phase populate/advance loop block (extracted verbatim)."""
    if current_phase.value == "plan":
        try:
            _plan_complete_populate_result = _pkg._populate_contract_from_plan_safe(
                worktree_repo_path,
                pipeline_id,
                pipeline_mode,
                pipeline.issue_number,
                source="plan_complete",
                branch=pipeline.branch,
            )
            # #2627 follow-up: populate-succeeded-but-empty is the
            # orthogonal failure mode flagged in the issue.  The
            # draft existed (so neither PlanDraftMissing variant
            # fired) but the populator did not produce a contract
            # with tasks the implement-phase agents can act on.
            # Synthesize a raise so the same FAILED-cleanup
            # handler below runs.
            #
            # Routes through
            # :func:`_populate_result_is_empty_contract` so the two
            # empty-contract call sites (this handler and the
            # ``start_phase=implement`` safety net) can't drift out
            # of agreement.  This widens the original
            # ``EMPTY_RESULT`` / ``PARSE_FAILED`` check to cover
            # every non-success outcome plus the POPULATED-with-no-
            # slices case (#2627 review).
            if _pkg._populate_result_is_empty_contract(_plan_complete_populate_result):
                # Pre-raise OVERSEER_ALERT mirroring the two
                # ``PlanDraftMissing*`` wrapper-side emits at
                # :func:`_populate_contract_from_plan_safe` so the
                # discriminator the FAILED-cleanup logger uses
                # (``OVERSEER_ALERT plan_populate_produced_empty_contract``)
                # is also emitted before the raise.  Without this
                # the third fail-loud branch had no pre-raise log
                # while the two draft-missing branches did,
                # asymmetric audit (#2627 review).
                _pkg.logger.error(
                    "OVERSEER_ALERT plan_populate_produced_empty_contract",
                    pipeline_id=pipeline_id,
                    branch=pipeline.branch,
                    outcome=_plan_complete_populate_result.outcome.value,
                    slice_count=_plan_complete_populate_result.slice_count,
                    note=(
                        "plan populate did not produce a contract with "
                        "tasks the implement-phase agents can act on; "
                        "blocking phase advance (#2627)"
                    ),
                )
                raise _pkg.PopulateProducedEmptyContractError(
                    _plan_complete_populate_result.outcome,
                    slice_count=_plan_complete_populate_result.slice_count,
                )
        except (
            _pkg.PlanDraftMissingOnLocalError,
            _pkg.PlanDraftMissingOnLocalAndOriginError,
            _pkg.PopulateProducedEmptyContractError,
        ) as missing_err:
            # Mirror the slice-gate failure handler at the
            # implement-phase entry: mark FAILED in state,
            # then run the same cleanup sequence as the
            # ``if phase_failed:`` block above (teardown phase
            # overseer, report pipeline status, best-effort push
            # for backup) so both load-bearing failure paths
            # have a uniform cleanup story.  Re #2337 / #2627
            # reviews.
            teardown_reason, log_event = _pkg._empty_contract_failure_metadata(missing_err)
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = _pkg.PipelineStatus.FAILED
                phase_execution.error = str(missing_err)
                phase_execution.completed_at = _pkg.datetime.now(_pkg.UTC)
                pipeline.status = _pkg.PipelineStatus.FAILED
                pipeline.error = str(missing_err)
                store.save_pipeline(pipeline)
            # #2627 follow-up: emit the dedicated empty-contract
            # HITL so the operator sees an actionable decision
            # (repopulate / restart-plan / abort) inline with the
            # FAILED status, instead of having to dig through
            # pipeline.error and the generic consensus-timeout
            # decision.
            _hitl_reason = _pkg._empty_contract_hitl_reason(missing_err)
            _pkg._emit_empty_contract_hitl(
                pipeline_id,
                pipeline,
                store,
                reason=_hitl_reason,
                draft_slice_count=None,
                gate="plan_complete",
                phase=current_phase,
            )
            _pkg.logger.error(
                log_event,
                pipeline_id=pipeline_id,
                error=str(missing_err),
            )
            # Stop the phase-scoped overseer on failure.
            # Hold the lock to prevent the poll thread from seeing
            # the container as EXITED and respawning it.
            with overseer_lock:
                if overseer_container_id and phase_overseer_active:
                    phase_overseer_active = False
                    _pkg._teardown_phase_overseer(
                        spawner,
                        overseer_container_id,
                        pipeline_id,
                        phase_label=str(current_phase),
                        reason=teardown_reason,
                    )
            _pkg.report_pipeline_status(
                pipeline,
                event_type="pipeline.failed",
                message=f"Pipeline failed: {(pipeline.error or 'unknown')[:100]}",
            )
            _pkg._emit_pipeline_event(pipeline, "pipeline.failed")
            # Best-effort: push worktree branch to remote so work
            # is backed up before the pipeline exits.
            if pipeline.branch and worktree_repo_path != repo_path:
                try:
                    spawner.gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_repo_path),
                        branch=pipeline.branch,
                        mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                    )
                except Exception as push_err:
                    _pkg.logger.warning(
                        "Best-effort push on failure failed",
                        pipeline_id=pipeline_id,
                        error=str(push_err),
                    )
            return pipeline, phase_overseer_active, "break"
    return pipeline, phase_overseer_active, None


def _run_pending_phase_init(
    pipeline,
    phase_execution,
    *,
    current_phase,
    pipeline_id,
    repo_path,
    store,
    worktree_repo_path,
):
    """PENDING-phase init loop block (extracted verbatim; pure fall-through)."""
    if phase_execution.status == _pkg.PipelineStatus.PENDING:
        # Record branch tip SHA for completion signal verification.
        # This allows the completion handler to detect if a commit
        # was pushed to a different branch than expected.
        # NOTE: Intentional TOCTOU — the SHA is captured before
        # acquiring the state lock, so a push between rev-parse and
        # lock acquisition could make it stale.  Acceptable because
        # phase_start_sha is only used for advisory "no new commits"
        # logging, not for correctness decisions.
        phase_start_sha: str | None = None
        try:
            _sha_result = _pkg.subprocess.run(
                ["git", "rev-parse", f"origin/{pipeline.branch}"],
                capture_output=True,
                text=True,
                cwd=str(worktree_repo_path),
                timeout=10,
                check=False,
            )
            if _sha_result.returncode == 0:
                phase_start_sha = _sha_result.stdout.strip()
        except Exception:
            pass  # Non-fatal — verification is best-effort

        with _pkg.get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)
            phase_execution = pipeline.get_phase_execution(current_phase)
            phase_execution.status = _pkg.PipelineStatus.RUNNING
            phase_execution.started_at = _pkg.datetime.now(_pkg.UTC)
            phase_execution.phase_start_sha = phase_start_sha
            pipeline.status = _pkg.PipelineStatus.RUNNING
            store.save_pipeline(pipeline)

        # Report phase start to collaborator
        _pkg.report_pipeline_status(
            pipeline,
            event_type="phase.started",
            message=f"Phase {current_phase.value} started",
        )
        _pkg._emit_pipeline_event(pipeline, "phase.started")

        # #2777 (cq-4, TASK-1-2) — implement-phase entry
        # backstop. Calls the new
        # ``_open_context_pr_at_implement_start`` opener for
        # the runner-driven paths that bypass
        # ``advance_phase`` REST (inline ``_run_pipeline``
        # auto-advance and the HITL-approval recovery in
        # ``start_pipeline`` both leave
        # ``phase_execution.status`` as PENDING and spawn the
        # runner directly; the backstop catches both per
        # #2593). The opener is idempotent so re-firing here
        # after a successful advance_phase call is a one-
        # round-trip ``gh pr list`` no-op.
        #
        # reviewer_code_holistic blocker 1 fix: v1 deleted
        # this site under the (incorrect) "single canonical
        # site" plan AC; the four soft-fail call sites are in
        # fact the only context-PR opener calls on the
        # runner-driven paths, so the deletion silently
        # stranded slice stacks on ``egg/<id>/work``.
        # Restored under the new idempotent opener.
        if current_phase == _pkg.PipelinePhase.IMPLEMENT:
            try:
                _pkg._open_context_pr_at_implement_start(pipeline_id, repo_path=repo_path)
            except _pkg.ContextPrCreationError as ctx_err:
                _pkg.logger.warning(
                    "Context PR opener: implement-entry backstop "
                    "failed (continuing — hard-require enforced at "
                    "advance_phase and the implement-start plan "
                    "pre-flight gate) (#2777, #3100)",
                    pipeline_id=pipeline_id,
                    reason=ctx_err.reason,
                    error=str(ctx_err),
                )
            except Exception as backstop_err:  # noqa: BLE001
                _pkg.logger.warning(
                    "Context PR opener: implement-entry backstop "
                    "outer wrapper raised (continuing) (#2777)",
                    pipeline_id=pipeline_id,
                    error=str(backstop_err),
                )
    return pipeline, phase_execution
