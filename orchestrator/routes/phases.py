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


# Valid phase transitions
PHASE_TRANSITIONS = {
    PipelinePhase.REFINE: [PipelinePhase.PLAN, PipelinePhase.IMPLEMENT],
    PipelinePhase.PLAN: [PipelinePhase.IMPLEMENT],
    PipelinePhase.IMPLEMENT: [PipelinePhase.PR],
    PipelinePhase.PR: [],  # Terminal phase
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

    try:
        from consensus import get_consensus_evaluator
    except ImportError:
        from ..consensus import get_consensus_evaluator  # type: ignore[no-redef]

    cleared = get_message_store().clear(pipeline_id)
    get_consensus_evaluator().clear(pipeline_id)

    # Clear BRC tracker if it exists
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
                        {
                            "cycle": ct.cycle,
                            "started_at": ct.started_at.isoformat() if ct.started_at else None,
                            "completed_at": ct.completed_at.isoformat()
                            if ct.completed_at
                            else None,
                            "commit_sha": ct.commit_sha,
                        }
                        for ct in phase_execution.cycle_timings
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

        # When leaving the plan phase, parse the plan draft's yaml-tasks
        # appendix into the contract's pr/phases fields.  _run_pipeline's
        # per-phase block only runs this for the thread that owned the
        # plan phase; a force=true advance replaces that thread before it
        # reaches the populate step, leaving contract.pr empty and the
        # PR phase's auto-PR path falling back to placeholder title/body
        # (see #1941).
        #
        # Commit the result in-process so _sync_worktree_with_remote in
        # the newly-spawned thread pushes rather than resets the change.
        # Failures warn and continue — the advance-phase path is a recovery
        # hammer; blocking it on populate failures would defeat the purpose.
        if previous_phase == PipelinePhase.PLAN:
            try:
                from routes import resolve_worktree_path
                from routes.pipelines import (
                    _commit_statefiles_to_worktree,
                    _pipeline_identifier,
                    _populate_contract_from_plan_safe,
                )

                worktree_path = resolve_worktree_path(pipeline_id, store.repo_path)
                pipeline_mode = pipeline.mode.value if pipeline.mode else "issue"
                _populate_contract_from_plan_safe(
                    worktree_path,
                    pipeline_id,
                    pipeline_mode,
                    pipeline.issue_number,
                )
                try:
                    _commit_statefiles_to_worktree(
                        worktree_path,
                        "Populate contract from plan on plan-phase exit",
                        pipeline_identifier=_pipeline_identifier(
                            pipeline.issue_number, pipeline_id
                        ),
                        pipeline_id=pipeline_id,
                    )
                except Exception as commit_err:
                    logger.warning(
                        "Failed to commit populated contract on plan exit (continuing)",
                        pipeline_id=pipeline_id,
                        error=str(commit_err),
                    )
            except Exception as exit_err:
                logger.warning(
                    "Failed to run plan-exit populate (continuing)",
                    pipeline_id=pipeline_id,
                    error=str(exit_err),
                )

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

    # Contract decisions are optional — babysit-pr pipelines set
    # has_contract=False, and issue pipelines may reach this endpoint before
    # a contract has been populated.
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
        except (OSError, ValueError, ContractValidationError):
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
                "next_phase": "pr"
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

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Contract populated from plan",
            "data": {
                "phase_count": 2,
                "task_count": 6
            }
        }
    """
    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id)

        # Resolve worktree path for contract/draft access
        from routes import resolve_worktree_path

        worktree_path = resolve_worktree_path(pipeline_id, store.repo_path)

        # Import and call the populate function
        from routes.pipelines import _populate_contract_from_plan

        _populate_contract_from_plan(
            repo_path=worktree_path,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline.mode.value if pipeline.mode else "issue",
            issue_number=pipeline.issue_number,
        )

        # Read back the contract to report counts. Contracts are keyed by
        # pipeline_id; the loader's compat shim covers legacy paths.
        try:
            from egg_contracts.loader import load_contract

            contract = load_contract(pipeline_id, worktree_path)
            task_count = sum(len(s.tasks) for s in contract.slices)
            return make_success_response(
                "Contract populated from plan",
                data={
                    "phase_count": len(contract.slices),
                    "task_count": task_count,
                },
            )
        except Exception:
            # Populate succeeded but we can't read counts — still success
            return make_success_response("Contract populated from plan")

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
                logger.warning(
                    "contract_populate_forest_violation",
                    pipeline_id=pipeline_id,
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
