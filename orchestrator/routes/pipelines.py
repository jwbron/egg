"""
Pipeline CRUD endpoints for egg-orchestrator.
"""

import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


# Import orchestrator modules - try relative import first
try:
    from ..container_spawner import ContainerSpawnError, get_container_spawner
    from ..models import AgentRole, PipelineStatus
    from ..state_store import (
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStoreError,
        StateValidationError,
        get_state_store,
    )
except ImportError:
    from container_spawner import ContainerSpawnError, get_container_spawner  # type: ignore
    from models import AgentRole, PipelineStatus  # type: ignore
    from state_store import (  # type: ignore
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStoreError,
        StateValidationError,
        get_state_store,
    )

logger = get_logger("orchestrator.pipelines")

pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/api/v1/pipelines")


from routes import get_repo_path  # noqa: E402 — shared helper


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


@pipelines_bp.route("", methods=["GET"])
def list_pipelines() -> tuple[Response, int]:
    """
    List all pipelines.

    Query params:
        repo_path: Path to repository (optional)
        active_only: Only return active pipelines (default: false)

    Response:
        {
            "success": true,
            "data": {
                "pipelines": [
                    {"id": "issue-123", "status": "running", ...},
                    ...
                ]
            }
        }
    """
    repo_path = get_repo_path()
    active_only = request.args.get("active_only", "false").lower() == "true"

    try:
        store = get_state_store(repo_path)

        if active_only:
            pipelines = store.get_active_pipelines()
        else:
            pipeline_ids = store.list_pipelines()
            pipelines = []
            for pid in pipeline_ids:
                try:
                    pipelines.append(store.load_pipeline(pid))
                except StateStoreError:
                    continue

        # Convert to response format
        pipeline_data = [
            {
                "id": p.id,
                "issue_number": p.issue_number,
                "repo": p.repo,
                "branch": p.branch,
                "status": p.status.value,
                "current_phase": p.current_phase.value,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
            }
            for p in pipelines
        ]

        return make_success_response(
            f"Found {len(pipelines)} pipeline(s)",
            data={"pipelines": pipeline_data},
        )

    except StateStoreError as e:
        logger.error("Failed to list pipelines", error=str(e))
        return make_error_response(f"Failed to list pipelines: {e}", status_code=500)


@pipelines_bp.route("/<pipeline_id>", methods=["GET"])
def get_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Get a pipeline by ID.

    URL params:
        pipeline_id: Pipeline ID (e.g., "issue-123")

    Query params:
        repo_path: Path to repository (optional)

    Response:
        {
            "success": true,
            "data": {
                "pipeline": {...}
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        return make_success_response(
            "Pipeline retrieved",
            data={"pipeline": pipeline.model_dump(mode="json")},
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except StateValidationError as e:
        logger.error("Pipeline validation failed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(
            f"Pipeline state is invalid: {e}",
            status_code=500,
        )


@pipelines_bp.route("", methods=["POST"])
def create_pipeline() -> tuple[Response, int]:
    """
    Create a new pipeline.

    Request body:
        {
            "issue_number": 123,
            "repo": "owner/name",
            "branch": "egg/issue-123",
            "config": {...}  // optional
        }

    Response:
        {
            "success": true,
            "message": "Pipeline created",
            "data": {
                "pipeline": {...}
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    mode = data.get("mode", "issue")

    if mode == "local":
        # Local mode: prompt required, issue_number/repo/branch optional
        prompt = data.get("prompt")
        if not prompt:
            return make_error_response("Missing prompt (required for local mode)")

        # Local pipelines always use the base EGG_REPO_PATH — not a repo-specific
        # subdirectory — so that list/get/start resolve to the same path.
        repo_path = Path(os.environ.get("EGG_REPO_PATH", "."))
        if not repo_path.is_absolute():
            repo_path = Path.cwd() / repo_path

        try:
            store = get_state_store(repo_path)
            pipeline = store.create_pipeline(
                issue_number=data.get("issue_number"),
                repo=data.get("repo"),
                branch=data.get("branch"),
                config=data.get("config"),
                mode="local",
                prompt=prompt,
            )

            logger.info(
                "Local pipeline created",
                pipeline_id=pipeline.id,
                prompt=prompt[:100],
            )

            return make_success_response(
                "Pipeline created",
                data={"pipeline": pipeline.model_dump(mode="json")},
            )

        except StateStoreError as e:
            if "already exists" in str(e):
                return make_error_response(str(e), status_code=409)
            logger.error("Failed to create local pipeline", error=str(e))
            return make_error_response(f"Failed to create pipeline: {e}", status_code=500)

    # Issue mode: existing behavior
    issue_number = data.get("issue_number")
    repo = data.get("repo")
    branch = data.get("branch")

    if not issue_number:
        return make_error_response("Missing issue_number")
    if not repo:
        return make_error_response("Missing repo")
    if not branch:
        return make_error_response("Missing branch")

    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.create_pipeline(
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            config=data.get("config"),
            mode="issue",
        )

        logger.info(
            "Pipeline created",
            pipeline_id=pipeline.id,
            issue_number=issue_number,
        )

        return make_success_response(
            "Pipeline created",
            data={"pipeline": pipeline.model_dump(mode="json")},
        )

    except StateStoreError as e:
        if "already exists" in str(e):
            return make_error_response(str(e), status_code=409)
        logger.error("Failed to create pipeline", error=str(e))
        return make_error_response(f"Failed to create pipeline: {e}", status_code=500)


@pipelines_bp.route("/<pipeline_id>", methods=["PATCH"])
def update_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Update a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "status": "running",
            "current_phase": "plan",
            ...
        }

    Response:
        {
            "success": true,
            "data": {
                "pipeline": {...}
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.update_pipeline(pipeline_id, data)

        logger.info("Pipeline updated", pipeline_id=pipeline_id)

        return make_success_response(
            "Pipeline updated",
            data={"pipeline": pipeline.model_dump(mode="json")},
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except StateValidationError as e:
        return make_error_response(
            f"Invalid update: {e}",
            status_code=400,
        )


@pipelines_bp.route("/<pipeline_id>", methods=["DELETE"])
def delete_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Delete a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Pipeline deleted"
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        store.delete_pipeline(pipeline_id)

        logger.info("Pipeline deleted", pipeline_id=pipeline_id)

        return make_success_response("Pipeline deleted")

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


@pipelines_bp.route("/<pipeline_id>/status", methods=["GET"])
def get_pipeline_status(pipeline_id: str) -> tuple[Response, int]:
    """
    Get pipeline status summary.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "id": "issue-123",
                "status": "running",
                "current_phase": "implement",
                "pending_decisions": 0
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        pending_decisions = len(pipeline.get_pending_decisions())

        return make_success_response(
            "Status retrieved",
            data={
                "id": pipeline.id,
                "status": pipeline.status.value,
                "current_phase": pipeline.current_phase.value,
                "pending_decisions": pending_decisions,
                "updated_at": pipeline.updated_at.isoformat(),
            },
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )


def _run_pipeline(pipeline_id: str, repo_path: Path) -> None:
    """Run a pipeline by spawning containers for each phase.

    This runs in a background thread. It spawns a container for the current
    phase, waits for it to complete, then advances to the next phase.

    Args:
        pipeline_id: Pipeline ID
        repo_path: Path to repository
    """
    from routes.phases import get_phase_transitions

    try:
        store = get_state_store(repo_path)
        spawner = get_container_spawner()
        pipeline = store.load_pipeline(pipeline_id)
        pipeline_mode = getattr(pipeline, "mode", "issue")
        transitions = get_phase_transitions(pipeline_mode)

        # Map pipeline mode to gateway session mode
        gateway_mode = "local" if pipeline_mode == "local" else "public"

        while True:
            pipeline = store.load_pipeline(pipeline_id)

            if pipeline.status in (PipelineStatus.FAILED, PipelineStatus.CANCELLED):
                logger.info(
                    "Pipeline stopped", pipeline_id=pipeline_id, status=pipeline.status.value
                )
                break

            current_phase = pipeline.current_phase

            # Start the current phase
            phase_execution = pipeline.get_phase_execution(current_phase)
            if phase_execution.status == PipelineStatus.PENDING:
                phase_execution.status = PipelineStatus.RUNNING
                phase_execution.started_at = datetime.utcnow()
                pipeline.status = PipelineStatus.RUNNING
                store.save_pipeline(pipeline)

            # Spawn a container for this phase
            logger.info(
                "Spawning container for phase",
                pipeline_id=pipeline_id,
                phase=current_phase.value,
                mode=gateway_mode,
            )

            try:
                spawned = spawner.spawn_agent_container(
                    pipeline_id=pipeline_id,
                    agent_role=AgentRole.CODER,
                    issue_number=pipeline.issue_number,
                    mode=gateway_mode,
                    wait_for_gateway=False,
                    repos=[pipeline.repo] if pipeline.repo else [],
                    phase=current_phase.value,
                    extra_env={
                        "EGG_PIPELINE_ID": pipeline_id,
                        "EGG_PIPELINE_PHASE": current_phase.value,
                        "EGG_PIPELINE_MODE": pipeline_mode,
                        **({"EGG_PIPELINE_PROMPT": pipeline.prompt} if pipeline.prompt else {}),
                    },
                )

                # Wait for the container to finish
                docker_client = spawner.docker
                final_info = docker_client.wait_for_container(
                    spawned.container_info.container_id,
                    timeout=3600,
                )

                # Clean up the container so the next phase can reuse the name
                try:
                    spawner.remove_agent_container(
                        spawned.container_info.container_id,
                        force=True,
                        cleanup_session=True,
                    )
                except Exception as cleanup_err:
                    logger.warning(
                        "Failed to clean up phase container",
                        container_id=spawned.container_info.container_id[:12],
                        error=str(cleanup_err),
                    )

                if final_info.exit_code != 0:
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.FAILED
                    phase_execution.error = f"Container exited with code {final_info.exit_code}"
                    phase_execution.completed_at = datetime.utcnow()
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.error = phase_execution.error
                    store.save_pipeline(pipeline)
                    logger.error(
                        "Phase failed",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        exit_code=final_info.exit_code,
                    )
                    break

            except ContainerSpawnError as e:
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = PipelineStatus.FAILED
                phase_execution.error = str(e)
                phase_execution.completed_at = datetime.utcnow()
                pipeline.status = PipelineStatus.FAILED
                pipeline.error = str(e)
                store.save_pipeline(pipeline)
                logger.error("Failed to spawn container", pipeline_id=pipeline_id, error=str(e))
                break

            # Phase succeeded — mark complete and advance
            pipeline = store.load_pipeline(pipeline_id)
            phase_execution = pipeline.get_phase_execution(current_phase)
            phase_execution.status = PipelineStatus.COMPLETE
            phase_execution.completed_at = datetime.utcnow()

            # Determine next phase
            next_phases = transitions.get(current_phase, [])
            if not next_phases:
                # Terminal phase — pipeline complete
                pipeline.status = PipelineStatus.COMPLETE
                store.save_pipeline(pipeline)
                logger.info("Pipeline complete", pipeline_id=pipeline_id)
                break

            # Advance to next phase
            next_phase = next_phases[0]
            pipeline.current_phase = next_phase
            store.save_pipeline(pipeline)

            logger.info(
                "Phase advanced",
                pipeline_id=pipeline_id,
                from_phase=current_phase.value,
                to_phase=next_phase.value,
            )

    except Exception as e:
        logger.error(
            "Pipeline execution failed", pipeline_id=pipeline_id, error=str(e), exc_info=True
        )
        try:
            store = get_state_store(repo_path)
            pipeline = store.load_pipeline(pipeline_id)
            pipeline.status = PipelineStatus.FAILED
            pipeline.error = str(e)
            store.save_pipeline(pipeline)
        except Exception:
            pass


@pipelines_bp.route("/<pipeline_id>/start", methods=["POST"])
def start_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Start pipeline execution.

    Spawns containers for each phase in sequence, advancing through
    the phase DAG until completion or failure. Runs in a background thread.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Pipeline started",
            "data": {
                "pipeline_id": "local-a1b2c3d4",
                "status": "running"
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        if pipeline.status == PipelineStatus.RUNNING:
            return make_error_response(
                f"Pipeline {pipeline_id} is already running",
                status_code=409,
            )

        if pipeline.status in (PipelineStatus.COMPLETE, PipelineStatus.FAILED):
            return make_error_response(
                f"Pipeline {pipeline_id} is already {pipeline.status.value}",
                status_code=409,
            )

        # Mark pipeline as running
        pipeline.status = PipelineStatus.RUNNING
        store.save_pipeline(pipeline)

        # Run the pipeline in a background thread
        thread = threading.Thread(
            target=_run_pipeline,
            args=(pipeline_id, repo_path),
            daemon=True,
            name=f"pipeline-{pipeline_id}",
        )
        thread.start()

        logger.info("Pipeline started", pipeline_id=pipeline_id)

        return make_success_response(
            "Pipeline started",
            data={
                "pipeline_id": pipeline_id,
                "status": "running",
                "current_phase": pipeline.current_phase.value,
            },
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
