"""
Deployment validation check endpoints for egg-orchestrator.

Provides REST endpoints for managing the devserver lifecycle during
deployment validation checks. The sandbox (checker) uses these endpoints
to coordinate with the orchestrator, which manages the Docker infrastructure.
"""

import sys
import threading
from pathlib import Path

from flask import Blueprint, Response, jsonify

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


from devserver import (
    DevserverError,
    DevserverManager,
    DevserverStatusValue,
)
from egg_contracts.deployment import load_deployment_config
from routes import get_repo_path, resolve_worktree_path
from state_store import PipelineNotFoundError, get_state_store

logger = get_logger("orchestrator.routes.checks")

checks_bp = Blueprint("checks", __name__, url_prefix="/api/v1/pipelines")

# Active DevserverManager instances keyed by pipeline_id.
# Guarded by _devservers_lock since waitress serves requests from multiple threads.
_active_devservers: dict[str, DevserverManager] = {}
_devservers_lock = threading.Lock()


def get_devserver_manager(pipeline_id: str) -> DevserverManager | None:
    """Get the active DevserverManager for a pipeline.

    Args:
        pipeline_id: Pipeline identifier.

    Returns:
        DevserverManager if one exists for this pipeline, None otherwise.
    """
    with _devservers_lock:
        return _active_devservers.get(pipeline_id)


def teardown_devserver(pipeline_id: str) -> None:
    """Tear down the devserver for a pipeline and remove from tracking.

    Safe to call even if no devserver exists for the pipeline.

    Args:
        pipeline_id: Pipeline identifier.
    """
    with _devservers_lock:
        manager = _active_devservers.pop(pipeline_id, None)
    if manager:
        try:
            manager.teardown()
            logger.info(
                "Devserver torn down via lifecycle tracking",
                pipeline_id=pipeline_id,
            )
        except Exception as e:
            logger.warning(
                "Error tearing down devserver",
                pipeline_id=pipeline_id,
                error=str(e),
            )


@checks_bp.route("/<pipeline_id>/deployment-check/start", methods=["POST"])
def start_deployment_check(pipeline_id: str) -> tuple[Response, int]:
    """Start the devserver stack for deployment validation.

    Loads the DeploymentConfig from the target repo, determines changed
    files from the pipeline's worktree, and starts the devserver stack.

    Returns service endpoints the checker can use for validation.

    Returns:
        200 with DevserverStatus on success.
        404 if pipeline not found.
        409 if devserver already running.
        422 if no deployment config exists.
    """
    # Atomically check if already running
    with _devservers_lock:
        if pipeline_id in _active_devservers:
            existing = _active_devservers[pipeline_id]
            if existing.status.status in (
                DevserverStatusValue.STARTING,
                DevserverStatusValue.HEALTHY,
                DevserverStatusValue.UNHEALTHY,
            ):
                return jsonify({
                    "success": False,
                    "message": "Devserver already running for this pipeline",
                    "status": existing.status.to_dict(),
                }), 409

    # Resolve paths
    repo_path = get_repo_path()
    try:
        store = get_state_store(repo_path)
        store.load_pipeline(pipeline_id)
    except PipelineNotFoundError:
        return jsonify({
            "success": False,
            "message": f"Pipeline not found: {pipeline_id}",
        }), 404

    worktree_path = resolve_worktree_path(pipeline_id, repo_path)

    # Load deployment config
    deployment_config = load_deployment_config(worktree_path)
    if deployment_config is None:
        return jsonify({
            "success": False,
            "message": "No deployment config found (.egg/deployment.yml)",
        }), 422

    # Create and start devserver
    manager = DevserverManager(
        pipeline_id=pipeline_id,
        repo_path=repo_path,
        worktree_path=worktree_path,
    )

    try:
        status = manager.start(deployment_config)

        # Atomically register the manager; check again in case a concurrent
        # request raced past the initial check while we were starting.
        with _devservers_lock:
            if pipeline_id in _active_devservers:
                # Another request won the race — tear down ours
                manager.teardown()
                existing = _active_devservers[pipeline_id]
                return jsonify({
                    "success": False,
                    "message": "Devserver already running for this pipeline",
                    "status": existing.status.to_dict(),
                }), 409
            _active_devservers[pipeline_id] = manager

        return jsonify({
            "success": True,
            "message": "Devserver started",
            "status": status.to_dict(),
        }), 200

    except DevserverError as e:
        logger.error(
            "Failed to start devserver",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        # Clean up on failure
        manager.teardown()
        return jsonify({
            "success": False,
            "message": f"Failed to start devserver: {e}",
        }), 500


@checks_bp.route("/<pipeline_id>/deployment-check/status", methods=["GET"])
def get_deployment_check_status(pipeline_id: str) -> tuple[Response, int]:
    """Get the current status of the devserver for a pipeline.

    Returns:
        200 with DevserverStatus.
        404 if no devserver started for this pipeline.
    """
    with _devservers_lock:
        manager = _active_devservers.get(pipeline_id)
    if manager is None:
        return jsonify({
            "success": False,
            "message": f"No devserver running for pipeline: {pipeline_id}",
        }), 404

    return jsonify({
        "success": True,
        "status": manager.status.to_dict(),
    }), 200


@checks_bp.route("/<pipeline_id>/deployment-check/teardown", methods=["POST"])
def teardown_deployment_check(pipeline_id: str) -> tuple[Response, int]:
    """Tear down the devserver stack for a pipeline.

    Idempotent — calling when no devserver is running returns 200.

    Returns:
        200 on successful teardown.
    """
    teardown_devserver(pipeline_id)

    return jsonify({
        "success": True,
        "message": "Devserver torn down",
    }), 200
