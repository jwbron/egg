"""
Container lifecycle endpoints for egg-orchestrator.

Provides REST endpoints for spawning, managing, and monitoring sandbox containers.
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


import os

from container_monitor import get_container_monitor
from docker_client import (
    ContainerNotFoundError,
    ContainerOperationError,
    DockerClientError,
    ImageNotFoundError,
    InvalidContainerIdError,
    get_docker_client,
)
from kubernetes_client import (
    JobOperationError,
    KubernetesClientError,
    PodNotFoundError,
    get_kubernetes_client,
)
from kubernetes_monitor import get_kubernetes_monitor
from lifecycle_auth import require_lifecycle_secret
from models import AgentRole
from sandbox_template import SandboxTemplate, create_sandbox_config

# Runtime detection: use Kubernetes when EGG_RUNTIME=kubernetes
_RUNTIME = os.environ.get("EGG_RUNTIME", "docker")


def _get_backend():
    """Get the appropriate container backend for the current runtime."""
    if _RUNTIME == "kubernetes":
        return get_kubernetes_client()
    return get_docker_client()


def _get_monitor():
    """Get the appropriate monitor for the current runtime."""
    if _RUNTIME == "kubernetes":
        return get_kubernetes_monitor()
    return get_container_monitor()


logger = get_logger("orchestrator.containers")

containers_bp = Blueprint("containers", __name__, url_prefix="/api/v1/pipelines")


def make_error_response(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create an error response."""
    response: dict[str, Any] = {"success": False, "message": message}
    if details:
        response["details"] = details
    return jsonify(response), status_code


def make_success_response(
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


@containers_bp.route("/<pipeline_id>/spawn", methods=["POST"])
@require_lifecycle_secret
def spawn_container(pipeline_id: str) -> tuple[Response, int]:
    """
    Spawn a sandbox container for a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "agent_role": "coder",  // optional
            "session_token": "...",  // optional
            "issue_number": 123,  // optional
            "private_mode": false,  // optional
            "config": {  // optional
                "memory_limit": "4g",
                "cpu_limit": 2.0
            }
        }

    Response:
        {
            "success": true,
            "data": {
                "container_id": "abc123...",
                "container_name": "egg-sandbox-issue-496-coder",
                "status": "pending"
            }
        }
    """
    raw = request.get_json()
    if raw is not None and not isinstance(raw, dict):
        return make_error_response("Request body must be a JSON object")
    data = raw if raw is not None else {}

    # Parse agent role
    agent_role = None
    if data.get("agent_role"):
        try:
            agent_role = AgentRole(data["agent_role"])
        except ValueError:
            return make_error_response(
                f"Invalid agent role: {data['agent_role']}",
                status_code=400,
            )

    # Build sandbox config
    config_overrides = data.get("config", {})
    sandbox_config = create_sandbox_config(
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        session_token=data.get("session_token"),
        issue_number=data.get("issue_number"),
        private_mode=data.get("private_mode", False),
        **config_overrides,
    )

    # Create container
    template = SandboxTemplate(sandbox_config)
    docker_config = template.to_docker_config()

    try:
        backend = _get_backend()
        info = backend.create_container(
            name=template.get_container_name(),
            **docker_config,
        )

        # Start container (k8s Jobs auto-start, but start_container is safe to call)
        info = backend.start_container(info.container_id)

        logger.info(
            "Container spawned",
            pipeline_id=pipeline_id,
            container_id=info.container_id[:12],
            agent_role=agent_role.value if agent_role else None,
        )

        return make_success_response(
            "Container spawned",
            data={
                "container_id": info.container_id,
                "container_name": info.container_name,
                "status": info.status.value,
            },
        )

    except ImageNotFoundError as e:
        return make_error_response(str(e), status_code=404)
    except (ContainerOperationError, JobOperationError) as e:
        logger.error("Container spawn failed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(f"Failed to spawn container: {e}", status_code=500)
    except (DockerClientError, KubernetesClientError) as e:
        logger.error("Backend error", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(f"Backend error: {e}", status_code=500)


@containers_bp.route("/<pipeline_id>/containers", methods=["GET"])
def list_pipeline_containers(pipeline_id: str) -> tuple[Response, int]:
    """
    List containers for a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        all: Include stopped containers (default: true)

    Response:
        {
            "success": true,
            "data": {
                "containers": [
                    {
                        "container_id": "abc123...",
                        "status": "running",
                        "agent_role": "coder"
                    }
                ]
            }
        }
    """
    include_all = request.args.get("all", "true").lower() == "true"

    try:
        backend = _get_backend()
        containers = backend.list_containers(
            all=include_all,
            labels={"egg.pipeline.id": pipeline_id},
        )

        container_data = [
            {
                "container_id": c.container_id,
                "container_name": c.container_name,
                "status": c.status.value,
                "agent_role": c.agent_role.value if c.agent_role else None,
                "started_at": c.started_at.isoformat() if c.started_at else None,
                "exited_at": c.exited_at.isoformat() if c.exited_at else None,
                "exit_code": c.exit_code,
                "pod_name": getattr(c, "pod_name", None),
                "job_name": getattr(c, "job_name", None),
            }
            for c in containers
        ]

        return make_success_response(
            f"Found {len(containers)} container(s)",
            data={"containers": container_data},
        )

    except (DockerClientError, KubernetesClientError) as e:
        return make_error_response(f"Backend error: {e}", status_code=500)


@containers_bp.route("/<pipeline_id>/containers/<container_id>", methods=["GET"])
def get_container(pipeline_id: str, container_id: str) -> tuple[Response, int]:
    """
    Get container info.

    URL params:
        pipeline_id: Pipeline ID
        container_id: Container ID

    Response:
        {
            "success": true,
            "data": {
                "container": {...}
            }
        }
    """
    try:
        backend = _get_backend()
        info = backend.get_container_info(container_id)

        return make_success_response(
            "Container retrieved",
            data={
                "container": {
                    "container_id": info.container_id,
                    "container_name": info.container_name,
                    "status": info.status.value,
                    "agent_role": info.agent_role.value if info.agent_role else None,
                    "started_at": info.started_at.isoformat() if info.started_at else None,
                    "exited_at": info.exited_at.isoformat() if info.exited_at else None,
                    "exit_code": info.exit_code,
                    "pod_name": getattr(info, "pod_name", None),
                    "job_name": getattr(info, "job_name", None),
                }
            },
        )

    except InvalidContainerIdError:
        return make_error_response(
            f"Invalid container ID format: {container_id}",
            status_code=400,
        )
    except ContainerNotFoundError, PodNotFoundError:
        return make_error_response(
            f"Container {container_id} not found",
            status_code=404,
        )
    except (DockerClientError, KubernetesClientError) as e:
        return make_error_response(f"Backend error: {e}", status_code=500)


@containers_bp.route("/<pipeline_id>/containers/<container_id>", methods=["DELETE"])
@require_lifecycle_secret
def remove_container(pipeline_id: str, container_id: str) -> tuple[Response, int]:
    """
    Remove a container.

    URL params:
        pipeline_id: Pipeline ID
        container_id: Container ID

    Query params:
        force: Force removal of running container (default: false)

    Response:
        {
            "success": true,
            "message": "Container removed"
        }
    """
    force = request.args.get("force", "false").lower() == "true"

    try:
        backend = _get_backend()
        backend.remove_container(container_id, force=force)

        logger.info(
            "Container removed",
            pipeline_id=pipeline_id,
            container_id=container_id[:12],
        )

        return make_success_response("Container removed")

    except InvalidContainerIdError:
        return make_error_response(
            f"Invalid container ID format: {container_id}",
            status_code=400,
        )
    except ContainerNotFoundError, PodNotFoundError:
        return make_error_response(
            f"Container {container_id} not found",
            status_code=404,
        )
    except (ContainerOperationError, JobOperationError) as e:
        return make_error_response(str(e), status_code=400)
    except (DockerClientError, KubernetesClientError) as e:
        return make_error_response(f"Backend error: {e}", status_code=500)


@containers_bp.route("/<pipeline_id>/containers/<container_id>/stop", methods=["POST"])
@require_lifecycle_secret
def stop_container(pipeline_id: str, container_id: str) -> tuple[Response, int]:
    """
    Stop a running container.

    URL params:
        pipeline_id: Pipeline ID
        container_id: Container ID

    Request body:
        {
            "timeout": 10  // optional, seconds before kill
        }

    Response:
        {
            "success": true,
            "data": {
                "container_id": "...",
                "exit_code": 0
            }
        }
    """
    raw = request.get_json()
    if raw is not None and not isinstance(raw, dict):
        return make_error_response("Request body must be a JSON object")
    data = raw if raw is not None else {}
    timeout = data.get("timeout", 10)

    try:
        backend = _get_backend()
        info = backend.stop_container(container_id, timeout=timeout)

        logger.info(
            "Container stopped",
            pipeline_id=pipeline_id,
            container_id=container_id[:12],
            exit_code=info.exit_code,
        )

        return make_success_response(
            "Container stopped",
            data={
                "container_id": info.container_id,
                "exit_code": info.exit_code,
            },
        )

    except InvalidContainerIdError:
        return make_error_response(
            f"Invalid container ID format: {container_id}",
            status_code=400,
        )
    except ContainerNotFoundError, PodNotFoundError:
        return make_error_response(
            f"Container {container_id} not found",
            status_code=404,
        )
    except (ContainerOperationError, JobOperationError) as e:
        return make_error_response(str(e), status_code=400)
    except (DockerClientError, KubernetesClientError) as e:
        return make_error_response(f"Backend error: {e}", status_code=500)


@containers_bp.route("/<pipeline_id>/containers/<container_id>/logs", methods=["GET"])
def get_container_logs(pipeline_id: str, container_id: str) -> tuple[Response, int]:
    """
    Get container logs.

    URL params:
        pipeline_id: Pipeline ID
        container_id: Container ID

    Query params:
        tail: Number of lines from end (default: 100)

    Response:
        {
            "success": true,
            "data": {
                "logs": "..."
            }
        }
    """
    try:
        tail = int(request.args.get("tail", request.args.get("lines", 100)))
    except ValueError, TypeError:
        tail = 100

    try:
        backend = _get_backend()
        logs = backend.get_container_logs(container_id, tail=tail)

        return make_success_response(
            "Logs retrieved",
            data={"logs": logs},
        )

    except InvalidContainerIdError:
        return make_error_response(
            f"Invalid container ID format: {container_id}",
            status_code=400,
        )
    except ContainerNotFoundError, PodNotFoundError:
        return make_error_response(
            f"Container {container_id} not found",
            status_code=404,
        )
    except (DockerClientError, KubernetesClientError) as e:
        return make_error_response(f"Backend error: {e}", status_code=500)


@containers_bp.route("/<pipeline_id>/containers/<container_id>/health", methods=["GET"])
def check_container_health(pipeline_id: str, container_id: str) -> tuple[Response, int]:
    """
    Check container health.

    URL params:
        pipeline_id: Pipeline ID
        container_id: Container ID

    Response:
        {
            "success": true,
            "data": {
                "healthy": true,
                "status": "running"
            }
        }
    """
    try:
        monitor = _get_monitor()
        health = monitor.check_container_health(container_id)

        return make_success_response("Health checked", data=health)

    except InvalidContainerIdError:
        return make_error_response(
            f"Invalid container ID format: {container_id}",
            status_code=400,
        )
