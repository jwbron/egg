"""
Phase transition endpoints for egg-orchestrator.

Provides REST endpoints for advancing pipeline phases with validation.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Add parent directory to path for imports
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from decision_queue import get_decision_queue
from lifecycle_auth import require_lifecycle_secret
from models import (
    DecisionStatus,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from peer_consensus import get_peer_consensus_tracker
from state_store import (
    InvalidPipelineIdError,
    PipelineNotFoundError,
    VersionConflictError,
    get_pipeline_state_lock,
)

logger = get_logger("orchestrator.phases")

phases_bp = Blueprint("phases", __name__, url_prefix="/api/v1/pipelines")


# Valid phase transitions.
#
# Issue #1557 — Jira-epic SDLC support: ``PLAN`` gains ``APPLY`` as a
# valid successor, and the new ``APPLY`` phase advances only to
# ``IMPLEMENT``. The orchestrator-side scheduler in
# :func:`orchestrator.routes.pipelines._next_phases_for_epic` picks
# ``APPLY`` only when ``Pipeline.is_epic`` is true; non-epic pipelines
# continue to advance ``PLAN → IMPLEMENT`` directly (``IMPLEMENT`` is
# listed before ``APPLY`` so the default ``next_phases[0]`` semantics
# preserve the pre-#1557 behaviour for callers that don't go through
# the epic-aware helper).
PHASE_TRANSITIONS = {
    PipelinePhase.REFINE: [PipelinePhase.PLAN, PipelinePhase.IMPLEMENT],
    PipelinePhase.PLAN: [PipelinePhase.IMPLEMENT, PipelinePhase.APPLY],
    PipelinePhase.APPLY: [PipelinePhase.IMPLEMENT],
    # IMPLEMENT is now terminal — the PR phase was removed in #2777 (cq-4).
    # The context PR opens up-front at the plan→implement boundary via
    # ``_open_context_pr_at_implement_start``; slice PRs stack on it.
    PipelinePhase.IMPLEMENT: [],
}


def make_error_response(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
    reason: str | None = None,
) -> tuple[Response, int]:
    """Create an error response.

    ``reason`` is a stable, machine-readable enum-like code that disambiguates
    responses sharing the same HTTP status (especially 409, where distinct
    gates — health checks vs. unresolved HITL — would otherwise collapse into
    one signal). Callers should switch on ``reason`` rather than parsing
    ``message``. See #1939.
    """
    response: dict[str, Any] = {"success": False, "message": message}
    if reason is not None:
        response["reason"] = reason
    if details is not None:
        response["details"] = details
    return jsonify(response), status_code


def make_success_response(
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


from routes import get_state_store_for_pipeline  # noqa: E402 — shared helper


def _clear_concurrent_state(pipeline_id: str) -> None:
    """Clear ephemeral message store and consensus state on phase transition."""
    try:
        from message_store import get_message_store
    except ImportError:
        from ..message_store import get_message_store  # type: ignore[no-redef]

    cleared = get_message_store().clear(pipeline_id)

    # Clear BRC tracker if it exists. The legacy ConsensusEvaluator was
    # removed in cq-5 of #2777; the BRC tracker is the only consensus
    # state that needs clearing on a phase transition.
    try:
        from peer_consensus import remove_peer_consensus_tracker

        remove_peer_consensus_tracker(pipeline_id)
    except ImportError:
        pass

    if cleared:
        logger.debug(
            "Cleared concurrent state on phase transition",
            pipeline_id=pipeline_id,
            messages_cleared=cleared,
        )


def validate_phase_transition(
    current_phase: PipelinePhase,
    target_phase: PipelinePhase,
) -> tuple[bool, str]:
    """Validate a phase transition.

    Args:
        current_phase: Current pipeline phase
        target_phase: Target phase to transition to

    Returns:
        Tuple of (is_valid, error_message)
    """
    transitions = PHASE_TRANSITIONS
    if target_phase not in transitions.get(current_phase, []):
        valid_targets = transitions.get(current_phase, [])
        if not valid_targets:
            return False, f"Phase {current_phase.value} is terminal"
        return False, (
            f"Cannot transition from {current_phase.value} to {target_phase.value}. "
            f"Valid transitions: {[p.value for p in valid_targets]}"
        )
    return True, ""


@phases_bp.route("/<pipeline_id>/phase", methods=["GET"])
def get_current_phase(pipeline_id: str) -> tuple[Response, int]:
    """
    Get current pipeline phase.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "current_phase": "implement",
                "status": "running",
                "phase_execution": {...}
            }
        }
    """
    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id)

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)

        return make_success_response(
            "Phase retrieved",
            data={
                "current_phase": pipeline.current_phase.value,
                "status": pipeline.status.value,
                "phase_execution": {
                    "phase": phase_execution.phase.value,
                    "status": phase_execution.status.value,
                    "started_at": phase_execution.started_at.isoformat()
                    if phase_execution.started_at
                    else None,
                    "work_started_at": phase_execution.work_started_at.isoformat()
                    if phase_execution.work_started_at
                    else None,
                    "completed_at": phase_execution.completed_at.isoformat()
                    if phase_execution.completed_at
                    else None,
                    "review_cycles": phase_execution.review_cycles,
                    "hitl_review_cycles": phase_execution.hitl_review_cycles,
                    "cycle_timings": [
                        ct.model_dump(mode="json") for ct in phase_execution.cycle_timings
                    ],
                    "agent_exits": [
                        ae.model_dump(mode="json") for ae in phase_execution.agent_exits
                    ],
                },
            },
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


@phases_bp.route("/<pipeline_id>/phase", methods=["POST"])
@require_lifecycle_secret
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
        store, pipeline = get_state_store_for_pipeline(pipeline_id)
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
        with get_pipeline_state_lock(pipeline_id):
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
        _clear_concurrent_state(pipeline_id)

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


@phases_bp.route("/<pipeline_id>/phase/start", methods=["POST"])
@require_lifecycle_secret
def start_phase(pipeline_id: str) -> tuple[Response, int]:
    """
    Start execution of current phase.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Phase 'implement' marked running (does not spawn agents)",
            "data": {
                "phase": "implement",
                "status": "running"
            }
        }

    Note: this endpoint only flips phase_execution.status to RUNNING. It
    does NOT spawn agents — agent spawning is driven by the _run_pipeline
    loop. Intended for operator recovery; not the way to advance a
    completed phase — use advance_phase for that.
    """
    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id)
        original_version = pipeline.version

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)

        if phase_execution.status == PipelineStatus.RUNNING:
            return make_error_response(
                f"Phase {pipeline.current_phase.value} is already running",
                reason="phase_already_running",
            )

        phase_execution.status = PipelineStatus.RUNNING
        phase_execution.started_at = datetime.now(UTC)
        phase_execution.work_started_at = datetime.now(UTC)
        pipeline.status = PipelineStatus.RUNNING

        store.save_pipeline(pipeline, expected_version=original_version)

        logger.info(
            "Phase started",
            pipeline_id=pipeline_id,
            phase=pipeline.current_phase.value,
        )

        return make_success_response(
            f"Phase '{pipeline.current_phase.value}' marked running (does not spawn agents)",
            data={
                "phase": pipeline.current_phase.value,
                "status": phase_execution.status.value,
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


# Context marker prefix used on the 3-way conditional-ACK HITL gate
# decision (#2004). The resolve_decision handler matches on this prefix
# to dispatch approve+accept / reject / address-in-pipeline behavior.
CONDITIONAL_ACK_GATE_MARKER = "conditional_ack_gate:"

# User-facing option labels for the conditional-ACK HITL gate. Exposed as
# module-level constants so the resolve_decision handler and tests can
# reference the same strings without duplication (#2004).
CONDITIONAL_ACK_APPROVE = "Approve and accept obligations"
CONDITIONAL_ACK_REJECT = "Reject and force NACK"
CONDITIONAL_ACK_ADDRESS = "Address in-pipeline (invalidate ACK)"
CONDITIONAL_ACK_OPTIONS = [
    CONDITIONAL_ACK_APPROVE,
    CONDITIONAL_ACK_REJECT,
    CONDITIONAL_ACK_ADDRESS,
]


def _existing_conditional_ack_gate(pipeline: Pipeline) -> str | None:
    """Return the id of the pending conditional-ACK gate decision, if any.

    The gate decision is identified by a ``CONDITIONAL_ACK_GATE_MARKER``
    prefix on the decision's context field. Only pending decisions count —
    a previously-resolved gate for a prior conditional ACK does not block
    a new round if the producer later attaches a fresh condition.
    """
    for decision in pipeline.decisions:
        if decision.status != DecisionStatus.PENDING:
            continue
        if (decision.context or "").startswith(CONDITIONAL_ACK_GATE_MARKER):
            return decision.id
    return None


def _ensure_conditional_ack_gate(
    pipeline: Pipeline,
    repo_path: Path,
) -> str | None:
    """Queue the 3-way conditional-ACK HITL gate if conditions are live.

    Looks up the peer-consensus tracker for this pipeline and, if there
    are any active pre-merge conditions, enqueues a ``choice`` HITL
    decision (#2004). The decision's context embeds the conditions as
    JSON so the resolve_decision handler can dispatch without querying
    the tracker (which may have been torn down by then).

    Returns the decision id when a new gate is queued, the existing
    pending-decision id when one is already in flight, or ``None`` when
    no conditions exist (or the tracker is unavailable).
    """
    tracker = get_peer_consensus_tracker(pipeline.id)
    if tracker is None:
        return None
    try:
        conditions = tracker.get_pre_merge_conditions()
    except Exception:
        # Never block phase completion on a tracker read failure — the PR
        # body renderer applies the same guard (#1998).
        logger.warning(
            "Failed to read pre-merge conditions from tracker",
            pipeline_id=pipeline.id,
            exc_info=True,
        )
        return None
    if not conditions:
        return None

    existing_id = _existing_conditional_ack_gate(pipeline)
    if existing_id is not None:
        return existing_id

    # Render the conditions into the decision question so humans see the
    # obligation text up front without having to fetch the raw context.
    question_lines = [
        "Reviewer(s) issued a conditional ACK with pre-merge obligations:",
        "",
    ]
    for c in conditions:
        reviewer = c.get("reviewer", "unknown")
        condition = str(c.get("condition", "")).strip()
        if not condition:
            continue
        question_lines.append(f"- {reviewer}: {condition}")
    question_lines.extend(
        [
            "",
            "How should we proceed?",
        ]
    )
    question = "\n".join(question_lines)

    # Embed the conditions list in the context so the resolve handler can
    # act on specific (reviewer, producer) edges without re-querying the
    # tracker. Prefixed with CONDITIONAL_ACK_GATE_MARKER so the handler
    # can detect these decisions unambiguously.
    context_payload = {"conditions": conditions}
    context = CONDITIONAL_ACK_GATE_MARKER + json.dumps(context_payload)

    queue = get_decision_queue(pipeline.id, repo_path)
    decision = queue.queue_decision(
        question=question,
        context=context,
        options=list(CONDITIONAL_ACK_OPTIONS),
        decision_type="choice",
        phase=pipeline.current_phase,
    )
    logger.info(
        "Queued conditional-ACK HITL gate",
        pipeline_id=pipeline.id,
        decision_id=decision.id,
        condition_count=len(conditions),
    )
    return decision.id


def _collect_unresolved_phase_decisions(
    pipeline: Pipeline,
    store_repo_path: Path,
) -> list[str]:
    """Return IDs of decisions scoped to the current phase that are still pending.

    Checks two decision stores:

    - ``pipeline.decisions`` — orchestrator-side HITL queue (e.g. phase_gate
      prompts).
    - The SDLC contract's ``decisions`` list — agent-authored HITL points
      created via ``egg-contract add-decision``.

    Decisions with no phase tag are skipped for backward compatibility — we
    cannot prove they belong to the current phase, and older persisted state
    may predate the contract-side ``phase`` field (added alongside this
    guard).
    """
    current_phase = pipeline.current_phase

    unresolved = [
        d.id
        for d in pipeline.decisions
        if d.status == DecisionStatus.PENDING and d.phase == current_phase
    ]

    # Contract decisions are optional — ISSUE-mode pipelines may reach this
    # endpoint before a contract has been populated.
    if pipeline.has_contract:
        try:
            from egg_contracts.loader import (
                ContractNotFoundError,
                ContractValidationError,
                load_contract,
            )
        except ImportError:
            # egg_contracts not installed — cannot scan contract decisions.
            logger.warning(
                "Failed to scan contract decisions for unresolved entries",
                pipeline_id=pipeline.id,
                exc_info=True,
            )
            return unresolved

        try:
            from routes import resolve_worktree_path
            from routes.pipelines import _pipeline_identifier

            worktree_path = resolve_worktree_path(pipeline.id, store_repo_path)
            contract_id = _pipeline_identifier(pipeline.issue_number, pipeline.id)
            try:
                contract = load_contract(contract_id, worktree_path)
            except ContractNotFoundError:
                contract = None
            if contract is not None:
                unresolved.extend(
                    d.id for d in contract.decisions if not d.resolved and d.phase == current_phase
                )
        except OSError, ValueError, ContractValidationError:
            # OSError covers filesystem failures loading the contract,
            # ValueError covers serialization/validation issues (pydantic V2
            # raises ValueError for invalid data), and
            # ContractValidationError covers corrupt/invalid contract JSON.
            # Programming errors (AttributeError, TypeError, NameError)
            # are left to propagate so they surface during development.
            logger.warning(
                "Failed to scan contract decisions for unresolved entries",
                pipeline_id=pipeline.id,
                exc_info=True,
            )

    return unresolved


@phases_bp.route("/<pipeline_id>/phase/complete", methods=["POST"])
@require_lifecycle_secret
def complete_phase(pipeline_id: str) -> tuple[Response, int]:
    """
    Mark current phase as complete.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "artifacts": {...},       // optional, phase artifacts
            "force": false,           // optional, skip pending-decision guard
            "force_reason": "..."     // optional, audit note for force=true
        }

    Response:
        {
            "success": true,
            "message": "Phase 'implement' marked complete; call advance_phase to transition",
            "data": {
                "phase": "implement",
                "current_phase": "implement",
                "next_phase": null
            }
        }

    Note: this endpoint only flips phase_execution.status to COMPLETE. It
    does NOT advance pipeline.current_phase — callers must call
    /phase (advance_phase) next. The ``next_phase`` field is the
    suggested next transition, not the new current_phase.
    """
    # silent=True: Content-Type: application/json with an empty body would
    # otherwise raise BadRequest(400), which breaks callers that omit
    # `artifacts` (the field is documented as optional). See #1755.
    data = request.get_json(silent=True) or {}

    artifacts = data.get("artifacts")
    if artifacts is not None:
        # Reject non-dict artifacts and dicts with non-string values at the
        # boundary — PhaseExecution.artifacts is typed as dict[str, str], and
        # pydantic's default config does not validate on assignment, so a bad
        # value would persist to disk and then fail validation on every
        # subsequent read. See #1755.
        if not isinstance(artifacts, dict):
            return make_error_response(
                f"artifacts must be a JSON object (got {type(artifacts).__name__})",
                status_code=400,
                reason="invalid_artifacts",
            )
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in artifacts.items()):
            return make_error_response(
                "artifacts must be a JSON object with string values",
                status_code=400,
                reason="invalid_artifacts",
            )

    force = bool(data.get("force", False))
    force_reason = data.get("force_reason")
    if force_reason is not None and not isinstance(force_reason, str):
        return make_error_response(
            "force_reason must be a string",
            status_code=400,
            reason="invalid_force_reason",
        )
    if isinstance(force_reason, str) and not force_reason.strip():
        # Treat empty/whitespace-only strings the same as absent — an empty
        # reason has no audit value, and this keeps validation symmetric with
        # the artifact-recording path (which uses `if force_reason:`).
        force_reason = None

    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id)
        original_version = pipeline.version

        # If any reviewer issued a conditional ACK, surface the 3-way HITL
        # gate (#2004) before the generic unresolved-decisions guard below.
        # The gate decision is newly-queued on first call (returning 409 via
        # the guard), then resolved out-of-band by the resolve_decision
        # handler, which dispatches approve+accept / reject / address
        # behavior on the approval matrix and contract. Skipped under
        # force=true so operators can still drain stuck pipelines.
        if not force:
            gate_id = _ensure_conditional_ack_gate(pipeline, store.repo_path)
            if gate_id is not None:
                # queue_decision writes through the state store, so the
                # in-memory ``pipeline`` is stale. Reload only when a gate
                # was actually queued or already existed; this keeps the
                # common path (no conditions) from hitting the store twice.
                pipeline = store.load_pipeline(pipeline_id)
                original_version = pipeline.version

        # Block advance while the current phase still has unresolved HITL
        # decisions. The lifecycle secret authorises *who* can advance; this
        # guard enforces *when* — a human or MCP client authorised to call
        # this endpoint must still resolve outstanding HITL input first, or
        # pass force=true to explicitly abandon it. See #1788.
        unresolved_ids = _collect_unresolved_phase_decisions(pipeline, store.repo_path)
        if unresolved_ids and not force:
            return make_error_response(
                (
                    f"Phase '{pipeline.current_phase.value}' has "
                    f"{len(unresolved_ids)} unresolved HITL decision"
                    f"{'s' if len(unresolved_ids) != 1 else ''}. "
                    "Resolve them or pass force=true to abandon."
                ),
                status_code=409,
                details={
                    "phase": pipeline.current_phase.value,
                    "unresolved_decision_ids": unresolved_ids,
                },
                reason="unresolved_hitl_decisions",
            )

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
        phase_execution.status = PipelineStatus.COMPLETE
        phase_execution.completed_at = datetime.now(UTC)

        # Store artifacts if provided
        if artifacts:
            phase_execution.artifacts = artifacts

        # Record the force override alongside any caller-supplied artifacts
        # so the abandoned decisions remain on the frozen phase history for
        # audit, even though they were never resolved. Values must be
        # strings (PhaseExecution.artifacts is dict[str, str]).
        if force and unresolved_ids:
            merged = dict(phase_execution.artifacts)
            merged["force_completed_decisions"] = json.dumps(unresolved_ids)
            if force_reason:
                merged["force_reason"] = force_reason
            phase_execution.artifacts = merged
            logger.warning(
                "Phase force-completed with unresolved HITL decisions",
                pipeline_id=pipeline_id,
                phase=pipeline.current_phase.value,
                unresolved_decision_ids=unresolved_ids,
                force_reason=force_reason,
            )

        # Determine next phase
        next_phases = PHASE_TRANSITIONS.get(pipeline.current_phase, [])
        next_phase = next_phases[0] if next_phases else None

        store.save_pipeline(pipeline, expected_version=original_version)

        # Persist BRC history for the phase being completed before
        # _clear_concurrent_state wipes the message store — otherwise
        # the consensus transcript for this phase is lost when callers
        # complete a phase externally (e.g. the #1813 unstick flow).
        # See #1827.
        from routes.pipelines import _persist_phase_brc_history

        _persist_phase_brc_history(pipeline, store, pipeline.current_phase.value)

        # Clear ephemeral inter-agent messaging and consensus state
        _clear_concurrent_state(pipeline_id)

        logger.info(
            "Phase completed",
            pipeline_id=pipeline_id,
            phase=pipeline.current_phase.value,
        )

        return make_success_response(
            (
                f"Phase '{pipeline.current_phase.value}' marked complete; "
                "call advance_phase to transition"
            ),
            data={
                "phase": pipeline.current_phase.value,
                # Echo current_phase to make it explicit that this endpoint
                # did NOT advance the pipeline — the pointer is unchanged.
                # next_phase is the *suggested* transition, not the new
                # current_phase. See #1940.
                "current_phase": pipeline.current_phase.value,
                "next_phase": next_phase.value if next_phase else None,
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


@phases_bp.route("/<pipeline_id>/phase/populate-contract", methods=["POST"])
@require_lifecycle_secret
def populate_contract(pipeline_id: str) -> tuple[Response, int]:
    """
    Populate a pipeline's SDLC contract from its plan draft.

    Reads the plan document from the pipeline's worktree, extracts task
    structure, and writes tasks and acceptance criteria to the contract.
    On the ``POPULATED`` outcome the route also commits the contract to
    the orchestrator's local worktree and pushes the work branch to
    origin so fresh agent spawns (``restart_phase``, ``restart_agent``,
    post-cancel restart) pull the populated state on respawn (#2629).

    URL params:
        pipeline_id: Pipeline ID

    Response (200 — ``POPULATED``):
        {
            "success": true,
            "message": "Contract populated from plan",
            "data": {
                "phase_count": 2,
                "task_count": 6,
                "pushed_to_origin": true
            }
        }

    ``pushed_to_origin`` is the operator's signal for whether agents
    will see the populated state on respawn. ``True`` iff
    ``push_worktree_branch`` reported success (a no-op fast-forward push
    counts; a no-op commit alone does not). ``False`` means the commit
    or push failed (or the push was not attempted because
    ``pipeline.branch`` is unset or the worktree resolves to
    ``store.repo_path``) and the operator must commit and push
    themselves before respawning.

    Pre-populate sync (#2792, non-destructive since #2979): the route
    runs :func:`_sync_worktree_with_remote` before reading the draft.
    When the rebase autoresolve cannot reconcile a divergence the sync
    is non-destructive — it leaves the worktree at the local HEAD
    (committed work intact) and reports ``diverged_unreconciled``.  This
    route cannot block on the operator, so it pauses the pipeline
    (``AWAITING_HUMAN``, not ``FAILED``) via
    :func:`_emit_divergence_reconcile_hitl` and returns HTTP 409 with
    ``reason="divergence_reconcile_unacked"`` and ``backup_ref`` /
    ``local_only_commit_shas`` in ``details``.  The operator reconciles
    the orchestrator-side worktree, acks the reconcile HITL (visible in
    ``/sdlc``), and re-runs this endpoint — refusing to populate against
    an un-reconciled worktree matches the #2337 / #2627 fail-loud posture
    without discarding work.

    Error responses include a machine-readable ``reason`` code (#1939,
    #2627):

    - 400 ``invalid_pipeline_id``
    - 404 ``pipeline_not_found`` / ``draft_missing`` / ``no_draft_path``
    - 409 ``divergence_reconcile_unacked`` (#2792, #2979)
    - 422 ``parse_failed`` / ``empty_result`` / forest violations
      (structured body)
    - 500 ``contract_load_failed`` / ``egg_contracts_unavailable`` /
      ``unexpected_exception`` / ``populate_contract_failed``
    """
    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id)

        # Resolve worktree path for contract/draft access
        from routes import resolve_worktree_path

        worktree_path = resolve_worktree_path(pipeline_id, store.repo_path)

        # Import and call the populate function
        from routes.pipelines import (
            PopulateOutcome,
            _commit_statefiles_to_worktree,
            _compute_gateway_mode,
            _emit_divergence_reconcile_hitl,
            _find_pending_divergence_reconcile_decision,
            _get_spawner,
            _pipeline_identifier,
            _populate_contract_from_plan,
            _sync_worktree_with_remote,
        )

        # #2979 follow-up: if a prior populate_contract already paused the
        # pipeline on a reconcile HITL and the operator has not yet
        # resolved it, surface the existing decision instead of re-running
        # the sync and appending a duplicate HITL.  Re-emit would still be
        # correctness-safe (the abort path resolves the most recent
        # decision), but each retry would bloat ``pipeline.decisions`` and
        # confuse /sdlc.  Returning 409 here keeps the route idempotent
        # under operator retries against an already-paused pipeline.
        existing_reconcile_decision = _find_pending_divergence_reconcile_decision(pipeline)
        if existing_reconcile_decision is not None:
            logger.info(
                "populate_contract: pipeline already paused on reconcile HITL; "
                "skipping re-emit and returning 409",
                pipeline_id=pipeline_id,
                decision_id=existing_reconcile_decision.id,
            )
            return make_error_response(
                "Pipeline is already paused on a worktree-reconcile HITL from a "
                "prior populate_contract call. Reconcile the worktree, resolve "
                "the existing decision (visible in /sdlc), and re-run "
                "populate_contract.",
                status_code=409,
                reason="divergence_reconcile_unacked",
                details={
                    "diverged_unreconciled": True,
                    "decision_id": existing_reconcile_decision.id,
                    "already_paused": True,
                },
            )

        # #2792/#2979: reconcile the worktree before reading the draft so
        # a divergence here is surfaced the same way the phase-boundary
        # sync does.  Since #2979 the sync is non-destructive: when the
        # rebase autoresolve can't reconcile a divergence it leaves the
        # worktree at the local HEAD (committed work intact) and reports
        # ``diverged_unreconciled``.  This route cannot block on the
        # operator the way the in-loop phase-boundary callers do, so it
        # pauses the pipeline (AWAITING_HUMAN, NOT FAILED), emits the
        # reconcile HITL, and returns 409 — refusing to populate against
        # an un-reconciled worktree.  The operator reconciles the worktree,
        # resolves the HITL, and re-runs populate_contract.
        populate_sync_outcome = None
        if pipeline.branch and worktree_path != store.repo_path:
            gateway_mode_for_sync, _ = _compute_gateway_mode(pipeline)
            try:
                populate_sync_outcome = _sync_worktree_with_remote(
                    _get_spawner(),
                    pipeline_id,
                    worktree_path,
                    gateway_mode=gateway_mode_for_sync,
                    base_branch=pipeline.base_branch,
                    pipeline_branch=pipeline.branch,
                )
            except Exception as sync_err:  # noqa: BLE001
                logger.warning(
                    "populate_contract: pre-populate sync raised (continuing)",
                    pipeline_id=pipeline_id,
                    error=str(sync_err),
                )

        diverged_unreconciled = bool(
            populate_sync_outcome and populate_sync_outcome.diverged_unreconciled
        )
        divergence_backup_ref = populate_sync_outcome.backup_ref if populate_sync_outcome else None
        divergence_local_only = (
            list(populate_sync_outcome.local_only_commit_shas) if populate_sync_outcome else []
        )

        if diverged_unreconciled:
            # Do NOT run the populator against an un-reconciled worktree.
            # Pause the pipeline+phase (AWAITING_HUMAN — nothing was
            # discarded; the divergence is recoverable), emit the reconcile
            # HITL, and return 409.  The operator reconciles the
            # orchestrator-side worktree, resolves the HITL, and re-runs
            # populate_contract against the reconciled state (#2979).
            logger.error(
                "populate_contract: refusing to populate over unreconciled divergence",
                pipeline_id=pipeline_id,
                backup_ref=divergence_backup_ref,
                local_only_commit_count=len(divergence_local_only),
            )
            _emit_divergence_reconcile_hitl(
                pipeline_id,
                store,
                phase=pipeline.current_phase,
                backup_ref=divergence_backup_ref,
                local_only_commit_shas=divergence_local_only,
            )
            return make_error_response(
                "Worktree diverged from origin during pre-populate sync and "
                "could not be auto-reconciled; the pipeline is paused. Operator "
                "must reconcile the worktree and ack the reconcile HITL (visible "
                "in /sdlc), then re-run populate_contract.",
                status_code=409,
                reason="divergence_reconcile_unacked",
                details={
                    "diverged_unreconciled": True,
                    "backup_ref": divergence_backup_ref,
                    "local_only_commit_shas": divergence_local_only,
                },
            )

        _populate_endpoint_result = _populate_contract_from_plan(
            repo_path=worktree_path,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline.mode.value if pipeline.mode else "issue",
            issue_number=pipeline.issue_number,
        )

        # #2627 follow-up: surface non-POPULATED outcomes as 4xx/5xx so
        # callers don't get a misleading 200 when the populate step
        # silently produced an empty contract.  Forest-violation is
        # handled separately by the existing class-name branch in the
        # outer ``except`` (HTTP 422 with structured errors).
        _outcome = _populate_endpoint_result.outcome
        if _outcome == PopulateOutcome.POPULATED:
            # Persist the populated contract back to origin so fresh
            # agent spawns (restart_phase, restart_agent, post-cancel
            # restart) pull the populated state on respawn rather than
            # the empty contract on origin.  Without this, the route
            # mutates only the orchestrator's local worktree and is
            # unusable as a recovery primitive — the implement-start
            # guard would refuse to demote to monolithic and the
            # pipeline would wedge.  See #2629.
            #
            # Failures here are fail-soft: ``pushed_to_origin`` in the
            # response data tells the caller whether the contract is
            # visible to agents.  ``False`` means the operator must
            # commit and push themselves before respawning.
            pushed_to_origin = False
            if pipeline.branch and worktree_path != store.repo_path:
                try:
                    identifier = _pipeline_identifier(pipeline.issue_number, pipeline_id)
                    _commit_statefiles_to_worktree(
                        worktree_path,
                        f"Populate contract for {identifier} (#2629)",
                        pipeline_identifier=identifier,
                        pipeline_id=pipeline_id,
                    )
                    # Push unconditionally — a no-op commit does NOT
                    # imply origin matches local.  The per-pipeline
                    # worktree is long-lived (see
                    # ``resolve_worktree_path``) and may carry commits
                    # ahead of origin from a prior failed push.
                    # Pushing unconditionally fast-forwards in the safe
                    # case (origin already matches → no-op push) and
                    # delivers the un-pushed commit in the dangerous
                    # one (the exact wedge #2629 was opened against).
                    # The ``populate_contract`` route is an
                    # operator-initiated recovery primitive, not a hot
                    # loop, so the gateway round-trip is cheap relative
                    # to the correctness benefit.
                    gateway_mode, _ = _compute_gateway_mode(pipeline)
                    push_result = _get_spawner().gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_path),
                        branch=pipeline.branch,
                        mode=gateway_mode,
                        base_branch=pipeline.base_branch,
                    )
                    pushed_to_origin = bool(push_result)
                    if not pushed_to_origin:
                        logger.warning(
                            "populate_contract: push failed (continuing)",
                            pipeline_id=pipeline_id,
                            detail=push_result.describe(),
                        )
                except Exception as persist_err:  # noqa: BLE001
                    logger.warning(
                        "populate_contract: persist to origin failed (continuing)",
                        pipeline_id=pipeline_id,
                        error=str(persist_err),
                    )

            return make_success_response(
                "Contract populated from plan",
                data={
                    "phase_count": _populate_endpoint_result.slice_count,
                    "task_count": _populate_endpoint_result.task_count,
                    "pushed_to_origin": pushed_to_origin,
                },
            )
        if _outcome in {PopulateOutcome.DRAFT_MISSING, PopulateOutcome.NO_DRAFT_PATH}:
            return make_error_response(
                f"Plan draft not available ({_outcome.value})",
                status_code=404,
                reason=_outcome.value,
            )
        if _outcome in {PopulateOutcome.PARSE_FAILED, PopulateOutcome.EMPTY_RESULT}:
            return make_error_response(
                f"Plan populate produced non-POPULATED outcome ({_outcome.value})",
                status_code=422,
                reason=_outcome.value,
            )
        # CONTRACT_LOAD_FAILED / EGG_CONTRACTS_UNAVAILABLE /
        # UNEXPECTED_EXCEPTION — server-side failure.
        return make_error_response(
            f"Plan populate failed ({_outcome.value})",
            status_code=500,
            reason=_outcome.value,
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
    except Exception as e:
        # #2137 TASK-2-2: forest-violation errors are routed as 422 with
        # the inlined structured-error body so the plan reviewer can
        # cite them verbatim. We branch on the class name to avoid an
        # import cycle (routes/pipelines.py imports from routes/phases.py).
        if e.__class__.__name__ == "ForestValidationError":
            try:
                body, status = e.to_response()  # type: ignore[attr-defined]
                # #3046 — ForestValidationError covers both the forest-shape
                # and file-overlap-ordering rejections. Use a
                # discriminator-agnostic event name and include
                # ``reason=`` so operators grepping the audit log catch
                # both cases.
                logger.warning(
                    "contract_populate_dag_rejection",
                    pipeline_id=pipeline_id,
                    reason=getattr(e, "reason", None),
                    errors=getattr(e, "errors", None),
                )
                return jsonify(body), status
            except Exception:  # noqa: BLE001
                # Fall through to the generic 500 path if to_response
                # is missing or shaped unexpectedly.
                pass
        logger.error(
            "contract_populate_endpoint_failed",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )
        return make_error_response(
            f"Failed to populate contract: {e}",
            status_code=500,
            reason="populate_contract_failed",
        )


@phases_bp.route("/<pipeline_id>/phase/fail", methods=["POST"])
@require_lifecycle_secret
def fail_phase(pipeline_id: str) -> tuple[Response, int]:
    """
    Mark current phase as failed.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "error": "Error message"  // required
        }

    Response:
        {
            "success": true,
            "message": "Phase marked as failed"
        }
    """
    # silent=True: tolerate empty body with Content-Type: application/json.
    # Same defense-in-depth as advance_phase — see #1787.
    data = request.get_json(silent=True) or {}

    error_message = data.get("error")
    if not error_message:
        return make_error_response("Missing error message", reason="missing_error_message")

    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id)
        original_version = pipeline.version

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
        phase_execution.status = PipelineStatus.FAILED
        phase_execution.error = error_message
        phase_execution.completed_at = datetime.now(UTC)

        pipeline.status = PipelineStatus.FAILED
        pipeline.error = error_message

        store.save_pipeline(pipeline, expected_version=original_version)

        # Clear ephemeral inter-agent messaging and consensus state
        _clear_concurrent_state(pipeline_id)

        logger.error(
            "Phase failed",
            pipeline_id=pipeline_id,
            phase=pipeline.current_phase.value,
            error=error_message,
        )

        return make_success_response(
            "Phase marked as failed",
            data={
                "phase": pipeline.current_phase.value,
                "error": error_message,
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
