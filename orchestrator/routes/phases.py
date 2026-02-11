"""
Phase transition endpoints for egg-orchestrator.

Provides REST endpoints for advancing pipeline phases with validation.
"""

import sys
from datetime import datetime
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


from models import (
    PhaseExecution,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from state_store import InvalidPipelineIdError, PipelineNotFoundError, VersionConflictError, get_state_store

logger = get_logger("orchestrator.phases")

phases_bp = Blueprint("phases", __name__, url_prefix="/api/v1/pipelines")


# Valid phase transitions
PHASE_TRANSITIONS = {
    PipelinePhase.REFINE: [PipelinePhase.PLAN],
    PipelinePhase.PLAN: [PipelinePhase.IMPLEMENT],
    PipelinePhase.IMPLEMENT: [PipelinePhase.PR],
    PipelinePhase.PR: [],  # Terminal phase
}


def make_error_response(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create an error response."""
    response: dict[str, Any] = {"success": False, "message": message}
    if details:
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


def get_repo_path() -> Path:
    """Get the repository path from request or environment."""
    import os

    repo_path = request.args.get("repo_path")
    if repo_path:
        return Path(repo_path)

    data = request.get_json(silent=True) or {}
    if data.get("repo_path"):
        return Path(data["repo_path"])

    env_path = os.environ.get("EGG_REPO_PATH")
    if env_path:
        return Path(env_path)

    return Path.cwd()


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
    if target_phase not in PHASE_TRANSITIONS.get(current_phase, []):
        valid_targets = PHASE_TRANSITIONS.get(current_phase, [])
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
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)

        return make_success_response(
            "Phase retrieved",
            data={
                "current_phase": pipeline.current_phase.value,
                "status": pipeline.status.value,
                "phase_execution": {
                    "phase": phase_execution.phase.value,
                    "status": phase_execution.status.value,
                    "started_at": phase_execution.started_at.isoformat() if phase_execution.started_at else None,
                    "completed_at": phase_execution.completed_at.isoformat() if phase_execution.completed_at else None,
                    "review_cycles": phase_execution.review_cycles,
                },
            },
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


@phases_bp.route("/<pipeline_id>/phase", methods=["POST"])
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
    repo_path = get_repo_path()
    data = request.get_json() or {}

    target_phase_str = data.get("target_phase")
    if not target_phase_str:
        return make_error_response("Missing target_phase")

    try:
        target_phase = PipelinePhase(target_phase_str)
    except ValueError:
        return make_error_response(
            f"Invalid phase: {target_phase_str}. "
            f"Valid phases: {[p.value for p in PipelinePhase]}"
        )

    force = data.get("force", False)

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        original_version = pipeline.version  # Capture version for optimistic locking

        previous_phase = pipeline.current_phase

        # Validate transition unless forced
        if not force:
            is_valid, error = validate_phase_transition(previous_phase, target_phase)
            if not is_valid:
                return make_error_response(error, status_code=400)

            # Check if current phase is complete
            current_execution = pipeline.get_phase_execution(previous_phase)
            if current_execution.status not in (PipelineStatus.COMPLETE, PipelineStatus.PENDING):
                return make_error_response(
                    f"Current phase {previous_phase.value} is not complete "
                    f"(status: {current_execution.status.value})"
                )

        # Mark previous phase as complete
        prev_execution = pipeline.get_phase_execution(previous_phase)
        prev_execution.status = PipelineStatus.COMPLETE
        prev_execution.completed_at = datetime.utcnow()

        # Transition to target phase
        pipeline.current_phase = target_phase
        pipeline.status = PipelineStatus.RUNNING

        # Initialize target phase execution
        target_execution = pipeline.get_phase_execution(target_phase)
        target_execution.status = PipelineStatus.RUNNING
        target_execution.started_at = datetime.utcnow()

        # Save updated pipeline with optimistic locking
        store.save_pipeline(pipeline, expected_version=original_version)

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
        )
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


@phases_bp.route("/<pipeline_id>/phase/start", methods=["POST"])
def start_phase(pipeline_id: str) -> tuple[Response, int]:
    """
    Start execution of current phase.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Phase started",
            "data": {
                "phase": "implement",
                "status": "running"
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        original_version = pipeline.version

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)

        if phase_execution.status == PipelineStatus.RUNNING:
            return make_error_response(
                f"Phase {pipeline.current_phase.value} is already running"
            )

        phase_execution.status = PipelineStatus.RUNNING
        phase_execution.started_at = datetime.utcnow()
        pipeline.status = PipelineStatus.RUNNING

        store.save_pipeline(pipeline, expected_version=original_version)

        logger.info(
            "Phase started",
            pipeline_id=pipeline_id,
            phase=pipeline.current_phase.value,
        )

        return make_success_response(
            "Phase started",
            data={
                "phase": pipeline.current_phase.value,
                "status": phase_execution.status.value,
            },
        )

    except VersionConflictError:
        return make_error_response(
            f"Concurrent modification detected for pipeline {pipeline_id}. Please retry.",
            status_code=409,
        )
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


@phases_bp.route("/<pipeline_id>/phase/complete", methods=["POST"])
def complete_phase(pipeline_id: str) -> tuple[Response, int]:
    """
    Mark current phase as complete.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "artifacts": {...}  // optional, phase artifacts
        }

    Response:
        {
            "success": true,
            "message": "Phase completed",
            "data": {
                "phase": "implement",
                "next_phase": "pr"
            }
        }
    """
    repo_path = get_repo_path()
    data = request.get_json() or {}

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        original_version = pipeline.version

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
        phase_execution.status = PipelineStatus.COMPLETE
        phase_execution.completed_at = datetime.utcnow()

        # Store artifacts if provided
        if data.get("artifacts"):
            phase_execution.artifacts = data["artifacts"]

        # Determine next phase
        next_phases = PHASE_TRANSITIONS.get(pipeline.current_phase, [])
        next_phase = next_phases[0] if next_phases else None

        store.save_pipeline(pipeline, expected_version=original_version)

        logger.info(
            "Phase completed",
            pipeline_id=pipeline_id,
            phase=pipeline.current_phase.value,
        )

        return make_success_response(
            "Phase completed",
            data={
                "phase": pipeline.current_phase.value,
                "next_phase": next_phase.value if next_phase else None,
            },
        )

    except VersionConflictError:
        return make_error_response(
            f"Concurrent modification detected for pipeline {pipeline_id}. Please retry.",
            status_code=409,
        )
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


@phases_bp.route("/<pipeline_id>/phase/fail", methods=["POST"])
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
    repo_path = get_repo_path()
    data = request.get_json() or {}

    error_message = data.get("error")
    if not error_message:
        return make_error_response("Missing error message")

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        original_version = pipeline.version

        phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
        phase_execution.status = PipelineStatus.FAILED
        phase_execution.error = error_message
        phase_execution.completed_at = datetime.utcnow()

        pipeline.status = PipelineStatus.FAILED
        pipeline.error = error_message

        store.save_pipeline(pipeline, expected_version=original_version)

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
        )
    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
