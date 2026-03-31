"""
Decision endpoints for HITL integration.

Provides REST endpoints for queuing, polling, and resolving
human-in-the-loop decisions.
"""

import sys
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


import re

from decision_queue import (
    DecisionAlreadyResolvedError,
    DecisionNotFoundError,
    get_decision_queue,
)
from events import EventType, emit_event
from models import PipelinePhase
from peer_consensus import get_peer_consensus_tracker
from state_store import InvalidPipelineIdError, PipelineNotFoundError

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


from routes import get_state_store_for_pipeline  # noqa: E402 — shared helper


def _handle_restart_agent(pipeline_id: str, question: str) -> None:
    """Stop and respawn a stalled agent container.

    Parses the agent role from the HITL decision question
    (format: ``"Agent <role> issue: ..."``) and uses the Docker client
    to stop the old container.  A ``CONTAINER_STOPPED`` event is emitted
    so the pipeline orchestration loop can decide whether to respawn.

    Args:
        pipeline_id: Pipeline ID.
        question: The decision question text containing the agent role.
    """
    match = re.match(r"Agent\s+(\S+)\s+issue:", question)
    if not match:
        logger.warning(
            "Could not parse agent role from restart decision",
            pipeline_id=pipeline_id,
            question=question[:120],
        )
        return

    agent_role = match.group(1)
    logger.info(
        "Restarting agent via HITL decision",
        pipeline_id=pipeline_id,
        agent_role=agent_role,
    )

    try:
        from docker_client import get_docker_client

        docker_client = get_docker_client()
        containers = docker_client.list_containers(
            all=False,
            labels={"egg.pipeline.id": pipeline_id, "egg.agent.role": agent_role},
        )
        if not containers:
            logger.warning(
                "No running container found for agent",
                pipeline_id=pipeline_id,
                agent_role=agent_role,
            )
            return

        container = containers[0]
        docker_client.stop_container(container.container_id, timeout=10)
        logger.info(
            "Stopped stalled agent container for restart",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            container_id=container.container_id[:12],
        )

        # Emit event so health monitor / pipeline loop can track the stop
        try:
            emit_event(
                EventType.CONTAINER_STOPPED,
                pipeline_id=pipeline_id,
                data={
                    "container_id": container.container_id,
                    "agent_role": agent_role,
                    "reason": "hitl_restart",
                },
            )
        except Exception:
            logger.debug("Failed to emit CONTAINER_STOPPED event", exc_info=True)

    except Exception:
        logger.warning(
            "Failed to restart agent container",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            exc_info=True,
        )


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
    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
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

    pending_only = request.args.get("pending_only", "false").lower() == "true"

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)

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
                "decision_type": d.decision_type,
                "questions": d.questions,
                "status": d.status.value,
                "created_at": d.created_at.isoformat(),
                "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
                "resolution": d.resolution,
                "phase": d.phase.value if d.phase else None,
                "content_changed": d.content_changed,
            }
            for d in decisions
        ]

        return make_success_response(
            f"Found {len(decisions)} decision(s)",
            data={"decisions": decision_data},
        )

    except Exception as e:
        logger.error("Failed to list decisions", pipeline_id=pipeline_id, error=str(e))
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
            "options": ["Option A", "Option B"]  // optional
        }

    Response:
        {
            "success": true,
            "data": {
                "decision": {...}
            }
        }
    """
    data = request.get_json() or {}

    question = data.get("question")
    if not question:
        return make_error_response("Missing question")

    VALID_DECISION_TYPES = ("phase_gate", "choice", "feedback")
    decision_type = data.get("decision_type", "choice")
    if decision_type not in VALID_DECISION_TYPES:
        return make_error_response(
            f"Invalid decision_type '{decision_type}'. Must be one of: {', '.join(VALID_DECISION_TYPES)}"
        )

    phase_str = data.get("phase")
    phase = None
    if phase_str is not None:
        try:
            phase = PipelinePhase(phase_str)
        except ValueError:
            valid_phases = [p.value for p in PipelinePhase]
            return make_error_response(
                f"Invalid phase '{phase_str}'. Must be one of: {', '.join(valid_phases)}"
            )

    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
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

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        decision = queue.queue_decision(
            question=question,
            context=data.get("context", ""),
            options=data.get("options"),
            decision_type=decision_type,
            questions=data.get("questions"),
            phase=phase,
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
                    "decision_type": decision.decision_type,
                    "questions": decision.questions,
                    "status": decision.status.value,
                    "created_at": decision.created_at.isoformat(),
                    "phase": decision.phase.value if decision.phase else None,
                }
            },
        )

    except Exception as e:
        logger.error("Failed to queue decision", pipeline_id=pipeline_id, error=str(e))
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
    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
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

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        decision = queue.get_decision(decision_id)

        return make_success_response(
            "Decision retrieved",
            data={
                "decision": {
                    "id": decision.id,
                    "question": decision.question,
                    "context": decision.context,
                    "options": decision.options,
                    "decision_type": decision.decision_type,
                    "questions": decision.questions,
                    "status": decision.status.value,
                    "created_at": decision.created_at.isoformat(),
                    "resolved_at": decision.resolved_at.isoformat()
                    if decision.resolved_at
                    else None,
                    "resolution": decision.resolution,
                    "phase": decision.phase.value if decision.phase else None,
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
    data = request.get_json() or {}

    resolution = data.get("resolution")
    if not resolution:
        return make_error_response("Missing resolution")

    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
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

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        decision = queue.resolve_decision(decision_id, resolution)

        logger.info(
            "Decision resolved",
            pipeline_id=pipeline_id,
            decision_id=decision_id,
        )

        try:
            emit_event(
                EventType.DECISION_RESOLVED,
                pipeline_id=pipeline_id,
                data={
                    "decision_id": decision_id,
                    "resolution": decision.resolution,
                },
            )
        except Exception:
            logger.warning(
                "Failed to emit DECISION_RESOLVED event",
                pipeline_id=pipeline_id,
                decision_id=decision_id,
                exc_info=True,
            )

        # Handle "Restart agent" resolution (#1428).
        # The overseer creates decisions with question format:
        #   "Agent <role> issue: <message>"
        # When the human resolves with "Restart agent", stop the old
        # container and respawn a replacement.
        if decision.resolution == "Restart agent":
            _handle_restart_agent(pipeline_id, decision.question)

        # Handle "Continue without" resolution for failed reviewer decisions.
        # The concurrent executor stores "failed_role:<role>" in the decision
        # context when a reviewer crashes. Excuse the reviewer so consensus
        # can proceed without their ACK.
        if decision.resolution == "Continue without" and decision.context.startswith(
            "failed_role:"
        ):
            failed_role = decision.context.removeprefix("failed_role:")
            tracker = get_peer_consensus_tracker(pipeline_id)
            if tracker:
                try:
                    excuse_result = tracker.excuse_reviewer(failed_role)
                    logger.info(
                        "Excused reviewer after 'Continue without' decision",
                        pipeline_id=pipeline_id,
                        failed_role=failed_role,
                        affected_producers=excuse_result.get("affected_producers"),
                    )
                except Exception:
                    logger.warning(
                        "Failed to excuse reviewer",
                        pipeline_id=pipeline_id,
                        failed_role=failed_role,
                        exc_info=True,
                    )

        return make_success_response(
            "Decision resolved",
            data={
                "decision": {
                    "id": decision.id,
                    "status": decision.status.value,
                    "resolution": decision.resolution,
                    "resolved_at": decision.resolved_at.isoformat()
                    if decision.resolved_at
                    else None,
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
    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
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

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
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
    try:
        store, _pipeline = get_state_store_for_pipeline(pipeline_id)
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

    try:
        queue = get_decision_queue(pipeline_id, store.repo_path)
        status = queue.get_queue_status()

        return make_success_response("Status retrieved", data=status)

    except Exception as e:
        logger.error("Failed to get queue status", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(f"Failed to get status: {e}", status_code=500)
