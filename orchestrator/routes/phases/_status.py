"""Phase status read and simple status-flip endpoints: get_current_phase,
start_phase, fail_phase (#3312 decomposition).
"""

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
from ._responses import make_error_response, make_success_response


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
        store, pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)

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
        store, pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
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
        store, pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
        original_version = pipeline.version

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
        phase_execution.status = PipelineStatus.FAILED
        phase_execution.error = error_message
        phase_execution.completed_at = datetime.now(UTC)

        pipeline.status = PipelineStatus.FAILED
        pipeline.error = error_message

        store.save_pipeline(pipeline, expected_version=original_version)

        # Clear ephemeral inter-agent messaging and consensus state
        _pkg._clear_concurrent_state(pipeline_id)

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
