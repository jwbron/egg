"""
Decision endpoints for HITL integration.

Provides REST endpoints for queuing, polling, and resolving
human-in-the-loop decisions.
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


from decision_queue import (
    DecisionAlreadyResolvedError,
    DecisionNotFoundError,
    DecisionTimeoutError,
    get_decision_queue,
)

logger = get_logger("orchestrator.decisions")

decisions_bp = Blueprint("decisions", __name__, url_prefix="/api/v1/pipelines")


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


@decisions_bp.route("/<pipeline_id>/decisions", methods=["GET"])
def list_decisions(pipeline_id: str) -> tuple[Response, int]:
    """
    List decisions for a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        pending_only: Only return pending decisions (default: false)

    Response:
        {
            "success": true,
            "data": {
                "decisions": [
                    {
                        "id": "decision-1",
                        "question": "...",
                        "status": "pending",
                        ...
                    }
                ]
            }
        }
    """
    repo_path = get_repo_path()
    pending_only = request.args.get("pending_only", "false").lower() == "true"

    try:
        queue = get_decision_queue(pipeline_id, repo_path)

        if pending_only:
            decisions = queue.get_pending_decisions()
        else:
            pipeline = queue._load_pipeline()
            decisions = pipeline.decisions

        decision_data = [
            {
                "id": d.id,
                "question": d.question,
                "context": d.context,
                "options": d.options,
                "status": d.status.value,
                "created_at": d.created_at.isoformat(),
                "timeout_seconds": d.timeout_seconds,
                "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
                "resolution": d.resolution,
            }
            for d in decisions
        ]

        return make_success_response(
            f"Found {len(decisions)} decision(s)",
            data={"decisions": decision_data},
        )

    except Exception as e:
        logger.error("Failed to list decisions", error=str(e))
        return make_error_response(f"Failed to list decisions: {e}", status_code=500)


@decisions_bp.route("/<pipeline_id>/decisions", methods=["POST"])
def queue_decision(pipeline_id: str) -> tuple[Response, int]:
    """
    Queue a new decision for human review.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "question": "Which approach should we use?",
            "context": "Additional context...",  // optional
            "options": ["Option A", "Option B"],  // optional
            "timeout_seconds": 3600  // optional
        }

    Response:
        {
            "success": true,
            "data": {
                "decision": {...}
            }
        }
    """
    repo_path = get_repo_path()
    data = request.get_json() or {}

    question = data.get("question")
    if not question:
        return make_error_response("Missing question")

    try:
        queue = get_decision_queue(pipeline_id, repo_path)
        decision = queue.queue_decision(
            question=question,
            context=data.get("context", ""),
            options=data.get("options"),
            timeout_seconds=data.get("timeout_seconds"),
        )

        logger.info(
            "Decision queued",
            pipeline_id=pipeline_id,
            decision_id=decision.id,
        )

        return make_success_response(
            "Decision queued",
            data={
                "decision": {
                    "id": decision.id,
                    "question": decision.question,
                    "status": decision.status.value,
                    "created_at": decision.created_at.isoformat(),
                }
            },
        )

    except Exception as e:
        logger.error("Failed to queue decision", error=str(e))
        return make_error_response(f"Failed to queue decision: {e}", status_code=500)


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>", methods=["GET"])
def get_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    """
    Get a specific decision.

    URL params:
        pipeline_id: Pipeline ID
        decision_id: Decision ID

    Response:
        {
            "success": true,
            "data": {
                "decision": {...}
            }
        }
    """
    repo_path = get_repo_path()

    try:
        queue = get_decision_queue(pipeline_id, repo_path)
        decision = queue.get_decision(decision_id)

        return make_success_response(
            "Decision retrieved",
            data={
                "decision": {
                    "id": decision.id,
                    "question": decision.question,
                    "context": decision.context,
                    "options": decision.options,
                    "status": decision.status.value,
                    "created_at": decision.created_at.isoformat(),
                    "timeout_seconds": decision.timeout_seconds,
                    "resolved_at": decision.resolved_at.isoformat() if decision.resolved_at else None,
                    "resolution": decision.resolution,
                }
            },
        )

    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found",
            status_code=404,
        )


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>/resolve", methods=["POST"])
def resolve_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    """
    Resolve a pending decision.

    URL params:
        pipeline_id: Pipeline ID
        decision_id: Decision ID

    Request body:
        {
            "resolution": "Selected option or free-form response"
        }

    Response:
        {
            "success": true,
            "data": {
                "decision": {...}
            }
        }
    """
    repo_path = get_repo_path()
    data = request.get_json() or {}

    resolution = data.get("resolution")
    if not resolution:
        return make_error_response("Missing resolution")

    try:
        queue = get_decision_queue(pipeline_id, repo_path)
        decision = queue.resolve_decision(decision_id, resolution)

        logger.info(
            "Decision resolved",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
        )

        return make_success_response(
            "Decision resolved",
            data={
                "decision": {
                    "id": decision.id,
                    "status": decision.status.value,
                    "resolution": decision.resolution,
                    "resolved_at": decision.resolved_at.isoformat() if decision.resolved_at else None,
                }
            },
        )

    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found",
            status_code=404,
        )
    except DecisionAlreadyResolvedError as e:
        return make_error_response(str(e), status_code=409)


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>/cancel", methods=["POST"])
def cancel_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    """
    Cancel a pending decision.

    URL params:
        pipeline_id: Pipeline ID
        decision_id: Decision ID

    Response:
        {
            "success": true,
            "message": "Decision cancelled"
        }
    """
    repo_path = get_repo_path()

    try:
        queue = get_decision_queue(pipeline_id, repo_path)
        decision = queue.cancel_decision(decision_id)

        logger.info(
            "Decision cancelled",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
        )

        return make_success_response(
            "Decision cancelled",
            data={
                "decision": {
                    "id": decision.id,
                    "status": decision.status.value,
                }
            },
        )

    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found",
            status_code=404,
        )


@decisions_bp.route("/<pipeline_id>/decisions/status", methods=["GET"])
def get_queue_status(pipeline_id: str) -> tuple[Response, int]:
    """
    Get decision queue status.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "total_decisions": 5,
                "pending": 2,
                "resolved": 3,
                "pending_decisions": [...]
            }
        }
    """
    repo_path = get_repo_path()

    try:
        queue = get_decision_queue(pipeline_id, repo_path)
        status = queue.get_queue_status()

        return make_success_response("Status retrieved", data=status)

    except Exception as e:
        logger.error("Failed to get queue status", error=str(e))
        return make_error_response(f"Failed to get status: {e}", status_code=500)


@decisions_bp.route("/<pipeline_id>/decisions/check-timeouts", methods=["POST"])
def check_timeouts(pipeline_id: str) -> tuple[Response, int]:
    """
    Check and handle timed-out decisions.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "timed_out": 1,
                "decisions": [...]
            }
        }
    """
    repo_path = get_repo_path()

    try:
        queue = get_decision_queue(pipeline_id, repo_path)
        timed_out = queue.check_timeouts()

        return make_success_response(
            f"{len(timed_out)} decision(s) timed out",
            data={
                "timed_out": len(timed_out),
                "decisions": [
                    {
                        "id": d.id,
                        "question": d.question,
                    }
                    for d in timed_out
                ],
            },
        )

    except Exception as e:
        logger.error("Failed to check timeouts", error=str(e))
        return make_error_response(f"Failed to check timeouts: {e}", status_code=500)
