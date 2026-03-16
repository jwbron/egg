"""Progress event endpoints for structured agent progress tracking.

Provides REST endpoints for agents to emit and query structured progress
events, enabling stall detection and adaptive health monitoring.
"""

import sys
import uuid
from datetime import datetime, timezone
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


from events import EventType, emit_event
from models import ProgressEvent, ProgressState
from progress_store import get_progress_store

logger = get_logger("orchestrator.progress")

progress_bp = Blueprint("progress", __name__, url_prefix="/api/v1/pipelines")

VALID_STATES = {s.value for s in ProgressState}


def _make_error(message: str, status_code: int = 400) -> tuple[Response, int]:
    return jsonify({"success": False, "message": message}), status_code


def _make_success(message: str, data: dict[str, Any] | None = None) -> tuple[Response, int]:
    resp: dict[str, Any] = {"success": True, "message": message}
    if data:
        resp["data"] = data
    return jsonify(resp), 200


@progress_bp.route("/<pipeline_id>/progress", methods=["POST"])
def emit_progress(pipeline_id: str) -> tuple[Response, int]:
    """Emit a structured progress event.

    Request body:
        {
            "agent_role": "coder",
            "step": "running tests",
            "state": "working",
            "detail": "pytest suite 3/5",
            "blocker": ""
        }
    """
    body = request.get_json()
    if not body:
        return _make_error("Missing request body")

    agent_role = body.get("agent_role")
    if not agent_role:
        return _make_error("Missing required field: agent_role")

    step = body.get("step")
    if not step:
        return _make_error("Missing required field: step")

    state = body.get("state")
    if not state:
        return _make_error("Missing required field: state")

    if state not in VALID_STATES:
        return _make_error(
            f"Invalid state '{state}'. Must be one of: {', '.join(sorted(VALID_STATES))}"
        )

    event_id = str(uuid.uuid4())

    event = ProgressEvent(
        id=event_id,
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        step=step,
        state=ProgressState(state),
        detail=body.get("detail", ""),
        blocker=body.get("blocker", ""),
        timestamp=datetime.now(timezone.utc),
    )

    store = get_progress_store()
    store.add_event(event)

    # Emit event on EventBus for monitoring
    emit_event(
        EventType.PROGRESS_EMITTED,
        pipeline_id,
        data={
            "event_id": event_id,
            "agent_role": agent_role,
            "step": step,
            "state": state,
        },
    )

    logger.info(
        "Progress event emitted",
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        step=step,
        state=state,
    )

    return _make_success(
        "Progress event recorded",
        data={"event": event.model_dump(mode="json")},
    )


@progress_bp.route("/<pipeline_id>/progress", methods=["GET"])
def query_progress(pipeline_id: str) -> tuple[Response, int]:
    """Query progress events for a pipeline.

    Query params:
        agent_role: Filter by agent role
        since: ISO timestamp to filter events after
        limit: Max events to return (default 100)
    """
    agent_role = request.args.get("agent_role")

    since = None
    since_str = request.args.get("since")
    if since_str:
        try:
            since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return _make_error("Invalid 'since' timestamp format. Use ISO 8601.")

    try:
        limit = int(request.args.get("limit", "100"))
    except (ValueError, TypeError):
        return _make_error("Invalid limit parameter: must be an integer")

    store = get_progress_store()
    events = store.get_events(
        pipeline_id,
        agent_role=agent_role,
        since=since,
        limit=limit,
    )

    return _make_success(
        "Progress events retrieved",
        data={
            "events": [e.model_dump(mode="json") for e in events],
            "count": len(events),
        },
    )
