"""Anchor CRUD endpoints for agent state persistence.

Provides REST endpoints for storing, retrieving, and managing agent
anchor files via Redis for cross-agent access and durable storage.
"""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Regex for valid agent IDs: alphanumeric, hyphens, underscores only
_VALID_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Add parent directory to path for imports
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

# Add shared directory to path
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_anchor.validator import check_size_budget, validate_anchor

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.anchors")

anchors_bp = Blueprint("anchors", __name__, url_prefix="/api/v1/anchors")

# Redis connection for anchor storage
_redis_client = None
_ANCHOR_TTL_SECONDS = 7 * 24 * 3600  # 7 days for failed pipelines


def _get_redis():
    """Get or create Redis client for anchor storage."""
    global _redis_client
    if _redis_client is None:
        try:
            import redis

            _redis_client = redis.Redis(
                host="localhost",
                port=6379,
                db=0,
                decode_responses=True,
            )
        except Exception as e:
            logger.warning("Redis not available for anchors: %s", e)
            return None
    return _redis_client


def _anchor_key(pipeline_id: str, agent_id: str) -> str:
    """Get the Redis key for an anchor."""
    return f"anchor:{pipeline_id}:{agent_id}"


def _validate_agent_id(agent_id: str) -> str | None:
    """Validate agent_id. Returns error message if invalid, None if valid."""
    if not agent_id or len(agent_id) > 128:
        return "agent_id must be 1-128 characters"
    if not _VALID_AGENT_ID_RE.match(agent_id):
        return "agent_id must contain only alphanumeric characters, hyphens, and underscores"
    return None


def _make_error(message: str, status_code: int = 400) -> tuple[Response, int]:
    return jsonify({"success": False, "message": message}), status_code


def _make_success(
    message: str, data: dict[str, Any] | None = None, status_code: int = 200
) -> tuple[Response, int]:
    resp: dict[str, Any] = {"success": True, "message": message}
    if data:
        resp["data"] = data
    return jsonify(resp), status_code


@anchors_bp.route("/<agent_id>", methods=["POST"])
def create_or_update_anchor(agent_id: str) -> tuple[Response, int]:
    """Create or update an agent's anchor.

    Stores the anchor in Redis for cross-agent access.
    Validates against JSON Schema on write.
    """
    agent_id_error = _validate_agent_id(agent_id)
    if agent_id_error:
        return _make_error(agent_id_error)

    body = request.get_json()
    if not body:
        return _make_error("Missing request body")

    # Validate agent_id consistency between URL and body
    body_agent_id = body.get("agent_id")
    if body_agent_id and body_agent_id != agent_id:
        return _make_error("agent_id in body does not match URL parameter")

    # Validate the anchor data against JSON Schema
    errors = validate_anchor(body)
    if errors:
        return _make_error(f"Schema validation failed: {'; '.join(errors)}")

    # Check size budget
    budget = check_size_budget(body)
    if not budget.within_budget:
        return _make_error(f"Anchor too large: {'; '.join(budget.errors)}")
    for warning in budget.warnings:
        logger.warning(warning, agent_id=agent_id)

    # Extract pipeline_id from anchor data
    pipeline_id = body.get("pipeline_id", "unknown")

    # Store in Redis
    r = _get_redis()
    if r:
        try:
            key = _anchor_key(pipeline_id, agent_id)
            # Check if this is a new anchor or an update
            is_new = not r.exists(key)
            # Set with a default 24h TTL, refreshed on each write
            r.set(key, json.dumps(body), ex=24 * 3600)
            logger.info(
                "Anchor stored",
                agent_id=agent_id,
                pipeline_id=pipeline_id,
            )
        except Exception as e:
            logger.warning("Failed to store anchor in Redis: %s", e)
            return _make_error(f"Failed to store anchor: {e}", 500)
    else:
        return _make_error("Redis not available", 503)

    status_code = 201 if is_new else 200
    return _make_success("Anchor stored", data={"agent_id": agent_id}, status_code=status_code)


@anchors_bp.route("/<agent_id>", methods=["GET"])
def get_anchor(agent_id: str) -> tuple[Response, int]:
    """Retrieve an agent's anchor from Redis."""
    agent_id_error = _validate_agent_id(agent_id)
    if agent_id_error:
        return _make_error(agent_id_error)

    pipeline_id = request.args.get("pipeline_id")

    if not pipeline_id:
        return _make_error("Could not determine pipeline_id", 400)

    r = _get_redis()
    if not r:
        return _make_error("Redis not available", 503)

    try:
        key = _anchor_key(pipeline_id, agent_id)
        data = r.get(key)
        if data:
            return _make_success("Anchor retrieved", data={"anchor": json.loads(data)})
        else:
            return _make_error(f"No anchor found for agent {agent_id}", 404)
    except Exception as e:
        return _make_error(f"Failed to retrieve anchor: {e}", 500)


@anchors_bp.route("/<agent_id>", methods=["DELETE"])
def delete_anchor(agent_id: str) -> tuple[Response, int]:
    """Delete an agent's anchor from Redis."""
    agent_id_error = _validate_agent_id(agent_id)
    if agent_id_error:
        return _make_error(agent_id_error)

    pipeline_id = request.args.get("pipeline_id")

    if not pipeline_id:
        return _make_error("Could not determine pipeline_id", 400)

    r = _get_redis()
    if not r:
        return _make_error("Redis not available", 503)

    try:
        key = _anchor_key(pipeline_id, agent_id)
        deleted = r.delete(key)
        if deleted:
            return _make_success("Anchor deleted", data={"agent_id": agent_id})
        else:
            return _make_error(f"No anchor found for agent {agent_id}", 404)
    except Exception as e:
        return _make_error(f"Failed to delete anchor: {e}", 500)


@anchors_bp.route("/team/<pipeline_id>", methods=["GET"])
def get_team_anchor(pipeline_id: str) -> tuple[Response, int]:
    """Get a combined team anchor view for a pipeline.

    Generates a projection from all individual agent anchors in the pipeline,
    including agent statuses, decisions, dependency graph, and BRC consensus summary.
    This is never directly written — always generated on read.
    """
    r = _get_redis()
    if not r:
        return _make_error("Redis not available", 503)

    try:
        # Scan for all anchors in this pipeline
        pattern = f"anchor:{pipeline_id}:*"
        keys = list(r.scan_iter(pattern))

        agents: list[dict[str, Any]] = []
        all_decisions: list[dict[str, Any]] = []
        brc_summary: dict[str, Any] = {
            "confirmed": [],
            "pending": [],
            "working": [],
        }

        for key in keys:
            data = r.get(key)
            if not data:
                continue
            anchor = json.loads(data)
            agent_id = anchor.get("agent_id", "unknown")

            agents.append(
                {
                    "agent_id": agent_id,
                    "role": anchor.get("role", "unknown"),
                    "status": anchor.get("status", "unknown"),
                    "task": anchor.get("task", {}),
                    "current_progress": (
                        anchor.get("progress", [])[-1] if anchor.get("progress") else None
                    ),
                    "files_modified": anchor.get("files_modified", []),
                    "errors": len(anchor.get("errors_encountered", [])),
                }
            )

            # Collect decisions (copy to avoid mutating deserialized data)
            for d in anchor.get("decisions", []):
                all_decisions.append({**d, "from_agent": agent_id})

            # BRC summary
            brc = anchor.get("brc_state", {})
            brc_phase = brc.get("phase", "orient")
            if brc_phase == "confirmed":
                brc_summary["confirmed"].append(agent_id)
            elif brc_phase in ("working", "proposed", "reviewing"):
                brc_summary["working"].append(agent_id)
            else:
                brc_summary["pending"].append(agent_id)

        team_anchor = {
            "pipeline_id": pipeline_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "agent_count": len(agents),
            "agents": agents,
            "decisions": all_decisions,
            "brc_consensus": brc_summary,
        }

        return _make_success("Team anchor generated", data={"team_anchor": team_anchor})

    except Exception as e:
        logger.error("Failed to generate team anchor: %s", e)
        return _make_error(f"Failed to generate team anchor: {e}", 500)


@anchors_bp.route("/gc/<pipeline_id>", methods=["POST"])
def gc_anchors(pipeline_id: str) -> tuple[Response, int]:
    """Garbage collect anchors for a pipeline.

    For completed pipelines: archive to checkpoint then clear from Redis.
    For failed pipelines: set 7-day TTL.
    """
    raw = request.get_json()
    if raw is not None and not isinstance(raw, dict):
        return _make_error("Request body must be a JSON object")
    body = raw if raw is not None else {}
    pipeline_status = body.get("status", "completed")

    r = _get_redis()
    if not r:
        return _make_error("Redis not available", 503)

    try:
        pattern = f"anchor:{pipeline_id}:*"
        keys = list(r.scan_iter(pattern))

        if pipeline_status == "failed":
            # Set TTL for failed pipeline anchors
            for key in keys:
                r.expire(key, _ANCHOR_TTL_SECONDS)
            return _make_success(
                f"Set {_ANCHOR_TTL_SECONDS}s TTL on {len(keys)} anchor(s)",
                data={"keys": len(keys), "ttl_seconds": _ANCHOR_TTL_SECONDS},
            )
        else:
            # Delete anchors for completed pipelines
            deleted = 0
            for key in keys:
                deleted += r.delete(key)
            return _make_success(
                f"Deleted {deleted} anchor(s)",
                data={"deleted": deleted},
            )

    except Exception as e:
        return _make_error(f"GC failed: {e}", 500)
