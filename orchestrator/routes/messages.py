"""Message endpoints for inter-agent communication.

Provides REST endpoints for agents to send, poll, and check status of
messages during concurrent phase execution.
"""

import os
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
from message_store import HEARTBEAT_STATES, Message, MessageType, get_message_store
from routes import get_state_store_for_pipeline
from state_store import InvalidPipelineIdError, PipelineNotFoundError

logger = get_logger("orchestrator.messages")

messages_bp = Blueprint("messages", __name__, url_prefix="/api/v1/pipelines")

# Default cap on the ``wait`` query parameter.  Operators can raise this via
# the ``EGG_MESSAGE_POLL_MAX_WAIT`` env var; doing so REQUIRES raising the
# gateway's Squid idle timeout in lockstep or long polls will return 504.
# See docs/reference/agent-wait-patterns.md.
DEFAULT_POLL_MAX_WAIT_SECONDS = 60

# Threshold above which we log a WARNING at startup (and each lookup) naming
# the gateway coupling.  RISK-4 in issue #1897.
POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS = 90


def _get_poll_max_wait() -> int:
    """Return the effective ``wait=`` cap in seconds.

    Reads ``EGG_MESSAGE_POLL_MAX_WAIT`` (default 60). Values <= 0 fall back to
    the default. Coupled with the gateway's Squid idle timeout —
    see ``log_poll_max_wait_startup`` for the warning and
    ``docs/reference/agent-wait-patterns.md`` for the operator checklist.
    """
    raw = os.environ.get("EGG_MESSAGE_POLL_MAX_WAIT", "").strip()
    if not raw:
        return DEFAULT_POLL_MAX_WAIT_SECONDS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_MAX_WAIT_SECONDS
    if val <= 0:
        return DEFAULT_POLL_MAX_WAIT_SECONDS
    return val


def log_poll_max_wait_startup() -> None:
    """Emit a startup log line for the effective poll cap.

    Called by the orchestrator app at import time. When the cap exceeds
    ``POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS`` we escalate to WARNING and
    name the gateway Squid coupling so the operator has a fighting chance
    of not stepping on the 504 rake.
    """
    import warnings as _warnings

    cap = _get_poll_max_wait()
    if cap > POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS:
        msg = (
            f"EGG_MESSAGE_POLL_MAX_WAIT={cap}s exceeds the safe threshold "
            f"({POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS}s); ensure the gateway "
            "Squid idle timeout ConfigMap key is raised in lockstep or long "
            "polls will return 504. See docs/reference/agent-wait-patterns.md."
        )
        logger.warning(msg)
        _warnings.warn(msg, stacklevel=2)
    else:
        logger.info(
            "EGG_MESSAGE_POLL_MAX_WAIT effective cap: %ds", cap
        )


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

    # HEARTBEAT schema validation (issue #1897): metadata MUST contain a
    # ``state`` field in HEARTBEAT_STATES. ``WAITING_ON_ROLE`` requires
    # ``waiting_on``. A free-form ``since`` (epoch or ISO ts) may also be
    # provided. Bodies are freeform — only metadata is validated.
    metadata_raw = body.get("metadata", {}) or {}
    if message_type == MessageType.HEARTBEAT:
        if not isinstance(metadata_raw, dict):
            return _make_error(
                "HEARTBEAT metadata must be an object with a 'state' field."
            )
        state = metadata_raw.get("state")
        if state not in HEARTBEAT_STATES:
            return _make_error(
                "HEARTBEAT metadata.state must be one of "
                f"{sorted(HEARTBEAT_STATES)} (got {state!r})."
            )
        if state == "WAITING_ON_ROLE" and not metadata_raw.get("waiting_on"):
            return _make_error(
                "HEARTBEAT state=WAITING_ON_ROLE requires metadata.waiting_on "
                "(the role this agent is waiting on)."
            )

    msg = Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role=to_role_raw,
        message_type=message_type,
        subject=body.get("subject", ""),
        body=body.get("body", ""),
        metadata=metadata_raw,
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

    # Long-polling support. Cap is configurable via EGG_MESSAGE_POLL_MAX_WAIT
    # (default 60s). If the cap is raised above the gateway's idle timeout,
    # requests will return 504; see docs/reference/agent-wait-patterns.md.
    try:
        wait = min(max(int(request.args.get("wait", "0")), 0), _get_poll_max_wait())
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

    # NOTE: historical code fell back to a non-blocking read when the backend
    # did not accept ``wait``. That silent fallback has been removed (issue
    # #1897) — both backends now support ``wait`` natively. A TypeError here
    # indicates a regression and must propagate so CI catches it.
    messages = message_store.get_messages(pipeline_id, **kwargs)

    messages = _apply_delphi_filter(pipeline_id, role, messages)

    return _make_success(
        "Messages retrieved",
        data={
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
        },
    )


def _apply_delphi_filter(
    pipeline_id: str, role: str | None, messages: list[Message]
) -> list[Message]:
    """Apply Delphi visibility filtering to messages for a reviewer role.

    Extracted from ``poll_messages`` so it can be reused by the new
    ``/messages/wait`` endpoint (issue #1897).
    """
    if not role:
        return messages

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        get_peer_consensus_tracker = None  # type: ignore[assignment]

    if not get_peer_consensus_tracker:
        return messages

    tracker = get_peer_consensus_tracker(pipeline_id)
    if not tracker or not tracker.graph.is_reviewer(role):
        return messages

    filtered_messages = []
    for msg in messages:
        if msg.message_type == "CONSENSUS_PROPOSE":
            producer = msg.from_role
            if tracker.graph.get_edge(role, producer):
                if not tracker.matrix.has_reviewed(role, producer):
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
    return filtered_messages


@messages_bp.route("/<pipeline_id>/messages/wait", methods=["GET"])
def wait_messages(pipeline_id: str) -> tuple[Response, int]:
    """Block on a typed message event.

    Query params:
        for: message type to wait for (repeatable, required, >= 1)
        role: filter messages for this role (returns targeted + broadcast)
        from: filter messages from this sender role
        since_id: return only messages after this ID
        timeout: seconds to block (clamped by EGG_MESSAGE_POLL_MAX_WAIT)
        limit: max messages to return (default 100)

    Responses:
        200 — list of matching messages (possibly empty on timeout)
        400 — missing ``for`` parameter
        404 — pipeline not found

    Issue #1897: gives agents a first-class, event-driven primitive so
    they don't have to simulate blocking with sleep-and-poll loops.
    """
    try:
        get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError as e:
        return _make_error(str(e), 400)
    except PipelineNotFoundError as e:
        return _make_error(str(e), 404)

    wait_for_types = request.args.getlist("for")
    if not wait_for_types:
        return _make_error(
            "Missing 'for' query parameter — specify at least one message type "
            "to wait for (repeatable)."
        )

    role = request.args.get("role")
    from_role = request.args.get("from")
    since_id = request.args.get("since_id")
    try:
        limit = int(request.args.get("limit", "100"))
    except (ValueError, TypeError):
        return _make_error("Invalid limit parameter: must be an integer")

    try:
        timeout = min(max(int(request.args.get("timeout", "0")), 0), _get_poll_max_wait())
    except (ValueError, TypeError):
        timeout = 0

    if timeout <= 0:
        # A wait endpoint with no timeout is a bug.  Force at least 1 second
        # so the caller actually observes blocking semantics.
        timeout = 1

    message_store = get_message_store()

    messages = message_store.get_messages(
        pipeline_id,
        role=role,
        since_id=since_id,
        limit=limit,
        wait=timeout,
        wait_for_types=wait_for_types,
    )

    # Apply the from-role filter last (server-side; cheap).
    if from_role:
        messages = [m for m in messages if m.from_role == from_role]

    messages = _apply_delphi_filter(pipeline_id, role, messages)

    return _make_success(
        "Wait completed",
        data={
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
            "matched": bool(messages),
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
