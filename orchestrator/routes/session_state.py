"""Cross-pod warm-resume session-state endpoints (#3278).

The orchestrator owns the durable, off-pod copy of each role's Claude Code
session (pointer + transcript) keyed ``(pipeline, slice, role)`` — see
``orchestrator/session_state_store.py``. The sandbox has no direct write access
to that store; it reaches it only through these routes (``egg-orch
session-state pull|push`` → ``orchestrator_request``), so the orchestrator stays
the single writer.

- ``POST /<pid>/session-state`` — an event pod, on exit, *pushes* its updated
  session (``session_id`` + ``window_occupancy`` + the JSONL ``transcript``).
- ``GET  /<pid>/session-state`` — the next event pod for the same ``(slice,
  role)``, on startup, *pulls* the prior session so it can re-materialise the
  transcript and warm-resume.
- ``DELETE /<pid>/session-state`` - operator-facing *eviction* (#3537): drops
  the record so the role's next spawn cold-reseeds instead of warm-resuming a
  session that has encoded a wrong conclusion about the world.
  ``restart_agent`` with ``fresh_session: true`` calls the same store method.

Both are best-effort by contract: a miss / failure degrades to a safe cold
reseed in the agent, so a GET miss returns ``200`` with ``found: false`` rather
than ``404`` (the caller treats "nothing to resume" and "store unavailable"
identically — it cold-starts).
"""

import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from session_state_store import get_session_state_store

logger = get_logger("orchestrator.session_state")

session_state_bp = Blueprint("session_state", __name__, url_prefix="/api/v1/pipelines")


def _make_error(message: str, status_code: int = 400) -> tuple[Response, int]:
    return jsonify({"success": False, "message": message}), status_code


# Cap for the advisory provenance strings. The only one today is an ISO-8601
# timestamp (~24 chars); this is generous room for that, and a bound on a
# client-supplied value that otherwise reaches the structured log stream
# unmeasured.
_MAX_PROVENANCE_STR = 64


def _advisory_str(block: dict, key: str) -> str | None:
    """A bounded string off the advisory block, or ``None``.

    The provenance block never gates the push, so a wrong-typed member is
    dropped rather than rejected — but "dropped" has to mean it does not reach
    the log line either, which is what a bare ``block.get(key)`` would let it
    do."""
    value = block.get(key)
    return value[:_MAX_PROVENANCE_STR] if isinstance(value, str) else None


def _advisory_int(block: dict, key: str) -> int | None:
    """An integer off the advisory block, or ``None``.

    ``bool`` is excluded for the same reason it is everywhere else in egg:
    ``isinstance(True, int)`` is True, and ``entries: true`` is not a count."""
    value = block.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@session_state_bp.route("/<pipeline_id>/session-state", methods=["POST"])
def push_session_state(pipeline_id: str) -> tuple[Response, int]:
    """Persist (overwrite) a role's warm-resume record.

    Request body::

        {
            "role": "coder",
            "slice_id": "slice-3",            # optional; null/absent → pipeline-level
            "session_id": "uuid",
            "window_occupancy": 123456,        # optional
            "transcript": "<jsonl text>",      # optional (pointer-only when absent)
            "transcript_provenance": {         # optional; logged, not stored
                "tail_timestamp": "2026-07-29T04:21:34.335Z",
                "entries": 3472,
                "assistant_turns": 1837,
                "bytes": 4788605
            }
        }
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _make_error("Request body must be a JSON object")

    role = body.get("role")
    if not role or not isinstance(role, str):
        return _make_error("Missing required field: role")

    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        return _make_error("Missing required field: session_id")

    slice_id = body.get("slice_id")
    if slice_id is not None and not isinstance(slice_id, str):
        return _make_error("slice_id must be a string or null")

    occupancy = body.get("window_occupancy")
    if occupancy is not None and (isinstance(occupancy, bool) or not isinstance(occupancy, int)):
        return _make_error("window_occupancy must be an integer or null")

    transcript = body.get("transcript")
    if transcript is not None and not isinstance(transcript, str):
        return _make_error("transcript must be a string or null")

    # Which transcript this push carried (#3692). The push is a single
    # on-exit snapshot, so a record can be a faithful capture of an early
    # moment while the session goes on working for another hour — the blob is
    # well-formed JSONL either way, which is what makes a stale record and a
    # current one indistinguishable by inspection. Logging the tail timestamp
    # against the push time turns that gap into a readable number. Advisory
    # and client-supplied: it describes the payload, it does not gate it, so a
    # malformed block — or a malformed member of a well-formed block — is
    # dropped rather than failing the push. Each member is type-checked on the
    # way out, matching the handling of every other field on this route: these
    # are the only client-supplied values here that reach a log line, and
    # "advisory" is a reason not to reject the push, not a reason to forward
    # arbitrary JSON into the structured log stream.
    prov = body.get("transcript_provenance")
    prov = prov if isinstance(prov, dict) else {}
    stored = get_session_state_store().put(
        pipeline_id,
        slice_id,
        role,
        session_id=session_id,
        window_occupancy=occupancy,
        transcript=transcript,
    )
    logger.info(
        "session-state push",
        event_type="system",
        event_subtype="session_state_push",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        role=role,
        stored=stored,
        has_transcript=transcript is not None,
        transcript_tail_timestamp=_advisory_str(prov, "tail_timestamp"),
        transcript_entries=_advisory_int(prov, "entries"),
        transcript_assistant_turns=_advisory_int(prov, "assistant_turns"),
        transcript_bytes=_advisory_int(prov, "bytes"),
    )
    # ``stored=False`` is not an error: an empty session_id or oversized transcript
    # simply degrades to a cold reseed next event. Report it so the caller can log.
    return jsonify({"success": True, "stored": stored}), 200


@session_state_bp.route("/<pipeline_id>/session-state/index", methods=["GET"])
def list_session_state(pipeline_id: str) -> tuple[Response, int]:
    """Return the operator-facing index of stored records (#3547).

    Transcripts are the one artifact that always survives a one-shot agent
    run, but until this route the only reader was the next event pod's
    ``session-state pull``. This index lets an operator (via the
    ``get_agent_transcript`` MCP tool) discover which ``(slice, role)``
    transcripts are currently readable; metadata only, no transcript bodies.
    """
    records = get_session_state_store().list_records(pipeline_id)
    return jsonify({"success": True, "records": records}), 200


@session_state_bp.route("/<pipeline_id>/session-state", methods=["GET"])
def pull_session_state(pipeline_id: str) -> tuple[Response, int]:
    """Return a role's warm-resume record, or ``found: false`` on any miss.

    Query params: ``role`` (required), ``slice_id`` (optional).
    """
    role = request.args.get("role")
    if not role:
        return _make_error("Missing required query param: role")
    slice_id = request.args.get("slice_id") or None

    record = get_session_state_store().get(pipeline_id, slice_id, role)
    if record is None:
        # Benign "nothing to resume" and "store unavailable" are intentionally
        # indistinguishable here — the agent cold-starts either way.
        return jsonify({"success": True, "found": False}), 200

    data: dict[str, Any] = {
        "session_id": record.session_id,
        "window_occupancy": record.window_occupancy,
        "transcript": record.transcript,
    }
    return jsonify({"success": True, "found": True, "data": data}), 200


@session_state_bp.route("/<pipeline_id>/session-state", methods=["DELETE"])
def evict_session_state(pipeline_id: str) -> tuple[Response, int]:
    """Evict a role's warm-resume record so the next spawn cold-reseeds (#3537).

    Query params: ``role`` (required), ``slice_id`` (optional). ``deleted``
    reports whether a record existed; a miss is not an error (the desired
    end state - no record - already holds).
    """
    role = request.args.get("role")
    if not role:
        return _make_error("Missing required query param: role")
    slice_id = request.args.get("slice_id") or None

    deleted = get_session_state_store().delete(pipeline_id, slice_id, role)
    logger.info(
        "session-state evict",
        event_type="system",
        event_subtype="session_state_evict",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        role=role,
        deleted=deleted,
    )
    return jsonify({"success": True, "deleted": deleted}), 200
