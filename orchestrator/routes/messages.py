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
from message_store import (
    HEARTBEAT_STATES,
    Message,
    MessageType,
    coerce_deprecated_message_type,
    get_message_store,
)
from routes import get_state_store_for_pipeline
from state_store import InvalidPipelineIdError, PipelineNotFoundError

logger = get_logger("orchestrator.messages")

# Delegate env-var reads to the central ``env_config`` module (issue
# #1897) so routes and the CLI agree on a single source of truth.
try:
    from env_config import (
        DEFAULT_MESSAGE_POLL_MAX_WAIT_SECONDS,
        MESSAGE_POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS,
        get_heartbeat_rate_limit,
        get_message_poll_max_wait,
        log_message_poll_max_wait_startup,
    )
    from heartbeat import get_heartbeat_coordinator
except ImportError:  # pragma: no cover
    from ..env_config import (  # type: ignore[no-redef,import-not-found]
        DEFAULT_MESSAGE_POLL_MAX_WAIT_SECONDS,
        MESSAGE_POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS,
        get_heartbeat_rate_limit,
        get_message_poll_max_wait,
        log_message_poll_max_wait_startup,
    )
    from ..heartbeat import get_heartbeat_coordinator  # type: ignore[no-redef]

# Back-compat aliases (the helpers below are re-exported so existing
# callers and tests continue to work).
_get_poll_max_wait = get_message_poll_max_wait
log_poll_max_wait_startup = log_message_poll_max_wait_startup
POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS = MESSAGE_POLL_MAX_WAIT_WARN_THRESHOLD_SECONDS
DEFAULT_POLL_MAX_WAIT_SECONDS = DEFAULT_MESSAGE_POLL_MAX_WAIT_SECONDS

# In-flight long-poll gauge (RISK-3, issue #1897). Incremented when a
# caller enters a blocking read and decremented when the call returns.
# Operators alert when this approaches the configured
# ``EGG_ORCH_WAITRESS_THREADS`` value.
try:
    from metrics import get_metrics_registry

    _inflight_long_polls = get_metrics_registry().gauge(
        "egg_inflight_long_polls",
        labels={"endpoint": "messages"},
    )
except Exception:  # pragma: no cover - metrics best-effort
    _inflight_long_polls = None


def _track_long_poll_start() -> None:
    if _inflight_long_polls is not None:
        try:
            _inflight_long_polls.inc()
        except Exception:  # pragma: no cover
            pass


def _track_long_poll_end() -> None:
    if _inflight_long_polls is not None:
        try:
            _inflight_long_polls.dec()
        except Exception:  # pragma: no cover
            pass


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
            "message_type": "PROGRESS" | "STATUS" | "HANDOFF" | "HEARTBEAT" | ...,
            "subject": "Implementation update",
            "body": "Completed task 1-1",
            "metadata": {}
        }

    Note: the ``QUESTION`` message type was removed in issue #1897.
    Prefer the NACK ``--reason`` channel (``### Non-blocking``) to ask
    questions atomically with the review verdict.
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

    # Normalise deprecated message_type values (issue #1897) before any
    # downstream logic sees them — e.g. a replayed ``QUESTION`` becomes
    # ``PROGRESS`` so the audit trail is preserved without a now-unknown
    # enum member circulating through the bus.
    message_type = coerce_deprecated_message_type(message_type)

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
            return _make_error("HEARTBEAT metadata must be an object with a 'state' field.")
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
    if wait > 0:
        _track_long_poll_start()
    try:
        messages = message_store.get_messages(pipeline_id, **kwargs)
    finally:
        if wait > 0:
            _track_long_poll_end()

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
                            k: v for k, v in payload.items() if k in ("version", "commit_sha")
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

    _track_long_poll_start()
    try:
        # from_role is applied INSIDE the blocking read (message_store
        # level) so a message with a matching TYPE but the wrong
        # sender does not unblock us — prevents client-side spin.
        #
        # from_tip is set when no since_id is supplied so only events
        # arriving AFTER this call can unblock the wait. Without it,
        # repeated wait-loop invocations all re-match the same
        # already-seen event because the store starts scanning from
        # "0-0" (issue #1925). Callers that want cursor-passing
        # semantics pass ``since_id`` explicitly, which disables
        # from_tip below.
        messages = message_store.get_messages(
            pipeline_id,
            role=role,
            since_id=since_id,
            limit=limit,
            wait=timeout,
            wait_for_types=wait_for_types,
            from_role=from_role,
            from_tip=since_id is None,
        )
    finally:
        _track_long_poll_end()

    messages = _apply_delphi_filter(pipeline_id, role, messages)

    return _make_success(
        "Wait completed",
        data={
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
            "matched": bool(messages),
        },
    )


@messages_bp.route("/<pipeline_id>/heartbeat", methods=["POST"])
def post_heartbeat(pipeline_id: str) -> tuple[Response, int]:
    """Dedicated HEARTBEAT endpoint with per-role dedup + rate limit.

    Request body::

        {
            "from_role": "coder",
            "state": "WORKING" | "WAITING_ON_ROLE" | "PROPOSED" | "IDLE",
            "waiting_on": "tester",  # required when state=WAITING_ON_ROLE
            "since": "2026-04-23T07:00:00Z",  # optional
            "body": "short human-readable summary"  # optional
        }

    Responses:
        200 — heartbeat stored (or silently deduped).
        400 — missing / invalid field.
        404 — pipeline not found.
        429 — rate limit exceeded; response body carries
              ``retry_after`` seconds.

    Implementation notes:
        * ``(state, waiting_on)`` tuples that match the role's
          most-recent heartbeat are **silently deduped** (no bus
          message written) so re-entering the same state twice is
          idempotent.  See plan TASK-3-2.
        * Rate limit: ``EGG_HEARTBEAT_RATE_LIMIT`` per minute per
          ``(pipeline_id, role)``, default 20/min.  See plan
          TASK-3-4.
    """
    body = request.get_json() or {}

    from_role = body.get("from_role")
    if not from_role:
        return _make_error("Missing from_role")

    state = body.get("state")
    if state not in HEARTBEAT_STATES:
        return _make_error(f"state must be one of {sorted(HEARTBEAT_STATES)} (got {state!r}).")
    waiting_on = body.get("waiting_on")
    if state == "WAITING_ON_ROLE" and not waiting_on:
        return _make_error(
            "state=WAITING_ON_ROLE requires waiting_on (the role this agent is waiting on)."
        )

    # Validate pipeline.
    try:
        _store, pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError as e:
        return _make_error(str(e), 400)
    except PipelineNotFoundError as e:
        return _make_error(str(e), 404)

    coordinator = get_heartbeat_coordinator()

    # Dedup first — duplicates are no-ops and should not consume rate
    # budget (review NB1, issue #1897).
    if coordinator.is_duplicate(pipeline_id, from_role, state, waiting_on):
        return _make_success(
            "HEARTBEAT deduped (unchanged state)",
            data={"deduped": True},
        )

    # Rate limit — cheap check, bounds load.
    #
    # The 429 response shape is specified in issue #1897 reviewer_contract
    # blocker 5: the body MUST carry ``error: "rate_limited"`` and
    # ``retry_after: N`` so clients (``cmd_message_heartbeat``, external
    # integrators) can parse a stable contract, and the HTTP
    # ``Retry-After`` response header MUST echo N so standards-compliant
    # HTTP clients can honour it without parsing the JSON.
    limit = get_heartbeat_rate_limit()
    decision = coordinator.check_rate_limit(pipeline_id, from_role, limit)
    if not decision.allowed:
        retry_after = int(decision.retry_after_seconds)
        resp = jsonify(
            {
                "success": False,
                "error": "rate_limited",
                "message": (
                    f"HEARTBEAT rate limit exceeded "
                    f"({limit}/min per role); retry after {retry_after}s."
                ),
                "retry_after": retry_after,
            }
        )
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    # Emit as a normal HEARTBEAT message on the bus so downstream
    # consumers (HealthMonitor, overseer, UI) see it.
    metadata = {"state": state}
    if waiting_on:
        metadata["waiting_on"] = waiting_on
    if body.get("since"):
        metadata["since"] = body["since"]

    msg = Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role="all",
        message_type=MessageType.HEARTBEAT,
        subject=f"heartbeat: {state}",
        body=body.get("body", ""),
        metadata=metadata,
        phase=pipeline.current_phase.value,
    )
    store = get_message_store()
    store.add_message(msg)
    coordinator.record_state(pipeline_id, from_role, state, waiting_on)

    # Emit an event so SSE consumers and HealthMonitor see it.
    emit_event(
        EventType.MESSAGE_SENT,
        pipeline_id,
        data={
            "message_id": msg.id,
            "from_role": from_role,
            "to_role": msg.to_role,
            "message_type": MessageType.HEARTBEAT,
        },
    )

    return _make_success(
        "HEARTBEAT stored",
        data={"message": msg.to_dict(), "deduped": False},
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
