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
from slice_id_validation import extract_slice_id as _extract_slice_id
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


# Heartbeat states that bypass the ``(state, waiting_on)`` dedup filter
# in the heartbeat route. ``WAITING_FOR_EVENT`` (issue #2036) is a
# liveness keep-alive emitted by ``mcp__brc__wait_loop`` while it's
# blocked — periodic identical beats are exactly the signal the
# overseer's stall detector consumes, so dedup must not collapse them.
# Rate-limit (20/min per slice+role; #2471) still applies.
_DEDUP_EXEMPT_HEARTBEAT_STATES: frozenset[str] = frozenset({"WAITING_FOR_EVENT"})

# Minimum seconds between gateway-session fan-outs per (pipeline_id,
# slice_id, role) (#2076 NB2, slice-scoped per #2471).  The dedup
# early-return path bypasses the per-(slice, role) heartbeat rate limit
# by design (#1897 NB1: dedup'd heartbeats are no-ops and must not
# consume rate budget), so without a separate cap a misbehaving agent
# hot-looping with identical state could amplify into the gateway at
# the agent's emission rate.  The gateway's idle window
# is 60 minutes, so fanning out every 30 s is far more than enough to
# keep the session alive; the cap exists purely to bound amplification.
_GATEWAY_FANOUT_MIN_INTERVAL_SECONDS: float = 30.0


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
    if body is None:
        return _make_error("Missing request body")
    if not isinstance(body, dict):
        return _make_error("Request body must be a JSON object")

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
    except ValueError, TypeError:
        return _make_error("Invalid limit parameter: must be an integer")

    # Long-polling support. Cap is configurable via EGG_MESSAGE_POLL_MAX_WAIT
    # (default 60s). If the cap is raised above the gateway's idle timeout,
    # requests will return 504; see docs/reference/agent-wait-patterns.md.
    try:
        wait = min(max(int(request.args.get("wait", "0")), 0), _get_poll_max_wait())
    except ValueError, TypeError:
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
        messages, meta = message_store.get_messages_with_meta(pipeline_id, **kwargs)
    finally:
        if wait > 0:
            _track_long_poll_end()

    messages = _apply_delphi_filter(pipeline_id, role, messages)

    data: dict[str, Any] = {
        "messages": [m.to_dict() for m in messages],
        "count": len(messages),
    }
    # Structured staleness signal (issue #2464). Only emitted when True so
    # responses stay byte-identical for the common case; consumers that
    # care (the sandbox CLI cursor file, agent wait_loop) clear their
    # cached cursor when they see this and re-snap to tip.
    if meta.since_id_stale:
        data["since_id_stale"] = True
    return _make_success("Messages retrieved", data=data)


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


# Message types that are produced *as a side effect* of a producer's
# own confirm reaching global consensus. A producer in WORKING/PROPOSED
# that waits on these would be waiting on itself — its own confirm is
# part of what generates the signal — and would deadlock until the
# overseer's stall detector intervened (#2064).
_PRODUCER_PENDING_CONFIRM_REJECTED_FOR_TYPES: frozenset[str] = frozenset({"CONSENSUS_CONFIRMED"})


def _check_producer_pending_confirm_guard(
    pipeline_id: str,
    role: str | None,
    wait_for_types: list[str],
) -> tuple[Response, int] | None:
    """Reject ``wait`` calls where a non-confirmed producer waits on
    ``CONSENSUS_CONFIRMED``.

    A producer's own ``mcp__brc__confirm`` is part of what generates
    the global ``CONSENSUS_CONFIRMED`` signal, so a producer in
    ``WORKING`` or ``PROPOSED`` that blocks on it would wait on
    itself (#2064). Rather than letting the overseer's heartbeat-stall
    detector bail the pipeline out minutes later, we surface the bug
    immediately with an actionable error.

    The guard intentionally ignores the route's ``from`` filter — even
    a wait narrowly scoped to a peer's per-agent ``CONSENSUS_CONFIRMED``
    is still part of a chain that requires this producer's own confirm
    to fire first. No documented producer pattern waits this way while
    in ``WORKING``/``PROPOSED``, so the over-rejection is harmless; any
    future cross-producer sync that wants to bypass it should update
    both this guard and the wait_loop client contract.

    Returns ``None`` when the wait should proceed; otherwise an error
    response tuple ready to return from the route.
    """
    if not role:
        return None
    blocking = _PRODUCER_PENDING_CONFIRM_REJECTED_FOR_TYPES.intersection(wait_for_types)
    if not blocking:
        return None
    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        get_peer_consensus_tracker = None  # type: ignore[assignment]
    if not get_peer_consensus_tracker:
        return None
    tracker = get_peer_consensus_tracker(pipeline_id)
    if tracker is None:
        return None
    if not tracker.is_producer_pending_confirm(role):
        return None
    sorted_blocking = sorted(blocking)
    return _make_error(
        f"Producer '{role}' cannot wait on {sorted_blocking} before its own "
        "consensus_confirmed has succeeded — its own confirm is part of "
        "what generates that signal, so the wait would deadlock (#2064). "
        "Call mcp__brc__confirm first; if it returns status='pending_acks' "
        "(e.g. another producer hasn't proposed yet, or your reviewers "
        "haven't ACKed), wait on the prerequisite events instead — "
        "CONSENSUS_PROPOSE from missing producers, CONSENSUS_ACK from "
        "your reviewers, or CONSENSUS_RE_REVIEW — then retry confirm."
    )


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
    # #2725: ``from_producer`` is the repeatable set form of ``from``.
    # ``from`` (singular) wins when both are provided so legacy callers
    # see no behaviour change. An explicit-but-empty list (e.g.
    # ``?from_producer=&from_producer=``) is rejected here rather than
    # silently dropped — silent acceptance would sleep the caller
    # through every event, which is worse than the wake-storm.
    raw_from_producers = request.args.getlist("from_producer")
    from_producers = [r for r in raw_from_producers if r]
    if raw_from_producers and not from_producers:
        return _make_error(
            "Invalid 'from_producer' parameter: must list at least one non-empty role"
        )
    # #2725: optional slice scope. Null-on-message is a passthrough so
    # OVERSEER_ALERT and global phase signals still wake slice-scoped
    # waiters.
    slice_id_arg = request.args.get("slice")
    if slice_id_arg is not None:
        try:
            slice_id_arg = _extract_slice_id({"slice_id": slice_id_arg})
        except ValueError as exc:
            return _make_error(f"Invalid slice: {exc}")
    since_id = request.args.get("since_id")
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError, TypeError:
        return _make_error("Invalid limit parameter: must be an integer")

    try:
        timeout = min(max(int(request.args.get("timeout", "0")), 0), _get_poll_max_wait())
    except ValueError, TypeError:
        timeout = 0

    if timeout <= 0:
        # A wait endpoint with no timeout is a bug.  Force at least 1 second
        # so the caller actually observes blocking semantics.
        timeout = 1

    guard_response = _check_producer_pending_confirm_guard(pipeline_id, role, wait_for_types)
    if guard_response is not None:
        return guard_response

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
        messages, meta = message_store.get_messages_with_meta(
            pipeline_id,
            role=role,
            since_id=since_id,
            limit=limit,
            wait=timeout,
            wait_for_types=wait_for_types,
            from_role=from_role,
            from_roles=from_producers or None,
            slice_id=slice_id_arg,
            from_tip=since_id is None,
        )
    finally:
        _track_long_poll_end()

    messages = _apply_delphi_filter(pipeline_id, role, messages)

    # Cursor returned on every response so callers can thread it into the
    # next ``since_id=`` and avoid missing events that arrive between
    # successive wait calls (issue #1995). On match: the last delivered
    # message ID. On timeout: the current stream tip so the next call
    # resumes strictly after what this call would have seen.
    if messages:
        cursor: str | None = messages[-1].id
    else:
        cursor = message_store.get_latest_id(pipeline_id)

    data: dict[str, Any] = {
        "messages": [m.to_dict() for m in messages],
        "count": len(messages),
        "matched": bool(messages),
        "cursor": cursor,
    }
    # Structured staleness signal (issue #2464). See poll_messages above.
    if meta.since_id_stale:
        data["since_id_stale"] = True
    return _make_success("Wait completed", data=data)


@messages_bp.route("/<pipeline_id>/heartbeat", methods=["POST"])
def post_heartbeat(pipeline_id: str) -> tuple[Response, int]:
    """Dedicated HEARTBEAT endpoint with per-role dedup + rate limit.

    Request body::

        {
            "from_role": "coder",
            "state": "WORKING" | "WAITING_ON_ROLE" | "WAITING_FOR_EVENT" | "PROPOSED" | "IDLE",
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
        * ``(state, waiting_on)`` tuples that match the most-recent
          heartbeat for this ``(pipeline_id, slice_id, role)`` are
          **silently deduped** (no bus message written) so re-entering
          the same state twice is idempotent.  See plan TASK-3-2.
        * Rate limit: ``EGG_HEARTBEAT_RATE_LIMIT`` per minute per
          ``(pipeline_id, slice_id, role)``, default 20/min.  See plan
          TASK-3-4.  Slice-scoping (#2471) keeps sibling slices that
          share a role from sharing each other's rate budget.
    """
    raw = request.get_json()
    if raw is not None and not isinstance(raw, dict):
        return _make_error("Request body must be a JSON object")
    body = raw if raw is not None else {}

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

    # Optional slice scope (#2451): slice-scoped agents forward
    # ``EGG_SLICE_ID`` so the gateway-session fan-out can reconstruct
    # the slice-scoped container_id that ``kubernetes_spawner``
    # registered. Pipeline-level agents send no ``slice_id`` and this
    # resolves to ``None``.
    try:
        slice_id = _extract_slice_id(body)
    except ValueError as exc:
        return _make_error(f"Invalid slice_id: {exc}")

    # Validate pipeline.
    try:
        _store, pipeline = get_state_store_for_pipeline(pipeline_id)
    except InvalidPipelineIdError as e:
        return _make_error(str(e), 400)
    except PipelineNotFoundError as e:
        return _make_error(str(e), 404)

    coordinator = get_heartbeat_coordinator()

    # Dedup first — duplicates are no-ops and should not consume rate
    # budget (review NB1, issue #1897). States in
    # ``_DEDUP_EXEMPT_HEARTBEAT_STATES`` skip this check; see the
    # constant's docstring for the rationale.
    #
    # Note: the gateway-session fan-out below runs *after* dedup but
    # *before* rate-limit.  Dedup'd heartbeats still fan out so an agent
    # stuck in a single state (e.g. ``WORKING`` through a slow
    # ``make test``) keeps its gateway session alive even when its BRC
    # state hasn't changed.  Rate-limited heartbeats do not fan out: by
    # definition the agent already got plenty of refreshes in the last
    # minute, and a hot-looping agent shouldn't amplify into the
    # gateway.  ``_refresh_gateway_session`` itself applies a separate
    # per-role cooldown (#2076 NB2) to bound dedup-path amplification
    # without consuming rate budget.
    if state not in _DEDUP_EXEMPT_HEARTBEAT_STATES and coordinator.is_duplicate(
        pipeline_id, slice_id, from_role, state, waiting_on
    ):
        _refresh_gateway_session(pipeline_id, from_role, slice_id)
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
    decision = coordinator.check_rate_limit(pipeline_id, slice_id, from_role, limit)
    if not decision.allowed:
        retry_after = int(decision.retry_after_seconds)
        resp = jsonify(
            {
                "success": False,
                "error": "rate_limited",
                "message": (
                    f"HEARTBEAT rate limit exceeded "
                    f"({limit}/min per slice+role); retry after {retry_after}s."
                ),
                "retry_after": retry_after,
            }
        )
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    # Refresh the agent's gateway session liveness (#2068).  Runs after
    # dedup and rate-limit gates: every accepted-or-deduped heartbeat
    # fans out (dedup'd path above), but rate-limited ones do not.
    # Best-effort: the gateway may be unreachable (tests, dev runs
    # without a gateway) and a missing session is a 404; never fail the
    # heartbeat on this path.
    _refresh_gateway_session(pipeline_id, from_role, slice_id)

    # Emit as a normal HEARTBEAT message on the bus so downstream
    # consumers (HealthMonitor, overseer, UI) see it.
    metadata: dict[str, Any] = {"state": state}
    if waiting_on:
        metadata["waiting_on"] = waiting_on
    if body.get("since"):
        metadata["since"] = body["since"]
    # Tag with slice_id so the implement-phase BRC writer can partition
    # this HEARTBEAT into the correct per-slice transcript (#2548).
    # Pipeline-level (non-slice) heartbeats leave the metadata off
    # entirely.
    if slice_id:
        metadata["slice_id"] = slice_id

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
    coordinator.record_state(pipeline_id, slice_id, from_role, state, waiting_on)

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


def _refresh_gateway_session(pipeline_id: str, from_role: str, slice_id: str | None = None) -> None:
    """Best-effort POST to the gateway so the BRC heartbeat counts as session liveness.

    Container-id normalization: k8s names are RFC-1123 labels (no
    underscores), so ``kubernetes_spawner.JOB_NAME_FORMAT`` is filled
    with ``agent_role.value.replace("_", "-")``.  ``from_role`` arrives
    from ``EGG_AGENT_ROLE`` which is the underscore form, so we mirror
    the same normalization here — otherwise roles like
    ``reviewer_refine`` build a container_id that never matches the
    registered session and the gateway returns 404.  See
    ``orchestrator/kubernetes_spawner.py:370-375`` for the reference
    pattern.

    Slice scope (#2451): slice-scoped agents register sessions under
    ``egg-agent-{pid}-{slice_id}-{role}`` (``JOB_NAME_FORMAT_SLICE``);
    pipeline-level agents register under ``egg-agent-{pid}-{role}``.
    When ``slice_id`` is supplied (forwarded from the heartbeat body
    via ``EGG_SLICE_ID``) the slice segment is embedded so the lookup
    matches; without it every slice-scoped agent's heartbeat fan-out
    would silently 404. The throttle key is also slice-aware so a
    sibling slice's fan-out does not suppress this slice's refresh.

    Trust model: ``from_role`` is taken at face value from the request
    body and is **not** correlated against the calling container's
    session.  This matches the existing message-bus trust model — any
    agent in any container can already post messages claiming to be
    another role — but with this fan-out a misbehaving agent can keep
    a sibling's gateway session alive past the idle timeout.  Tracked
    as a follow-up; spoofing here doesn't grant any new capability,
    only extends an existing session's lifetime.

    Throttle: per-role cooldown via
    ``HeartbeatCoordinator.should_fan_out_gateway_session`` (#2076 NB2)
    bounds amplification on the dedup early-return path, which bypasses
    the heartbeat rate limiter by design.  The cooldown is well below
    the gateway's 60-minute idle window so it does not risk session
    expiry under any realistic heartbeat cadence.
    """
    coordinator = get_heartbeat_coordinator()
    # The coordinator's throttle key is now ``(pipeline_id, slice_id,
    # role)`` (#2471) so concurrent slices that share a role (e.g. two
    # reviewer-code agents in different slices of the same wave) do not
    # suppress each other's fan-outs. Pass ``slice_id`` directly — no
    # synthetic role-string composition needed.
    if not coordinator.should_fan_out_gateway_session(
        pipeline_id, slice_id, from_role, _GATEWAY_FANOUT_MIN_INTERVAL_SECONDS
    ):
        return
    try:
        try:
            from gateway_client import get_gateway_client
        except ImportError:  # pragma: no cover
            from ..gateway_client import (
                get_gateway_client,  # type: ignore[no-redef,import-not-found]
            )

        # Mirror kubernetes_spawner.JOB_NAME_FORMAT's role normalization
        # — k8s labels disallow underscores, so the registered
        # container_id uses hyphens.
        normalized_role = from_role.replace("_", "-")
        if slice_id:
            container_id = f"egg-agent-{pipeline_id}-{slice_id}-{normalized_role}"
        else:
            container_id = f"egg-agent-{pipeline_id}-{normalized_role}"
        get_gateway_client().heartbeat_session_by_container(container_id)
    except Exception as exc:  # pragma: no cover - logging only
        logger.warning(
            "Gateway session heartbeat fan-out failed",
            pipeline_id=pipeline_id,
            from_role=from_role,
            slice_id=slice_id,
            error=str(exc),
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
