"""
Health check endpoint for egg-orchestrator.
"""

from datetime import datetime

from flask import Blueprint, Response, jsonify

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
