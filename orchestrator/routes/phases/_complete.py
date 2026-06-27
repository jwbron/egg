"""complete_phase: mark the current phase complete, gating on conditional-ACK
HITL plus unresolved HITL decisions and contract gaps (#3312 decomposition).
"""

import json
from datetime import UTC, datetime

import routes.phases as _pkg
from flask import Response, request
from models import PipelineStatus
from state_store import (
    InvalidPipelineIdError,
    PipelineNotFoundError,
    VersionConflictError,
)

from . import logger
from ._gates import (
    _collect_unresolved_contract_gaps,
    _collect_unresolved_phase_decisions,
    _ensure_conditional_ack_gate,
)
from ._responses import make_error_response, make_success_response
from ._transitions import PHASE_TRANSITIONS


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
        store, pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
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

        # Block finalize while the contract carries unresolved TaskGaps.
        # A tester→coder gap left open ships into the committed contract
        # and fails test_models_gaps.py red in CI on the already-open PR
        # (#3298 class 4). Mirror the unresolved-HITL guard above so the
        # failure is symmetric and surfaced early; force=true abandons.
        # See #3300.
        unresolved_gap_ids = _collect_unresolved_contract_gaps(pipeline, store.repo_path)
        if unresolved_gap_ids and not force:
            return make_error_response(
                (
                    f"Phase '{pipeline.current_phase.value}' has "
                    f"{len(unresolved_gap_ids)} unresolved contract gap"
                    f"{'s' if len(unresolved_gap_ids) != 1 else ''}. "
                    "Resolve them (set the gap's resolved=true) or pass "
                    "force=true to ship with the gap open."
                ),
                status_code=409,
                details={
                    "phase": pipeline.current_phase.value,
                    "unresolved_gap_ids": unresolved_gap_ids,
                },
                reason="unresolved_contract_gaps",
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
        if force and (unresolved_ids or unresolved_gap_ids):
            merged = dict(phase_execution.artifacts)
            if unresolved_ids:
                merged["force_completed_decisions"] = json.dumps(unresolved_ids)
            if unresolved_gap_ids:
                merged["force_completed_gaps"] = json.dumps(unresolved_gap_ids)
            if force_reason:
                merged["force_reason"] = force_reason
            phase_execution.artifacts = merged
            logger.warning(
                "Phase force-completed with unresolved HITL decisions / contract gaps",
                pipeline_id=pipeline_id,
                phase=pipeline.current_phase.value,
                unresolved_decision_ids=unresolved_ids,
                unresolved_gap_ids=unresolved_gap_ids,
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
        _pkg._clear_concurrent_state(pipeline_id)

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
