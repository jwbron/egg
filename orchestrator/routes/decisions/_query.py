"""Decision read + create endpoints: list / get / queue (#3312 decomposition)."""

import routes.decisions as _pkg
from decision_queue import DecisionNotFoundError
from flask import Response, request
from models import PipelinePhase
from state_store import InvalidPipelineIdError, PipelineNotFoundError

from . import logger
from ._responses import make_error_response, make_success_response


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
        store, _pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
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
        queue = _pkg.get_decision_queue(pipeline_id, store.repo_path)

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
    raw = request.get_json()
    if raw is not None and not isinstance(raw, dict):
        return make_error_response("Request body must be a JSON object")
    data = raw if raw is not None else {}

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
        store, _pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
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
        queue = _pkg.get_decision_queue(pipeline_id, store.repo_path)
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
        store, _pipeline = _pkg.get_state_store_for_pipeline(pipeline_id)
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
        queue = _pkg.get_decision_queue(pipeline_id, store.repo_path)
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
