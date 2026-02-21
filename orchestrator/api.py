#!/usr/bin/env python3
"""
egg-orchestrator REST API.

Provides REST endpoints for SDLC pipeline orchestration, including:
- Pipeline CRUD operations
- Container lifecycle management
- HITL decision queue
- Health checks

Usage:
    python api.py [--host HOST] [--port PORT] [--debug]
"""

import argparse
import os
import sys
import time
from pathlib import Path

from flask import Flask, Response, g, jsonify, request
from waitress import serve

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.api")

app = Flask(__name__)


# Register blueprints
try:
    from routes.checks import checks_bp
    from routes.containers import containers_bp
    from routes.decisions import decisions_bp
    from routes.health import health_bp
    from routes.metrics import metrics_bp
    from routes.phases import phases_bp
    from routes.pipelines import pipelines_bp
    from routes.signals import signals_bp
    from webhooks import webhooks_bp

    app.register_blueprint(checks_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(pipelines_bp)
    app.register_blueprint(containers_bp)
    app.register_blueprint(phases_bp)
    app.register_blueprint(signals_bp)
    app.register_blueprint(decisions_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(webhooks_bp)
except ImportError:
    from .routes.checks import checks_bp  # type: ignore[no-redef]
    from .routes.containers import containers_bp  # type: ignore[no-redef]
    from .routes.decisions import decisions_bp  # type: ignore[no-redef]
    from .routes.health import health_bp  # type: ignore[no-redef]
    from .routes.metrics import metrics_bp  # type: ignore[no-redef]
    from .routes.phases import phases_bp  # type: ignore[no-redef]
    from .routes.pipelines import pipelines_bp  # type: ignore[no-redef]
    from .routes.signals import signals_bp  # type: ignore[no-redef]
    from .webhooks import webhooks_bp  # type: ignore[no-redef]

    app.register_blueprint(checks_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(pipelines_bp)
    app.register_blueprint(containers_bp)
    app.register_blueprint(phases_bp)
    app.register_blueprint(signals_bp)
    app.register_blueprint(decisions_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(webhooks_bp)


@app.before_request
def log_request_start() -> None:
    """Record request start time for duration logging."""
    g.start_time = time.monotonic()


@app.after_request
def log_request_end(response: Response) -> Response:
    """Log every request with method, path, status, and duration."""
    duration_ms = round((time.monotonic() - getattr(g, "start_time", time.monotonic())) * 1000)
    logger.info(
        "Request",
        method=request.method,
        path=request.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


@app.errorhandler(Exception)
def handle_unhandled_exception(e: Exception) -> tuple[Response, int]:
    """Return JSON for all unhandled exceptions."""
    from werkzeug.exceptions import HTTPException

    if isinstance(e, HTTPException):
        return jsonify(
            {
                "success": False,
                "message": e.description or str(e),
            }
        ), e.code

    # Log unexpected errors
    logger.error("Unhandled exception", error=str(e), exc_info=True)

    return jsonify(
        {
            "success": False,
            "message": "Internal server error",
        }
    ), 500


@app.route("/")
def index() -> tuple[Response, int]:
    """Root endpoint with service info."""
    return jsonify(
        {
            "service": "egg-orchestrator",
            "version": "0.1.0",
            "endpoints": {
                "health": "/api/v1/health",
                "pipelines": "/api/v1/pipelines",
                "metrics": "/api/v1/metrics",
                "webhooks": "/api/v1/webhooks",
            },
        }
    ), 200


def main() -> None:
    """Run the orchestrator API server."""
    parser = argparse.ArgumentParser(description="egg-orchestrator REST API")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=9849, help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")

    args = parser.parse_args()

    # Override from environment
    host = os.environ.get("ORCHESTRATOR_HOST", args.host)
    port = int(os.environ.get("ORCHESTRATOR_PORT", args.port))
    debug = os.environ.get("ORCHESTRATOR_DEBUG", "").lower() == "true" or args.debug

    logger.info(
        "Starting egg-orchestrator",
        host=host,
        port=port,
        debug=debug,
    )

    repo_path = os.environ.get("EGG_REPO_PATH", "not set")
    host_repo_map = os.environ.get("EGG_HOST_REPO_MAP", "not set")
    logger.info(
        "Configuration",
        repo_path=repo_path,
        host_repo_map=host_repo_map,
    )

    # Reconcile any pipelines left in RUNNING state from a previous crash.
    if repo_path != "not set":
        try:
            from docker_client import get_docker_client
            from startup_reconciliation import reconcile_stale_containers
            from state_store import get_state_store

            recovered = reconcile_stale_containers(get_state_store(repo_path), get_docker_client())
            if recovered:
                logger.warning("Recovered stale pipelines on startup", count=recovered)
        except Exception as reconcile_err:
            logger.warning(
                "Startup reconciliation failed",
                error=str(reconcile_err),
            )

    if debug:
        # Use Flask's built-in server for development
        app.run(host=host, port=port, debug=True)
    else:
        # Use waitress for production
        serve(app, host=host, port=port, threads=16)


if __name__ == "__main__":
    main()
