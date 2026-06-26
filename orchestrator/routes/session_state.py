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


@session_state_bp.route("/<pipeline_id>/session-state", methods=["POST"])
def push_session_state(pipeline_id: str) -> tuple[Response, int]:
    """Persist (overwrite) a role's warm-resume record.

    Request body::

        {
            "role": "coder",
            "slice_id": "slice-3",            # optional; null/absent → pipeline-level
            "session_id": "uuid",
            "window_occupancy": 123456,        # optional
            "transcript": "<jsonl text>"       # optional (pointer-only when absent)
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
    )
    # ``stored=False`` is not an error: an empty session_id or oversized transcript
    # simply degrades to a cold reseed next event. Report it so the caller can log.
    return jsonify({"success": True, "stored": stored}), 200


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
