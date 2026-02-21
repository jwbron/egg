"""
Health check endpoint for egg-orchestrator.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify

# Add paths for imports
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

health_bp = Blueprint("health", __name__, url_prefix="/api/v1")


@health_bp.route("/health", methods=["GET"])
def health_check() -> tuple[Response, int]:
    """
    Health check endpoint.

    Returns basic service status and component health.

    Response:
        {
            "status": "healthy",
            "service": "egg-orchestrator",
            "timestamp": "2024-01-15T12:00:00Z",
            "components": {
                "state_store": "ok",
                "docker": "ok"
            }
        }
    """
    # Basic health response
    response = {
        "status": "healthy",
        "service": "egg-orchestrator",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "components": {
            "state_store": "ok",
            "docker": "unknown",  # Will be updated when docker client is available
        },
    }

    return jsonify(response), 200


@health_bp.route("/ready", methods=["GET"])
def readiness_check() -> tuple[Response, int]:
    """
    Readiness check endpoint.

    Indicates if the service is ready to accept requests.
    Used by container orchestrators for traffic routing.

    Response:
        {"ready": true}
    """
    return jsonify({"ready": True}), 200


@health_bp.route("/live", methods=["GET"])
def liveness_check() -> tuple[Response, int]:
    """
    Liveness check endpoint.

    Indicates if the service is alive and should not be restarted.
    Used by container orchestrators for health monitoring.

    Response:
        {"alive": true}
    """
    return jsonify({"alive": True}), 200


@health_bp.route("/pipelines/<pipeline_id>/health", methods=["GET"])
def pipeline_health_check(pipeline_id: str) -> tuple[Response, int]:
    """
    On-demand health check for a specific pipeline.

    Runs all registered health checks (Tier 1 + Tier 2) and returns results.

    Response:
        {
            "pipeline_id": "issue-99",
            "status": "healthy",
            "results": [...],
            "timestamp": "2024-01-15T12:00:00Z"
        }
    """
    runner = current_app.config.get("HEALTH_CHECK_RUNNER")
    if runner is None:
        return jsonify(
            {
                "pipeline_id": pipeline_id,
                "status": "unknown",
                "message": "Health check runner not initialized",
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        ), 503

    try:
        from state_store import PipelineNotFoundError, get_state_store

        repo_path = os.environ.get("EGG_REPO_PATH", str(Path.cwd()))
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
    except PipelineNotFoundError:
        return jsonify(
            {
                "error": f"Pipeline {pipeline_id} not found",
            }
        ), 404
    except Exception as e:
        return jsonify(
            {
                "error": f"Failed to load pipeline: {e}",
            }
        ), 500

    try:
        from health_checks.context import PipelineHealthContext
        from health_checks.types import HealthTrigger

        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path(repo_path),
            trigger=HealthTrigger.ON_DEMAND.value,
            state_store=store,
        )
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)

        # Determine aggregate status
        aggregate_status = "healthy"
        for r in results:
            if r.status.value == "failed":
                aggregate_status = "failed"
                break
            if r.status.value == "degraded":
                aggregate_status = "degraded"

        return jsonify(
            {
                "pipeline_id": pipeline_id,
                "status": aggregate_status,
                "results": [r.to_dict() for r in results],
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        ), 200

    except Exception as e:
        return jsonify(
            {
                "error": f"Health check execution failed: {e}",
            }
        ), 500
