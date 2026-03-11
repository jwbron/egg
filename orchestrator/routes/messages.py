"""Message endpoints for inter-agent communication.

Provides REST endpoints for agents to send, poll, and check status of
messages during concurrent phase execution.
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


from events import EventType, emit_event
from message_store import Message, get_message_store
from state_store import InvalidPipelineIdError, PipelineNotFoundError, get_state_store

logger = get_logger("orchestrator.messages")

messages_bp = Blueprint("messages", __name__, url_prefix="/api/v1/pipelines")


def _make_error(message: str, status_code: int = 400) -> tuple[Response, int]:
    return jsonify({"success": False, "message": message}), status_code


def _make_success(message: str, data: dict[str, Any] | None = None) -> tuple[Response, int]:
    resp: dict[str, Any] = {"success": True, "message": message}
    if data:
        resp["data"] = data
    return jsonify(resp), 200


@messages_bp.route("/<pipeline_id>/messages", methods=["POST"])
def send_message(pipeline_id: str) -> tuple[Response, int]:
    """Send a message to the inter-agent message bus.

    Request body:
        {
            "from_role": "coder",
            "to_role": "tester" | "all",
            "message_type": "PROGRESS" | "QUESTION" | "STATUS" | ...,
            "subject": "Implementation update",
            "body": "Completed task 1-1",
            "metadata": {}
        }
    """
    body = request.get_json()
    if not body:
        return _make_error("Missing request body")

    from_role = body.get("from_role")
    if not from_role:
        return _make_error("Missing from_role")

    message_type = body.get("message_type")
    if not message_type:
        return _make_error("Missing message_type")

    # Validate pipeline exists
    try:
        store = get_state_store()
        pipeline = store.load_pipeline(pipeline_id)
    except (InvalidPipelineIdError, PipelineNotFoundError) as e:
        return _make_error(str(e), 404)

    # Skip strict role validation — agents may send before being registered in phase execution

    msg = Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role=body.get("to_role", "all"),
        message_type=message_type,
        subject=body.get("subject", ""),
        body=body.get("body", ""),
        metadata=body.get("metadata", {}),
        phase=pipeline.current_phase.value,
    )

    message_store = get_message_store()
    message_store.add_message(msg)

    # Emit event for SSE streaming and audit
    emit_event(
        EventType.MESSAGE_SENT,
        pipeline_id,
        data={
            "message_id": msg.id,
            "from_role": from_role,
            "to_role": msg.to_role,
            "message_type": message_type,
        },
    )

    logger.info(
        "Message sent",
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role=msg.to_role,
        message_type=message_type,
    )

    return _make_success("Message sent", data={"message": msg.to_dict()})


@messages_bp.route("/<pipeline_id>/messages", methods=["GET"])
def poll_messages(pipeline_id: str) -> tuple[Response, int]:
    """Poll for messages.

    Query params:
        role: Filter messages for this role (returns targeted + broadcast)
        since_id: Return only messages after this ID
        limit: Max messages to return (default 100)
    """
    role = request.args.get("role")
    since_id = request.args.get("since_id")
    limit = int(request.args.get("limit", "100"))

    message_store = get_message_store()
    messages = message_store.get_messages(
        pipeline_id,
        role=role,
        since_id=since_id,
        limit=limit,
    )

    return _make_success(
        "Messages retrieved",
        data={
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
        },
    )


@messages_bp.route("/<pipeline_id>/messages/status", methods=["GET"])
def message_status(pipeline_id: str) -> tuple[Response, int]:
    """Get message bus status for a pipeline."""
    message_store = get_message_store()
    status = message_store.get_status(pipeline_id)
    return _make_success("Status retrieved", data=status)
