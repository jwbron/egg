"""advance_phase: the plan-exit phase-transition endpoint and its
pre-state-lock validator / populate / context-PR-opener sequence
(#3312 decomposition).
"""

from datetime import UTC, datetime

import routes.phases as _pkg
from flask import Response, request
from models import PipelinePhase, PipelineStatus
from state_store import (
    InvalidPipelineIdError,
    PipelineNotFoundError,
    VersionConflictError,
)

from . import logger
from ._responses import make_error_response, make_success_response
from ._transitions import validate_phase_transition


def advance_phase(pipeline_id: str) -> tuple[Response, int]:
    """
    Advance pipeline to next phase.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "target_phase": "plan",  // required
            "force": false  // optional, skip validation
        }

    Response:
        {
            "success": true,
            "message": "Phase advanced to plan",
            "data": {
                "previous_phase": "refine",
                "current_phase": "plan"
            }
        }
    """
    # silent=True: tolerate empty body with Content-Type: application/json,
    # which would otherwise raise BadRequest(400) before reaching the
    # "Missing target_phase" error below. See #1787.
    data = request.get_json(silent=True) or {}

    target_phase_str = data.get("target_phase")
    if not target_phase_str:
        return make_error_response("Missing target_phase", reason="missing_target_phase")

    try:
        target_phase = PipelinePhase(target_phase_str)
    except ValueError:
        return make_error_response(
            f"Invalid phase: {target_phase_str}. Valid phases: {[p.value for p in PipelinePhase]}",
            reason="invalid_phase",
        )

    force = data.get("force", False)

    try:
        store, pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
        original_version = pipeline.version  # Capture version for optimistic locking

        previous_phase = pipeline.current_phase

        # Validate transition unless forced
        if not force:
            is_valid, error = validate_phase_transition(previous_phase, target_phase)
            if not is_valid:
                return make_error_response(
                    error, status_code=400, reason="invalid_phase_transition"
                )

            # Check if current phase is complete
            current_execution = pipeline.get_phase_execution(previous_phase)
            if current_execution.status not in (PipelineStatus.COMPLETE, PipelineStatus.PENDING):
                return make_error_response(
                    f"Current phase {previous_phase.value} is not complete "
                    f"(status: {current_execution.status.value})",
                    reason="previous_phase_not_complete",
                )

        # Gate phase advance on health checks.  Runs all Tier 1 and Tier 2
        # checks; if any returns FAIL_PIPELINE, the transition is blocked
        # with 409 Conflict so the caller can inspect health_results.
        # Skipped when force=true (escape hatch for stuck pipelines).
        # Health check errors degrade gracefully — the advance proceeds.
        if not force:
            try:
                from flask import current_app

                hc_runner = current_app.config.get("HEALTH_CHECK_RUNNER")
                if hc_runner is not None:
                    from health_checks.context import PipelineHealthContext
                    from health_checks.runner import worst_action
                    from health_checks.types import HealthAction, HealthTrigger

                    try:
                        from docker_client import get_docker_client

                        dc = get_docker_client()
                    except Exception:
                        dc = None

                    ctx = PipelineHealthContext(
                        pipeline=pipeline,
                        repo_path=store.repo_path,
                        trigger=HealthTrigger.PHASE_COMPLETE.value,
                        docker_client=dc,
                        state_store=store,
                    )
                    hc_results = hc_runner.run(ctx, HealthTrigger.PHASE_COMPLETE)
                    if worst_action(hc_results) == HealthAction.FAIL_PIPELINE:
                        return make_error_response(
                            "Health checks indicate pipeline should fail before advancing phase",
                            status_code=409,
                            details={
                                "health_results": [r.to_dict() for r in hc_results],
                            },
                            reason="health_checks_failed",
                        )
            except ImportError:
                pass  # Health check module not available — proceed without gating
            except Exception as hc_err:
                # Graceful degradation: log and allow the advance to proceed
                logger.debug(
                    "PHASE_COMPLETE health check failed",
                    pipeline_id=pipeline_id,
                    error=str(hc_err),
                )

        # ----------------------------------------------------------
        # #2777 (slice-1a, reviewer_concurrency NACK fix) — run the
        # plan-exit pre-lock work (validator, populate, opener) BEFORE
        # the state-lock-protected phase mutation. Failures here MUST
        # surface as a 422 / 500 BEFORE we mutate ``current_phase`` or
        # bump ``run_epoch``, otherwise a malformed plan or a gateway
        # failure leaves the pipeline in IMPLEMENT / RUNNING with no
        # runner thread driving it — the orphan-state hazard
        # reviewer_concurrency flagged on slice-1a v1.
        #
        # Ordering: validator (cheap, reads on-disk plan) → populate
        # (writes contract.pr.title/description that the opener reads)
        # → commit statefiles (so the new thread will push the
        # populated contract rather than reset it) → opener
        # (idempotent ``gh pr list``, then ``gh pr create`` if needed,
        # writes ``contract.pr.context_pr_number`` under its own
        # per-pipeline state lock).
        #
        # ``previous_phase`` is the pre-lock TOCTOU-vulnerable value
        # captured at line 281; the lock-acquired block below
        # re-derives it from the freshly-loaded pipeline. A concurrent
        # advance_phase that wins the lock-acquired race before us
        # will leave us with stale ``previous_phase``; in that case
        # the lock-acquired ``validate_phase_transition`` rejects the
        # second caller with 400, so the only cost of a stale read is
        # one wasted validator + opener cycle — no state corruption.
        # The opener is idempotent on its inner ``gh pr list``
        # pre-flight so a second caller that races a successful
        # first opener call re-persists the same PR number.
        if previous_phase == PipelinePhase.PLAN:
            # ---- AC-1a plan-phase pre-flight validator (#2777) ----
            # Run BEFORE populate so a malformed plan surfaces as a
            # typed 422 with the missing field name rather than the
            # populate path's silent warn-log. Validator only fires
            # on plan→implement (the new context-PR opener that
            # depends on the validated fields only runs there).
            # ``force=True`` bypasses the validator so operators can
            # still unstick a pipeline whose plan draft is unrecoverable.
            #
            # tester v3 NACK fix: the outer conditional stays at
            # ``previous_phase == PLAN`` (not narrowed to
            # ``and target_phase == IMPLEMENT``) so the populate block
            # below runs on ANY plan-exit per the #1941 contract.
            # Only the validator and opener arms narrow to
            # plan→implement; populate runs uniformly.
            if target_phase == PipelinePhase.IMPLEMENT and not force:
                # reviewer_code_holistic blocker 2: distinguish
                # "infra unavailable" (preflight could not run; surface
                # as 500 so the operator retries rather than believing
                # validation passed) from "plan is malformed" (typed
                # 422 with missing fields) from "draft file absent"
                # (a startup-flow that legitimately produces no plan
                # draft — skip the validator silently). Each branch
                # uses NARROWLY-typed exception handlers; no
                # `except Exception` swallow-all paths gate the new
                # feature.
                try:
                    from routes import resolve_worktree_path as _resolve_wt_for_validator
                    from routes.pipelines import _get_draft_path
                except ImportError as _imp_err:
                    logger.warning(
                        "Plan pre-flight validator: import of "
                        "validator dependencies failed (#2777)",
                        pipeline_id=pipeline_id,
                        error=str(_imp_err),
                    )
                    return make_error_response(
                        f"Plan pre-flight unavailable: {_imp_err}",
                        500,
                        reason="preflight_unavailable",
                    )

                # ``resolve_worktree_path`` and ``_get_draft_path`` are
                # pure helpers; the only realistic failure is an
                # OSError on the worktree probe. Catch OSError
                # specifically (rather than bare Exception) so a
                # programming error in those helpers surfaces loudly.
                try:
                    _validator_worktree = _resolve_wt_for_validator(pipeline_id, store.repo_path)
                    _draft_rel = _get_draft_path(
                        "plan",
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline_id,
                    )
                except OSError as _resolve_err:
                    logger.warning(
                        "Plan pre-flight validator: worktree probe / "
                        "draft-path resolution failed (#2777)",
                        pipeline_id=pipeline_id,
                        error=str(_resolve_err),
                    )
                    return make_error_response(
                        f"Plan pre-flight unavailable: {_resolve_err}",
                        500,
                        reason="preflight_unavailable",
                    )

                if _draft_rel is None:
                    # No draft path declared (e.g. ``start_phase=plan``
                    # pipelines that operate without a writable draft
                    # bucket). Skip the validator — there is no plan to
                    # validate. This is a legitimate skip, not a silent
                    # bypass.
                    logger.info(
                        "Plan pre-flight validator: no draft path "
                        "declared for pipeline; skipping (#2777)",
                        pipeline_id=pipeline_id,
                    )
                elif not (_validator_worktree / _draft_rel).exists():
                    # Draft path declared but file absent on disk —
                    # populate will surface this as the canonical
                    # "draft_missing" outcome. Skip the validator to
                    # let populate's structured warn-log handle it
                    # without producing a duplicate signal.
                    logger.info(
                        "Plan pre-flight validator: plan draft file absent; skipping (#2777)",
                        pipeline_id=pipeline_id,
                        draft_path=str(_validator_worktree / _draft_rel),
                    )
                else:
                    _plan_path = _validator_worktree / _draft_rel
                    try:
                        plan_text = _plan_path.read_text()
                    except OSError as _read_err:
                        logger.warning(
                            "Plan pre-flight validator: failed to read plan draft (#2777)",
                            pipeline_id=pipeline_id,
                            error=str(_read_err),
                        )
                        return make_error_response(
                            f"Plan pre-flight unavailable: {_read_err}",
                            500,
                            reason="preflight_unavailable",
                        )

                    try:
                        from egg_contracts.plan_parser import (
                            PlanPreflightError,
                            validate_plan_preflight,
                        )
                    except ImportError as _imp_err:
                        logger.warning(
                            "Plan pre-flight validator: import of plan_parser failed (#2777)",
                            pipeline_id=pipeline_id,
                            error=str(_imp_err),
                        )
                        return make_error_response(
                            f"Plan pre-flight unavailable: {_imp_err}",
                            500,
                            reason="preflight_unavailable",
                        )

                    try:
                        validate_plan_preflight(plan_text)
                    except PlanPreflightError as preflight_err:
                        logger.warning(
                            "Plan pre-flight validation failed at plan→implement advance (#2777)",
                            pipeline_id=pipeline_id,
                            missing_fields=preflight_err.missing_fields,
                        )
                        return make_error_response(
                            str(preflight_err),
                            422,
                            details={
                                "missing_fields": preflight_err.missing_fields,
                            },
                            reason="preflight_invalid_plan",
                        )

            # ---- Populate contract from plan + commit statefiles ----
            # When leaving the plan phase, parse the plan draft's
            # yaml-tasks appendix into the contract's pr/phases fields.
            # _run_pipeline's per-phase block only runs this for the
            # thread that owned the plan phase; a force=true advance
            # replaces that thread before it reaches the populate step,
            # leaving contract.pr empty and the PR-phase auto-PR path
            # falling back to placeholder title/body (see #1941).
            # Commit the result in-process so _sync_worktree_with_remote
            # in the newly-spawned thread pushes rather than resets the
            # change. Failures warn and continue — the advance-phase
            # path is a recovery hammer; blocking it on populate
            # failures would defeat the purpose. The new context-PR
            # opener below requires ``contract.pr.title`` /
            # ``description`` so populate must succeed; if it fails
            # the opener will surface a ``missing_pr_metadata``
            # ``ContextPrCreationError`` and the 422 below catches it.
            try:
                from routes import resolve_worktree_path
                from routes.pipelines import (
                    PopulateOutcome,
                    _commit_statefiles_to_worktree,
                    _pipeline_identifier,
                    _populate_contract_from_plan_safe,
                )

                _plan_exit_worktree = resolve_worktree_path(pipeline_id, store.repo_path)
                _plan_exit_mode = pipeline.mode.value if pipeline.mode else "issue"
                _plan_exit_populate_result = _populate_contract_from_plan_safe(
                    _plan_exit_worktree,
                    pipeline_id,
                    _plan_exit_mode,
                    pipeline.issue_number,
                    source="advance_phase_force" if force else "advance_phase_rest",
                )
                # #1941 force semantics preserved.
                if _plan_exit_populate_result.outcome != PopulateOutcome.POPULATED:
                    logger.warning(
                        "Plan-exit populate produced non-POPULATED outcome",
                        pipeline_id=pipeline_id,
                        outcome=_plan_exit_populate_result.outcome.value,
                    )
                try:
                    _commit_statefiles_to_worktree(
                        _plan_exit_worktree,
                        "Populate contract from plan on plan-phase exit",
                        pipeline_identifier=_pipeline_identifier(
                            pipeline.issue_number, pipeline_id
                        ),
                        pipeline_id=pipeline_id,
                    )
                except Exception as commit_err:  # noqa: BLE001
                    logger.warning(
                        "Failed to commit populated contract on plan exit (continuing)",
                        pipeline_id=pipeline_id,
                        error=str(commit_err),
                    )
            except Exception as exit_err:  # noqa: BLE001
                logger.warning(
                    "Failed to run plan-exit populate (continuing)",
                    pipeline_id=pipeline_id,
                    error=str(exit_err),
                )

            # ---- Context PR opener (#2777, cq-4 hard-required) ----
            # #2593 — open the doc-only base/context PR on the
            # advance_phase REST/MCP path too. Before this, the hook
            # was wired into only the inline ``_run_pipeline``
            # auto-advance path, so operators who cleared the plan
            # gate via this endpoint silently left the slice stack
            # rooted on ``/work`` with no PR to ``main``.
            #
            # #2777 (cq-4, TASK-1-2) — replaced the legacy soft-fail
            # wrapper with the new hard-required, idempotent
            # ``_open_context_pr_at_implement_start`` opener.
            #
            # Only fires on plan→implement; other target phases (e.g.
            # plan→pr force-advance) skip the opener because there
            # is no slice stack to root on a context PR.
            #
            # reviewer egg-reviewer blocker #1 fix: gated on
            # ``not force`` to match the validator's force gate above.
            # The exact failure modes operators reach for ``force`` to
            # bypass (gateway outage, ``gh`` auth churn, GitHub rate-
            # limit window) are also exactly the failure modes of the
            # opener, so a force-advance designed to unstick a sick
            # gateway must not itself be blocked by that same sick
            # gateway. Convergence on force=True still happens via
            # the four runner-side backstops (slice-loop entry,
            # implement-entry backstop, ``_run_pipeline`` auto-
            # advance, HITL resume) once the gateway recovers; those
            # call sites log-and-continue (best-effort), so they will
            # retry the opener every time the implement phase enters
            # the runner.
            if target_phase == PipelinePhase.IMPLEMENT and not force:
                try:
                    from routes.pipelines import (
                        ContextPrCreationError,
                        _open_context_pr_at_implement_start,
                    )

                    _open_context_pr_at_implement_start(pipeline_id)
                except ContextPrCreationError as ctx_err:
                    # Hard-required: do NOT swallow. Surface as 422
                    # so the operator sees the missing-PR / gateway-
                    # failure rather than discovering it as a stranded
                    # slice stack later. The state-lock-protected
                    # mutation below has NOT yet run at this point,
                    # so the pipeline remains in PLAN / its prior
                    # status — no orphan state.
                    logger.warning(
                        "Context PR opener failed at advance_phase (#2777, cq-4 hard-required)",
                        pipeline_id=pipeline_id,
                        reason=ctx_err.reason,
                        error=str(ctx_err),
                    )
                    return make_error_response(
                        f"Context PR could not be opened: {ctx_err}",
                        422,
                        details={"reason": ctx_err.reason},
                        reason="context_pr_open_failed",
                    )
                except Exception as ctx_outer_err:  # noqa: BLE001
                    # Defence in depth: import / lookup failures that
                    # are NOT ContextPrCreationError still surface as
                    # a 5xx so the rejection reaches the operator.
                    logger.warning(
                        "Context PR opener: outer wrapper raised on advance_phase (#2777)",
                        pipeline_id=pipeline_id,
                        error=str(ctx_outer_err),
                    )
                    return make_error_response(
                        f"Context PR opener wrapper failed: {ctx_outer_err}",
                        500,
                        reason="context_pr_open_wrapper_failed",
                    )

        # Acquire the pipeline state lock so the phase transition and
        # run_epoch bump are atomic with respect to any running
        # _run_pipeline thread.  This matches restart_phase's pattern.
        with _pkg.get_pipeline_state_lock(pipeline_id):
            # Re-load pipeline under the lock to guard against concurrent
            # modifications between the earlier read and lock acquisition.
            pipeline = store.load_pipeline(pipeline_id)
            original_version = pipeline.version

            # TOCTOU guard: re-derive previous_phase from the reloaded
            # pipeline.  Between the initial read and lock acquisition,
            # another request may have already advanced the pipeline.
            previous_phase = pipeline.current_phase
            if not force:
                is_valid, error = validate_phase_transition(previous_phase, target_phase)
                if not is_valid:
                    return make_error_response(
                        error, status_code=400, reason="invalid_phase_transition"
                    )
                current_execution = pipeline.get_phase_execution(previous_phase)
                if current_execution.status not in (
                    PipelineStatus.COMPLETE,
                    PipelineStatus.PENDING,
                ):
                    return make_error_response(
                        f"Current phase {previous_phase.value} is not complete "
                        f"(status: {current_execution.status.value})",
                        reason="previous_phase_not_complete",
                    )

            # Mark previous phase as complete
            prev_execution = pipeline.get_phase_execution(previous_phase)
            prev_execution.status = PipelineStatus.COMPLETE
            prev_execution.completed_at = datetime.now(UTC)

            # Transition to target phase
            pipeline.current_phase = target_phase
            pipeline.status = PipelineStatus.RUNNING

            # Initialize target phase execution
            target_execution = pipeline.get_phase_execution(target_phase)
            target_execution.status = PipelineStatus.RUNNING
            target_execution.started_at = datetime.now(UTC)
            target_execution.work_started_at = datetime.now(UTC)

            # Bump run_epoch so any stale _run_pipeline thread from the
            # previous phase detects the advance and exits gracefully.
            pipeline.run_epoch = datetime.now(UTC)
            # ``updated_at`` is unconditionally set by ``StateStore.save_pipeline``.

            # Save updated pipeline with optimistic locking
            store.save_pipeline(pipeline, expected_version=original_version)

        # Persist BRC history for the outgoing phase before the message
        # store is wiped — otherwise advance_phase (and especially
        # --force, used to unstick #1813) silently drops that phase's
        # consensus transcript.  Runs for both normal advances and
        # force=true.  See #1827.
        from routes.pipelines import _persist_phase_brc_history

        _persist_phase_brc_history(pipeline, store, previous_phase.value)

        # Clear ephemeral inter-agent messaging and consensus state.
        # Intentionally called after lock release but before thread spawn:
        # the new phase starts with fresh concurrent state, and any racing
        # advance_phase call would be blocked by the optimistic lock above.
        _pkg._clear_concurrent_state(pipeline_id)

        # #2777 (slice-1a) — the plan-exit work (validator, populate,
        # commit, context-PR opener) is now performed BEFORE the
        # state-lock-protected phase mutation above (see the comment
        # block immediately preceding ``with get_pipeline_state_lock``).
        # Running those steps post-mutation would leave the pipeline
        # in IMPLEMENT / RUNNING with no runner thread driving it on
        # any failure path, which is the orphan-state hazard
        # reviewer_concurrency surfaced on slice-1a v1.

        # Launch a new _run_pipeline thread to process the target phase.
        # Without this, the pipeline stays in RUNNING state with no thread
        # driving agent spawning or consensus detection.  See #1672.
        from routes.pipelines import _spawn_pipeline_run_thread

        _spawn_pipeline_run_thread(pipeline_id, store.repo_path, pipeline.run_epoch)

        logger.info(
            "Phase advanced",
            pipeline_id=pipeline_id,
            from_phase=previous_phase.value,
            to_phase=target_phase.value,
        )

        return make_success_response(
            f"Phase advanced to {target_phase.value}",
            data={
                "previous_phase": previous_phase.value,
                "current_phase": target_phase.value,
            },
        )

    except VersionConflictError:
        return make_error_response(
            f"Concurrent modification detected for pipeline {pipeline_id}. Please retry.",
            status_code=409,
            reason="version_conflict",
        )
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
            reason="invalid_pipeline_id",
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
            reason="pipeline_not_found",
        )
