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
from routes import get_state_store_for_pipeline
from state_store import InvalidPipelineIdError, PipelineNotFoundError

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

    # Catch the common shell-escape footgun where a caller sends e.g.
    # `--to $role` in a context that didn't expand the variable. The message
    # would otherwise be stored with a literal "$role" to_role and silently
    # fail to deliver to any real agent poll. See issue #1814.
    to_role_raw = body.get("to_role", "all")
    if isinstance(to_role_raw, str) and to_role_raw.startswith("$"):
        return _make_error(
            f"to_role looks like an unexpanded shell variable: {to_role_raw!r}. "
            "Pass the literal role name (e.g. 'architect') or 'all'."
        )
    if isinstance(from_role, str) and from_role.startswith("$"):
        return _make_error(f"from_role looks like an unexpanded shell variable: {from_role!r}.")

    # Validate pipeline exists
    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError as e:
        return _make_error(str(e), 400)
    except PipelineNotFoundError as e:
        return _make_error(str(e), 404)

    # Skip strict role validation — agents may send before being registered in phase execution

    msg = Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role=to_role_raw,
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
    # Validate pipeline exists (consistent with send_message)
    try:
        get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError as e:
        return _make_error(str(e), 400)
    except PipelineNotFoundError as e:
        return _make_error(str(e), 404)

    role = request.args.get("role")
    since_id = request.args.get("since_id")
    try:
        limit = int(request.args.get("limit", "100"))
    except (ValueError, TypeError):
        return _make_error("Invalid limit parameter: must be an integer")

    # Long-polling support
    try:
        wait = min(max(int(request.args.get("wait", "0")), 0), 60)
    except (ValueError, TypeError):
        wait = 0

    message_store = get_message_store()

    kwargs: dict[str, Any] = {
        "role": role,
        "since_id": since_id,
        "limit": limit,
    }
    if wait > 0:
        kwargs["wait"] = wait

    try:
        messages = message_store.get_messages(pipeline_id, **kwargs)
    except TypeError:
        # Fallback for in-memory store that doesn't support wait
        kwargs.pop("wait", None)
        messages = message_store.get_messages(pipeline_id, **kwargs)

    # Delphi visibility filtering: redact CONSENSUS_PROPOSE messages for
    # reviewers who haven't yet submitted their independent ACK/NACK.
    # Instead of dropping the message entirely (which causes deadlocks when
    # reviewers depend on polling to discover proposals), send a redacted
    # copy with body cleared and payload summary stripped, preserving the
    # message header so reviewers know a proposal exists.
    if role:
        try:
            from peer_consensus import get_peer_consensus_tracker
        except ImportError:
            get_peer_consensus_tracker = None  # type: ignore[assignment]

        if get_peer_consensus_tracker:
            tracker = get_peer_consensus_tracker(pipeline_id)
            if tracker and tracker.graph.is_reviewer(role):
                filtered_messages = []
                for msg in messages:
                    if msg.message_type == "CONSENSUS_PROPOSE":
                        producer = msg.from_role
                        # Only redact if this reviewer is assigned to this producer
                        if tracker.graph.get_edge(role, producer):
                            if not tracker.matrix.has_reviewed(role, producer):
                                # Redact: preserve header but strip body and
                                # sensitive payload fields.  Top-level
                                # metadata.version / metadata.commit_sha are
                                # intentionally kept — reviewers need them to
                                # identify which proposal to evaluate.  Only
                                # the nested payload dict is filtered.
                                redacted_metadata = dict(msg.metadata)
                                if "payload" in redacted_metadata:
                                    payload = redacted_metadata["payload"]
                                    redacted_metadata["payload"] = {
                                        k: v
                                        for k, v in payload.items()
                                        if k in ("version", "commit_sha")
                                    }
                                redacted_metadata["delphi_redacted"] = True
                                redacted_msg = msg.model_copy(
                                    update={
                                        "body": "",
                                        "metadata": redacted_metadata,
                                    }
                                )
                                filtered_messages.append(redacted_msg)
                                continue
                    filtered_messages.append(msg)
                messages = filtered_messages

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
    # Validate pipeline exists (consistent with send_message)
    try:
        get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError as e:
        return _make_error(str(e), 400)
    except PipelineNotFoundError as e:
        return _make_error(str(e), 404)

    message_store = get_message_store()
    status = message_store.get_status(pipeline_id)
    return _make_success("Status retrieved", data=status)
