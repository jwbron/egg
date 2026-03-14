"""
Pipeline CRUD endpoints for egg-orchestrator.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from docker.errors import DockerException
from flask import Blueprint, Response, jsonify, request, stream_with_context

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
    from ..decision_queue import get_decision_queue
    from ..docker_client import ContainerNotFoundError, ContainerOperationError, DockerClientError
    from ..models import (
        AgentExecutionStatus,
        AgentRole,
        AggregatedReviewResult,
        ContainerStatus,
        CycleTiming,
        Pipeline,
        PipelinePhase,
        PipelineStatus,
        ReviewVerdict,
    )
    from ..state_store import (
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStore,
        StateStoreError,
        StateValidationError,
        get_pipeline_state_lock,
        get_state_store,
    )
except ImportError:
    from container_spawner import ContainerSpawnError, get_container_spawner  # type: ignore
    from decision_queue import get_decision_queue  # type: ignore
    from docker_client import (  # type: ignore
        ContainerNotFoundError,
        ContainerOperationError,
        DockerClientError,
    )
    from models import (  # type: ignore
        AgentExecutionStatus,
        AgentRole,
        AggregatedReviewResult,
        ComplexityTier,
        ContainerStatus,
        CycleTiming,
        DecisionStatus,
        Pipeline,
        PipelinePhase,
        PipelineStatus,
        ReviewVerdict,
    )
    from state_store import (  # type: ignore
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStore,
        StateStoreError,
        StateValidationError,
        get_pipeline_state_lock,
        get_state_store,
    )

if TYPE_CHECKING:
    from egg_container import MountSpec

    try:
        from ..container_spawner import ContainerSpawner
    except ImportError:
        from container_spawner import ContainerSpawner  # type: ignore

logger = get_logger("orchestrator.pipelines")

# Base directory where the gateway creates per-pipeline worktrees.
# Must match the gateway's WORKTREE_BASE_DIR and docker-compose volume mounts.
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")

# Sentinel header used in tester gap summaries. Checked in prompt-building
# functions to adapt language when tester findings are present.
TESTER_FINDINGS_HEADER = "### tester findings"


def _pipeline_identifier(
    issue_number: int | None,
    pipeline_id: str,
) -> int | str:
    """Derive the pipeline identifier used for namespaced .egg-state filenames.

    Prefers ``issue_number`` when available, falling back to ``pipeline_id``.
    """
    return issue_number if issue_number is not None else pipeline_id


# Network constants for sandbox container URLs
try:
    from egg_config import (
        ORCHESTRATOR_EXTERNAL_IP,
        ORCHESTRATOR_ISOLATED_IP,
        ORCHESTRATOR_PORT,
    )
except ImportError:
    ORCHESTRATOR_ISOLATED_IP = "172.32.0.3"
    ORCHESTRATOR_EXTERNAL_IP = "172.33.0.3"
    ORCHESTRATOR_PORT = 9849

try:
    from egg_config.validators import validate_checks
except ImportError:

    def validate_checks(checks: list) -> list[dict[str, str]]:  # type: ignore[misc]
        if not isinstance(checks, list):
            return []
        return [
            {"name": str(c["name"]), "command": str(c["command"])}
            for c in checks
            if isinstance(c, dict) and "name" in c and "command" in c
        ]


pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/api/v1/pipelines")


from egg_agent import build_agent_command  # noqa: E402
from routes import get_repo_path  # noqa: E402 — shared helper

try:
    from gateway_client import get_gateway_client
except ImportError:
    from orchestrator.gateway_client import get_gateway_client  # type: ignore

# Import status reporter for real-time updates
try:
    from status_reporter import get_status_reporter, report_pipeline_status
except ImportError:
    # Fallback if status_reporter not available
    def get_status_reporter():  # type: ignore[misc]
        return None

    def report_pipeline_status(pipeline, event_type=None, message=None):  # type: ignore[misc]
        pass


# Import event bus for SSE streaming.
# report_pipeline_status dispatches to StatusReporter handlers, but the
# SSE stream subscribes to the EventBus — a separate system.  We need to
# emit events to both so SSE clients see live updates.
try:
    from events import EventType
    from events import emit_event as _emit_event
except ImportError:
    _emit_event = None  # type: ignore[assignment]

# Map report_pipeline_status event_type strings to EventType enum values
_EVENT_TYPE_MAP: dict[str, "EventType"] = {}
if _emit_event is not None:
    _EVENT_TYPE_MAP = {
        "phase.started": EventType.PHASE_STARTED,
        "phase.completed": EventType.PHASE_COMPLETED,
        "phase.revision_requested": EventType.PHASE_STARTED,  # re-entering phase
        "pipeline.completed": EventType.PIPELINE_COMPLETED,
        "pipeline.failed": EventType.PIPELINE_FAILED,
        "decision.created": EventType.DECISION_CREATED,
    }


def _emit_pipeline_event(
    pipeline: Pipeline,
    event_type_str: str,
) -> None:
    """Emit a pipeline event to the EventBus for SSE streaming."""
    if _emit_event is None:
        return
    mapped = _EVENT_TYPE_MAP.get(event_type_str)
    if mapped is None:
        return
    _emit_event(
        mapped,
        pipeline.id,
        data={
            "status": pipeline.status.value,
            "phase": pipeline.current_phase.value,
        },
    )


# Import visualization modules for DAG endpoint
try:
    from dag_visualizer import (
        generate_status_report,
        render_compact_status,
        render_pipeline_dag,
        render_progress_bar,
    )

    _DAG_VISUALIZER_AVAILABLE = True
except ImportError:
    _DAG_VISUALIZER_AVAILABLE = False

# Import SSE streaming support
try:
    from sse import create_sse_stream

    _SSE_AVAILABLE = True
except ImportError:
    _SSE_AVAILABLE = False

# Import unified SSE streaming support
try:
    from unified_sse import create_unified_sse_stream

    _UNIFIED_SSE_AVAILABLE = True
except ImportError:
    _UNIFIED_SSE_AVAILABLE = False


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


def _resolve_pipeline(pipeline_id: str, base_path: Path) -> tuple[StateStore, Pipeline]:
    """Load a pipeline, resolving the correct repo subdirectory.

    The StateStore uses a global shared worktree, so it can find any
    pipeline regardless of which ``repo_path`` is used.  When
    ``base_path`` is a parent directory (no ``.git``), we resolve the
    correct repo subdirectory using the pipeline's ``repo`` field so
    that ``store.repo_path`` points to the actual git repository
    (needed for reading draft files, verdict files, contracts, etc.).

    Returns:
        (store, pipeline) tuple

    Raises:
        PipelineNotFoundError: if the pipeline cannot be found anywhere
        InvalidPipelineIdError: if the ID format is invalid
    """
    # Try the base path first
    try:
        store = get_state_store(base_path)
        pipeline = store.load_pipeline(pipeline_id)
    except PipelineNotFoundError:
        # If base_path is not itself a git repo, scan subdirectories
        store = None
        pipeline = None
        if not (base_path / ".git").exists():
            for child in sorted(base_path.iterdir()):
                if child.is_dir() and (child / ".git").exists():
                    try:
                        store = get_state_store(child)
                        pipeline = store.load_pipeline(pipeline_id)
                        return store, pipeline
                    except (PipelineNotFoundError, StateStoreError):
                        continue
        raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found") from None

    # The global worktree means the pipeline is always found at base_path,
    # even when base_path is a parent directory (e.g. /home/egg/repos/).
    # Resolve to the correct repo subdirectory using the pipeline's repo field.
    # NOTE: The pipeline was loaded from the original store, but both stores
    # share the same underlying worktree state — only repo_path differs.
    if not (base_path / ".git").exists():
        if pipeline.repo:
            repo_name = pipeline.repo.split("/")[-1]
            candidate = base_path / repo_name
            if candidate.exists() and (candidate / ".git").exists():
                store = get_state_store(candidate)
            else:
                logger.warning(
                    "Repo subdirectory not found for pipeline",
                    pipeline_id=pipeline_id,
                    repo=pipeline.repo,
                    candidate=str(candidate),
                )
        else:
            logger.warning(
                "Pipeline has no repo field, cannot resolve subdirectory",
                pipeline_id=pipeline_id,
            )

    return store, pipeline


def _collect_all_pipelines(base_path: Path) -> list:
    """Collect pipelines from base_path and all repo subdirectories.

    All StateStore instances share a single global worktree, so we deduplicate
    by pipeline ID to avoid returning the same pipeline multiple times when
    both the base path and child repo paths resolve to the same store.
    """
    seen: set[str] = set()
    pipelines = []

    def _add_from_store(store):
        for pid in store.list_pipelines():
            if pid in seen:
                continue
            try:
                pipelines.append(store.load_pipeline(pid))
                seen.add(pid)
            except StateStoreError:
                continue

    # Check base path itself
    if (base_path / ".egg-state" / "pipelines").exists():
        _add_from_store(get_state_store(base_path))

    # Check repo subdirectories if base_path is not a git repo
    if not (base_path / ".git").exists():
        for child in sorted(base_path.iterdir()):
            if child.is_dir() and (child / ".git").exists():
                try:
                    _add_from_store(get_state_store(child))
                except StateStoreError:
                    continue

    return pipelines


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
        all_pipelines = _collect_all_pipelines(repo_path)

        if active_only:
            pipelines = [
                p
                for p in all_pipelines
                if p.status
                not in (
                    PipelineStatus.COMPLETE,
                    PipelineStatus.FAILED,
                    PipelineStatus.CANCELLED,
                )
            ]
        else:
            pipelines = all_pipelines

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
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)

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

    network_mode = data.get("network_mode")
    if network_mode is not None and network_mode not in ("public", "private"):
        return make_error_response(
            f"Invalid network_mode: {network_mode!r} (must be 'public' or 'private')"
        )

    issue_number = data.get("issue_number")
    repo = data.get("repo")
    branch = data.get("branch")
    prompt = data.get("prompt")

    if not repo:
        return make_error_response("Missing repo")

    # Issue-driven pipelines require a branch; prompt-driven ones do not
    if issue_number and not branch:
        return make_error_response("Missing branch")

    # Prompt-driven pipelines use the base EGG_REPO_PATH so that
    # list/get/start resolve to the same path.
    if not issue_number:
        repo_path = Path(os.environ.get("EGG_REPO_PATH", "."))
        if not repo_path.is_absolute():
            repo_path = Path.cwd() / repo_path
    else:
        repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.create_pipeline(
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            config=data.get("config"),
            prompt=prompt,
            network_mode=network_mode,
        )

        # Contract creation is deferred to _run_pipeline so it writes
        # into the per-pipeline worktree instead of the main repo.

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


def _mark_pipeline_records_terminated(
    store: "StateStore",
    pipeline_id: str,
) -> "Pipeline":
    """Mark all running containers and agents as stopped after pipeline termination.

    Called when a pipeline transitions to a terminal state (cancelled or failed).
    After Docker containers are force-removed, the pipeline state still shows
    them as "running". This reloads the latest state from the store (to avoid
    overwriting coordinator updates made between the status change and container
    cleanup), marks running records as stopped, and saves.

    Returns the updated pipeline so the caller can use it in the response.
    """
    pipeline = store.load_pipeline(pipeline_id)
    now = datetime.utcnow()
    changed = False

    for phase_exec in pipeline.phases.values():
        for container in phase_exec.containers:
            if container.status in (
                ContainerStatus.PENDING,
                ContainerStatus.CREATING,
                ContainerStatus.RUNNING,
            ):
                container.status = ContainerStatus.REMOVED
                container.exited_at = now
                changed = True

        for agent in phase_exec.agents:
            if agent.status in (
                AgentExecutionStatus.PENDING,
                AgentExecutionStatus.RUNNING,
            ):
                agent.status = AgentExecutionStatus.FAILED
                agent.completed_at = now
                agent.error = f"Pipeline {pipeline.status.value}"
                changed = True

    if pipeline.coordinator_state:
        for spawn_record in pipeline.coordinator_state.agents_spawned:
            if spawn_record.status == "running":
                spawn_record.status = "cancelled"
                spawn_record.completed_at = now
                changed = True

    if changed:
        store.save_pipeline(pipeline)
        logger.info(
            "Synced pipeline state after termination",
            pipeline_id=pipeline_id,
        )

    return pipeline


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
        store, _pipeline = _resolve_pipeline(pipeline_id, repo_path)
        pipeline = store.update_pipeline(pipeline_id, data)

        # If pipeline is being cancelled or failed, clean up containers
        # and cancel any pending decisions so wait_for_decision() unblocks.
        if pipeline.status in (PipelineStatus.CANCELLED, PipelineStatus.FAILED):
            try:
                dq = get_decision_queue(pipeline_id, repo_path)
                pending = dq.get_pending_decisions()
                for decision in pending:
                    dq.cancel_decision(decision.id)
                if pending:
                    logger.info(
                        "Cancelled pending decisions after pipeline status change",
                        pipeline_id=pipeline_id,
                        decisions_cancelled=len(pending),
                    )
            except Exception as e:
                logger.warning(
                    "Failed to cancel pending decisions",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

            try:
                spawner = get_container_spawner()
                removed = spawner.cleanup_pipeline(pipeline_id, force=True)
                if removed > 0:
                    logger.info(
                        "Cleaned up pipeline containers after status change",
                        pipeline_id=pipeline_id,
                        status=pipeline.status.value,
                        containers_removed=removed,
                    )
            except (DockerClientError, DockerException) as e:
                logger.warning(
                    "Failed to clean up pipeline containers",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )
            except Exception as e:
                logger.error(
                    "Unexpected error during pipeline container cleanup",
                    pipeline_id=pipeline_id,
                    error=str(e),
                    exc_info=True,
                )

            # Sync pipeline state: reload latest state (coordinator may have
            # written updates between status change and container cleanup),
            # mark all running records as stopped, and re-save.
            try:
                pipeline = _mark_pipeline_records_terminated(store, pipeline_id)
            except Exception as e:
                logger.warning(
                    "Failed to sync pipeline state after termination",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

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


def _cleanup_remote_branches(
    pipeline_id: str,
    pipeline: "Pipeline",
    repo_path: Path,
) -> None:
    """Best-effort cleanup of remote worktree branches for a pipeline.

    Iterates all containers across all phase executions and deletes their
    remote worktree branches (``egg/{container_id}/work``).  Failures are
    logged as warnings and do not block pipeline deletion.
    """
    branches: set[str] = set()
    for phase_exec in pipeline.phases.values():
        for container in phase_exec.containers:
            branches.add(f"egg/{container.container_id}/work")

    if not branches:
        return

    gateway_client = get_gateway_client()
    repo_path_str = str(repo_path)

    deleted = 0
    for branch in sorted(branches):
        if gateway_client.delete_remote_branch(pipeline_id, repo_path_str, branch):
            deleted += 1

    if deleted:
        logger.info(
            "Cleaned up remote worktree branches",
            pipeline_id=pipeline_id,
            branches_deleted=deleted,
            branches_total=len(branches),
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
        store, _pipeline = _resolve_pipeline(pipeline_id, repo_path)

        # Clean up any running containers for this pipeline
        try:
            spawner = get_container_spawner()
            removed = spawner.cleanup_pipeline(pipeline_id, force=True)
            if removed > 0:
                logger.info(
                    "Cleaned up pipeline containers",
                    pipeline_id=pipeline_id,
                    containers_removed=removed,
                )
        except (DockerClientError, DockerException) as e:
            logger.warning(
                "Failed to clean up pipeline containers",
                pipeline_id=pipeline_id,
                error=str(e),
            )
        except Exception as e:
            logger.error(
                "Unexpected error during pipeline container cleanup",
                pipeline_id=pipeline_id,
                error=str(e),
                exc_info=True,
            )

        # Clean up remote worktree branches (best-effort)
        try:
            _cleanup_remote_branches(pipeline_id, _pipeline, repo_path)
        except Exception as e:
            logger.warning(
                "Failed to clean up remote worktree branches",
                pipeline_id=pipeline_id,
                error=str(e),
            )

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
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)

        pending = pipeline.get_pending_decisions()

        data = {
            "id": pipeline.id,
            "status": pipeline.status.value,
            "current_phase": pipeline.current_phase.value,
            "pending_decisions": len(pending),
            "updated_at": pipeline.updated_at.isoformat(),
        }

        # Include first pending decision details so the collaborator
        # doesn't need a second round-trip to fetch it
        if pending:
            d = pending[0]
            data["pending_decision"] = {
                "id": d.id,
                "question": d.question,
                "context": d.context,
                "options": d.options,
                "created_at": d.created_at.isoformat(),
            }

        # Include concurrent execution monitoring when enabled
        concurrent_data = _get_concurrent_status(pipeline)
        if concurrent_data:
            data["concurrent"] = concurrent_data

        return make_success_response("Status retrieved", data=data)

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


def _get_concurrent_status(pipeline: "Pipeline") -> dict | None:
    """Get concurrent execution monitoring data for a pipeline.

    Returns None if concurrent execution is not enabled for this pipeline.
    Returns a dict with the following structure when concurrent mode is active::

        {
            "enabled": True,
            "max_concurrent_agents": int,
            "messages": {"total": int, "by_type": {"PROGRESS": int, ...}},
            "consensus": {
                "agents": {"coder": {"state": "READY", ...}, ...},
                "is_complete": bool,
                "blocking_agents": ["role", ...]  # agents not yet READY
            },
            "agents": [{"role": str, "status": str}, ...]  # from phase execution
        }

    Dependencies on other concurrent-mode modules (message_store, consensus) are
    imported lazily and degrade gracefully to empty structures when unavailable.
    """
    config = pipeline.config
    if not getattr(config, "concurrent_execution", False):
        return None

    result: dict = {
        "enabled": True,
        "max_concurrent_agents": getattr(config, "max_concurrent_agents", 6),
    }

    # Message store provides aggregate counts of inter-agent messages by type.
    # This module is implemented in phase-1 of the concurrent execution feature;
    # ImportError is expected until that phase lands.
    try:
        from ..message_store import get_message_store  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("Message store not available for status")
        get_message_store = None  # type: ignore[assignment]

    if get_message_store is not None:
        store = get_message_store()
        msg_status = store.get_status(pipeline.id)
        result["messages"] = {
            "total": msg_status.get("total", 0),
            "by_type": msg_status.get("by_type", {}),
        }
    else:
        result["messages"] = {"total": 0, "by_type": {}}

    # Consensus evaluator tracks per-agent readiness states and determines
    # whether all agents agree the phase is complete. Implemented in phase-3;
    # blocking_agents lists roles that are not yet READY (WORKING or BLOCKED).
    try:
        from ..consensus import get_consensus_evaluator  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("Consensus evaluator not available for status")
        get_consensus_evaluator = None  # type: ignore[assignment]

    if get_consensus_evaluator is not None:
        evaluator = get_consensus_evaluator()
        consensus_state = evaluator.get_state(pipeline.id)
        result["consensus"] = {
            "agents": {
                role: {
                    "state": readiness.state.value,
                    "reason": readiness.reason,
                    "updated_at": readiness.timestamp.isoformat() if readiness.timestamp else None,
                }
                for role, readiness in consensus_state.get("agents", {}).items()
            },
            "is_complete": consensus_state.get("is_complete", False),
            "blocking_agents": consensus_state.get("blocking_agents", []),
        }
    else:
        result["consensus"] = {
            "agents": {},
            "is_complete": False,
            "blocking_agents": [],
        }

    # Agent lifecycle info from the phase execution record — shows which agents
    # are spawned for the current phase and their container-level status.
    current_phase_name = pipeline.current_phase.value
    phase_exec = pipeline.phases.get(current_phase_name)
    if phase_exec and hasattr(phase_exec, "agents"):
        agents_info = []
        for agent in phase_exec.agents:
            if hasattr(agent, "role"):
                role = agent.role.value if hasattr(agent.role, "value") else str(agent.role)
            else:
                role = str(agent)
            if hasattr(agent, "status"):
                status = agent.status.value if hasattr(agent.status, "value") else "unknown"
            else:
                status = "unknown"
            agents_info.append({"role": role, "status": status})
        result["agents"] = agents_info

    return result


def _read_shared_criteria(
    filename: str,
    user_override: str | None = None,
    repo_path: str | None = None,
) -> str | None:
    """Read shared criteria from file, checking user override first.

    Search order:
    1. .egg/<user_override> in the repo (if user_override provided)
    2. shared/prompts/<filename> relative to source tree
    3. /app/prompts/<filename> (Docker container path)

    Returns the file content, or None if no file found (caller uses inline fallback).
    """
    # Check user override first
    if user_override and repo_path:
        override_path = Path(repo_path) / ".egg" / user_override
        if override_path.is_file() and override_path.stat().st_size > 0:
            return override_path.read_text()

    # Try source tree path (development / tests)
    source_path = Path(__file__).parent.parent.parent / "shared" / "prompts" / filename
    if source_path.is_file():
        return source_path.read_text()

    # Try Docker container path (production)
    docker_path = Path("/app/prompts") / filename
    if docker_path.is_file():
        return docker_path.read_text()

    return None


def _get_agent_design_criteria() -> str:
    """Return agent-mode design review criteria."""
    content = _read_shared_criteria("agent-design-criteria.md")
    if content is not None:
        return content
    logger.warning("Shared agent-design-criteria.md not found, using inline fallback")
    return (
        "Flag these **clear** anti-patterns:\n\n"
        "1. **Excessive pre-fetching** — Baking large diffs (10KB+) or full file contents "
        "into prompts instead of letting the agent fetch what it needs\n"
        "2. **Structured output for humans** — Requiring JSON when output goes directly "
        "to humans rather than machines\n"
        "3. **Post-processing pipelines** — Scripts that parse agent output to take actions "
        "the agent could take directly\n"
        "4. **Rigid procedures** — Micromanaging step-by-step procedures when objectives "
        "would suffice\n"
        "5. **Prompt-level security** — Using instructions for constraints that should be "
        "sandbox-enforced\n"
        "6. **Direct LLM API calls outside sandbox** — Calling the Anthropic API from "
        "orchestrator, gateway, or shared code instead of delegating to sandbox containers\n"
        "7. **Direct API calls bypassing the Agent SDK** — Using raw HTTP calls to the "
        "Anthropic API instead of run_agent() (in-sandbox) or build_agent_command() "
        "(orchestrator-spawned containers). Unlike item 6 (scoped to infra code), "
        "this applies everywhere including sandbox code.\n"
        "8. **Hardcoded model identifiers** — Using full model IDs (date-pinned or "
        "version-pinned) instead of short aliases (sonnet, opus, haiku)\n"
    )


def _get_code_review_criteria(repo_path: str | None = None) -> str:
    """Return code review criteria."""
    content = _read_shared_criteria(
        "code-review-criteria.md",
        user_override="review-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    logger.warning("Shared code-review-criteria.md not found, using inline fallback")
    return (
        "### Security (highest priority)\n"
        "- Injection vulnerabilities (SQL, command, XSS, LDAP, path traversal)\n"
        "- Authentication/authorization flaws\n"
        "- Credential exposure, hardcoded secrets\n"
        "- SSRF, open redirects, unsafe deserialization\n\n"
        "### Correctness\n"
        "- Logic errors, off-by-one, boundary conditions\n"
        "- Race conditions, deadlocks, concurrency bugs\n"
        "- Null/undefined handling, missing error paths\n"
        "- Resource leaks (connections, file handles, memory)\n"
        "- End-to-end feature functionality: verify new features work in their "
        "real execution environment\n\n"
        "### Robustness\n"
        "- Missing input validation at trust boundaries\n"
        "- Unhandled exceptions that could crash the system\n"
        "- Missing retry logic for transient failures\n"
        "- Inadequate timeouts for external calls\n\n"
        "### Design\n"
        "- Violations of existing codebase patterns\n"
        "- Breaking changes to public interfaces\n"
        "- Tight coupling that will hinder future changes\n\n"
        "### Severity Classification\n\n"
        "**Blocking** (request changes):\n"
        "- Security vulnerabilities\n"
        "- Non-functional features — the feature's core purpose does not work "
        "end-to-end\n"
        "- Logic errors that produce incorrect results\n"
        "- Breaking changes to existing functionality\n"
        "- Resource leaks or crashes\n"
        "- Pre-existing broken or inconsistent behavior in code the PR "
        "modifies\n\n"
        "**Non-blocking** (suggestions):\n"
        "- Code quality improvements (naming, structure, duplication)\n"
        "- Defense-in-depth additions\n"
        "- Missing edge case handling that doesn't affect the core feature\n"
        "- Documentation gaps\n"
        "- Style or convention deviations not caught by linters\n\n"
        "**Do not dismiss issues as 'not a regression'**: If a PR modifies "
        "code that has existing broken or inconsistent behavior, the issue is "
        "blocking even if the PR didn't introduce it. A PR that adds a new "
        "code path through already-inconsistent logic makes the inconsistency "
        "worse.\n\n"
        "**Beware of false analogies**: When comparing new code to existing "
        "patterns, verify the analogy holds at the execution-model level. "
        "Two features may look structurally similar in config but have "
        "completely different execution paths. If the existing pattern works "
        "via mechanism A but the new code relies on mechanism B that doesn't "
        "exist, the comparison is invalid — classify based on actual "
        "functionality, not superficial similarity.\n"
    )


def _get_contract_review_criteria(repo_path: str | None = None) -> str:
    """Return contract verification criteria."""
    content = _read_shared_criteria(
        "contract-review-criteria.md",
        user_override="contract-rules.md",
        repo_path=repo_path,
    )
    if content is not None:
        return content
    logger.warning("Shared contract-review-criteria.md not found, using inline fallback")
    return (
        "### Task Verification\n"
        "For each task in the contract, verify:\n"
        "1. The described functionality is present in the code\n"
        "2. The acceptance criteria for the task is satisfied\n"
        "3. If a commit is linked, verify it relates to the task\n"
        "4. Where applicable, tests cover the new functionality\n\n"
        "### Phase Consistency\n"
        "- All tasks in completed phases are actually implemented\n"
        "- Phase status matches task completion state\n"
        "- No orphaned code exists that isn't covered by any task\n\n"
        "### Acceptance Criteria Verification\n"
        "For each acceptance criterion:\n"
        "1. Examine the implementation to verify it meets the criterion\n"
        "2. Note any gaps in your review\n\n"
        "### Contract Integrity\n"
        "- No implementation changes violate previously verified criteria\n"
        "- New changes don't break existing contract compliance\n"
        "- All required files listed in tasks are present\n"
    )


def _get_refine_review_criteria() -> str:
    """Return review criteria for the dedicated refine reviewer."""
    return (
        "### 1. Problem Understanding\n"
        "- Does the analysis correctly identify the core problem or feature request?\n"
        "- Is the current behavior (if applicable) accurately described?\n"
        "- Are the goals and desired outcomes clear?\n\n"
        "### 2. Research Quality\n"
        "- Has the agent explored the relevant parts of the codebase?\n"
        "- Are existing patterns and conventions identified?\n"
        "- Is the technical context accurate and thorough?\n\n"
        "### 3. Options Analysis\n"
        "- Are the proposed options meaningfully different?\n"
        "- Are trade-offs clearly articulated for each option?\n"
        "- Is the reasoning logical and well-founded?\n\n"
        "### 4. Constraints and Dependencies\n"
        "- Are technical constraints identified (performance, compatibility, etc.)?\n"
        "- Are dependencies on other code or systems noted?\n"
        "- Are potential risks or complications surfaced?\n\n"
        "### 5. Open Questions\n"
        "- Are open questions specific enough for a human to answer?\n"
        "- Do questions address genuine ambiguities?\n"
        "- Are questions actionable?\n"
        "- Are ALL uncertainties and assumptions surfaced? The analysis should not "
        "proceed with unvalidated assumptions when it could ask the human instead.\n\n"
        "### 6. Recommendation Quality\n"
        "- Is there a clear recommended approach?\n"
        "- Is the recommendation justified with specific reasons?\n"
        "- Does the recommendation align with the analysis findings?\n\n"
        "### 7. HITL Decision Registration\n"
        "- Run `egg-contract show` and verify that contract decisions or feedback "
        "items exist for every open question in the analysis.\n"
        "- If open questions appear as prose text without corresponding "
        "`<!-- egg-hitl-decision ... -->` or `<!-- egg-hitl-feedback ... -->` "
        "markers (generated by `egg-contract`), flag as `needs_revision` — "
        "the agent must re-run `egg-contract add-decision` or "
        "`egg-contract add-feedback` for each question.\n"
        "- If there are zero open questions, verify that the requirements are "
        "genuinely unambiguous and no assumptions were made silently.\n"
    )


def _get_plan_review_criteria() -> str:
    """Return review criteria for the dedicated plan reviewer."""
    return (
        "### 1. Alignment with Analysis\n"
        "- Does the plan implement the recommended approach from the analysis?\n"
        "- If the plan deviates from the analysis, is the reason explained?\n"
        "- Are all requirements from the analysis addressed?\n\n"
        "### 2. Task Breakdown\n"
        "- Are tasks discrete, actionable, and properly scoped?\n"
        "- Is each task small enough to implement in a single pass?\n"
        "- Are task boundaries clear (no overlapping responsibilities)?\n\n"
        "### 3. Acceptance Criteria\n"
        "- Does each task have clear, testable acceptance criteria?\n"
        "- Are criteria specific enough to verify completion?\n"
        "- Do criteria cover both happy path and edge cases?\n\n"
        "### 4. Dependency Ordering\n"
        "- Are task dependencies correctly identified?\n"
        "- Is the ordering logical (foundations before features)?\n"
        "- Are there opportunities for parallelism that are missed?\n\n"
        "### 5. Risk Assessment\n"
        "- Are technical risks identified (security, performance, compatibility)?\n"
        "- Are mitigation strategies concrete and actionable?\n"
        "- Is the rollback plan realistic?\n\n"
        "### 6. Test Strategy\n"
        "- Is the test strategy appropriate for the scope of changes?\n"
        "- Are both unit and integration tests considered?\n"
        "- Are test scenarios aligned with acceptance criteria?\n\n"
        "### 7. Completeness\n"
        "- Does the plan cover all aspects of the original request?\n"
        "- Are documentation updates included where needed?\n"
        "- Are there any obvious gaps or missing tasks?\n"
    )


def _get_review_criteria_for_type(
    reviewer_type: str, phase: str, repo_path: str | None = None
) -> str:
    """Dispatch to the correct criteria function based on reviewer type."""
    if reviewer_type == "agent-design":
        return _get_agent_design_criteria()
    elif reviewer_type == "code":
        return _get_code_review_criteria(repo_path=repo_path)
    elif reviewer_type == "contract":
        return _get_contract_review_criteria(repo_path=repo_path)
    elif reviewer_type == "refine":
        return _get_refine_review_criteria()
    elif reviewer_type == "plan":
        return _get_plan_review_criteria()
    else:
        raise ValueError(f"Unknown reviewer type: {reviewer_type}")


def _get_reviewer_scope_preamble(reviewer_type: str, phase: str) -> str:
    """Return a scope preamble that tells the reviewer what to focus on."""
    if reviewer_type == "agent-design":
        return (
            "This is a specialized **agent-mode design review**. Focus ONLY on "
            "agent-mode design principles. Do NOT review general code quality, "
            "security, or correctness — other reviewers handle those.\n\n"
            "**Only flag issues if you find clear agent-mode design anti-patterns.** "
            "If the output has no agent-mode concerns, a brief approval is acceptable "
            "— you do not need to produce a lengthy analysis when there are no concerns."
        )
    elif reviewer_type == "code":
        return (
            "This is a **comprehensive code review**. Focus on security, correctness, "
            "and robustness. Agent-mode design alignment is handled by another reviewer.\n\n"
            "**Be direct.** Do not soften feedback. State issues clearly and explain "
            "why they matter.\n\n"
            "**Be thorough.** Find ALL issues on the first pass. Do not stop after "
            "identifying a few problems.\n\n"
            "**Analysis format:** Provide file-by-file analysis covering each changed "
            "file. For each file, note what changed, whether the change is correct, "
            "and any issues or observations."
        )
    elif reviewer_type == "contract":
        return (
            "This is a **contract verification review**. Verify that the implementation "
            "matches the contract and all acceptance criteria are met. Do NOT review "
            "general code quality or security — other reviewers handle those.\n\n"
            "**Analysis format:** Provide a criterion-by-criterion verification — for each "
            "acceptance criterion, state whether it is met and cite the specific evidence."
        )
    elif reviewer_type == "refine":
        return (
            "This is a **refine phase review**. Focus on the quality and completeness "
            "of the analysis produced during the refine phase. Evaluate problem "
            "understanding, codebase research, options analysis, and the recommended "
            "approach. Agent-mode design alignment is handled by another reviewer.\n\n"
            "**Analysis format:** Provide section-by-section evaluation of the refine "
            "output — assess each major section for depth, accuracy, and completeness."
        )
    elif reviewer_type == "plan":
        return (
            "This is a **plan phase review**. Focus on the quality and completeness "
            "of the implementation plan. Evaluate task breakdown, acceptance criteria, "
            "dependency ordering, risk assessment, and test strategy. Agent-mode "
            "design alignment is handled by another reviewer.\n\n"
            "**Analysis format:** Provide section-by-section evaluation of the plan — "
            "assess task decomposition, acceptance criteria quality, dependency ordering, "
            "and risk coverage."
        )
    else:
        raise ValueError(f"Unknown reviewer type: {reviewer_type}")


def _verdict_path_for_type(
    phase: str,
    reviewer_type: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
    plan_phase_id: str | None = None,
) -> str:
    """Return the relative verdict file path for a given reviewer type.

    Uses issue_number as prefix when available, otherwise pipeline_id.

    When plan_phase_id is provided (Tier 3 phase-level dispatch), it is included
    in the path to avoid race conditions between parallel phase reviewers:
    e.g., 123-implement-phase-1-code-review.json.
    """
    phase_segment = f"{phase}-{plan_phase_id}" if plan_phase_id else phase
    prefix = _pipeline_identifier(issue_number, pipeline_id or "unknown")
    return f".egg-state/reviews/{prefix}-{phase_segment}-{reviewer_type}-review.json"


def _get_draft_path(
    phase: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str | None:
    """Return relative path to the draft file for a phase.

    Uses issue_number as prefix when available, otherwise pipeline_id.
    """
    prefix = _pipeline_identifier(issue_number, pipeline_id or "unknown")
    if phase == "refine":
        return f".egg-state/drafts/{prefix}-analysis.md"
    elif phase == "implement":
        return None
    else:
        return f".egg-state/drafts/{prefix}-{phase}.md"


def _read_phase_draft(
    repo_path: Path,
    phase: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
    max_chars: int = 32000,
) -> str | None:
    """Read draft file contents. Truncates at max_chars.

    Returns None when the draft cannot be found (no path configured or
    file missing on disk).
    """
    draft_rel = _get_draft_path(phase, issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        return None
    draft_path = repo_path / draft_rel
    if not draft_path.exists():
        return None
    content = draft_path.read_text(encoding="utf-8")
    if len(content) > max_chars:
        return content[:max_chars] + f"\n\n... (truncated, {len(content)} chars total)"
    return content


def _summarize_issue(prompt: str | None, issue_number: int | None = None) -> str:
    """Extract a 1-2 sentence summary from the issue title and first paragraph.

    Used to give execution agents (tester, documenter, integrator) a brief
    orientation without embedding the full issue body. Analysis agents
    (architect, task_planner, risk_analyst) still receive the full issue.

    Extracts the first markdown heading (or first non-empty line) as the title,
    then the first paragraph as supporting context.
    """
    if not prompt or not prompt.strip():
        return f"Working on issue #{issue_number}." if issue_number else ""

    lines = prompt.strip().splitlines()

    # Extract title: first markdown heading, or first non-empty line
    title = ""
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            title = s.lstrip("# ").strip()
        else:
            title = s
        body_start = i + 1
        break

    # Extract first paragraph after title (up to ~300 chars)
    first_para_lines: list[str] = []
    for line in lines[body_start:]:
        s = line.strip()
        if not s:
            if first_para_lines:
                break
            continue
        first_para_lines.append(s)

    first_para = " ".join(first_para_lines)
    if len(first_para) > 300:
        first_para = first_para[:297] + "..."

    # Build summary
    issue_ref = f" (issue #{issue_number})" if issue_number else ""
    summary = f"**Background**: {title}{issue_ref}"
    if first_para:
        summary += f"\n\n{first_para}"

    return summary


def _extract_plan_overview(plan_text: str) -> str:
    """Extract the plan overview section (before individual phase details).

    Returns the summary/overview portion of the plan, stopping before
    individual phase task listings (### Phase N: ...) and the yaml-tasks
    appendix. This gives the coder high-level context without the full plan.
    """
    lines = plan_text.splitlines()
    overview_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Stop at individual phase headings
        if stripped.startswith("### Phase ") or stripped.startswith("### phase-"):
            break
        # Stop at the yaml-tasks appendix
        if "yaml-tasks" in stripped:
            break
        # Stop at structured task appendix
        if stripped.startswith("## Structured Task Appendix"):
            break
        # Stop at issue-to-task mapping (detailed reference section)
        if stripped.startswith("## Issue-to-Task Mapping"):
            break
        overview_lines.append(line)

    # Trim trailing blank lines
    while overview_lines and not overview_lines[-1].strip():
        overview_lines.pop()

    return "\n".join(overview_lines)


def _build_role_context(
    role_value: str,
    prompt: str | None,
    issue_number: int | None = None,
    phase_obj=None,
    all_phases=None,
) -> str:
    """Build role-appropriate context to replace raw issue body embedding.

    Analysis roles (architect, task_planner, risk_analyst) receive the full
    issue body since they need it for problem understanding and planning.

    Execution roles (tester, documenter, integrator) receive a brief summary
    with structured task information and pointers to full context.

    Args:
        role_value: Agent role string
        prompt: Original task prompt (full issue body)
        issue_number: GitHub issue number
        phase_obj: Current plan phase object (Tier 3 context)
        all_phases: All contract phases (Tier 3 context)

    Returns:
        Role-appropriate context string to embed in the agent prompt
    """
    # Analysis roles need the full issue body for problem understanding
    if role_value in ("architect", "task_planner", "risk_analyst"):
        if prompt:
            return f"## Task Description\n\n{prompt}\n"
        return ""

    lines: list[str] = []

    # Brief summary for execution roles
    summary = _summarize_issue(prompt, issue_number)
    if summary:
        lines.append(f"## Background\n\n{summary}\n")

    # Phase-specific context (Tier 3)
    if phase_obj is not None:
        lines.append(f"## Phase Scope: {phase_obj.name} ({phase_obj.id})\n")

        if role_value == "tester":
            lines.append(
                f"Focus your testing on code changed in plan phase `{phase_obj.id}`. "
                "The following tasks were implemented in this phase:\n"
            )
        elif role_value == "documenter":
            lines.append(
                f"Focus your documentation on changes from plan phase `{phase_obj.id}`. "
                "The following tasks were implemented in this phase:\n"
            )
        else:
            lines.append("The following tasks were implemented in this phase:\n")

        for task in phase_obj.tasks:
            lines.append(f"- **{task.id}**: {task.description}")
            if getattr(task, "acceptance_criteria", None):
                lines.append(f"  - Acceptance: {task.acceptance_criteria}")
            if getattr(task, "files_affected", None):
                lines.append(f"  - Files: {', '.join(task.files_affected)}")
        lines.append("")

    # All-phases summary for integrator
    if all_phases and role_value == "integrator":
        lines.append("## Implementation Summary\n")
        for phase in all_phases:
            status = getattr(phase, "status", "unknown")
            task_count = len(phase.tasks) if phase.tasks else 0
            files: set[str] = set()
            for t in phase.tasks or []:
                files.update(getattr(t, "files_affected", None) or [])
            files_str = f" — files: {', '.join(sorted(files))}" if files else ""
            lines.append(
                f"- **{phase.id}** ({phase.name}): {task_count} tasks [{status}]{files_str}"
            )
        lines.append("")
    elif all_phases and phase_obj is not None and role_value in ("tester", "documenter"):
        # Brief orientation about other phases for context
        other_phases = [p for p in all_phases if p.id != phase_obj.id]
        if other_phases:
            lines.append("### Other Phases (for orientation)\n")
            for phase in other_phases:
                status = getattr(phase, "status", "unknown")
                lines.append(f"- {phase.id}: {phase.name} [{status}]")
            lines.append("")

    # Context pointers — agents can get more detail on demand
    lines.append("## For More Context\n")
    if issue_number:
        lines.append(f"- Full issue: `gh issue view {issue_number}`")
    lines.append("- Changed files: `git diff HEAD~10..HEAD` or check handoff data")
    lines.append("- Coder output: check `EGG_HANDOFF_DATA` environment variable")
    lines.append(
        "- Prior agent sessions: `egg-checkpoint context --pipeline $EGG_PIPELINE_ID` "
        "(see checkpoint rule for details)"
    )
    lines.append("")

    return "\n".join(lines)


def _render_contract_tasks(
    repo_path: str,
    pipeline_id: str,
    pipeline_mode: str,
    issue_number: int | None = None,
) -> str | None:
    """Load contract and render tasks as a markdown checklist.

    Returns None if the contract cannot be loaded.
    """
    try:
        from egg_contracts.loader import load_contract
        from egg_contracts.models import TaskStatus
    except ImportError:
        return None

    # Use issue_number as contract identifier when available
    contract_id: int | str = _pipeline_identifier(issue_number, pipeline_id)

    try:
        contract = load_contract(contract_id, Path(repo_path))
    except Exception:
        return None

    if not contract.phases:
        return None

    lines = ["## Contract Tasks\n"]
    for phase in contract.phases:
        if not phase.tasks:
            continue
        lines.append(f"### {phase.name}\n")
        for task in phase.tasks:
            check = "x" if task.status == TaskStatus.COMPLETE else " "
            lines.append(f"- [{check}] **{task.id}**: {task.description}")
            if task.acceptance_criteria:
                lines.append(f"  - Acceptance: {task.acceptance_criteria}")
            if task.files_affected:
                lines.append(f"  - Files: {', '.join(task.files_affected)}")
        lines.append("")

    return "\n".join(lines) if len(lines) > 1 else None


def _check_short_circuit_signal(
    repo_path: Path,
    pipeline_mode: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> bool:
    """Check the refine analysis draft for a short-circuit signal.

    Looks for the *last* fenced YAML block containing ``short_circuit: true``
    in the analysis.  Returns True if found.
    """
    draft_rel = _get_draft_path("refine", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        return False
    draft_path = repo_path / draft_rel
    if not draft_path.exists():
        return False
    content = draft_path.read_text(encoding="utf-8")
    if not content.strip():
        return False

    # Look for a fenced YAML block containing short_circuit: true.
    # Only the *last* YAML block is checked to avoid false positives from
    # example/quoted YAML earlier in the document.  The refine prompt
    # instructs the LLM to place the metadata block at the very end.
    yaml_block_pattern = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)
    matches = list(yaml_block_pattern.finditer(content))
    if matches:
        block = matches[-1].group(1)
        if re.search(r"^\s*short_circuit\s*:\s*true\s*$", block, re.MULTILINE):
            return True

    return False


def _check_high_complexity_signal(
    repo_path: Path,
    pipeline_mode: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> tuple[str, bool]:
    """Check the refine analysis draft for a complexity tier signal.

    Looks for the *last* fenced YAML block containing ``complexity_tier``
    in the analysis. Returns a tuple of (tier, parallel_phases).

    Returns:
        Tuple of (complexity_tier, parallel_phases).
        complexity_tier is one of "low", "mid", "high".
        Defaults to ("mid", False) if no signal is found.
    """
    draft_rel = _get_draft_path("refine", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        return "mid", False
    draft_path = repo_path / draft_rel
    if not draft_path.exists():
        return "mid", False
    content = draft_path.read_text(encoding="utf-8")
    if not content.strip():
        return "mid", False

    # Look for a fenced YAML block containing complexity_tier.
    # Only the *last* YAML block is checked to avoid false positives.
    yaml_block_pattern = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)
    matches = list(yaml_block_pattern.finditer(content))
    if not matches:
        return "mid", False

    block = matches[-1].group(1)

    # Parse the YAML block to extract complexity_tier and parallel_phases
    try:
        data = yaml.safe_load(block)
        if not isinstance(data, dict):
            return "mid", False

        tier = str(data.get("complexity_tier", "mid")).lower()
        if tier not in ("low", "mid", "high"):
            tier = "mid"

        parallel_phases = bool(data.get("parallel_phases", False))
        return tier, parallel_phases
    except Exception:
        # Fall back to regex parsing if YAML parsing fails
        tier_match = re.search(r"^\s*complexity_tier\s*:\s*(low|mid|high)\s*$", block, re.MULTILINE)
        tier = tier_match.group(1) if tier_match else "mid"

        parallel_match = re.search(r"^\s*parallel_phases\s*:\s*true\s*$", block, re.MULTILINE)
        parallel_phases = bool(parallel_match)

        return tier, parallel_phases


def _build_review_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    reviewer_type: str = "code",
    issue_number: int | None = None,
    review_cycle: int = 1,
    prior_feedback: str | None = None,
    repo_path: str | None = None,
    last_reviewed_commit: str | None = None,
    plan_phase_id: str | None = None,
) -> str:
    """Build a review prompt for the reviewer agent.

    Tells the reviewer to evaluate the draft for the given phase and write
    a typed verdict JSON file to .egg-state/reviews/.
    """
    draft_path = _get_draft_path(phase, issue_number=issue_number, pipeline_id=pipeline_id)

    verdict_path = _verdict_path_for_type(
        phase,
        reviewer_type,
        issue_number=issue_number,
        pipeline_id=pipeline_id,
        plan_phase_id=plan_phase_id,
    )

    lines = [
        f"You are reviewing the **{phase}** phase output of the SDLC pipeline "
        f"({reviewer_type} reviewer).\n",
        "## Scope\n",
        _get_reviewer_scope_preamble(reviewer_type, phase),
        "",
        "## Context\n",
        f"Pipeline ID: {pipeline_id}",
        f"Phase: {phase}",
        f"Reviewer: {reviewer_type}",
        f"Review cycle: {review_cycle}",
        "",
        "## Your Task\n",
    ]

    # Delta review: for re-reviews with a known last-reviewed commit,
    # instruct the reviewer to focus on the delta.
    is_delta_review = review_cycle > 1 and last_reviewed_commit and not draft_path
    diff_command = (
        f"git diff {last_reviewed_commit}..HEAD" if is_delta_review else "git diff HEAD~10..HEAD"
    )

    if draft_path:
        lines.append(f"1. Read the draft at `{draft_path}`")
    else:
        lines.append(
            f"1. Review the implementation using `git log --oneline -10` and `{diff_command}`"
        )

    # Add procedural steps for code reviewer (matching GHA reviewer thoroughness)
    if reviewer_type == "code" and not draft_path:
        lines.append("2. Get the full diff and **review every changed file systematically**")
        lines.append(
            "3. Read surrounding context — check how changed code integrates with the rest of the codebase"
        )
        lines.append(
            "4. Trace data flow from input to output, especially for security-sensitive paths"
        )
        lines.append(
            "5. Verify end-to-end functionality — for new features, trace the complete "
            "execution path in the real deployment environment. Check that config files, "
            "environment variables, and dependencies are actually available where the code runs"
        )
        lines.append("6. Research when uncertain — look up library behavior, check documentation")
        lines.append("7. Consider edge cases the author may not have tested")
        lines.append("8. Evaluate against the criteria below")
        lines.append(f"9. Write your verdict to `{verdict_path}` as JSON")
        lines.append("10. Commit the verdict file")
    elif draft_path:
        # Expanded procedural steps for draft-based (non-code) reviewers
        lines.append("2. Read the draft thoroughly — do not skim")
        lines.append(
            "3. Cross-reference each section of the draft against the review criteria below"
        )
        lines.append("4. Cite specific sections, quotes, or omissions as evidence in your analysis")
        lines.append("5. Evaluate completeness — identify any criteria not adequately addressed")
        lines.append("6. Assess overall quality and coherence of the draft")
        lines.append(f"7. Write your verdict to `{verdict_path}` as JSON")
        lines.append("8. Commit the verdict file")
    else:
        lines.append("2. Evaluate it against the criteria below")
        lines.append(f"3. Write your verdict to `{verdict_path}` as JSON")
        lines.append("4. Commit the verdict file")
    lines.append("")

    # Review criteria
    lines.append("## Review Criteria\n")
    lines.append(_get_review_criteria_for_type(reviewer_type, phase, repo_path=repo_path))
    lines.append("")

    # Review conventions — quality standards aligned with PR reviewer thoroughness
    lines.append("## Review Conventions\n")
    if reviewer_type == "code":
        lines.append(
            "You are a critical part of the engineering infrastructure — the last line "
            "of defense before code reaches production. Your review must meet these "
            "quality standards:\n"
        )
    else:
        lines.append("Your review must meet these quality standards:\n")
    lines.append(
        "1. **Be comprehensive.** Review the entire scope, not just the obvious parts. "
        "Do not stop after finding the first few issues."
    )
    lines.append(
        "2. **Be specific.** Reference exact file paths, line numbers, function names, "
        "and code snippets. Vague feedback is not actionable."
    )
    lines.append(
        "3. **Be direct.** State issues plainly without hedging or softening language. "
        '"This will fail when X" not "you might want to consider X".'
    )
    lines.append(
        "4. **Suggest fixes.** When identifying a problem, include a concrete suggestion "
        "for how to resolve it."
    )
    lines.append(
        "5. **Provide context.** Explain *why* something is an issue — the impact, "
        "the risk, or the principle being violated."
    )
    lines.append("")

    # Verdict classification — only for code reviewers (aligned with review-conventions.md)
    # Non-code reviewers get appropriate guidance from their type-specific criteria
    # (e.g., _get_plan_review_criteria() already says "flag as needs_revision")
    if reviewer_type == "code":
        lines.append("### When to Use `needs_revision` vs `approved`\n")
        lines.append(
            "**Use `needs_revision` for**: Security vulnerabilities, logic errors, correctness "
            "issues, non-functional features (core purpose doesn't work end-to-end), missing "
            "error handling, resource leaks, breaking changes, violations of codebase patterns. "
            "When in doubt, use `needs_revision`."
        )
        lines.append(
            "**Use `approved` for**: No blocking issues found after thorough review. "
            "Non-blocking suggestions belong in the `suggestions` field."
        )
        lines.append("")
        lines.append(
            "**Key distinction**: A feature that doesn't work is a correctness issue, not a "
            "style issue. If the feature's core functionality is broken — not just degraded or "
            "missing edge cases — always use `needs_revision`, even if the code structure looks "
            "reasonable or matches an existing pattern."
        )
        lines.append(
            "**Pre-existing issues are still blocking**: If the code being reviewed modifies "
            "areas with existing broken or inconsistent behavior, use `needs_revision` — do not "
            'dismiss it as "not a regression." The code is already being changed in that area, '
            "making it the natural place to fix the issue. Code that adds new paths through "
            "already-broken logic makes the problem worse."
        )
        lines.append("")

    # Delta review directive for re-reviews
    if is_delta_review:
        lines.append("## Delta Review\n")
        lines.append(
            f"This is review cycle {review_cycle}. Focus on new changes since your "
            f"last review. Use `git diff {last_reviewed_commit}..HEAD` to see the "
            "delta. Verify prior feedback was addressed AND review new code thoroughly."
        )
        lines.append("")

    # Prior feedback for re-reviews
    if review_cycle > 1 and prior_feedback:
        lines.append("## Prior Review Feedback\n")
        lines.append(
            "This is a re-review. The previous review found issues. "
            "Verify that the following feedback was addressed:\n"
        )
        lines.append(prior_feedback)
        lines.append("")

    # Verdict format
    lines.append("## Verdict Format\n")
    lines.append(f"Write the following JSON to `{verdict_path}`:\n")
    lines.append("```json")
    lines.append("{")
    lines.append(f'  "reviewer": "{reviewer_type}",')
    lines.append('  "verdict": "approved" or "needs_revision",')
    lines.append('  "summary": "Brief summary of findings (1-2 sentences)",')
    lines.append('  "analysis": "Detailed analysis of the reviewed work (see below)",')
    lines.append('  "suggestions": "Non-blocking suggestions for improvement",')
    lines.append('  "feedback": "Blocking issues requiring revision before approval",')
    lines.append('  "timestamp": "ISO 8601 timestamp"')
    lines.append("}")
    lines.append("```\n")
    lines.append("**Field guidelines:**\n")
    lines.append(
        "- **analysis**: Always provide detailed analysis regardless of verdict. "
        "Describe what you reviewed, what you found, and your reasoning. "
        "Be thorough but concise (200-500 words)."
    )
    lines.append(
        "- **suggestions**: Non-blocking observations and improvement ideas. "
        "Include these even when approving — they help the team improve over time."
    )
    lines.append(
        "- **feedback**: Reserved for **blocking issues only** — problems that must "
        "be fixed before the work can be approved. Leave empty when approving."
    )
    lines.append(
        "\nIf the work meets all criteria, set verdict to `approved`. "
        "If significant issues remain, set verdict to `needs_revision` "
        "and provide actionable feedback in the `feedback` field."
    )

    # Phase restrictions for reviewers
    lines.append("")
    lines.append("## Phase Restrictions\n")
    lines.append("- You CAN read all source files and review artifacts")
    lines.append("- You CAN write verdict files to `.egg-state/reviews/`")
    if reviewer_type == "contract":
        lines.append(
            "- You CAN update the contract in `.egg-state/contracts/` (e.g. marking items as done)"
        )
    lines.append("- You CANNOT push code (git push)")
    lines.append("- You CANNOT create or update PRs")
    lines.append("- You CANNOT modify source files (src/, lib/, docs/, tests/)")
    lines.append("")

    return "\n".join(lines)


def _read_review_verdict(
    repo_path: Path,
    phase: str,
    reviewer_type: str = "code",
    pipeline_mode: str = "local",
    issue_number: int | None = None,
    pipeline_id: str | None = None,
    plan_phase_id: str | None = None,
) -> ReviewVerdict | None:
    """Read a typed review verdict JSON from the repo.

    Returns None if the file is missing or malformed (treated as approved
    for graceful degradation).
    """
    verdict_rel = _verdict_path_for_type(
        phase,
        reviewer_type,
        issue_number=issue_number,
        pipeline_id=pipeline_id,
        plan_phase_id=plan_phase_id,
    )
    verdict_file = repo_path / verdict_rel

    if not verdict_file.exists():
        logger.warning(
            "Verdict file not found, treating as approved",
            path=str(verdict_file),
            reviewer_type=reviewer_type,
        )
        return None

    try:
        raw = verdict_file.read_text()
        data = json.loads(raw)
        return ReviewVerdict(**data)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(
            "Failed to parse verdict file, treating as approved",
            path=str(verdict_file),
            reviewer_type=reviewer_type,
            error=str(e),
        )
        return None


def _read_tester_gaps(
    repo_path: Path,
    identifier: int | str | None = None,
) -> str | None:
    """Read tester output and extract gap findings for feedback to the coder.

    Reads `.egg-state/agent-outputs/{identifier}-tester-output.json` (with
    fallback to `tester-output.json`) and formats any test failures and gaps
    found into a summary string.

    Falls back to scanning the `summary` field for failure keywords when
    `gaps_found` is not present (backwards compat with old tester outputs).

    Args:
        repo_path: Path to the repository.
        identifier: Pipeline/issue identifier for namespaced filenames.

    Returns:
        Formatted gap summary string, or None if no gaps found.
    """
    outputs_dir = repo_path / ".egg-state" / "agent-outputs"

    # Try prefixed filename first, fall back to old global filename
    tester_output_file = None
    if identifier is not None:
        prefixed = outputs_dir / f"{identifier}-tester-output.json"
        if prefixed.exists():
            tester_output_file = prefixed
    if tester_output_file is None:
        tester_output_file = outputs_dir / "tester-output.json"

    if not tester_output_file.exists():
        return None

    try:
        raw = tester_output_file.read_text()
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Failed to parse tester output file",
            path=str(tester_output_file),
            error=str(e),
        )
        return None

    if not isinstance(data, dict):
        return None

    sections: list[str] = []

    tests_failed = data.get("tests_failed", 0)
    if tests_failed:
        sections.append(f"- **{tests_failed}** test(s) failed")

    gaps_found = data.get("gaps_found")
    if gaps_found and isinstance(gaps_found, list):
        # Cap at 10 gaps to avoid prompt bloat
        capped = gaps_found[:10]
        for gap in capped:
            gap_str = str(gap)[:200]
            sections.append(f"- {gap_str}")
        if len(gaps_found) > 10:
            sections.append(f"- ... and {len(gaps_found) - 10} more gaps")
    elif not tests_failed:
        # Backwards compat: scan summary for failure keywords
        summary = data.get("summary", "")
        if isinstance(summary, str) and any(
            kw in summary.lower() for kw in ("fail", "gap", "missing", "error", "deficien")
        ):
            sections.append(f"- Tester summary: {summary}")

    if not sections:
        return None

    return f"{TESTER_FINDINGS_HEADER}\n" + "\n".join(sections)


def _aggregate_review_verdicts(
    verdicts: dict[str, ReviewVerdict | None],
) -> AggregatedReviewResult:
    """Aggregate multiple typed review verdicts into an overall result.

    Returns:
        AggregatedReviewResult with:
        - verdict: "approved" or "needs_revision" (any needs_revision → overall needs_revision)
        - blocking_feedback: combined feedback from needs_revision verdicts only
        - advisory_content: analysis and suggestions from ALL verdicts (including approved)

        Missing/None verdicts are skipped.
    """
    overall = "approved"
    feedback_sections: list[str] = []
    advisory_sections: list[str] = []

    for reviewer_type, verdict in verdicts.items():
        if verdict is None:
            continue

        # Collect blocking feedback from needs_revision verdicts
        if verdict.verdict == "needs_revision":
            overall = "needs_revision"
            section = f"### {reviewer_type} reviewer\n"
            if verdict.feedback:
                section += verdict.feedback
            elif verdict.summary:
                section += verdict.summary
            feedback_sections.append(section)

        # Collect analysis and suggestions from ALL verdicts (including approved)
        advisory_parts: list[str] = []
        if verdict.analysis:
            advisory_parts.append(verdict.analysis)
        if verdict.suggestions:
            advisory_parts.append(f"**Suggestions:** {verdict.suggestions}")
        if advisory_parts:
            advisory_sections.append(
                f"### {reviewer_type} reviewer\n" + "\n\n".join(advisory_parts)
            )

    blocking_feedback = "\n\n".join(feedback_sections) if feedback_sections else ""
    advisory_content = "\n\n".join(advisory_sections) if advisory_sections else ""
    return AggregatedReviewResult(
        verdict=overall,
        blocking_feedback=blocking_feedback,
        advisory_content=advisory_content,
    )


def _sync_worktree_with_remote(
    spawner: "ContainerSpawner",
    pipeline_id: str,
    worktree_repo_path: Path,
    prior_phase_succeeded: bool = True,
) -> None:
    """Sync a worktree with its remote branch (best-effort).

    After an orchestrator restart, the local worktree branch may be behind
    the remote: commits pushed during previous phases (contracts, drafts,
    statefiles) exist on origin but not in the local checkout.  This function
    fetches those commits and resets the worktree so that all downstream code
    (contract loading, draft reading, etc.) sees the full pipeline state.

    When local is ahead of remote:
    - If the prior phase succeeded, push local commits to remote first,
      then reset to origin (preserves completed work).
    - If the prior phase failed or was killed, discard local commits and
      reset to remote (discards incomplete work).

    When local has diverged (ahead AND behind), attempt a fast-forward
    merge.  If the merge fails, log an error (pipeline may need manual
    intervention).

    Safe to call on every pipeline start because it is idempotent when the
    local branch is already up to date.
    """
    git_base = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(worktree_repo_path)]

    # Step 1: Authenticated fetch via gateway (gateway holds GitHub credentials)
    fetch_ok = spawner.gateway.fetch_worktree_branch(
        pipeline_id=pipeline_id,
        repo_path=str(worktree_repo_path),
    )
    if not fetch_ok:
        return

    # Step 2: Determine current branch
    try:
        result = subprocess.run(
            [*git_base, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        branch = result.stdout.strip()
        if not branch:
            return  # Detached HEAD — nothing to sync
    except Exception:
        return

    # Step 3: Verify remote tracking branch exists
    try:
        result = subprocess.run(
            [*git_base, "rev-parse", "--verify", f"origin/{branch}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return  # Remote branch not yet published (first pipeline run)
    except Exception:
        return

    # Step 3b: Check divergence between local and remote.
    local_ahead = 0
    remote_ahead = 0
    try:
        result = subprocess.run(
            [*git_base, "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) == 2:
                local_ahead = int(parts[0])
                remote_ahead = int(parts[1])
    except Exception:
        pass  # If check fails, proceed with reset (best-effort)

    # Step 3c: Handle local-ahead commits.
    if local_ahead > 0 and remote_ahead == 0:
        # Local is strictly ahead of remote (no divergence).
        if prior_phase_succeeded:
            # Prior phase completed successfully — push local work to remote
            # before resetting, so it's not lost.
            logger.info(
                "Prior phase succeeded — pushing local-ahead commits to remote",
                pipeline_id=pipeline_id,
                branch=branch,
                local_ahead=local_ahead,
            )
            push_ok = spawner.gateway.push_worktree_branch(
                pipeline_id=pipeline_id,
                repo_path=str(worktree_repo_path),
                branch=branch,
            )
            if push_ok:
                # Push succeeded — local and remote are now in sync.
                # Re-fetch to update the remote tracking ref so that
                # origin/{branch} reflects the pushed commits.
                spawner.gateway.fetch_worktree_branch(
                    pipeline_id=pipeline_id,
                    repo_path=str(worktree_repo_path),
                )
                return  # Already in sync — no reset needed
            else:
                logger.warning(
                    "Failed to push local-ahead commits (continuing with reset)",
                    pipeline_id=pipeline_id,
                    branch=branch,
                )
        else:
            # Prior phase failed — discard incomplete local work.
            logger.info(
                "Prior phase failed — discarding local-ahead commits",
                pipeline_id=pipeline_id,
                branch=branch,
                local_ahead=local_ahead,
            )
        # Fall through to reset (Step 4)

    elif local_ahead > 0 and remote_ahead > 0:
        # Divergence: local and remote both have unique commits.
        # Attempt fast-forward merge to reconcile.
        logger.info(
            "Local and remote have diverged — attempting merge",
            pipeline_id=pipeline_id,
            branch=branch,
            local_ahead=local_ahead,
            remote_ahead=remote_ahead,
        )
        try:
            merge_result = subprocess.run(
                [*git_base, "merge", "--ff-only", f"origin/{branch}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if merge_result.returncode == 0:
                logger.info(
                    "Fast-forward merge succeeded",
                    pipeline_id=pipeline_id,
                    branch=branch,
                )
                return  # Merge succeeded — worktree is now in sync
            else:
                logger.error(
                    "Cannot fast-forward merge diverged branches — "
                    "pipeline may need manual intervention",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    local_ahead=local_ahead,
                    remote_ahead=remote_ahead,
                    error=merge_result.stderr.strip(),
                )
                return  # Don't force-reset on divergence — signal the problem
        except Exception as merge_err:
            logger.error(
                "Merge attempt failed",
                pipeline_id=pipeline_id,
                error=str(merge_err),
            )
            return

    # Step 4: Reset local branch to remote.
    # This handles: local behind remote, local in-sync, and post-push reset.
    try:
        result = subprocess.run(
            [*git_base, "reset", "--hard", f"origin/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to reset worktree to remote (continuing with local state)",
                pipeline_id=pipeline_id,
                error=result.stderr.strip(),
            )
        else:
            logger.info(
                "Synced worktree with remote branch",
                pipeline_id=pipeline_id,
                branch=branch,
            )
    except Exception as sync_err:
        logger.warning(
            "Failed to reset worktree to remote (continuing with local state)",
            pipeline_id=pipeline_id,
            error=str(sync_err),
        )


def _commit_statefiles_to_worktree(
    worktree_path: Path,
    message: str,
) -> None:
    """Stage and commit all ``.egg-state/`` files in *worktree_path*.

    The old GitHub Actions workflow ran ``git add .egg-state/`` at every
    phase boundary to capture contracts, drafts, reviews, and check
    results.  The local orchestrator must do the same so these state
    files are deterministically present on the feature branch regardless
    of whether the agent happened to commit them.

    The commit is idempotent (skips when nothing is staged).
    Raises ``subprocess.CalledProcessError`` on git failure;
    both call sites catch and log rather than aborting the pipeline.
    """
    state_dir = worktree_path / ".egg-state"
    if not state_dir.exists():
        return  # Nothing to commit yet

    git_base = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(worktree_path)]

    subprocess.run(
        [*git_base, "add", ".egg-state/"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    # Only commit if there are staged changes (idempotent on re-runs)
    result = subprocess.run(
        [*git_base, "diff", "--cached", "--quiet", "--", ".egg-state/"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode == 0:
        return  # Nothing to commit

    subprocess.run(
        [*git_base, "commit", "--no-verify", "-m", message, "--", ".egg-state/"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _build_pr_body(
    pipeline: Pipeline,
    worktree_repo_path: Path,
) -> tuple[str, str]:
    """Build a PR title and body from contract state and git log.

    Uses the planner-generated PR metadata from the contract when available,
    falling back to the issue title and git log.

    Args:
        pipeline: The pipeline state
        worktree_repo_path: Path to the worktree repo directory

    Returns:
        Tuple of (title, body)
    """
    identifier = _pipeline_identifier(pipeline.issue_number, pipeline.id)
    pr_title: str | None = None
    pr_description: str | None = None

    # Try to load PR metadata from the contract (populated by the plan agent)
    try:
        from egg_contracts.loader import load_contract

        contract = load_contract(identifier, worktree_repo_path)
        if contract.pr:
            pr_title = contract.pr.title
            pr_description = contract.pr.description

        # Fall back to issue title if no PR title from contract
        if not pr_title and contract.issue:
            pr_title = contract.issue.title
    except Exception as e:
        logger.debug(
            "Could not load contract for PR metadata",
            pipeline_id=pipeline.id,
            error=str(e),
        )

    # Final fallback for title
    if not pr_title:
        pr_title = f"Implementation for pipeline {pipeline.id}"

    # Build commit log
    commit_log = ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            commit_log = result.stdout.strip()
    except Exception:
        pass

    # Build diff stats
    diff_stats = ""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "origin/main...HEAD"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            diff_stats = result.stdout.strip()
    except Exception:
        pass

    # Assemble body
    body_parts: list[str] = []

    if pr_description:
        body_parts.append(pr_description)
    elif pipeline.issue_number:
        body_parts.append(f"Closes #{pipeline.issue_number}")

    if commit_log:
        body_parts.append(f"## Commits\n\n```\n{commit_log}\n```")

    if diff_stats:
        body_parts.append(f"## Changes\n\n```\n{diff_stats}\n```")

    # Add pipeline context section
    if pipeline.id or pipeline.issue_number:
        context_parts = ["## Pipeline Context\n"]
        if pipeline.id:
            context_parts.append(f"Pipeline: `{pipeline.id}`")
        if pipeline.issue_number:
            context_parts.append(f"Issue: #{pipeline.issue_number}")
        body_parts.append("\n".join(context_parts))

    body_parts.append("Authored-by: egg")

    body = "\n\n".join(body_parts)

    return pr_title, body


def _auto_create_pr(
    pipeline: Pipeline,
    worktree_repo_path: Path,
    spawner: "ContainerSpawner",
) -> str | None:
    """Auto-create a PR for a pipeline without spawning an agent.

    Builds the PR title/body from contract state and git log, then
    creates the PR via the gateway.

    Args:
        pipeline: The pipeline state
        worktree_repo_path: Path to the worktree repo directory
        spawner: Container spawner (used to access gateway client)

    Returns:
        PR URL if creation succeeded, None otherwise
    """
    if not pipeline.repo or not pipeline.branch:
        logger.warning(
            "Cannot auto-create PR: missing repo or branch",
            pipeline_id=pipeline.id,
        )
        return None

    title, body = _build_pr_body(pipeline, worktree_repo_path)

    try:
        pr_url = spawner.gateway.create_pr(
            pipeline_id=pipeline.id,
            repo=pipeline.repo,
            title=title,
            body=body,
            head=pipeline.branch,
            issue_number=pipeline.issue_number,
            agent_role="orchestrator",
        )
        return pr_url
    except Exception as e:
        logger.error(
            "Auto PR creation failed",
            pipeline_id=pipeline.id,
            error=str(e),
        )
        return None


def _build_phase_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    prompt: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    review_feedback: str | None = None,
    review_cycle: int = 0,
    short_circuit: bool = False,
    repo_path: str | None = None,
) -> str:
    """Build a phase-specific prompt for the sandbox Claude invocation.

    Follows a structured prompt format:
    Context → Task → Restrictions → Completion.
    """
    # --- Context header ---
    lines = [f"You are in the **{phase}** phase of the SDLC pipeline.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append(f"Phase: {phase}")
    if repo:
        lines.append(f"Repository: {repo}")
    if branch:
        lines.append(f"Branch: {branch}")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    lines.append("")

    # --- Prior review feedback (revision cycles) ---
    if review_cycle > 0 and review_feedback:
        lines.append(f"## Prior Review Feedback (Cycle {review_cycle})\n")
        has_tester_findings = TESTER_FINDINGS_HEADER in review_feedback
        if has_tester_findings:
            lines.append(
                "The reviewer and tester found issues with your previous work. "
                "Address the feedback below and revise your draft **in-place** "
                "(overwrite the same file).\n"
            )
        else:
            lines.append(
                "The reviewer found issues with your previous draft. "
                "Address the feedback below and revise your draft **in-place** "
                "(overwrite the same file).\n"
            )
        lines.append(review_feedback)
        lines.append("")

    # --- Task description ---
    # Skip re-embedding the full task description on revision cycles for
    # implement phase — the coder already knows the task from cycle 0.
    if prompt and not (phase == "implement" and review_cycle > 0):
        lines.append("## Task Description\n")
        lines.append(prompt)
        lines.append("")

    # --- Phase-specific instructions ---
    lines.append("## Your Task\n")

    # Get the correct draft path based on mode
    analysis_path = _get_draft_path("refine", issue_number=issue_number, pipeline_id=pipeline_id)
    plan_path = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)

    if phase == "refine":
        lines.extend(
            [
                "Analyze this issue and produce a structured analysis document. Your goal is to:\n",
                "1. Understand the problem or feature request",
                "2. Research the current codebase to understand existing patterns",
                "3. Identify constraints and dependencies",
                "4. Consider multiple implementation approaches",
                "5. Recommend an approach with justification",
                "6. Surface **all** questions and uncertainties that need human input "
                "(do not self-limit — raise every ambiguity)",
                "",
                "**IMPORTANT**: Do NOT create an implementation plan, task breakdown, "
                "or phased rollout. That is the **plan** phase's job. Stay focused on "
                "**analysis**: understanding the problem, researching the codebase, "
                "evaluating options, and surfacing decisions for the human.",
                "",
                "## Output Format\n",
                "Create an analysis document following this template:\n",
                "````markdown",
                "# Analysis: [Issue Title]\n",
                "> Issue: #[number] | Phase: refine\n",
                "## Problem Statement\n",
                "[Describe the problem or feature request. "
                "What is the current state? What is the desired outcome?]\n",
                "## Current Behavior\n",
                "[Describe how the system currently works in the relevant area. "
                "Include code references where helpful.]\n",
                "## Constraints\n",
                "- [Technical constraints (compatibility, performance, security)]",
                "- [Business constraints (timeline, scope)]",
                "- [Dependencies on other systems or features]\n",
                "## Options Considered\n",
                "### Option A: [Name]\n",
                "**Approach**: [Brief description]\n",
                "**Pros**:",
                "- [Advantage 1]\n",
                "**Cons**:",
                "- [Disadvantage 1]\n",
                "### Option B: [Name]\n",
                "**Approach**: [Brief description]\n",
                "**Pros**:",
                "- [Advantage 1]\n",
                "**Cons**:",
                "- [Disadvantage 1]\n",
                "## Recommended Approach\n",
                "[Which option is recommended and why. Reference the option above.]\n",
                "## Open Questions\n",
                "**IMPORTANT: Every open question MUST be registered as a contract "
                "decision or feedback item using `egg-contract`.** Do not just write "
                "questions as prose — they will not be seen by the human unless "
                "registered.\n",
                "Surface **all** uncertainties, ambiguities, and assumptions that need "
                "human input. Do not limit yourself to a small number — every genuine "
                "ambiguity, missing requirement, unstated assumption, or design choice "
                "that could go multiple ways should be raised here. It is far better to "
                "ask too many questions than to proceed with incorrect assumptions.\n",
                "**Multiple-choice questions** — RUN this command for each question "
                "where the human must pick from discrete options:",
                "```bash",
                'egg-contract add-decision --question "Which approach should we use?" \\',
                '  --options "Option A" "Option B" "Option C" --format markdown',
                "```",
                "Copy the markdown output into your analysis. The human can check "
                'a checkbox to select an option. An "Other (explain in reply)" '
                "option is auto-appended.\n",
                "**Open-ended questions** — EXECUTE this command for free-form "
                "questions where you need the human to provide text answers:",
                "```bash",
                "egg-contract add-feedback \\",
                '  --question "What is the expected request volume?" \\',
                '  --question "Are there any constraints on third-party dependencies?" \\',
                "  --format markdown",
                "```",
                "This creates a dedicated comment for the human to fill in answers. "
                'They edit the comment to add their responses and check "Submit '
                'feedback" when done. The pipeline will resume with the feedback '
                "available in the contract.\n",
                "**DO NOT:**",
                "- Write questions as plain markdown text without running "
                "`egg-contract add-decision` or `egg-contract add-feedback`",
                "- Use custom HTML comment markers like "
                "`<!-- DECISION: ... -->` instead of the contract CLI",
                "- Skip registration because you think the questions are minor — "
                "register every question\n",
                "---\n",
                "*Authored-by: egg*",
                "````\n",
                "",
            ]
        )
        lines.extend(
            [
                "## Complexity Assessment\n",
                "After completing your analysis, assess the task complexity:",
                "- **low**: Single-file change, straightforward bug fix, small config update, typo fix",
                "- **medium**: Multi-file change with clear scope, feature addition with known patterns",
                "- **high**: Architectural change, new subsystem, cross-cutting concern, "
                "many independent phases that could be parallelized",
                "",
                "Add a metadata block at the very end of your analysis "
                "with the appropriate complexity tier:\n",
                "For **low** complexity (skip plan phase, go directly to implementation):",
                "```yaml",
                "# metadata",
                "short_circuit: true",
                "complexity_tier: low",
                "```\n",
                "For **medium** complexity (standard plan + implement flow):",
                "```yaml",
                "# metadata",
                "complexity_tier: mid",
                "```\n",
                "For **high** complexity (phase-level dispatch with per-phase "
                "implement cycles and optional parallel execution):",
                "```yaml",
                "# metadata",
                "complexity_tier: high",
                "parallel_phases: true",
                "```\n",
                "Set `parallel_phases: true` only when the plan phases are truly "
                "independent and can be implemented in parallel without conflicts.",
                "",
            ]
        )
        lines.extend(
            [
                f"Write your analysis to `{analysis_path}`.",
                "Commit and push the draft when done.\n",
                "**IMPORTANT**: Do NOT post your analysis directly to the issue. "
                "The pipeline will have an internal reviewer check your analysis. "
                "If revisions are needed, you'll be re-invoked with feedback. "
                "Only after internal review passes will the analysis be posted "
                "for human approval.",
                "",
            ]
        )

    elif phase == "plan":
        lines.extend(
            [
                "Create a detailed implementation plan.",
                "",
                "**CRITICAL CONSTRAINT — One Issue = One Workflow = One PR.**",
                "All tasks belong to a single pull request. Use phases and commits to",
                "organise the work within that PR — do NOT propose multiple PRs.",
                "",
                "Steps:",
                "1. Review any prior analysis",
                "2. Break down the work into phases with discrete tasks",
                "3. Define clear acceptance criteria for each task",
                "4. Identify test strategy",
                "5. Consider rollback and risks",
                "",
                "## Output Format",
                "",
                "Write a markdown plan with a **yaml-tasks** structured appendix at the end.",
                "The prose section explains the approach; the appendix is machine-parsed.",
                "",
                "End your document with a fenced YAML block like this:",
                "",
                "````",
                "```yaml",
                "# yaml-tasks",
                "pr:",
                '  title: "Short imperative summary (≤70 chars)"',
                "  description: |",
                "    One-paragraph context and impact.",
                "phases:",
                "  - id: 1",
                "    name: Phase Name",
                "    goal: What this phase achieves",
                "    tasks:",
                "      - id: TASK-1-1",
                "        description: What to do",
                "        acceptance: How to verify it is done",
                "        files:",
                "          - path/to/file.py",
                "```",
                "````",
                "",
                "Do NOT use a `pr_plan` key or propose multiple PRs.",
                "",
                f"Write your plan to `{plan_path}`.",
                "Commit and push the draft when done.",
                "",
            ]
        )

    elif phase == "implement":
        # Embed plan or analysis text directly on first cycle
        # (avoids file-I/O turns inside the sandbox).
        draft_embedded = False
        if repo_path and review_cycle == 0:
            draft_phase = "refine" if short_circuit else "plan"
            draft_text = _read_phase_draft(
                Path(repo_path),
                draft_phase,
                issue_number=issue_number,
                pipeline_id=pipeline_id,
            )
            if draft_text:
                label = "Analysis" if short_circuit else "Plan"
                lines.append(f"## {label}\n")
                lines.append(f"```markdown\n{draft_text}\n```\n")
                draft_embedded = True

            # Embed contract task checklist on first cycle
            contract_tasks = _render_contract_tasks(
                repo_path, pipeline_id, pipeline_mode, issue_number
            )
            if contract_tasks:
                lines.append(contract_tasks)
                lines.append("")

        if review_cycle == 0:
            # Build numbered step list; only include the "review" step
            # when the draft wasn't already embedded above.
            if short_circuit:
                lines.append(
                    "Implement the changes described in the analysis (plan phase was skipped):"
                )
            else:
                lines.append("Implement the changes described in the task and plan:")
            lines.append("")

            steps: list[str] = []
            if not draft_embedded:
                review_target = "analysis" if short_circuit else "plan"
                steps.append(f"Review the {review_target} (check `.egg-state/drafts/`)")
            steps.extend(
                [
                    "Implement the required changes",
                    "Run tests to verify correctness",
                    "Commit with descriptive messages",
                ]
            )
            for i, step in enumerate(steps, 1):
                lines.append(f"{i}. {step}")
            lines.append("")
        else:
            # Revision cycle: slim delta-focused prompt.
            # Guard: if review_feedback is unexpectedly missing, fall
            # back to including the task description so the coder isn't
            # left with a nearly empty prompt.
            if not review_feedback:
                if prompt:
                    lines.append("## Task Description\n")
                    lines.append(prompt)
                    lines.append("")

            lines.append("## Revision Instructions\n")
            if review_feedback:
                has_tester_findings = TESTER_FINDINGS_HEADER in review_feedback
                if has_tester_findings:
                    lines.extend(
                        [
                            "The reviewer and tester found issues with your implementation. "
                            "Focus on addressing the specific feedback above.\n",
                            "1. Review the feedback in the **Prior Review Feedback** section above",
                            "2. Check `git diff` to understand the current state of changes",
                            f"3. Check `.egg-state/agent-outputs/"
                            f"{_pipeline_identifier(issue_number, pipeline_id)}"
                            f"-tester-output.json` for test failures and gaps",
                            "4. Fix the specific issues raised",
                            "5. Run tests to verify your fixes",
                            "6. Commit with descriptive messages",
                            "",
                        ]
                    )
                else:
                    lines.extend(
                        [
                            "The reviewer found issues with your implementation. "
                            "Focus on addressing the specific feedback above.\n",
                            "1. Review the feedback in the **Prior Review Feedback** section above",
                            "2. Check `git diff` to understand the current state of changes",
                            "3. Fix the specific issues raised by the reviewer",
                            "4. Run tests to verify your fixes",
                            "5. Commit with descriptive messages",
                            "",
                        ]
                    )
            else:
                lines.extend(
                    [
                        "A revision was requested but no specific feedback was provided. "
                        "Review the task description above and check `git diff` for the current state.\n",
                        "1. Review the task description above and check `git diff`",
                        "2. Verify the implementation meets the requirements",
                        "3. Run tests to verify correctness",
                        "4. Commit with descriptive messages",
                        "",
                    ]
                )

        # Contract CLI instructions for both local and issue mode
        lines.extend(
            [
                "Use the contract CLI to track progress:",
                "- `egg-contract show` — View current contract state",
                "- `egg-contract add-commit --task <id> --commit <sha>` — Link commit to task",
                "",
            ]
        )

    else:
        lines.append(f"Execute the {phase} phase.\n")

    # --- Phase restrictions ---
    lines.append("## Phase Restrictions\n")
    if issue_number is None and phase in ("refine", "plan"):
        lines.extend(
            [
                "In this phase:",
                "- You CAN push state files to git (contracts, drafts, checkpoints)",
                "- You CAN create HITL decisions (egg-contract add-decision)",
                "- You CAN create feedback requests (egg-contract add-feedback)",
                "- You CANNOT push code changes",
                "- You CANNOT create PRs (gh pr create)",
                "- You CANNOT post issue comments",
                "- You CAN read and modify local files",
                "- You CAN run tests",
                "- You CAN commit locally",
                "",
            ]
        )
    elif issue_number is None and phase == "implement":
        lines.extend(
            [
                "In this phase:",
                "- You CAN push code changes to git",
                "- You CANNOT push .egg-state/ files (except checkpoints)",
                "- You CANNOT create PRs (gh pr create)",
                "- You CAN read and modify local files",
                "- You CAN run tests",
                "- You CAN commit locally",
                "",
            ]
        )
    else:
        if phase in ("refine", "plan"):
            lines.extend(
                [
                    "- You CAN write drafts to `.egg-state/drafts/`",
                    "- You CAN push draft files (git push)",
                    "- You CAN create HITL decisions (egg-contract add-decision)",
                    "- You CAN create feedback requests (egg-contract add-feedback)",
                    "- You CANNOT post analysis/plan directly to the issue "
                    "(internal review must pass first)",
                    "- You CANNOT create PRs (gh pr create)",
                    "",
                ]
            )
        elif phase == "implement":
            lines.extend(
                [
                    "- You CAN push code (git push)",
                    "- You CAN link commits to tasks (egg-contract add-commit)",
                    "- You CANNOT create PRs (the pipeline manages the PR)",
                    "",
                ]
            )
    # --- Completion ---
    lines.append("## Phase Completion\n")
    if phase in ("refine", "plan"):
        lines.append(
            "When your draft is complete, commit and push it. "
            "The pipeline will have an internal reviewer evaluate your work. "
            "If revisions are needed, you'll be re-invoked with feedback. "
            "Only after internal review passes will the output be posted "
            "for human approval."
        )
    else:
        lines.append(
            "When you have completed your work for this phase, "
            "ensure everything is committed and exit successfully."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-agent execution helpers
# ---------------------------------------------------------------------------


def _build_agent_prompt(
    role_value: str,
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    prompt: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    review_feedback: str | None = None,
    review_cycle: int = 0,
    repo_path: str | None = None,
    short_circuit: bool = False,
    phase_obj=None,
    all_phases=None,
    concurrent: bool = False,
) -> str:
    """Build a role-specific prompt for multi-agent execution.

    For the CODER role, delegates to the existing _build_phase_prompt().
    Other roles (TESTER, DOCUMENTER, INTEGRATOR, ARCHITECT, etc.) get
    role-specific instructions.

    Execution roles (tester, documenter, integrator) receive a summarized
    background with structured task information instead of the full issue
    body. Analysis roles (architect, task_planner, risk_analyst) receive
    the full issue body.

    Note: Handoff data from prior waves is passed via the EGG_HANDOFF_DATA
    environment variable (set in _execute_wave_with_spawn_fn), not via
    the prompt — prompts are built once before execution starts.

    Args:
        role_value: Agent role string (e.g. "coder", "tester")
        phase: Pipeline phase name
        pipeline_id: Pipeline ID
        pipeline_mode: "issue" or "local"
        prompt: Original task prompt
        issue_number: GitHub issue number
        repo: Repository name
        branch: Branch name
        review_feedback: Feedback from prior review cycle
        review_cycle: Current review cycle number
        repo_path: Filesystem path to repository (for user override lookup)
        short_circuit: Whether short-circuit mode is enabled
        phase_obj: Current plan phase object (Tier 3 context, optional)
        all_phases: All contract phases (Tier 3 context, optional)
        concurrent: Whether agent runs in concurrent multi-agent mode.
            When True, adds consensus lifecycle preamble instructing the
            agent to stay alive, poll messages, and participate in consensus.

    Returns:
        Complete prompt string for the agent
    """
    # CODER and REFINER use the existing phase prompt (phase-specific
    # instructions are already tailored for refine vs implement etc.)
    if role_value in ("coder", "refiner"):
        return _build_phase_prompt(
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=prompt,
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            review_feedback=review_feedback,
            review_cycle=review_cycle,
            short_circuit=short_circuit,
            repo_path=repo_path,
        )

    # Build context header (shared across all roles)
    lines = [f"You are the **{role_value.upper()}** agent in the **{phase}** phase.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append(f"Phase: {phase}")
    lines.append(f"Mode: {pipeline_mode}")
    lines.append(f"Agent Role: {role_value}")
    if repo:
        lines.append(f"Repository: {repo}")
    if branch:
        lines.append(f"Branch: {branch}")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    lines.append("")

    # Concurrent mode: add consensus lifecycle preamble so agents understand
    # they must stay alive and participate in consensus, not just do their task.
    if concurrent:
        lines.extend(
            [
                "## CRITICAL: Concurrent Consensus Protocol\n",
                "You are running in CONCURRENT mode alongside other agents. "
                "Your job is NOT just your task — it is the **full lifecycle**:\n",
                "1. **BOOTSTRAP**: Check if the agents you depend on have produced work yet. "
                "If not, signal BLOCKED and poll every 30s until their work appears.",
                "2. **EXECUTE**: Do your assigned work (see Your Task below).",
                "3. **SIGNAL READY**: When your work is complete, run: "
                '`egg-orch signal readiness --state READY --reason "Work complete"`',
                "4. **STAY ALIVE & REACT**: Continue polling for messages with "
                "`egg-orch message poll`. If new commits land or another agent sends "
                "feedback, transition back to WORKING, address it, then signal READY again.",
                "5. **WAIT FOR STOP**: The orchestrator sends SIGTERM when consensus is "
                "reached. **You do NOT decide when to exit.** Use your remaining turns "
                "to poll and react.\n",
                "**If you exit before the orchestrator stops you, you have FAILED your role.** "
                "Completing your task is necessary but NOT sufficient — you must remain "
                "available to react to other agents' work until consensus.\n",
                "",
            ]
        )

    # Include role-appropriate context instead of the raw issue body.
    # Analysis roles (architect, task_planner, risk_analyst) receive the full
    # issue body. Execution roles (tester, documenter, integrator) receive a
    # brief summary with structured task information and context pointers.
    role_context = _build_role_context(
        role_value=role_value,
        prompt=prompt,
        issue_number=issue_number,
        phase_obj=phase_obj,
        all_phases=all_phases,
    )
    if role_context:
        lines.append(role_context)

    # Review feedback from prior cycles
    if review_feedback:
        lines.append("## Review Feedback\n")
        lines.append(review_feedback)
        lines.append("")

    # Derive the pipeline identifier for namespaced output filenames.
    _identifier = _pipeline_identifier(issue_number, pipeline_id)

    # Role-specific instructions
    lines.append("## Your Task\n")

    if role_value == "tester":
        lines.extend(
            [
                "Validate the changes and find gaps in the CODER agent's implementation:",
                "",
                "1. Review the changed files (available in handoff data or via git diff)",
                "2. Identify gaps: missing error handling, boundary conditions, uncovered branches",
                "3. Write or update tests targeting identified gaps and new/changed code",
                "4. Run all tests and record which pass and which fail",
                "5. Document gaps found in your handoff output (`gaps_found` field)",
                "6. Commit test files with descriptive messages",
                "",
                "Gap-finding focus:",
                "- Missing error handling and input validation",
                "- Boundary conditions and edge cases",
                "- Uncovered code paths and branches",
                "- Integration gaps between components",
                "",
                "Before writing tests, review the coder's session for context on what was changed and why:",
                "`egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement`",
                "",
            ]
        )
    elif role_value == "documenter":
        lines.extend(
            [
                "Update documentation for the changes made by the CODER agent:",
                "",
                "1. Review the changed files (available in handoff data or via git diff)",
                "2. Update relevant documentation (READMEs, docstrings, API docs)",
                "3. Add or update inline code comments where helpful",
                "4. Commit documentation changes with descriptive messages",
                "",
                "Focus on:",
                "- Accurate descriptions of new features or changes",
                "- Updated usage examples if APIs changed",
                "- Clear explanation of any breaking changes",
                "",
                "Find all changed files across agents:",
                "`egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files`",
                "",
            ]
        )
    elif role_value == "integrator":
        lines.extend(
            [
                "Verify integration of all changes from CODER and TESTER agents:",
                "",
                "1. Run the full test suite to verify all tests pass",
                "2. Check for integration issues between the changes",
                "3. Verify no regressions were introduced",
                "4. Produce an integration report",
                "",
                f"Write your integration report to `.egg-state/agent-outputs/{_identifier}-integrator-output.json`.",
                "",
                "Review pipeline overview and costs before integrating:",
                "`egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files` and "
                "`egg-checkpoint cost --pipeline $EGG_PIPELINE_ID`",
                "",
            ]
        )
    elif role_value == "architect":
        lines.extend(
            [
                "Analyze the task and produce an architecture analysis:",
                "",
                "1. Understand the problem or feature request from the issue",
                "2. Research the current codebase to understand existing patterns",
                "3. Identify key files, constraints, and dependencies",
                "4. Consider multiple implementation approaches",
                "5. Recommend an approach with justification and document technical decisions",
                "",
                f"Write your analysis to `.egg-state/agent-outputs/{_identifier}-architect-output.json`.",
                "",
                "### File Restrictions",
                "",
                f"You MUST only write to `.egg-state/agent-outputs/{_identifier}-architect-output.json`.",
                "Do NOT create or modify any other files. Specifically:",
                "- Do NOT modify analysis drafts (`.egg-state/drafts/*-analysis.md`) — "
                "these are finalized in the refine phase and are read-only",
                "- Do NOT create or modify contracts (`.egg-state/contracts/`)",
                "- Do NOT create or modify reviews (`.egg-state/reviews/`)",
                "- Do NOT create or modify plan drafts (`.egg-state/drafts/*-plan.md`)",
                "",
            ]
        )
    elif role_value == "task_planner":
        draft_path = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
        lines.extend(
            [
                "Decompose the architecture analysis into a single-PR implementation plan.",
                "",
                "**CRITICAL CONSTRAINT — One Issue = One Workflow = One PR.**",
                "All tasks belong to a single pull request. Use phases and commits to",
                "organise the work within that PR — do NOT propose multiple PRs.",
                "",
                "Steps:",
                "1. Review the architecture analysis from the ARCHITECT agent",
                "2. Break down the work into phases with discrete, actionable tasks",
                "3. Define clear acceptance criteria for each task",
                "4. Define dependency ordering between tasks",
                "5. Identify the test strategy",
                "",
                "## Output Format",
                "",
                "Write a markdown plan document with a **yaml-tasks** structured",
                "appendix at the end. The prose section should explain the approach;",
                "the appendix is machine-parsed for contract population.",
                "",
                "End your document with a fenced YAML block like this:",
                "",
                "````",
                "```yaml",
                "# yaml-tasks",
                "pr:",
                '  title: "Short imperative summary (≤70 chars)"',
                "  description: |",
                "    One-paragraph context and impact.",
                "phases:",
                "  - id: 1",
                "    name: Phase Name",
                "    goal: What this phase achieves",
                "    tasks:",
                "      - id: TASK-1-1",
                "        description: What to do",
                "        acceptance: How to verify it is done",
                "        files:",
                "          - path/to/file.py",
                "```",
                "````",
                "",
                "Do NOT use a `pr_plan` key or propose multiple PRs.",
                "",
                f"Write your plan to `{draft_path}`.",
                "",
            ]
        )
    elif role_value == "risk_analyst":
        lines.extend(
            [
                "Assess technical risks for the proposed implementation:",
                "",
                "1. Review the architecture analysis from the ARCHITECT agent",
                "2. Identify technical risks (security, performance, compatibility)",
                "3. Assess impact and likelihood of each risk",
                "4. Propose mitigation strategies and rollback plans",
                "5. Flag areas that need human review",
                "",
                f"Write your risk assessment to `.egg-state/agent-outputs/{_identifier}-risk_analyst-output.json`.",
                "",
            ]
        )
    elif role_value.startswith("reviewer_"):
        # Delegate to the detailed review prompt with criteria and verdict format
        reviewer_type = role_value.replace("reviewer_", "", 1).replace("_", "-")
        return _build_review_prompt(
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            reviewer_type=reviewer_type,
            issue_number=issue_number,
            review_cycle=review_cycle + 1,
            prior_feedback=review_feedback,
            repo_path=repo_path,
        )
    elif role_value == "coordinator":
        lines.extend(
            [
                "You are the COORDINATOR agent. Your mission: analyze the task, "
                "determine the optimal workflow, and drive the pipeline to completion "
                "by spawning and orchestrating specialized agents.",
                "",
                "**CRITICAL: You are an ORCHESTRATOR, not an implementer.**",
                "Do NOT read files, edit code, write documentation, or do any implementation work yourself.",
                "ALL implementation must be delegated to specialized agents (coder, tester, documenter, etc.).",
                "",
                "1. Run `egg-orch coordinator state $EGG_PIPELINE_ID` to check current state",
                "2. Choose a workflow based on the task type (see coordinator.md in your CLAUDE.md)",
                '3. Spawn agents using `egg-orch coordinator spawn $EGG_PIPELINE_ID --role <role> --context "<task>"`',
                "4. Wait for agents to complete, then advance or complete the pipeline",
                "",
                "Follow the detailed coordinator instructions in your CLAUDE.md.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"Execute your role as {role_value} for this phase.",
                "",
            ]
        )

    # Phase restrictions
    lines.append("## Phase Restrictions\n")
    if phase == "implement":
        lines.extend(
            [
                "- You CAN push code changes to git (git push)",
                "- You CAN link commits to tasks (egg-contract add-commit)",
                "- You CANNOT push .egg-state/ files (except checkpoints)",
                "- You CANNOT create PRs (the pipeline manages the PR)",
                "",
                "### Push Recovery",
                "",
                "If your push is rejected due to restricted files on the branch, "
                "create a clean branch from origin/main and cherry-pick only your "
                "code commits:",
                "```",
                "git checkout -b egg/<new-branch> origin/main",
                "git cherry-pick <your-commit-hash>",
                "git push origin egg/<new-branch>",
                "```",
                "Do NOT retry the same push — fix the branch first.",
                "After pushing to the new branch, use `egg-contract add-commit` to "
                "link your commits so the pipeline can track them on the new branch.",
                "",
            ]
        )
    elif phase in ("refine", "plan"):
        lines.extend(
            [
                "- You CAN write to `.egg-state/drafts/` and `.egg-state/agent-outputs/`",
                "- You CAN push these state files to git (git push)",
                "- You CAN create HITL decisions (egg-contract add-decision)",
                "- You CAN create feedback requests (egg-contract add-feedback)",
                "- You CANNOT modify production code (src/, lib/, gateway/, sandbox/, "
                "action/, docs/, tests/, test/)",
                "- You CANNOT modify contracts (.egg-state/contracts/) or CI config (.github/)",
                "- You CANNOT create PRs (gh pr create)",
                "",
                "### Push Recovery",
                "",
                "If your push is rejected due to restricted files on the branch, "
                "create a clean branch from origin/main and cherry-pick only your "
                "state file commits:",
                "```",
                "git checkout -b egg/<new-branch> origin/main",
                "git cherry-pick <your-commit-hash>",
                "git push origin egg/<new-branch>",
                "```",
                "Do NOT retry the same push — fix the branch first.",
                "After pushing to the new branch, use `egg-contract add-commit` to "
                "link your commits so the pipeline can track them on the new branch.",
                "",
            ]
        )

    lines.append("## Phase Completion\n")
    if concurrent:
        lines.extend(
            [
                "When you have completed your primary work:\n",
                "1. Commit all changes",
                '2. Run: `egg-orch signal readiness --state READY --reason "Work complete"`',
                "3. Enter a stay-alive polling loop:",
                "```bash",
                "while true; do",
                "  egg-orch message poll",
                '  sleep "${EGG_MESSAGE_POLL_INTERVAL:-30}"',
                "done",
                "```",
                "4. If a message arrives that affects your work, transition back to WORKING, "
                "address it, then signal READY again.",
                "5. **Do NOT exit.** The orchestrator will stop your container when consensus "
                "is reached.",
            ]
        )
    else:
        lines.append(
            "When you have completed your work, ensure everything is committed and exit successfully."
        )

    return "\n".join(lines)


def _build_phase_scoped_prompt(
    phase_obj,
    pipeline_id: str,
    pipeline_mode: str,
    pipeline: Pipeline,
    worktree_repo_path: Path,
    review_feedback: str | None = None,
    review_cycle: int = 0,
    all_phases=None,
) -> str:
    """Build a coder prompt scoped to a single plan phase's tasks.

    Filters tasks and files_affected to the current plan phase, preventing
    cross-phase context leakage. Embeds the plan overview (goals, approach,
    constraints) rather than the full plan, with other phases shown as
    one-line summaries for orientation.

    Args:
        phase_obj: Contract Phase model with id, name, tasks
        pipeline_id: Pipeline ID
        pipeline_mode: 'issue' or 'local'
        pipeline: Pipeline model
        worktree_repo_path: Path to worktree repo
        review_feedback: Optional review feedback for revision cycles
        review_cycle: Current review cycle number
        all_phases: All contract phases (for one-line summaries of other phases)

    Returns:
        Phase-scoped prompt string
    """
    lines = ["You are in the **implement** phase of the SDLC pipeline.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append("Phase: implement")
    lines.append(f"Mode: {pipeline_mode}")
    lines.append(f"Plan Phase: {phase_obj.id} — {phase_obj.name}")
    if pipeline.repo:
        lines.append(f"Repository: {pipeline.repo}")
    if pipeline.branch:
        lines.append(f"Branch: {pipeline.branch}")
    if pipeline.issue_number is not None:
        lines.append(f"Issue: #{pipeline.issue_number}")
    lines.append("")

    # Review feedback for revision cycles
    if review_cycle > 0 and review_feedback:
        lines.append(f"## Prior Review Feedback (Cycle {review_cycle})\n")
        lines.append(
            "The reviewer and tester found issues with your previous work for this phase. "
            "Address the feedback below.\n"
        )
        lines.append(review_feedback)
        lines.append("")

    # Embed plan overview (not the full plan) on first cycle
    draft_rel = None
    if review_cycle == 0:
        draft_text = _read_phase_draft(
            worktree_repo_path,
            "plan",
            issue_number=pipeline.issue_number,
            pipeline_id=pipeline_id,
        )
        draft_rel = _get_draft_path(
            "plan", issue_number=pipeline.issue_number, pipeline_id=pipeline_id
        )
        if draft_text:
            overview = _extract_plan_overview(draft_text)
            if overview:
                lines.append("## Plan Overview\n")
                lines.append(f"```markdown\n{overview}\n```\n")
            if draft_rel:
                lines.append(f"For full plan details: `cat {draft_rel}`\n")

    # One-line summaries of other phases for orientation
    if all_phases:
        other_phases = [p for p in all_phases if p.id != phase_obj.id]
        if other_phases:
            lines.append("### Other Phases (for orientation)\n")
            for phase in other_phases:
                status = getattr(phase, "status", "unknown")
                task_count = len(phase.tasks) if phase.tasks else 0
                lines.append(f"- {phase.id}: {phase.name} — {task_count} tasks [{status}]")
            lines.append("")

    # Phase-specific task checklist
    lines.append(f"## Your Scope: {phase_obj.name}\n")
    lines.append(
        f"You are implementing **only** the tasks in plan phase `{phase_obj.id}`. "
        "Do NOT implement tasks from other phases.\n"
    )
    lines.append("### Tasks\n")
    for task in phase_obj.tasks:
        status_marker = "x" if task.status == "complete" else " "
        lines.append(f"- [{status_marker}] **{task.id}**: {task.description}")
        if task.acceptance_criteria:
            lines.append(f"  - Acceptance: {task.acceptance_criteria}")
        if task.files_affected:
            lines.append(f"  - Files: {', '.join(task.files_affected)}")
    lines.append("")

    # Instructions
    lines.append("## Instructions\n")
    lines.append("1. Implement the required changes for this phase only")
    lines.append("2. Run tests to verify correctness")
    lines.append("3. Commit with descriptive messages")
    lines.append("")

    # Revision-specific checklist (only when feedback is actually present)
    if review_cycle > 0 and review_feedback:
        lines.append("### Revision Checklist\n")
        lines.append("- [ ] Review the feedback in **Prior Review Feedback** above")
        lines.append(
            f"- [ ] Check `.egg-state/agent-outputs/"
            f"{_pipeline_identifier(pipeline.issue_number, pipeline_id)}"
            f"-tester-output.json` for test failures and gaps"
        )
        lines.append("- [ ] Fix the specific issues raised")
        lines.append("- [ ] Run tests to verify fixes")
        lines.append(
            "- [ ] Check prior failed sessions: "
            "`egg-checkpoint list --issue $EGG_ISSUE_NUMBER --status failed`"
        )
        lines.append("")

    # Contract CLI
    lines.append("Use the contract CLI to track progress:")
    lines.append("- `egg-contract show` — View current contract state")
    lines.append("- `egg-contract add-commit --task <id> --commit <sha>` — Link commit to task")
    lines.append("")

    # Phase restrictions
    lines.append("## Phase Restrictions\n")
    lines.append("- You CAN push code (git push)")
    lines.append("- You CAN link commits to tasks (egg-contract add-commit)")
    lines.append("- You CANNOT create PRs (the pipeline manages the PR)")
    lines.append("")

    lines.append("## Phase Completion\n")
    lines.append(
        "When you have completed your work for this phase, "
        "ensure everything is committed and exit successfully."
    )

    return "\n".join(lines)


def _run_tier3_implement(
    pipeline_id: str,
    pipeline: Pipeline,
    spawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store,
    certs_volume: str | None,
    worktree_repo_path: Path,
) -> tuple[int, str]:
    """Run Tier 3 phase-level dispatch for the implement phase.

    Loops through plan phases in dependency order, running a full
    coder -> tester -> documenter -> checker -> code reviewer cycle for
    each phase's tasks. If the code reviewer rejects, the coder retries
    within that phase. Contract review runs once in the outer pipeline
    loop after all phases complete. After all phases, an integrator runs
    once.

    Args:
        pipeline_id: Pipeline ID
        pipeline: Pipeline model
        spawner: Container spawner
        repo_volumes: Volume mounts
        gateway_mode: Gateway mode
        repos: List of repos
        sandbox_env: Sandbox environment vars
        store: State store
        certs_volume: Certs volume name
        worktree_repo_path: Path to worktree repo

    Returns:
        (exit_code, combined_logs) — 0 on success
    """
    from egg_contracts import load_contract
    from egg_contracts.dependency_graph import PhaseDependencyGraph
    from egg_contracts.loader import (
        contract_exists,
        load_contract_from_branch,
        save_contract,
    )

    pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"
    contract_key = _pipeline_identifier(pipeline.issue_number, pipeline_id)

    # Ensure contract exists in the worktree.  Agent git checkout in a
    # prior phase (e.g. `git checkout -b egg/... origin/main`) may have
    # switched the working tree to a branch that doesn't have the
    # .egg-state/contracts/ file, since the contract was only committed
    # to the worktree's initial temp branch.
    #
    # Restore from the original worktree branch via git show so the full
    # contract (including plan phases) is preserved for Tier 3 dispatch.
    if not contract_exists(contract_key, worktree_repo_path):
        worktree_branch = f"egg/{pipeline_id}/work"
        logger.warning(
            "Contract missing from worktree, restoring from branch",
            pipeline_id=pipeline_id,
            contract_key=contract_key,
            branch=worktree_branch,
        )
        try:
            contract = load_contract_from_branch(
                contract_key, worktree_repo_path, branch=worktree_branch
            )
            save_contract(contract, worktree_repo_path)
            logger.info(
                "Contract restored from worktree branch",
                pipeline_id=pipeline_id,
                phase_count=len(contract.phases) if contract.phases else 0,
            )
        except Exception as exc:
            logger.error(
                "Failed to restore contract from worktree branch",
                pipeline_id=pipeline_id,
                branch=worktree_branch,
                error=str(exc),
            )
            raise

    # Load contract to get plan phases
    contract = load_contract(contract_key, worktree_repo_path)

    if not contract.phases:
        logger.warning(
            "No plan phases found in contract for Tier 3 dispatch, "
            "falling back to standard multi-agent implement",
            pipeline_id=pipeline_id,
        )
        return _run_multi_agent_phase(
            pipeline_id=pipeline_id,
            pipeline=pipeline,
            phase="implement",
            spawner=spawner,
            repo_volumes=repo_volumes,
            gateway_mode=gateway_mode,
            repos=repos,
            sandbox_env=sandbox_env,
            store=store,
            certs_volume=certs_volume,
            worktree_repo_path=worktree_repo_path,
        )

    # Build phase dependency graph
    phase_graph = PhaseDependencyGraph(contract.phases)
    if phase_graph.has_cycle():
        logger.error(
            "Phase dependency graph has cycles, falling back to sequential phase order",
            pipeline_id=pipeline_id,
        )
        phase_waves = None
        phase_order = [p.id for p in contract.phases]
    else:
        phase_waves = phase_graph.compute_waves()
        phase_order = phase_graph.get_sequential_order()

    # Map phase IDs to Phase objects
    phase_map = {p.id: p for p in contract.phases}

    # Populate wave data on the pipeline for DAG visualization.
    # Both fields are set inside the guard so they stay in sync —
    # plan_phase_names without plan_phase_waves would be orphaned metadata.
    if phase_waves is not None:
        pipeline.plan_phase_waves = [list(wave.phase_ids) for wave in phase_waves]
        pipeline.plan_phase_names = {p.id: p.name for p in contract.phases}
        store.save_pipeline(pipeline)

    all_logs: list[str] = []
    logs_lock = threading.Lock()
    cancel_event = threading.Event()  # Signals parallel phases to abort early
    max_retries = pipeline.config.max_review_cycles
    enable_parallel = pipeline.config.enable_parallel_phases and phase_waves is not None

    logger.info(
        "Starting Tier 3 phase-level dispatch",
        pipeline_id=pipeline_id,
        phase_count=len(phase_order),
        phase_order=phase_order,
        parallel=enable_parallel,
    )

    def _run_single_phase_cycle(phase_id: str) -> tuple[int, list[str]]:
        """Run a single phase implementation cycle.

        Runs coder -> tester -> documenter -> checker -> code reviewer for
        each plan phase, retrying on rejection.

        Checks ``cancel_event`` before each container spawn so that parallel
        phases can abort early when a sibling phase fails.
        """
        from concurrent.futures import ThreadPoolExecutor

        phase_logs: list[str] = []
        phase_obj = phase_map.get(phase_id)
        if phase_obj is None:
            logger.warning(
                "Phase not found in contract, skipping",
                pipeline_id=pipeline_id,
                phase_id=phase_id,
            )
            return 0, phase_logs

        logger.info(
            "Starting implement cycle for plan phase",
            pipeline_id=pipeline_id,
            phase_id=phase_id,
            phase_name=phase_obj.name,
        )

        phase_env = {**sandbox_env, "EGG_PLAN_PHASE_ID": phase_id}
        prior_feedback: str | None = None  # Combined reviewer feedback from prior cycle
        last_reviewed_commit: str | None = None  # HEAD before the previous cycle's coder

        tester_gap_summary: str | None = None  # Current cycle's tester gap findings

        for retry in range(max_retries + 1):
            # Reset tester gaps each cycle so stale findings don't accumulate
            tester_gap_summary = None

            # Capture HEAD before coder runs so reviewers on the next
            # retry can diff against this commit (delta reviews).
            cycle_head: str | None = None
            try:
                _head_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=str(worktree_repo_path),
                    timeout=10,
                )
                if _head_result.returncode == 0:
                    cycle_head = _head_result.stdout.strip()
            except Exception:
                pass

            # --- CODER ---
            coder_prompt = _build_phase_scoped_prompt(
                phase_obj=phase_obj,
                pipeline_id=pipeline_id,
                pipeline_mode=pipeline_mode,
                pipeline=pipeline,
                worktree_repo_path=worktree_repo_path,
                review_feedback=prior_feedback,
                review_cycle=retry,
                all_phases=contract.phases,
            )

            if cancel_event.is_set():
                phase_logs.append(f"--- coder ({phase_id}, retry={retry}) cancelled ---")
                return 1, phase_logs

            coder_exit, coder_logs = _spawn_and_wait(
                spawner=spawner,
                pipeline_id=pipeline_id,
                agent_role=AgentRole.CODER,
                issue_number=pipeline.issue_number,
                repo_volumes=repo_volumes,
                gateway_mode=gateway_mode,
                repos=repos,
                phase="implement",
                sandbox_env=phase_env,
                sandbox_command=build_agent_command(coder_prompt),
                store=store,
                certs_volume=certs_volume,
                branch=pipeline.branch,
                plan_phase_id=phase_id,
            )

            phase_logs.append(
                f"--- coder ({phase_id}, retry={retry}, exit={coder_exit}) ---\n{coder_logs}"
            )

            if coder_exit != 0:
                logger.error(
                    "Coder failed for plan phase",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    exit_code=coder_exit,
                )
                return 1, phase_logs

            # --- TESTER ---
            tester_prompt = _build_agent_prompt(
                role_value="tester",
                phase="implement",
                pipeline_id=pipeline_id,
                pipeline_mode=pipeline_mode,
                prompt=pipeline.prompt,
                issue_number=pipeline.issue_number,
                repo=pipeline.repo,
                branch=pipeline.branch,
                repo_path=str(worktree_repo_path),
                short_circuit=pipeline.short_circuit,
                phase_obj=phase_obj,
                all_phases=contract.phases,
            )

            if cancel_event.is_set():
                phase_logs.append(f"--- tester ({phase_id}, retry={retry}) cancelled ---")
                return 1, phase_logs

            tester_exit, tester_logs = _spawn_and_wait(
                spawner=spawner,
                pipeline_id=pipeline_id,
                agent_role=AgentRole.TESTER,
                issue_number=pipeline.issue_number,
                repo_volumes=repo_volumes,
                gateway_mode=gateway_mode,
                repos=repos,
                phase="implement",
                sandbox_env=phase_env,
                sandbox_command=build_agent_command(tester_prompt),
                store=store,
                certs_volume=certs_volume,
                branch=pipeline.branch,
                plan_phase_id=phase_id,
            )

            phase_logs.append(
                f"--- tester ({phase_id}, retry={retry}, exit={tester_exit}) ---\n{tester_logs}"
            )

            if tester_exit != 0:
                logger.warning(
                    "Tester failed for plan phase",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    exit_code=tester_exit,
                )

            # Read tester gap findings for potential feedback to coder.
            # Only read when tester succeeded — a failed tester may have left
            # stale output from a previous cycle on disk.
            if tester_exit == 0:
                tester_gap_summary = _read_tester_gaps(
                    worktree_repo_path,
                    identifier=_pipeline_identifier(pipeline.issue_number, pipeline_id),
                )
                if tester_gap_summary:
                    logger.info(
                        "Tester found gaps",
                        pipeline_id=pipeline_id,
                        phase_id=phase_id,
                    )
            else:
                logger.info(
                    "Skipping tester gap read due to non-zero exit",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    exit_code=tester_exit,
                )

            # --- DOCUMENTER ---
            if cancel_event.is_set():
                phase_logs.append(f"--- documenter ({phase_id}, retry={retry}) cancelled ---")
                return 1, phase_logs

            documenter_prompt = _build_agent_prompt(
                role_value="documenter",
                phase="implement",
                pipeline_id=pipeline_id,
                pipeline_mode=pipeline_mode,
                prompt=pipeline.prompt,
                issue_number=pipeline.issue_number,
                repo=pipeline.repo,
                branch=pipeline.branch,
                repo_path=str(worktree_repo_path),
                short_circuit=pipeline.short_circuit,
                phase_obj=phase_obj,
                all_phases=contract.phases,
            )

            documenter_exit, documenter_logs = _spawn_and_wait(
                spawner=spawner,
                pipeline_id=pipeline_id,
                agent_role=AgentRole.DOCUMENTER,
                issue_number=pipeline.issue_number,
                repo_volumes=repo_volumes,
                gateway_mode=gateway_mode,
                repos=repos,
                phase="implement",
                sandbox_env=phase_env,
                sandbox_command=build_agent_command(documenter_prompt),
                store=store,
                certs_volume=certs_volume,
                branch=pipeline.branch,
                plan_phase_id=phase_id,
            )

            phase_logs.append(
                f"--- documenter ({phase_id}, retry={retry}, exit={documenter_exit}) ---\n{documenter_logs}"
            )

            if documenter_exit != 0:
                logger.warning(
                    "Documenter failed for plan phase",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    exit_code=documenter_exit,
                )

            # --- CHECKER + AUTOFIXER ---
            if cancel_event.is_set():
                phase_logs.append(f"--- checker ({phase_id}, retry={retry}) cancelled ---")
                return 1, phase_logs

            repo_checks: list[dict] | None = None
            if pipeline.repo:
                try:
                    all_repo_checks = json.loads(os.environ.get("EGG_REPO_CHECKS", "{}"))
                except json.JSONDecodeError:
                    all_repo_checks = {}
                repo_lower = pipeline.repo.lower()
                for cfg_repo, cfg_checks in all_repo_checks.items():
                    if cfg_repo.lower() == repo_lower:
                        if isinstance(cfg_checks, list):
                            repo_checks = validate_checks(cfg_checks) or None
                        break

            check_fix_prompt = _build_check_and_fix_prompt(
                pipeline_id,
                pipeline_mode,
                repo=pipeline.repo,
                repo_checks=repo_checks,
                repo_path=str(worktree_repo_path),
                issue_number=pipeline.issue_number,
            )

            try:
                check_exit, _ = _spawn_and_wait(
                    spawner=spawner,
                    pipeline_id=pipeline_id,
                    agent_role=AgentRole.CHECKER,
                    issue_number=pipeline.issue_number,
                    repo_volumes=repo_volumes,
                    gateway_mode=gateway_mode,
                    repos=repos,
                    phase="implement",
                    sandbox_env={**phase_env, "EGG_AGENT_ROLE": "checker"},
                    sandbox_command=build_agent_command(check_fix_prompt, max_turns=100),
                    timeout=2700,
                    store=store,
                    certs_volume=certs_volume,
                    branch=pipeline.branch,
                    plan_phase_id=phase_id,
                )
                if check_exit != 0:
                    logger.warning(
                        "Checker+autofixer exited non-zero",
                        pipeline_id=pipeline_id,
                        phase_id=phase_id,
                        exit_code=check_exit,
                    )
            except ContainerSpawnError as e:
                logger.warning(
                    "Checker+autofixer failed to spawn, skipping checks",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    error=str(e),
                )

            # --- CODE REVIEWER ---
            if cancel_event.is_set():
                phase_logs.append(f"--- reviewers ({phase_id}, retry={retry}) cancelled ---")
                return 1, phase_logs

            reviewer_types = ["code"]
            reviewer_exits: dict[str, int] = {}
            reviewer_logs_map: dict[str, str] = {}

            def _spawn_one_reviewer(  # type: ignore[no-untyped-def]
                rtype: str,
                *,
                _retry=retry,
                _prior_feedback=prior_feedback,
                _last_reviewed_commit=last_reviewed_commit,
                _reviewer_exits=reviewer_exits,
                _reviewer_logs_map=reviewer_logs_map,
            ) -> None:
                review_prompt = _build_review_prompt(
                    phase="implement",
                    pipeline_id=pipeline_id,
                    pipeline_mode=pipeline_mode,
                    reviewer_type=rtype,
                    issue_number=pipeline.issue_number,
                    review_cycle=_retry + 1,
                    prior_feedback=_prior_feedback,
                    repo_path=str(worktree_repo_path),
                    last_reviewed_commit=_last_reviewed_commit,
                    plan_phase_id=phase_id,
                )
                try:
                    r_role = AgentRole(f"reviewer_{rtype}")
                except ValueError:
                    logger.warning(
                        "Unknown reviewer role, skipping",
                        pipeline_id=pipeline_id,
                        reviewer=rtype,
                    )
                    _reviewer_exits[rtype] = 0
                    _reviewer_logs_map[rtype] = ""
                    return
                try:
                    r_exit, r_logs = _spawn_and_wait(
                        spawner=spawner,
                        pipeline_id=pipeline_id,
                        agent_role=r_role,
                        issue_number=pipeline.issue_number,
                        repo_volumes=repo_volumes,
                        gateway_mode=gateway_mode,
                        repos=repos,
                        phase="implement",
                        sandbox_env={**phase_env, "EGG_AGENT_ROLE": f"reviewer_{rtype}"},
                        sandbox_command=build_agent_command(review_prompt, max_turns=50),
                        timeout=1800,
                        store=store,
                        certs_volume=certs_volume,
                        branch=pipeline.branch,
                        plan_phase_id=phase_id,
                    )
                    _reviewer_exits[rtype] = r_exit
                    _reviewer_logs_map[rtype] = r_logs
                except Exception as e:
                    logger.warning(
                        "Reviewer failed to spawn, skipping",
                        pipeline_id=pipeline_id,
                        reviewer=rtype,
                        error=str(e),
                    )
                    _reviewer_exits[rtype] = 0
                    _reviewer_logs_map[rtype] = ""

            with ThreadPoolExecutor(max_workers=len(reviewer_types)) as rev_pool:
                futures = [rev_pool.submit(_spawn_one_reviewer, rt) for rt in reviewer_types]
                for f in futures:
                    f.result()

            for rtype in reviewer_types:
                phase_logs.append(
                    f"--- reviewer_{rtype} ({phase_id}, retry={retry}, exit={reviewer_exits.get(rtype, -1)}) ---\n{reviewer_logs_map.get(rtype, '')}"
                )

            all_verdicts: dict[str, ReviewVerdict | None] = {
                rtype: _read_review_verdict(
                    worktree_repo_path,
                    "implement",
                    rtype,
                    pipeline_mode,
                    pipeline.issue_number,
                    pipeline_id,
                    plan_phase_id=phase_id,
                )
                for rtype in reviewer_types
            }

            agg_result = _aggregate_review_verdicts(all_verdicts)

            if agg_result.advisory_content:
                logger.info(
                    "Review advisory content (non-blocking)",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    advisory_preview=agg_result.advisory_content[:500],
                )

            if agg_result.verdict == "approved":
                logger.info(
                    "Phase approved by all reviewers",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    retry=retry,
                )
                break
            elif retry < max_retries:
                logger.info(
                    "Phase needs revision, retrying",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    retry=retry,
                )
                if tester_gap_summary and agg_result.blocking_feedback:
                    prior_feedback = f"{agg_result.blocking_feedback}\n\n{tester_gap_summary}"
                elif tester_gap_summary:
                    prior_feedback = tester_gap_summary
                else:
                    prior_feedback = agg_result.blocking_feedback
                last_reviewed_commit = cycle_head
                continue
            else:
                logger.warning(
                    "Phase exhausted review retries without approval",
                    pipeline_id=pipeline_id,
                    phase_id=phase_id,
                    max_retries=max_retries,
                )
                return 1, phase_logs

        logger.info(
            "Completed implement cycle for plan phase",
            pipeline_id=pipeline_id,
            phase_id=phase_id,
        )
        return 0, phase_logs

    # Execute phases — either sequentially or in parallel waves
    if enable_parallel and phase_waves:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        for wave in phase_waves:
            logger.info(
                "Executing phase wave",
                pipeline_id=pipeline_id,
                wave_number=wave.wave_number,
                phase_ids=wave.phase_ids,
                parallel=wave.is_parallel(),
            )

            if wave.is_parallel():
                # TODO: Per-phase worktree isolation is not yet wired in.
                # create_phase_worktree()/cleanup_phase_worktrees() exist in
                # gateway/worktree_manager.py but require gateway API calls
                # from the orchestrator. Until wired, parallel phases share
                # the same worktree — which can cause conflicts.
                logger.warning(
                    "Parallel phase execution does not yet use per-phase worktrees; "
                    "concurrent phases share the same filesystem",
                    pipeline_id=pipeline_id,
                    wave_number=wave.wave_number,
                )
                # Run independent phases concurrently
                failed = False
                with ThreadPoolExecutor(max_workers=pipeline.config.max_parallel_agents) as pool:
                    futures = {
                        pool.submit(_run_single_phase_cycle, pid): pid for pid in wave.phase_ids
                    }
                    for future in as_completed(futures):
                        pid = futures[future]
                        exit_code, phase_logs = future.result()
                        with logs_lock:
                            all_logs.extend(phase_logs)
                        if exit_code != 0:
                            failed = True
                            # Signal sibling phases to abort before their
                            # next container spawn.  f.cancel() alone is
                            # ineffective for already-running futures.
                            cancel_event.set()
                            for f in futures:
                                f.cancel()
                            break
                if failed:
                    return 1, "\n".join(all_logs)
            else:
                # Single phase in wave — run sequentially
                for pid in wave.phase_ids:
                    exit_code, phase_logs = _run_single_phase_cycle(pid)
                    all_logs.extend(phase_logs)
                    if exit_code != 0:
                        return 1, "\n".join(all_logs)
    else:
        # Sequential execution (default)
        for phase_id in phase_order:
            exit_code, phase_logs = _run_single_phase_cycle(phase_id)
            all_logs.extend(phase_logs)
            if exit_code != 0:
                return 1, "\n".join(all_logs)

    # After all phases: run integrator with Tier 3-specific instructions
    integrator_prompt = _build_agent_prompt(
        role_value="integrator",
        phase="implement",
        pipeline_id=pipeline_id,
        pipeline_mode=pipeline_mode,
        prompt=pipeline.prompt,
        issue_number=pipeline.issue_number,
        repo=pipeline.repo,
        branch=pipeline.branch,
        repo_path=str(worktree_repo_path),
        short_circuit=pipeline.short_circuit,
        all_phases=contract.phases,
    )
    # Append Tier 3-specific integrator instructions
    tier3_integrator_lines = [
        "",
        "## Tier 3 Integration Responsibilities\n",
        "This is a **high-complexity** (Tier 3) pipeline with multiple implementation phases.",
        "You have **write access** to source, test, and documentation files.\n",
        "Your responsibilities:",
        "1. Run the full test suite and fix any integration failures across phase boundaries",
        "2. Resolve merge conflicts between phase implementations if present",
        "3. Ensure all cross-phase dependencies work correctly end-to-end",
        "4. Fix broken imports, missing interfaces, or type mismatches between phases",
        "5. Run linters and fix any formatting issues introduced by phase coders",
        "6. Commit your integration fixes with descriptive messages",
        "",
        f"Phases implemented (in order): {', '.join(phase_order)}",
        "",
    ]
    integrator_prompt += "\n".join(tier3_integrator_lines)

    integrator_command = build_agent_command(integrator_prompt)

    integrator_exit, integrator_logs = _spawn_and_wait(
        spawner=spawner,
        pipeline_id=pipeline_id,
        agent_role=AgentRole.INTEGRATOR,
        issue_number=pipeline.issue_number,
        repo_volumes=repo_volumes,
        gateway_mode=gateway_mode,
        repos=repos,
        phase="implement",
        sandbox_env=sandbox_env,
        sandbox_command=integrator_command,
        store=store,
        certs_volume=certs_volume,
        branch=pipeline.branch,
        complexity_tier=pipeline.complexity_tier.value if pipeline.complexity_tier else None,
    )

    all_logs.append(f"--- integrator (exit={integrator_exit}) ---\n{integrator_logs}")

    if integrator_exit != 0:
        logger.error(
            "Integrator failed",
            pipeline_id=pipeline_id,
            exit_code=integrator_exit,
        )
        return 1, "\n".join(all_logs)

    return 0, "\n".join(all_logs)


def _read_last_review_feedback(
    repo_path: Path,
    pipeline_id: str,
    pipeline_mode: str,
    issue_number: int | None,
    plan_phase_id: str | None = None,
) -> str | None:
    """Read the most recent review feedback from the reviews directory.

    Returns:
        Review feedback string, or None if not found
    """
    verdict = _read_review_verdict(
        repo_path,
        "implement",
        "code",
        pipeline_mode,
        issue_number,
        pipeline_id,
        plan_phase_id=plan_phase_id,
    )
    if verdict and verdict.feedback:
        return verdict.feedback
    return None


def _run_multi_agent_phase(
    pipeline_id: str,
    pipeline: Pipeline,
    phase: str,
    spawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store,
    certs_volume: str | None,
    worktree_repo_path: Path,
    review_feedback: str | None = None,
    review_cycle: int = 0,
) -> tuple[int, str]:
    """Run a phase using multi-agent wave-based execution.

    Creates a MultiAgentExecutor with a spawner callable that wraps
    _spawn_and_wait() and runs agents in dependency-ordered waves.

    Returns:
        (exit_code, combined_logs) — 0 on success.
    """
    try:
        from dispatch import create_dispatcher
        from multi_agent import MultiAgentExecutor
    except ImportError:
        from ..dispatch import create_dispatcher  # type: ignore
        from ..multi_agent import MultiAgentExecutor  # type: ignore

    from egg_contracts.agent_roles import (
        AgentRole as ContractAgentRole,
    )
    from egg_contracts.agent_roles import (
        get_roles_for_phase,
    )
    from egg_contracts.orchestration import initialize_orchestration

    # Get pipeline mode
    pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"

    # Build agent-specific prompts for all roles in this phase.
    # Note: phase_obj / all_phases are not passed here because Tier 2
    # dispatch has no contract phases. _build_role_context() handles this
    # gracefully — execution roles still get a summarized background.
    roles = get_roles_for_phase(phase, include_reviewers=False)
    agent_prompts_by_role: dict = {}
    for contract_role in roles:
        role_str = contract_role.value
        prompt = _build_agent_prompt(
            role_value=role_str,
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=pipeline.prompt,
            issue_number=pipeline.issue_number,
            repo=pipeline.repo,
            branch=pipeline.branch,
            review_feedback=review_feedback,
            review_cycle=review_cycle,
            repo_path=str(worktree_repo_path),
            short_circuit=pipeline.short_circuit,
        )
        # Map using the orchestrator's AgentRole enum
        try:
            orch_role = AgentRole(role_str)
        except ValueError:
            # New roles not yet in orchestrator AgentRole — skip
            continue
        agent_prompts_by_role[orch_role] = prompt

    # Build spawner callable that wraps _spawn_and_wait()
    all_logs: list[str] = []
    logs_lock = threading.Lock()

    def spawn_fn(role: AgentRole, prompt_text: str, extra_env: dict[str, str]) -> tuple[int, str]:
        merged_env = {**sandbox_env, **extra_env}

        sandbox_command = build_agent_command(prompt_text)

        exit_code, container_logs = _spawn_and_wait(
            spawner=spawner,
            pipeline_id=pipeline_id,
            agent_role=role,
            issue_number=pipeline.issue_number,
            repo_volumes=repo_volumes,
            gateway_mode=gateway_mode,
            repos=repos,
            phase=phase,
            sandbox_env=merged_env,
            sandbox_command=sandbox_command,
            store=store,
            certs_volume=certs_volume,
            branch=pipeline.branch,
        )

        with logs_lock:
            all_logs.append(f"--- {role.value} (exit={exit_code}) ---\n{container_logs}")

        return exit_code, container_logs

    # Ensure contract exists in the worktree.  Agent git checkout in a
    # prior phase (e.g. `git checkout -b egg/... origin/main`) may have
    # switched the working tree to a branch that doesn't have the
    # .egg-state/contracts/ file, since the contract was only committed
    # to the worktree's initial temp branch.
    #
    # The recreated contract is intentionally minimal — it's used only for
    # dispatch coordination in the upcoming multi-agent phase.  Prior phase
    # state (agent outputs, task breakdowns, metadata) is not needed here
    # because orchestration state is re-initialized below via
    # initialize_orchestration(), and phase handoff data is persisted
    # separately via save_agent_output().
    from egg_contracts.loader import contract_exists, create_contract

    contract_key = _pipeline_identifier(pipeline.issue_number, pipeline_id)
    if not contract_exists(contract_key, worktree_repo_path):
        logger.warning(
            "Contract missing from worktree, recreating for multi-agent phase",
            pipeline_id=pipeline_id,
            phase=phase,
            contract_key=contract_key,
        )
        if pipeline.issue_number is not None:
            issue_url = f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
            create_contract(
                issue_number=pipeline.issue_number,
                title=f"Issue #{pipeline.issue_number}",
                url=issue_url,
                repo_root=worktree_repo_path,
            )
        else:
            create_contract(
                pipeline_id=str(contract_key),
                title=(pipeline.prompt or "")[:100],
                repo_root=worktree_repo_path,
            )

    # Create dispatcher and executor.
    # The contract's orchestration state defaults to implement-phase roles
    # (CODER, TESTER, DOCUMENTER, INTEGRATOR).  For other phases (e.g. plan)
    # we need to reinitialize with the correct roles so the dispatcher
    # dispatches ARCHITECT, TASK_PLANNER, RISK_ANALYST instead.
    dispatcher = create_dispatcher(pipeline, worktree_repo_path)

    phase_contract_roles = [ContractAgentRole(r.value) for r in roles]
    dispatcher.contract_orchestrator.state = initialize_orchestration(
        dispatcher.contract_orchestrator.contract,
        roles=phase_contract_roles,
    )

    # Validate: every dispatched role must have a prompt for this phase.
    # This catches misconfiguration early instead of silently skipping agents.
    dispatched_roles = {r.value for r in dispatcher.get_agents_to_run()}
    prompted_roles = {r.value for r in agent_prompts_by_role}
    unexpected = dispatched_roles - prompted_roles
    if unexpected:
        logger.warning(
            "Dispatcher returning agents with no prompt for this phase — "
            "check phase role configuration",
            pipeline_id=pipeline_id,
            phase=phase,
            unexpected_roles=sorted(unexpected),
            expected_roles=sorted(prompted_roles),
        )

    executor = MultiAgentExecutor(
        pipeline=pipeline,
        repo_path=worktree_repo_path,
        dispatcher=dispatcher,
        spawn_fn=spawn_fn,
        max_parallel_agents=pipeline.config.max_parallel_agents,
    )

    # Execute all waves
    completed_waves = executor.execute_all_waves(agent_prompts=agent_prompts_by_role)

    # Check for failures
    has_failures = any(w.has_failures for w in completed_waves)

    combined_logs = "\n".join(all_logs)

    if has_failures:
        return 1, combined_logs

    # After successful multi-agent plan phase, synthesize a plan draft
    # from agent outputs so _populate_contract_from_plan() and the HITL
    # gate can find it.
    if phase == "plan":
        _synthesize_plan_draft(
            worktree_repo_path, pipeline_id, pipeline_mode, pipeline.issue_number
        )

    return 0, combined_logs


def _run_concurrent_phase(
    pipeline_id: str,
    pipeline: Pipeline,
    phase: str,
    spawner,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    sandbox_env: dict[str, str],
    store,
    certs_volume: str | None,
    worktree_repo_path: Path,
) -> tuple[int, str]:
    """Run a phase using concurrent all-agents-at-once execution.

    Creates a ConcurrentPhaseExecutor that spawns all agents simultaneously,
    all sharing the pipeline branch. Each container receives a role-specific
    prompt built via ``_build_agent_prompt``. After spawning, waits for all
    containers to exit and records their state in the pipeline store.

    Returns:
        (exit_code, logs) — 0 on success.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from models import (
        AgentExecution as StateAgentExecution,
    )
    from models import (
        AgentExecutionStatus as StateAgentStatus,
    )
    from models import (
        ContainerInfo,
        ContainerStatus,
        PipelinePhase,
    )

    try:
        from concurrent_executor import ConcurrentPhaseExecutor
    except ImportError:
        from ..concurrent_executor import ConcurrentPhaseExecutor  # type: ignore

    phase_str = phase if isinstance(phase, str) else phase.value
    pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"

    # Build per-role prompts (matches _run_multi_agent_phase pattern).
    roles = [AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER]
    agent_prompts: dict[AgentRole, str] = {}
    for role in roles:
        prompt = _build_agent_prompt(
            role_value=role.value,
            phase=phase_str,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=pipeline.prompt,
            issue_number=pipeline.issue_number,
            repo=pipeline.repo,
            branch=pipeline.branch,
            repo_path=str(worktree_repo_path),
            short_circuit=pipeline.short_circuit,
            concurrent=True,
        )
        agent_prompts[role] = prompt

    # Create spawn function and executor.
    spawn_fn = spawner.create_concurrent_spawn_fn(
        pipeline_id=pipeline_id,
        issue_number=pipeline.issue_number,
        repo_volumes=repo_volumes,
        mode=gateway_mode,
        repos=repos,
        phase=phase_str,
        sandbox_env=sandbox_env,
        certs_volume=certs_volume,
    )

    max_concurrent = getattr(pipeline.config, "max_concurrent_agents", 6)
    executor = ConcurrentPhaseExecutor(
        pipeline=pipeline,
        spawn_fn=spawn_fn,
        max_concurrent=max_concurrent,
    )

    # Spawn all agents with their prompts.
    executions = executor.spawn_all(agent_prompts=agent_prompts)

    # Record spawned containers/agents in pipeline state.
    if store is not None:
        try:
            with get_pipeline_state_lock(pipeline_id):
                pip = store.load_pipeline(pipeline_id)
                phase_execution = pip.get_phase_execution(PipelinePhase(phase_str))
                for exec_info in executions:
                    if exec_info.container_id:
                        container_info = ContainerInfo(
                            container_id=exec_info.container_id,
                            container_name=f"{pipeline_id}-{exec_info.role.value}",
                            status=ContainerStatus.RUNNING,
                            started_at=datetime.utcnow(),
                            agent_role=exec_info.role,
                        )
                        phase_execution.containers.append(container_info)

                    agent_state = StateAgentExecution(
                        role=exec_info.role,
                        status=(
                            StateAgentStatus.RUNNING
                            if exec_info.status == StateAgentStatus.RUNNING
                            else StateAgentStatus.FAILED
                        ),
                        container_id=exec_info.container_id,
                        started_at=datetime.utcnow(),
                    )
                    phase_execution.agents.append(agent_state)
                store.save_pipeline(pip)
        except Exception as track_err:
            logger.warning(
                "Failed to record concurrent agents in pipeline state",
                pipeline_id=pipeline_id,
                error=str(track_err),
            )

    # Check for spawn failures before waiting.  Stop successfully-spawned
    # containers so they don't continue running after the phase is aborted.
    spawn_failures = [e for e in executions if e.status.value == "failed"]
    if spawn_failures:
        for e in executions:
            if e.container_id and e.status.value != "failed":
                try:
                    spawner.docker.stop_container(e.container_id, timeout=10)
                except Exception:
                    pass
        logs = "\n".join(
            f"--- {e.role.value} (status={e.status.value}, error={e.error}) ---" for e in executions
        )
        return 1, logs

    # Consensus-driven polling loop with container-exit fallback.
    #
    # The loop periodically checks consensus via executor.check_consensus().
    # When all agents signal READY, the phase completes immediately without
    # waiting for containers to exit.  If consensus is never reached (timeout
    # or all containers exit first), fall back to exit-code-based completion.
    active_executions = [e for e in executions if e.container_id]
    docker_client = spawner.docker
    all_logs: list[str] = []
    has_failures = [False]  # Mutable container for closure access
    # Lock protects all_logs and has_failures mutations from the
    # ThreadPoolExecutor threads in the timeout fallback path (step 6).
    # The main polling loop is single-threaded, but the lock is cheap
    # and makes the code safe regardless of GIL guarantees.
    _logs_lock = threading.Lock()

    poll_interval = 5  # seconds
    raw_timeout = getattr(pipeline.config, "consensus_timeout_minutes", 30)
    consensus_timeout = max(raw_timeout, 1) * 60  # minimum 1 minute
    start_time = time.monotonic()
    objection_decision_created = False

    # Track which containers have exited and their results.
    exited_containers: dict[str, ContainerInfo] = {}

    def _record_container_exit(exec_info: "StateAgentExecution", final_info: ContainerInfo) -> None:
        """Capture logs and update pipeline state for an exited container."""
        container_logs = ""
        if final_info.exit_code != 0:
            try:
                container_logs = docker_client.get_container_logs(
                    exec_info.container_id,
                    tail=200,
                )
            except Exception:
                pass

        with _logs_lock:
            if final_info.exit_code != 0:
                has_failures[0] = True
            all_logs.append(
                f"--- {exec_info.role.value} (exit={final_info.exit_code}) ---\n{container_logs}"
            )

        if store is not None:
            try:
                with get_pipeline_state_lock(pipeline_id):
                    pip = store.load_pipeline(pipeline_id)
                    pe = pip.get_phase_execution(PipelinePhase(phase_str))

                    for ci in pe.containers:
                        if ci.container_id == exec_info.container_id:
                            ci.status = final_info.status
                            ci.exited_at = final_info.exited_at
                            ci.exit_code = final_info.exit_code
                            break

                    for agent in pe.agents:
                        if agent.container_id == exec_info.container_id:
                            agent.completed_at = datetime.utcnow()
                            if final_info.exit_code == 0:
                                agent.status = StateAgentStatus.COMPLETE
                            else:
                                agent.status = StateAgentStatus.FAILED
                                agent.error = f"Container exited with code {final_info.exit_code}"
                            break

                    store.save_pipeline(pip)
            except Exception as track_err:
                logger.warning(
                    "Failed to update concurrent agent state",
                    container_id=exec_info.container_id,
                    error=str(track_err),
                )

    def _stop_running_containers() -> None:
        """Gracefully stop all containers that haven't exited yet."""
        for e in active_executions:
            if e.container_id not in exited_containers:
                try:
                    docker_client.stop_container(e.container_id, timeout=30)
                except Exception:
                    pass

    def _update_agents_complete() -> None:
        """Mark all running agents as COMPLETE in pipeline state (consensus path)."""
        if store is None:
            return
        try:
            with get_pipeline_state_lock(pipeline_id):
                pip = store.load_pipeline(pipeline_id)
                pe = pip.get_phase_execution(PipelinePhase(phase_str))
                for agent in pe.agents:
                    if agent.status == StateAgentStatus.RUNNING:
                        agent.status = StateAgentStatus.COMPLETE
                        agent.completed_at = datetime.utcnow()
                store.save_pipeline(pip)
        except Exception as track_err:
            logger.warning(
                "Failed to update agents to COMPLETE after consensus",
                pipeline_id=pipeline_id,
                error=str(track_err),
            )

    while True:
        elapsed = time.monotonic() - start_time

        # 1. Check consensus
        try:
            consensus = executor.check_consensus()
        except Exception as e:
            logger.warning(
                "Consensus check failed, continuing poll",
                pipeline_id=pipeline_id,
                error=str(e),
            )
            consensus = {"is_complete": False, "has_objections": False, "blocking_agents": []}

        # 2. Consensus reached — stop containers and return
        if consensus.get("is_complete"):
            if _emit_event is not None:
                _emit_event(
                    EventType.CONSENSUS_REACHED,
                    pipeline_id,
                    data={"elapsed_seconds": elapsed},
                )
            logger.info(
                "Consensus reached, stopping containers",
                pipeline_id=pipeline_id,
                elapsed_seconds=round(elapsed, 1),
                has_failures=has_failures[0],
            )
            _update_agents_complete()
            _stop_running_containers()
            combined_logs = (
                "\n".join(all_logs) if all_logs else "Consensus reached; phase complete."
            )
            # If any container failed before consensus was reached (e.g. OOM
            # kill), propagate the failure even though remaining agents agreed.
            # The HITL decision from handle_agent_failure is still pending but
            # callers need a non-zero exit to trigger failure handling.
            if has_failures[0]:
                return 1, combined_logs
            return 0, combined_logs

        # 3. Handle objections (create HITL decision once).
        #    The decision is fire-and-forget: resolution is processed by the
        #    orchestrator's decision queue (outside this function).  If the
        #    human selects "Override objections", the orchestrator updates
        #    agent readiness, which is picked up by check_consensus() on
        #    the next poll iteration.  "Abort phase" triggers pipeline
        #    cancellation via a separate control path.
        if consensus.get("has_objections") and not objection_decision_created:
            try:
                pipeline.add_decision(
                    question="Agent(s) objecting to phase completion. How to proceed?",
                    options=["Override objections", "Wait for resolution", "Abort phase"],
                    phase=pipeline.current_phase,
                )
                objection_decision_created = True
                logger.info(
                    "Objection detected, HITL decision created",
                    pipeline_id=pipeline_id,
                    blocking_agents=consensus.get("blocking_agents", []),
                )
            except Exception as e:
                logger.warning(
                    "Failed to create objection HITL decision",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

        # 4. Non-blocking check for exited containers
        for exec_info in active_executions:
            if exec_info.container_id in exited_containers:
                continue
            try:
                info = docker_client.get_container_info(exec_info.container_id)
            except (ContainerNotFoundError, ContainerOperationError) as e:
                logger.warning(
                    "Container lost during poll",
                    container_id=exec_info.container_id,
                    role=exec_info.role.value,
                    error=str(e),
                )
                info = ContainerInfo(
                    container_id=exec_info.container_id,
                    container_name=f"{pipeline_id}-{exec_info.role.value}",
                    status=ContainerStatus.FAILED,
                    exit_code=-1,
                    exited_at=datetime.utcnow(),
                )

            if info.status in (
                ContainerStatus.EXITED,
                ContainerStatus.FAILED,
                ContainerStatus.REMOVED,
            ):
                exited_containers[exec_info.container_id] = info
                _record_container_exit(exec_info, info)

                # Handle non-zero exit as agent failure
                if info.exit_code != 0:
                    try:
                        executor.handle_agent_failure(
                            role=exec_info.role.value,
                            error=f"Container exited with code {info.exit_code}",
                        )
                    except Exception as e:
                        logger.warning(
                            "handle_agent_failure error",
                            role=exec_info.role.value,
                            error=str(e),
                        )
                else:
                    # Clean exit (code 0): the consensus wrapper inside the
                    # container handles restarts if the agent didn't signal
                    # READY. We do NOT auto-register READY here — agents
                    # must explicitly participate in consensus.
                    logger.info(
                        "Container exited cleanly, wrapper handles consensus",
                        pipeline_id=pipeline_id,
                        role=exec_info.role.value,
                    )

        # 5. All containers exited — fall back to exit-code-based result
        if len(exited_containers) >= len(active_executions):
            combined_logs = "\n".join(all_logs)
            if has_failures[0]:
                return 1, combined_logs
            return 0, combined_logs

        # 6. Consensus timeout
        if elapsed >= consensus_timeout:
            if _emit_event is not None:
                _emit_event(
                    EventType.CONSENSUS_TIMEOUT,
                    pipeline_id,
                    data={
                        "timeout_minutes": consensus_timeout / 60,
                        "blocking_agents": consensus.get("blocking_agents", []),
                    },
                )
            logger.warning(
                "Consensus timeout reached, falling back to container exit",
                pipeline_id=pipeline_id,
                timeout_minutes=consensus_timeout / 60,
            )
            # Fire-and-forget HITL decision: the orchestrator's decision
            # queue handles resolution asynchronously.  This function falls
            # through to wait for remaining containers regardless.
            try:
                pipeline.add_decision(
                    question=f"Consensus not reached after {int(consensus_timeout / 60)} minutes. How to proceed?",
                    options=["Continue waiting", "Accept current state", "Abort phase"],
                    phase=pipeline.current_phase,
                )
            except Exception:
                pass

            # Fall back: wait for remaining containers with ThreadPoolExecutor
            remaining = [e for e in active_executions if e.container_id not in exited_containers]
            if remaining:
                with ThreadPoolExecutor(max_workers=len(remaining)) as pool:

                    def _wait_remaining(exec_info):
                        try:
                            final_info = docker_client.wait_for_container(
                                exec_info.container_id,
                                timeout=3600,
                            )
                        except (ContainerNotFoundError, ContainerOperationError):
                            final_info = ContainerInfo(
                                container_id=exec_info.container_id,
                                container_name=f"{pipeline_id}-{exec_info.role.value}",
                                status=ContainerStatus.FAILED,
                                exit_code=-1,
                                exited_at=datetime.utcnow(),
                            )
                        _record_container_exit(exec_info, final_info)

                    futures = {pool.submit(_wait_remaining, e): e for e in remaining}
                    for future in as_completed(futures):
                        exc = future.exception()
                        if exc:
                            logger.error(
                                "Error waiting for container after timeout",
                                role=futures[future].role.value,
                                error=str(exc),
                            )
                            with _logs_lock:
                                has_failures[0] = True

            combined_logs = "\n".join(all_logs)
            if has_failures[0]:
                return 1, combined_logs
            return 0, combined_logs

        # 7. Sleep before next poll
        time.sleep(poll_interval)


def _spawn_and_wait(
    spawner,
    pipeline_id: str,
    agent_role: AgentRole,
    issue_number: int | None,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    phase: str,
    sandbox_env: dict[str, str],
    sandbox_command: list[str],
    timeout: int = 3600,
    store=None,
    certs_volume: str | None = None,
    branch: str | None = None,
    complexity_tier: str | None = None,
    plan_phase_id: str | None = None,
    extra_mounts: "list[MountSpec] | None" = None,
) -> tuple[int, str]:
    """Spawn a container, wait for it to exit, clean up, return (exit_code, logs).

    If ``store`` is provided, the container is recorded in the phase execution
    state so that the status endpoint can report it while it runs.

    The container is launched via the shared ``build_sandbox_config()`` path,
    which handles GATEWAY_URL, proxy vars, DNS lockdown, extra_hosts, and
    .git shadow mounts automatically.

    Args:
        repo_volumes: Mapping of repo_name -> host_path for volume mounts.
            Each entry is mounted at /home/egg/repos/<name> in the container,
            with .git shadowed by /dev/null bind mounts to force gateway git operations.
        certs_volume: Docker named volume for gateway CA certs (mounted at
            /shared/certs read-only). If None, certs are not mounted.
        complexity_tier: Optional complexity tier for Tier 3 gateway restrictions.

    Returns:
        (exit_code, container_logs) — logs are captured before cleanup on failure.
    """
    from models import ContainerInfo, ContainerStatus, PipelinePhase

    spawned = spawner.spawn_agent_container(
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        issue_number=issue_number,
        mode=gateway_mode,
        wait_for_gateway=False,
        repos=repos,
        phase=phase,
        extra_env=sandbox_env,
        command=sandbox_command,
        repo_volumes=repo_volumes,
        certs_volume=certs_volume,
        branch=branch,
        complexity_tier=complexity_tier,
        extra_mounts=extra_mounts,
    )

    # Record container and agent in phase execution state
    if store is not None:
        try:
            from models import AgentExecution, AgentExecutionStatus

            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(PipelinePhase(phase))

                # Track container
                container_info = ContainerInfo(
                    container_id=spawned.container_info.container_id,
                    container_name=spawned.container_info.container_name,
                    status=ContainerStatus.RUNNING,
                    started_at=datetime.utcnow(),
                    agent_role=agent_role,
                )
                phase_execution.containers.append(container_info)

                # Track agent execution
                agent_execution = AgentExecution(
                    role=agent_role,
                    status=AgentExecutionStatus.RUNNING,
                    container_id=spawned.container_info.container_id,
                    started_at=datetime.utcnow(),
                    plan_phase_id=plan_phase_id,
                )
                phase_execution.agents.append(agent_execution)

                store.save_pipeline(pipeline)
        except Exception as track_err:
            logger.warning(
                "Failed to record container/agent in pipeline state",
                container_id=spawned.container_info.container_id[:12],
                error=str(track_err),
            )

    docker_client = spawner.docker
    try:
        final_info = docker_client.wait_for_container(
            spawned.container_info.container_id,
            timeout=timeout,
        )
    except (ContainerNotFoundError, ContainerOperationError) as e:
        logger.warning(
            "Container lost during wait, marking failed",
            container_id=spawned.container_info.container_id,
            error=str(e),
        )
        final_info = ContainerInfo(
            container_id=spawned.container_info.container_id,
            container_name=spawned.container_info.container_name,
            status=ContainerStatus.FAILED,
            exit_code=-1,
            exited_at=datetime.utcnow(),
        )

    container_logs = ""
    if final_info.exit_code != 0:
        try:
            container_logs = spawner.docker.get_container_logs(
                spawned.container_info.container_id,
                tail=200,
            )
        except Exception:
            pass

    # Update container and agent status in phase execution
    if store is not None:
        try:
            from models import AgentExecutionStatus

            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(PipelinePhase(phase))

                # Update container status
                for ci in phase_execution.containers:
                    if ci.container_id == spawned.container_info.container_id:
                        ci.status = final_info.status
                        ci.exited_at = final_info.exited_at
                        ci.exit_code = final_info.exit_code
                        break

                # Update agent status
                for agent in phase_execution.agents:
                    if agent.container_id == spawned.container_info.container_id:
                        agent.completed_at = datetime.utcnow()
                        if final_info.exit_code == 0:
                            agent.status = AgentExecutionStatus.COMPLETE
                        else:
                            agent.status = AgentExecutionStatus.FAILED
                            agent.error = f"Container exited with code {final_info.exit_code}"
                        break

                store.save_pipeline(pipeline)
        except Exception as track_err:
            logger.warning(
                "Failed to update container/agent status in pipeline state",
                container_id=spawned.container_info.container_id[:12],
                error=str(track_err),
            )

    # Always clean up the container
    try:
        spawner.remove_agent_container(
            spawned.container_info.container_id,
            force=True,
            cleanup_session=True,
        )
    except Exception as cleanup_err:
        logger.warning(
            "Failed to clean up container",
            container_id=spawned.container_info.container_id[:12],
            error=str(cleanup_err),
        )

    return final_info.exit_code, container_logs


# Phases that pause for human approval before advancing (HITL gates)
_HITL_GATE_PHASES = {"refine", "plan"}

# Keywords that indicate human approval at HITL gates
_APPROVE_KEYWORDS = {"approved", "approve", "lgtm", "yes", ""}

# Bare option labels that indicate "request changes" without actionable feedback
_BARE_OPTION_LABELS = {"request changes", "request_changes"}


def _parse_resolution(resolution: str | None) -> tuple[bool, str | None]:
    """Parse a HITL phase_gate resolution into (is_approved, feedback).

    Handles both JSON-structured resolutions and legacy bare-string formats.
    Used by the AWAITING_HUMAN recovery path in start_pipeline.

    Returns:
        (is_approved, feedback): is_approved is True for approve/select/submit_feedback
        actions, False for request_changes/change_approach. feedback contains the
        revision feedback text (if any) for non-approved resolutions.
    """
    if not resolution:
        return True, None

    resolution = resolution.strip()

    # JSON-first: try structured payload
    try:
        payload = json.loads(resolution)
        if isinstance(payload, dict) and "action" in payload:
            action = payload["action"]
            feedback_text = payload.get("feedback", "") or None

            if action in ("approve", "select", "submit_feedback"):
                return True, None
            elif action in ("request_changes", "change_approach"):
                return False, feedback_text
            # Unknown action — fall through to legacy matching
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    # Legacy bare-string resolution
    if resolution.lower() in _APPROVE_KEYWORDS:
        return True, None
    elif resolution.lower() in _BARE_OPTION_LABELS:
        return False, None
    elif resolution:
        # Free-text feedback — treat as request_changes
        return False, resolution

    return True, None


def _build_checker_prompt(
    pipeline_id: str,
    pipeline_mode: str,
    repo: str | None = None,
    repo_checks: list[dict] | None = None,
    issue_number: int | None = None,
) -> str:
    """Build a prompt for the checker agent that runs tests/lint.

    .. deprecated::
        Prefer :func:`_build_check_and_fix_prompt` which merges checker and
        autofixer into a single agent session to avoid context loss.

    The checker discovers and runs project test/lint commands, then
    writes structured results to .egg-state/checks/{identifier}-implement-results.json.

    Args:
        pipeline_id: Pipeline identifier.
        pipeline_mode: Pipeline mode (e.g. "local", "issue").
        repo: Target repository in "owner/repo" format.
        repo_checks: Pre-configured check commands from repositories.yaml.
        issue_number: GitHub issue number (used for namespaced filenames).
    """
    lines = [
        "You are the **checker** for the SDLC pipeline implement phase.\n",
        f"Pipeline ID: {pipeline_id}",
        f"Mode: {pipeline_mode}",
    ]
    if repo:
        repo_name = repo.split("/")[-1]
        lines.append(f"Repository: {repo}")
        lines.append(f"Working directory: ~/repos/{repo_name}")
    lines.append("")

    lines.append("## Your Task\n")

    if repo_checks:
        # Use explicitly configured check commands
        lines.append("Run the following check commands in order, then write results.\n")
        if repo:
            repo_name = repo.split("/")[-1]
            lines.append(f"First, `cd ~/repos/{repo_name}`.\n")
        for i, check in enumerate(repo_checks, 1):
            lines.append(f"{i}. **{check['name']}**: `{check['command']}`")
        lines.append("")
    else:
        # Fall back to discovery mode
        lines.append("Discover and run all project test and lint commands, then write results.\n")
        if repo:
            repo_name = repo.split("/")[-1]
            lines.append(f"Work in the `~/repos/{repo_name}` directory.\n")
        lines.extend(
            [
                "1. **Discover commands**: Look for Makefile, pyproject.toml, package.json, "
                "setup.cfg, tox.ini, or similar build/test configuration files",
                "2. **Run tests**: Execute the project's test suite (pytest, jest, go test, etc.)",
                "3. **Run linting**: Execute linters (ruff, eslint, golangci-lint, etc.)",
                "",
            ]
        )

    _ck_id = _pipeline_identifier(issue_number, pipeline_id)
    results_filename = f"{_ck_id}-implement-results.json"

    lines.extend(
        [
            f"After running checks, **write results** to `.egg-state/checks/{results_filename}`:\n",
            "```json",
            "{",
            '  "all_passed": true/false,',
            '  "checks": [',
            '    {"name": "pytest", "passed": true/false, "output": "first 2000 chars of output"},',
            '    {"name": "lint", "passed": true/false, "output": "first 2000 chars of output"}',
            "  ]",
            "}",
            "```\n",
            "**IMPORTANT**: Include the full command output in the `output` field (first 2000 "
            "characters if longer). This output is passed to the autofixer — summaries force "
            "it to re-run the checks.\n",
            "Then commit the results file.\n",
            "## Important\n",
            "- Always exit 0 regardless of check results (results are informational)",
            "- Write the results file even if all checks pass",
            "- If you cannot find any test/lint commands, write all_passed: true",
        ]
    )
    return "\n".join(lines)


def _build_autofix_prompt(
    pipeline_id: str,
    pipeline_mode: str,
    check_results: dict,
    repo: str | None = None,
    repo_path: str | None = None,
    issue_number: int | None = None,
) -> str:
    """Build a prompt for the autofixer agent.

    .. deprecated::
        Prefer :func:`_build_check_and_fix_prompt` which merges checker and
        autofixer into a single agent session to avoid context loss.

    Modeled on action/build-autofixer-prompt.sh. Tells the agent to read
    check failures, fix auto-fixable issues, and commit fixes.
    """
    failures = []
    for check in check_results.get("checks", []):
        if not check.get("passed", True):
            name = check.get("name", "unknown")
            output = check.get("output", "failed")
            failures.append(f"### {name}\n\n```\n{output}\n```\n")

    failure_summary = "\n".join(failures) if failures else "No specific failures recorded.\n"

    lines = [
        "You are the **autofixer** for the SDLC pipeline implement phase.\n",
        f"Pipeline ID: {pipeline_id}",
        f"Mode: {pipeline_mode}",
    ]
    if repo:
        repo_name = repo.split("/")[-1]
        lines.append(f"Repository: {repo}")
        lines.append(f"Working directory: ~/repos/{repo_name}")
    lines.extend(
        [
            "",
            "## Check Failures\n",
            failure_summary,
            "",
            "## Your Task\n",
            "**Fix ALL auto-fixable issues in a single pass.**\n",
        ]
    )
    if repo:
        repo_name = repo.split("/")[-1]
        lines.append(f"Work in the `~/repos/{repo_name}` directory.\n")
    _af_id = _pipeline_identifier(issue_number, pipeline_id)
    lines.extend(
        [
            "1. **Investigate the failures above**: The full check output is included — "
            f"do NOT re-read `.egg-state/checks/{_af_id}-implement-results.json`",
            "2. **Fix without committing yet**: For each auto-fixable issue "
            "(lint errors, formatting, simple type errors, obvious test fixes), make the fix",
            "3. **Verify locally**: Run the same checks again to confirm fixes work",
            "4. **Commit all fixes together** with a descriptive message\n",
        ]
    )

    # Load autofixer rules from shared file or use inline fallback
    autofixer_rules = _read_shared_criteria(
        "autofixer-rules.md",
        user_override="autofixer-rules.md",
        repo_path=repo_path,
    )
    if autofixer_rules is not None:
        lines.append(autofixer_rules)
    else:
        logger.warning("Shared autofixer-rules.md not found, using inline fallback")
        lines.extend(
            [
                "## Fix ALL Failures\n",
                '**Never skip a failure because it\'s "pre-existing".** '
                "Fix all checks on this branch.\n",
                "## Auto-fixable vs Report-only\n",
                "**Auto-fixable (commit fixes directly):**",
                "- Lint errors (formatting, import order, code style)",
                "- Type errors with clear fixes",
                "- Simple test failures with obvious fixes\n",
                "**Report only (explain what's needed):**",
                "- Complex logic errors requiring design decisions",
                "- Security issues requiring architectural changes",
                "- Failures that require understanding business requirements to resolve correctly",
            ]
        )

    return "\n".join(lines)


def _build_check_and_fix_prompt(
    pipeline_id: str,
    pipeline_mode: str,
    repo: str | None = None,
    repo_checks: list[dict] | None = None,
    repo_path: str | None = None,
    issue_number: int | None = None,
) -> str:
    """Build a combined check-and-fix prompt for a single agent session.

    Replaces the separate checker → autofixer loop with a single agent
    that runs checks, fixes auto-fixable issues, and repeats up to 3 times.
    This avoids context loss between separate container sessions.

    Args:
        pipeline_id: Pipeline identifier.
        pipeline_mode: Pipeline mode (e.g. "local", "issue").
        repo: Target repository in "owner/repo" format.
        repo_checks: Pre-configured check commands from repositories.yaml.
        repo_path: Filesystem path to repository (for loading autofixer rules).
        issue_number: GitHub issue number (used for namespaced filenames).
    """
    lines = [
        "You are the **checker and autofixer** for the SDLC pipeline implement phase.\n",
        f"Pipeline ID: {pipeline_id}",
        f"Mode: {pipeline_mode}",
    ]
    if repo:
        repo_name = repo.split("/")[-1]
        lines.append(f"Repository: {repo}")
        lines.append(f"Working directory: ~/repos/{repo_name}")
    lines.append("")

    lines.append("## Your Task\n")
    lines.append(
        "Run all project checks, fix auto-fixable issues, and repeat until checks "
        "pass or you have made 3 fix attempts.\n"
    )

    # Check commands section
    if repo_checks:
        lines.append("### Check Commands\n")
        lines.append("Run the following check commands in order:\n")
        if repo:
            repo_name = repo.split("/")[-1]
            lines.append(f"First, `cd ~/repos/{repo_name}`.\n")
        for i, check in enumerate(repo_checks, 1):
            lines.append(f"{i}. **{check['name']}**: `{check['command']}`")
        lines.append("")
    else:
        lines.append("### Discover Check Commands\n")
        if repo:
            repo_name = repo.split("/")[-1]
            lines.append(f"Work in the `~/repos/{repo_name}` directory.\n")
        lines.extend(
            [
                "1. **Discover commands**: Look for Makefile, pyproject.toml, package.json, "
                "setup.cfg, tox.ini, or similar build/test configuration files",
                "2. **Run tests**: Execute the project's test suite (pytest, jest, go test, etc.)",
                "3. **Run linting**: Execute linters (ruff, eslint, golangci-lint, etc.)",
                "",
            ]
        )

    # Fix rules
    lines.append("### Fix Rules\n")
    autofixer_rules = _read_shared_criteria(
        "autofixer-rules.md",
        user_override="autofixer-rules.md",
        repo_path=repo_path,
    )
    if autofixer_rules is not None:
        lines.append(autofixer_rules)
    else:
        lines.extend(
            [
                '**Never skip a failure because it\'s "pre-existing".** '
                "Fix all checks on this branch.\n",
                "**Auto-fixable (commit fixes directly):**",
                "- Lint errors (formatting, import order, code style)",
                "- Type errors with clear fixes",
                "- Simple test failures with obvious fixes\n",
                "**Report only (explain what's needed):**",
                "- Complex logic errors requiring design decisions",
                "- Security issues requiring architectural changes",
                "- Failures that require understanding business requirements to resolve correctly",
            ]
        )
    lines.append("")

    # Workflow
    lines.extend(
        [
            "### Workflow\n",
            "Repeat the following up to **3 times**:\n",
            "1. Run all checks",
            "2. If all pass, write the results file and stop",
            "3. If any fail, fix auto-fixable issues (do NOT commit yet)",
            "4. Re-run checks to verify fixes",
            "5. Commit all fixes together with a descriptive message",
            "",
            "### Results File\n",
            "After the final check run, write results to "
            f"`.egg-state/checks/{_pipeline_identifier(issue_number, pipeline_id)}-implement-results.json`:\n",
            "```json",
            "{",
            '  "all_passed": true/false,',
            '  "checks": [',
            '    {"name": "pytest", "passed": true/false, "output": "first 2000 chars of output"},',
            '    {"name": "lint", "passed": true/false, "output": "first 2000 chars of output"}',
            "  ]",
            "}",
            "```\n",
            "Include the full command output in the `output` field (first 2000 characters "
            "if longer).\n",
            "Then commit the results file.\n",
            "## Important\n",
            "- Always exit 0 regardless of check results (results are informational)",
            "- Write the results file even if all checks pass",
            "- If you cannot find any test/lint commands, write all_passed: true",
        ]
    )
    return "\n".join(lines)


# Minimum characters of non-heading content required for a synthesized plan
# draft to be written.  This prevents writing near-empty drafts that contain
# only section headings (e.g. when agents produced no meaningful output).
# A short but valid single-section output like "No architectural risks
# identified." is ~40 chars, so 50 provides a small buffer while still
# catching truly empty drafts.
_MIN_PLAN_DRAFT_CONTENT_LENGTH = 50


def _synthesize_plan_draft(
    repo_path: Path,
    pipeline_id: str,
    pipeline_mode: str = "local",
    issue_number: int | None = None,
) -> None:
    """Synthesize a plan draft from multi-agent plan outputs.

    In multi-agent plan mode, ARCHITECT and RISK_ANALYST write to
    .egg-state/agent-outputs/.  TASK_PLANNER writes the plan draft
    directly to .egg-state/drafts/{id}-plan.md.  This function combines
    the remaining agent outputs into the plan draft (if the task_planner
    has not already written one) so that _populate_contract_from_plan()
    and the HITL gate can find it.
    """
    draft_rel = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        logger.debug(
            "No draft path for plan phase, skipping synthesis",
            pipeline_id=pipeline_id,
        )
        return

    draft_path = repo_path / draft_rel
    if draft_path.exists():
        # Draft already written (e.g. by a single-agent run) — don't overwrite.
        return

    outputs_dir = repo_path / ".egg-state" / "agent-outputs"
    if not outputs_dir.is_dir():
        logger.warning(
            "No agent-outputs directory, cannot synthesize plan draft",
            pipeline_id=pipeline_id,
        )
        return

    # Derive the pipeline identifier for namespaced output filenames.
    _synth_id = _pipeline_identifier(issue_number, pipeline_id)

    sections: list[str] = []
    agent_files = [
        ("architect-output.json", "Architecture Analysis"),
        ("risk_analyst-output.json", "Risk Assessment"),
    ]

    for filename, heading in agent_files:
        # Try prefixed filename first, fall back to old global filename
        prefixed_file = outputs_dir / f"{_synth_id}-{filename}"
        if prefixed_file.exists():
            output_file = prefixed_file
        else:
            output_file = outputs_dir / filename
        if not output_file.exists():
            continue
        try:
            raw = output_file.read_text()
            data = json.loads(raw)
            # Agent outputs may contain a "content" or "output" key with
            # the main text, or may be the full JSON blob.
            content = data.get("content") or data.get("output") or json.dumps(data, indent=2)
        except json.JSONDecodeError:
            # Fall back to raw text if not valid JSON
            content = raw
        except Exception as e:
            logger.warning(
                "Failed to read agent output for plan draft",
                pipeline_id=pipeline_id,
                file=filename,
                error=str(e),
            )
            continue

        # Skip empty or whitespace-only outputs
        if not content or not content.strip():
            logger.warning(
                "Agent output is empty, skipping from plan draft",
                pipeline_id=pipeline_id,
                file=filename,
            )
            continue

        sections.append(f"## {heading}\n\n{content}")

    if not sections:
        logger.warning(
            "No agent outputs found to synthesize plan draft",
            pipeline_id=pipeline_id,
        )
        return

    draft_content = "\n\n".join(sections) + "\n"

    # Guard against a draft that has section headings but no real content.
    stripped = draft_content
    for _, heading in agent_files:
        stripped = stripped.replace(f"## {heading}", "")
    if len(stripped.strip()) < _MIN_PLAN_DRAFT_CONTENT_LENGTH:
        logger.warning(
            "Synthesized plan draft has insufficient content, not writing",
            pipeline_id=pipeline_id,
            content_length=len(stripped.strip()),
        )
        return

    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(draft_content)
    logger.info(
        "Synthesized plan draft from agent outputs",
        pipeline_id=pipeline_id,
        path=str(draft_path),
        sections=len(sections),
    )


def _populate_contract_from_plan(
    repo_path: Path,
    pipeline_id: str,
    pipeline_mode: str = "local",
    issue_number: int | None = None,
) -> None:
    """Read the plan draft and populate the contract with tasks.

    Extracts task structure from markdown headers in the plan draft
    and writes tasks + acceptance criteria to the contract.
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError:
        logger.warning("egg_contracts not available, skipping contract population")
        return

    # Resolve draft path
    draft_rel = _get_draft_path("plan", issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        logger.warning("No draft path for plan phase", pipeline_id=pipeline_id)
        return

    plan_path = repo_path / draft_rel
    if not plan_path.exists():
        logger.warning("Plan draft not found, skipping contract population", path=str(plan_path))
        return

    # Use issue_number as contract identifier when available
    contract_id: int | str = _pipeline_identifier(issue_number, pipeline_id)

    try:
        contract = load_contract(contract_id, repo_path)
    except Exception:
        logger.warning(
            "Contract not found for pipeline, skipping population", pipeline_id=pipeline_id
        )
        return

    try:
        from egg_contracts.plan_parser import parse_plan

        plan_text = plan_path.read_text()
        result = parse_plan(plan_text)

        if not result.success:
            logger.warning(
                "Plan parsing failed, skipping contract population",
                pipeline_id=pipeline_id,
                error=result.error,
            )
            return

        for warning in result.warnings:
            logger.warning(
                "Plan parse warning",
                pipeline_id=pipeline_id,
                message=warning.message,
                context=warning.context,
            )

        contract_phases = result.to_contract_phases()
        if contract_phases:
            contract.phases = contract_phases
            save_contract(contract, repo_path)
            task_count = sum(len(p.tasks) for p in contract_phases)
            logger.info(
                "Contract populated from plan",
                pipeline_id=pipeline_id,
                phase_count=len(contract_phases),
                task_count=task_count,
            )

    except Exception as e:
        logger.warning(
            "Failed to populate contract from plan",
            pipeline_id=pipeline_id,
            error=str(e),
        )


def _sync_pipeline_decisions_to_contract(
    repo_path: Path,
    pipeline_id: str,
    pipeline_mode: str = "local",
    issue_number: int | None = None,
) -> None:
    """Sync resolved non-phase-gate pipeline decisions to the contract.

    Converts HITLDecision objects from pipeline state into contract Decision
    objects so that implement-phase agents can see what was decided during
    refine/plan phases.

    Only syncs decisions with decision_type != "phase_gate" (substantive
    choices, not process-control gates).  Skips decisions already present
    in the contract (matched by question text) to avoid duplicates on
    re-runs after HITL revision cycles.
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Decision, DecisionOption, DecisionType
    except ImportError:
        logger.warning("egg_contracts not available, skipping decision sync")
        return

    # Load pipeline to get its decisions
    store = get_state_store(repo_path)
    try:
        pipeline = store.load_pipeline(pipeline_id)
    except Exception:
        logger.warning(
            "Pipeline not found, skipping decision sync",
            pipeline_id=pipeline_id,
        )
        return

    # Filter to resolved, non-phase-gate decisions
    substantive_decisions = [
        d
        for d in pipeline.decisions
        if d.decision_type != "phase_gate" and d.status == DecisionStatus.RESOLVED
    ]

    if not substantive_decisions:
        logger.debug("No substantive decisions to sync", pipeline_id=pipeline_id)
        return

    # Use issue_number as contract identifier when available
    contract_id: int | str = _pipeline_identifier(issue_number, pipeline_id)

    try:
        contract = load_contract(contract_id, repo_path)
    except Exception:
        logger.warning(
            "Contract not found, skipping decision sync",
            pipeline_id=pipeline_id,
        )
        return

    # Build set of existing contract decision questions for deduplication
    existing_questions = {d.question for d in contract.decisions}

    # Determine next decision ID (continue numbering after existing ones)
    max_existing_id = 0
    for d in contract.decisions:
        # Extract numeric suffix from "decision-N"
        try:
            num = int(d.id.split("-")[1])
            max_existing_id = max(max_existing_id, num)
        except (IndexError, ValueError):
            pass

    synced_count = 0
    for pipeline_decision in substantive_decisions:
        if pipeline_decision.question in existing_questions:
            continue

        max_existing_id += 1
        decision_id = f"decision-{max_existing_id}"

        # Convert pipeline options (list[str]) to contract DecisionOption objects
        contract_options = [
            DecisionOption(id=f"opt-{i + 1}", label=opt)
            for i, opt in enumerate(pipeline_decision.options)
        ]

        contract_decision = Decision(
            id=decision_id,
            question=pipeline_decision.question,
            type=DecisionType.HITL,
            options=contract_options,
            resolved=True,
            resolution=pipeline_decision.resolution,
            resolved_by="human",
            resolved_at=pipeline_decision.resolved_at,
        )
        contract.decisions.append(contract_decision)
        existing_questions.add(pipeline_decision.question)
        synced_count += 1

    if synced_count > 0:
        save_contract(contract, repo_path)
        logger.info(
            "Synced pipeline decisions to contract",
            pipeline_id=pipeline_id,
            synced_count=synced_count,
            total_contract_decisions=len(contract.decisions),
        )


def _run_pipeline(pipeline_id: str, repo_path: Path) -> None:
    """Run a pipeline by spawning containers for each phase.

    This runs in a background thread. For each phase it:
    1. Spawns a worker (CODER) container — or multi-agent wave execution
       for implement and plan phases when multi_agent is enabled
    2. For reviewed phases (refine, implement, plan): spawns reviewers
       as a separate step after all workers (and checkers) complete,
       then reads reviewer verdicts and loops back with feedback if
       revision is needed.
    3. Advances to the next phase once approved (or circuit-breaker hit)

    Args:
        pipeline_id: Pipeline ID
        repo_path: Path to repository
    """
    from routes.phases import PHASE_TRANSITIONS

    # Track which run of the pipeline this thread owns.  If the pipeline
    # is deleted and recreated with the same ID while we're still running,
    # the new run creates its own worktrees under the same path.  Without
    # this guard, our finally block would delete the *new* run's worktrees.
    run_created_at: datetime | None = None

    try:
        store = get_state_store(repo_path)
        spawner = get_container_spawner()
        pipeline = store.load_pipeline(pipeline_id)
        run_created_at = pipeline.created_at
        pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"
        transitions = PHASE_TRANSITIONS

        # Apply environment variable overrides for multi-agent config.
        # These come from CLI flags (--multi-agent, --max-parallel) passed
        # as env vars to the container.
        env_multi_agent = os.environ.get("EGG_MULTI_AGENT")
        if env_multi_agent is not None:
            pipeline.config.multi_agent = env_multi_agent == "1"
        env_max_parallel = os.environ.get("EGG_MAX_PARALLEL_AGENTS")
        if env_max_parallel is not None:
            try:
                pipeline.config.max_parallel_agents = int(env_max_parallel)
            except ValueError:
                logger.warning(
                    "Invalid EGG_MAX_PARALLEL_AGENTS value: %r, ignoring",
                    env_max_parallel,
                )

        # Map pipeline to gateway session mode.
        # If the pipeline has an explicit network_mode (e.g. "private"), use it;
        # otherwise default to "public".
        gateway_mode = pipeline.network_mode or "public"

        # Parse host repo map for volume mounts.  When the orchestrator
        # runs inside Docker, EGG_REPO_PATH is the *container* path but
        # volume mounts need *host* paths (since the Docker socket
        # operates on the host daemon).  EGG_HOST_REPO_MAP provides a
        # JSON mapping of repo_name -> host_path, auto-generated from
        # repositories.yaml by the egg launcher.
        host_repo_map_raw = os.environ.get("EGG_HOST_REPO_MAP", "{}")
        try:
            host_repo_map: dict[str, str] = json.loads(host_repo_map_raw)
        except json.JSONDecodeError as exc:
            logger.error(
                "Failed to parse EGG_HOST_REPO_MAP — no repos will be mounted in sandbox containers",
                raw_value=host_repo_map_raw,
            )
            raise ValueError(
                f"EGG_HOST_REPO_MAP contains invalid JSON: {host_repo_map_raw!r}"
            ) from exc

        # Create isolated worktrees via the gateway.  The gateway creates
        # per-pipeline worktrees from the main repos and returns host paths
        # suitable for Docker volume mounts.  All containers in a pipeline
        # share the same worktrees so they see each other's commits.
        #
        # We use the pipeline_id as the worktree container_id so all
        # containers in the pipeline share the same working trees.
        worktree_id = pipeline_id
        repo_volumes: dict[str, str] = {}
        worktree_repo_path = repo_path  # default; overridden when worktrees exist
        host_uid = int(os.environ.get("HOST_UID", 1000))
        host_gid = int(os.environ.get("HOST_GID", 1000))
        pipeline_repos = [pipeline.repo] if pipeline.repo else []

        if host_repo_map:
            try:
                # Request repos in owner/repo format if available, else bare names
                wt_repos = pipeline_repos if pipeline_repos else list(host_repo_map.keys())
                # Let the gateway resolve the remote default branch for each repo
                # (e.g., origin/main or origin/master) instead of hardcoding a
                # branch name.  See #860.
                wt_result = spawner.gateway.create_worktrees(
                    container_id=worktree_id,
                    repos=wt_repos,
                    uid=host_uid,
                    gid=host_gid,
                )

                if wt_result.success and wt_result.worktrees:
                    # Gateway returns worktrees keyed by repo name only (e.g., "egg"),
                    # stripping the owner prefix from "owner/repo" format. This matches
                    # the container mount target at /home/egg/repos/<name>.
                    repo_volumes = wt_result.worktrees

                    # Derive the orchestrator-accessible worktree path.
                    # Reviewer containers write verdict/draft/check files into
                    # the worktree, so the orchestrator must read from there.
                    # Match against pipeline.repo explicitly to avoid picking
                    # the wrong repo in multi-repo pipelines.
                    repo_short = pipeline.repo.split("/")[-1] if pipeline.repo else None
                    matched = False
                    if repo_short and repo_short in wt_result.worktrees:
                        candidate = WORKTREE_BASE_DIR / worktree_id / repo_short
                        if candidate.exists():
                            worktree_repo_path = candidate
                            matched = True
                    if not matched:
                        # Fallback: take the first existing worktree path
                        for name in wt_result.worktrees:
                            candidate = WORKTREE_BASE_DIR / worktree_id / name
                            if candidate.exists():
                                worktree_repo_path = candidate
                                break

                    logger.info(
                        "Worktrees created for pipeline",
                        pipeline_id=pipeline_id,
                        worktrees=list(repo_volumes.keys()),
                    )
                else:
                    raise RuntimeError(
                        f"Worktree creation returned no worktrees for pipeline {pipeline_id}: "
                        f"errors={wt_result.errors}"
                    )

                if wt_result.errors:
                    for err in wt_result.errors:
                        logger.warning("Worktree error", pipeline_id=pipeline_id, error=err)

            except RuntimeError:
                raise  # Re-raise our own RuntimeError
            except Exception as wt_err:
                raise RuntimeError(
                    f"Failed to create worktrees for pipeline {pipeline_id}: {wt_err}"
                ) from wt_err

        if not repo_volumes:
            raise RuntimeError(
                f"No repo volumes available for pipeline {pipeline_id} — "
                f"worktree creation is required"
            )

        # Sync worktree with remote before starting pipeline phases.  After an
        # orchestrator restart, the local worktree branch may be behind origin:
        # commits pushed by agents in previous phases (contracts, drafts,
        # statefiles) exist on the remote but not in the local checkout.
        # Fetching and resetting ensures downstream code (contract loading,
        # draft reading) sees the full pipeline state from prior phases.
        if worktree_repo_path != repo_path:
            # Determine whether the most recent prior phase completed
            # successfully — this controls whether local-ahead commits are
            # pushed (success) or discarded (failure).
            prior_phase_succeeded = True
            current_phase = pipeline.current_phase
            phase_order = [
                PipelinePhase.REFINE,
                PipelinePhase.PLAN,
                PipelinePhase.IMPLEMENT,
                PipelinePhase.PR,
            ]
            current_idx = phase_order.index(current_phase) if current_phase in phase_order else 0
            if current_idx > 0:
                prior_phase = phase_order[current_idx - 1]
                prior_exec = pipeline.phases.get(prior_phase.value)
                if prior_exec and prior_exec.status in (
                    PipelineStatus.FAILED,
                    PipelineStatus.CANCELLED,
                ):
                    prior_phase_succeeded = False

            _sync_worktree_with_remote(
                spawner,
                pipeline_id,
                worktree_repo_path,
                prior_phase_succeeded=prior_phase_succeeded,
            )

        # Resolve the certs named volume for gateway CA trust.
        # The docker-compose stack creates ${COMPOSE_PROJECT_NAME:-egg}-certs.
        certs_volume_raw = os.environ.get(
            "EGG_CERTS_VOLUME",
            os.environ.get("COMPOSE_PROJECT_NAME", "egg") + "-certs",
        )
        # Validate volume name: Docker allows [a-zA-Z0-9][a-zA-Z0-9_.-]*
        # We use a permissive check that rejects obvious shell metacharacters.
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", certs_volume_raw):
            logger.warning(
                "Invalid certs volume name, using default",
                raw_name=certs_volume_raw,
            )
            certs_volume = "egg-certs"
        else:
            certs_volume = certs_volume_raw

        # Create companion contract in the worktree (deferred from pipeline
        # creation so it doesn't pollute the main repo working directory).
        if not pipeline.contract_synced:
            try:
                from egg_contracts.loader import create_contract

                if pipeline.issue_number is not None:
                    issue_url = f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
                    create_contract(
                        issue_number=pipeline.issue_number,
                        title=f"Issue #{pipeline.issue_number}",
                        url=issue_url,
                        repo_root=worktree_repo_path,
                    )
                else:
                    create_contract(
                        pipeline_id=pipeline.id,
                        title=(pipeline.prompt or "")[:100],
                        repo_root=worktree_repo_path,
                    )
                pipeline.contract_synced = True

                # Commit all .egg-state/ files so they're on the feature branch
                issue_ref = (
                    f"issue #{pipeline.issue_number}"
                    if pipeline.issue_number is not None
                    else f"pipeline {pipeline_id}"
                )
                try:
                    _commit_statefiles_to_worktree(
                        worktree_repo_path,
                        f"Initialize SDLC contract for {issue_ref}",
                    )
                except subprocess.CalledProcessError as git_err:
                    logger.warning(
                        "Failed to commit statefiles to worktree (continuing)",
                        pipeline_id=pipeline_id,
                        error=str(git_err),
                    )

                # Push contract statefiles to remote so agents see them
                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                        )
                    except Exception as push_err:
                        logger.warning(
                            "Failed to push statefiles after contract init (continuing)",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.contract_synced = True
                    store.save_pipeline(pipeline, commit=False)
                logger.info(
                    "Pipeline contract created in worktree",
                    pipeline_id=pipeline_id,
                    mode=pipeline_mode,
                )
            except Exception as contract_err:
                logger.error(
                    "Failed to create contract in worktree",
                    pipeline_id=pipeline_id,
                    error=str(contract_err),
                )
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.error = f"Failed to create contract: {contract_err}"
                    store.save_pipeline(pipeline)
                return

        hitl_revision_feedback: str | None = None

        # Check for feedback preserved by the recovery path in start_pipeline.
        # When AWAITING_HUMAN recovery handles request_changes, it stores the
        # reviewer's feedback in phase_execution.hitl_feedback so the freshly
        # launched _run_pipeline thread can pass it to the re-running agent.
        try:
            with get_pipeline_state_lock(pipeline_id):
                _recovery_pipeline = store.load_pipeline(pipeline_id)
                _recovery_phase = _recovery_pipeline.get_phase_execution(
                    _recovery_pipeline.current_phase
                )
                if _recovery_phase.hitl_feedback:
                    hitl_revision_feedback = _recovery_phase.hitl_feedback
                    _recovery_phase.hitl_feedback = None
                    store.save_pipeline(_recovery_pipeline)
        except Exception:
            pass  # Non-fatal — feedback is best-effort

        while True:
            try:
                pipeline = store.load_pipeline(pipeline_id)
            except Exception:
                # Pipeline was deleted — exit quietly
                logger.info(
                    "Pipeline no longer exists, exiting thread",
                    pipeline_id=pipeline_id,
                )
                return

            # Detect recreation: another run now owns this pipeline ID
            if pipeline.created_at != run_created_at:
                logger.info(
                    "Pipeline was recreated, exiting old thread",
                    pipeline_id=pipeline_id,
                )
                return

            if pipeline.status in (PipelineStatus.FAILED, PipelineStatus.CANCELLED):
                logger.info(
                    "Pipeline stopped", pipeline_id=pipeline_id, status=pipeline.status.value
                )
                break

            current_phase = pipeline.current_phase

            # Start the current phase
            phase_execution = pipeline.get_phase_execution(current_phase)
            if phase_execution.status == PipelineStatus.PENDING:
                # Record branch tip SHA for completion signal verification.
                # This allows the completion handler to detect if a commit
                # was pushed to a different branch than expected.
                # NOTE: Intentional TOCTOU — the SHA is captured before
                # acquiring the state lock, so a push between rev-parse and
                # lock acquisition could make it stale.  Acceptable because
                # phase_start_sha is only used for advisory "no new commits"
                # logging, not for correctness decisions.
                phase_start_sha: str | None = None
                try:
                    _sha_result = subprocess.run(
                        ["git", "rev-parse", f"origin/{pipeline.branch}"],
                        capture_output=True,
                        text=True,
                        cwd=str(worktree_repo_path),
                        timeout=10,
                        check=False,
                    )
                    if _sha_result.returncode == 0:
                        phase_start_sha = _sha_result.stdout.strip()
                except Exception:
                    pass  # Non-fatal — verification is best-effort

                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.RUNNING
                    phase_execution.started_at = datetime.utcnow()
                    phase_execution.phase_start_sha = phase_start_sha
                    pipeline.status = PipelineStatus.RUNNING
                    store.save_pipeline(pipeline)

                # Report phase start to collaborator
                report_pipeline_status(
                    pipeline,
                    event_type="phase.started",
                    message=f"Phase {current_phase.value} started",
                )
                _emit_pipeline_event(pipeline, "phase.started")

            # Common sandbox environment for all containers in this phase.
            # GATEWAY_URL, RUNTIME_UID/GID, proxy vars, DNS lockdown, and
            # extra_hosts are now handled by the shared build_sandbox_config()
            # inside spawn_agent_container().  Only pipeline-specific vars go here.
            if gateway_mode == "private":
                orchestrator_ip = ORCHESTRATOR_ISOLATED_IP
            else:
                orchestrator_ip = ORCHESTRATOR_EXTERNAL_IP
            orchestrator_url = f"http://{orchestrator_ip}:{ORCHESTRATOR_PORT}"
            sandbox_env: dict[str, str] = {
                "EGG_PIPELINE_ID": pipeline_id,
                "EGG_PIPELINE_PHASE": current_phase.value,
                "EGG_PIPELINE_MODE": pipeline_mode,
                "EGG_ORCHESTRATOR_URL": orchestrator_url,
                "EGG_ORCHESTRATOR_MODE": "distributed",
            }
            if pipeline.branch:
                sandbox_env["EGG_BRANCH"] = pipeline.branch
            if pipeline.prompt:
                sandbox_env["EGG_PIPELINE_PROMPT"] = pipeline.prompt

            if pipeline.repo:
                repos = [pipeline.repo]
                sandbox_env["EGG_REPO"] = pipeline.repo
            else:
                repos = []

            phase_failed = False
            review_feedback: str | None = hitl_revision_feedback
            hitl_revision_feedback = None
            tester_gap_summary: str | None = None

            # --- Auto PR creation: skip agent spawn for PR phase ---
            if current_phase.value == "pr":
                logger.info(
                    "Auto-creating PR (skipping agent spawn)",
                    pipeline_id=pipeline_id,
                )

                # Record phase timing so metrics are accurate even without agent spawn
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.work_started_at = datetime.utcnow()
                    store.save_pipeline(pipeline)

                # Push latest commits before creating PR
                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                        )
                    except Exception as push_err:
                        logger.error(
                            "Pre-PR push failed — PR may reference stale code",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

                pr_url = _auto_create_pr(pipeline, worktree_repo_path, spawner)

                if pr_url:
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        phase_execution.artifacts = {"pr_url": pr_url}
                        store.save_pipeline(pipeline)
                else:
                    logger.warning(
                        "Auto PR creation returned no URL (PR may still have been created)",
                        pipeline_id=pipeline_id,
                    )

                # Fall through to phase completion below (skip inner review cycle)

            # --- Inner review cycle (skipped when auto-creating PR) ---
            else:
                while True:
                    # Reset tester gaps each cycle so stale findings don't accumulate
                    tester_gap_summary = None

                    # Reload to get latest review_cycles count
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        review_cycle = phase_execution.review_cycles

                        # Record when actual agent work begins (excludes sandbox setup
                        # and HITL waiting time from the phase duration).
                        phase_execution.work_started_at = datetime.utcnow()

                        # Capture HEAD commit for delta reviews: reviewers in
                        # subsequent cycles can diff against this to see only
                        # the changes made since the last review.
                        cycle_commit_sha: str | None = None
                        try:
                            _git_result = subprocess.run(
                                ["git", "rev-parse", "HEAD"],
                                capture_output=True,
                                text=True,
                                cwd=str(worktree_repo_path),
                                timeout=10,
                            )
                            if _git_result.returncode == 0:
                                cycle_commit_sha = _git_result.stdout.strip()
                        except Exception:
                            pass  # Non-fatal — delta review is best-effort

                        phase_execution.cycle_timings.append(
                            CycleTiming(
                                cycle=review_cycle,
                                started_at=phase_execution.work_started_at,
                                commit_sha=cycle_commit_sha,
                            )
                        )
                        store.save_pipeline(pipeline)

                    # 1. Spawn worker(s)
                    # Use multi-agent wave-based execution when enabled for
                    # implement and plan phases; single-CODER path otherwise.
                    # Tier 3 (high complexity) uses phase-level dispatch for implement.
                    # Coordinator mode delegates all dispatch to the coordinator agent.
                    use_coordinator = pipeline.config.coordinator_enabled

                    use_multi_agent = pipeline.config.multi_agent and current_phase.value in {
                        "implement",
                        "plan",
                    }
                    use_tier3 = (
                        current_phase.value == "implement"
                        and pipeline.complexity_tier == ComplexityTier.HIGH
                        and pipeline.config.multi_agent
                    )

                    try:
                        from multi_agent import is_concurrent_execution
                    except ImportError:
                        from ..multi_agent import is_concurrent_execution  # type: ignore[no-redef]

                    use_concurrent = is_concurrent_execution(pipeline) and current_phase.value in {
                        "implement"
                    }

                    if use_coordinator:
                        logger.info(
                            "Routing to coordinator executor",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            review_cycle=review_cycle,
                            mode=gateway_mode,
                        )

                        try:
                            from coordinator_executor import CoordinatorExecutor
                        except ImportError:
                            from ..coordinator_executor import (
                                CoordinatorExecutor,  # type: ignore[no-redef]
                            )

                        coord_executor = CoordinatorExecutor(repo_path=worktree_repo_path)
                        coord_executor.init_coordinator_state(pipeline_id)

                        coordinator_env = {
                            **sandbox_env,
                            "EGG_COORDINATOR_MODE": "true",
                            "EGG_COORDINATOR_TOOLS": "true",
                        }

                        coordinator_prompt = _build_agent_prompt(
                            role_value="coordinator",
                            phase=current_phase.value,
                            pipeline_id=pipeline_id,
                            pipeline_mode=pipeline_mode,
                            prompt=pipeline.prompt,
                            issue_number=pipeline.issue_number,
                            repo=pipeline.repo,
                            branch=pipeline.branch,
                            review_cycle=review_cycle,
                        )
                        coordinator_command = build_agent_command(coordinator_prompt)

                        try:
                            from egg_container import MountSpec

                            # Coordinator is a pure orchestrator — it should not read or
                            # modify repository files. Empty repo_volumes + tmpfs over
                            # ~/repos enforces this: the coordinator can only interact
                            # via egg-orch CLI commands, not the filesystem.
                            exit_code, container_logs = _spawn_and_wait(
                                spawner=spawner,
                                pipeline_id=pipeline_id,
                                agent_role=AgentRole.COORDINATOR,
                                issue_number=pipeline.issue_number,
                                repo_volumes={},
                                gateway_mode=gateway_mode,
                                repos=repos,
                                phase=current_phase,
                                sandbox_env=coordinator_env,
                                sandbox_command=coordinator_command,
                                store=store,
                                certs_volume=certs_volume,
                                branch=pipeline.branch,
                                extra_mounts=[
                                    MountSpec(
                                        mount_type="tmpfs",
                                        source=None,
                                        destination="/home/egg/repos",
                                    )
                                ],
                            )

                            # Handle coordinator completion (crash recovery, etc.)
                            result = coord_executor.handle_coordinator_completion(
                                pipeline_id, exit_code
                            )
                            if result == "respawn":
                                # Coordinator will be respawned — retry this phase
                                continue
                            elif result == "failed":
                                phase_failed = True
                                break
                            else:
                                # Coordinator completed successfully — skip generic dispatch
                                break

                        except ContainerSpawnError as e:
                            with get_pipeline_state_lock(pipeline_id):
                                pipeline = store.load_pipeline(pipeline_id)
                                phase_execution = pipeline.get_phase_execution(current_phase)
                                if phase_execution.cycle_timings:
                                    phase_execution.cycle_timings[
                                        -1
                                    ].completed_at = datetime.utcnow()
                                phase_execution.status = PipelineStatus.FAILED
                                phase_execution.error = str(e)
                                phase_execution.completed_at = datetime.utcnow()
                                pipeline.status = PipelineStatus.FAILED
                                pipeline.error = str(e)
                                store.save_pipeline(pipeline)
                            logger.error(
                                "Failed to spawn coordinator",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )
                            phase_failed = True
                            break

                    elif use_concurrent:
                        logger.info(
                            "Spawning concurrent phase execution",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            review_cycle=review_cycle,
                            mode=gateway_mode,
                        )

                        try:
                            exit_code, container_logs = _run_concurrent_phase(
                                pipeline_id=pipeline_id,
                                pipeline=pipeline,
                                phase=current_phase,
                                spawner=spawner,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                sandbox_env=sandbox_env,
                                store=store,
                                certs_volume=certs_volume,
                                worktree_repo_path=worktree_repo_path,
                            )
                        except ContainerSpawnError as e:
                            with get_pipeline_state_lock(pipeline_id):
                                pipeline = store.load_pipeline(pipeline_id)
                                phase_execution = pipeline.get_phase_execution(current_phase)
                                if phase_execution.cycle_timings:
                                    phase_execution.cycle_timings[
                                        -1
                                    ].completed_at = datetime.utcnow()
                                phase_execution.status = PipelineStatus.FAILED
                                phase_execution.error = str(e)
                                phase_execution.completed_at = datetime.utcnow()
                                pipeline.status = PipelineStatus.FAILED
                                pipeline.error = str(e)
                                store.save_pipeline(pipeline)
                            logger.error(
                                "Failed to spawn concurrent containers",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )
                            phase_failed = True
                            break

                    elif use_tier3:
                        logger.info(
                            "Spawning Tier 3 phase-level dispatch for implement",
                            pipeline_id=pipeline_id,
                            review_cycle=review_cycle,
                            mode=gateway_mode,
                        )

                        try:
                            exit_code, container_logs = _run_tier3_implement(
                                pipeline_id=pipeline_id,
                                pipeline=pipeline,
                                spawner=spawner,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                sandbox_env=sandbox_env,
                                store=store,
                                certs_volume=certs_volume,
                                worktree_repo_path=worktree_repo_path,
                            )
                        except ContainerSpawnError as e:
                            with get_pipeline_state_lock(pipeline_id):
                                pipeline = store.load_pipeline(pipeline_id)
                                phase_execution = pipeline.get_phase_execution(current_phase)
                                if phase_execution.cycle_timings:
                                    phase_execution.cycle_timings[
                                        -1
                                    ].completed_at = datetime.utcnow()
                                phase_execution.status = PipelineStatus.FAILED
                                phase_execution.error = str(e)
                                phase_execution.completed_at = datetime.utcnow()
                                pipeline.status = PipelineStatus.FAILED
                                pipeline.error = str(e)
                                store.save_pipeline(pipeline)
                            logger.error(
                                "Failed to spawn Tier 3 containers",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )
                            phase_failed = True
                            break

                    elif use_multi_agent:
                        logger.info(
                            "Spawning multi-agent wave execution for phase",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            review_cycle=review_cycle,
                            mode=gateway_mode,
                        )

                        try:
                            exit_code, container_logs = _run_multi_agent_phase(
                                pipeline_id=pipeline_id,
                                pipeline=pipeline,
                                phase=current_phase,
                                spawner=spawner,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                sandbox_env=sandbox_env,
                                store=store,
                                certs_volume=certs_volume,
                                worktree_repo_path=worktree_repo_path,
                                review_feedback=review_feedback,
                                review_cycle=review_cycle,
                            )
                        except ContainerSpawnError as e:
                            with get_pipeline_state_lock(pipeline_id):
                                pipeline = store.load_pipeline(pipeline_id)
                                phase_execution = pipeline.get_phase_execution(current_phase)
                                if phase_execution.cycle_timings:
                                    phase_execution.cycle_timings[
                                        -1
                                    ].completed_at = datetime.utcnow()
                                phase_execution.status = PipelineStatus.FAILED
                                phase_execution.error = str(e)
                                phase_execution.completed_at = datetime.utcnow()
                                pipeline.status = PipelineStatus.FAILED
                                pipeline.error = str(e)
                                store.save_pipeline(pipeline)
                            logger.error(
                                "Failed to spawn multi-agent containers",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )
                            phase_failed = True
                            break

                    else:
                        logger.info(
                            "Spawning worker for phase",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            review_cycle=review_cycle,
                            mode=gateway_mode,
                        )

                        phase_prompt = _build_phase_prompt(
                            phase=current_phase,
                            pipeline_id=pipeline_id,
                            pipeline_mode=pipeline_mode,
                            prompt=pipeline.prompt,
                            issue_number=pipeline.issue_number,
                            repo=pipeline.repo,
                            branch=pipeline.branch,
                            review_feedback=review_feedback,
                            review_cycle=review_cycle,
                            short_circuit=pipeline.short_circuit,
                            repo_path=str(worktree_repo_path),
                        )

                        sandbox_command = build_agent_command(phase_prompt)

                        # Use the REFINER role for the refine phase,
                        # CODER for all other single-agent phases.
                        single_agent_role = (
                            AgentRole.REFINER
                            if current_phase.value == "refine"
                            else AgentRole.CODER
                        )

                        try:
                            exit_code, container_logs = _spawn_and_wait(
                                spawner=spawner,
                                pipeline_id=pipeline_id,
                                agent_role=single_agent_role,
                                issue_number=pipeline.issue_number,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                phase=current_phase,
                                sandbox_env=sandbox_env,
                                sandbox_command=sandbox_command,
                                store=store,
                                certs_volume=certs_volume,
                                branch=pipeline.branch,
                            )
                        except ContainerSpawnError as e:
                            with get_pipeline_state_lock(pipeline_id):
                                pipeline = store.load_pipeline(pipeline_id)
                                phase_execution = pipeline.get_phase_execution(current_phase)
                                if phase_execution.cycle_timings:
                                    phase_execution.cycle_timings[
                                        -1
                                    ].completed_at = datetime.utcnow()
                                phase_execution.status = PipelineStatus.FAILED
                                phase_execution.error = str(e)
                                phase_execution.completed_at = datetime.utcnow()
                                pipeline.status = PipelineStatus.FAILED
                                pipeline.error = str(e)
                                store.save_pipeline(pipeline)
                            logger.error(
                                "Failed to spawn container", pipeline_id=pipeline_id, error=str(e)
                            )
                            phase_failed = True
                            break

                    if exit_code != 0:
                        error_msg = f"Container exited with code {exit_code}"
                        if container_logs:
                            log_lines = container_logs.strip().splitlines()
                            tail = "\n".join(log_lines[-10:])
                            error_msg += f"\n--- container logs (last 10 lines) ---\n{tail}"

                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            if phase_execution.cycle_timings:
                                phase_execution.cycle_timings[-1].completed_at = datetime.utcnow()
                            phase_execution.status = PipelineStatus.FAILED
                            phase_execution.error = error_msg
                            phase_execution.completed_at = datetime.utcnow()
                            pipeline.status = PipelineStatus.FAILED
                            pipeline.error = error_msg
                            store.save_pipeline(pipeline)
                        logger.error(
                            "Phase failed",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            exit_code=exit_code,
                            container_logs=container_logs[-2000:] if container_logs else "",
                        )
                        phase_failed = True
                        break

                    # 2. Combined check-and-fix (implement phase only)
                    # Tier 3 already runs checker per-phase, so skip here.
                    if current_phase.value == "implement" and not use_tier3:
                        # Look up configured check commands for this repo
                        repo_checks: list[dict] | None = None
                        if pipeline.repo:
                            try:
                                all_repo_checks = json.loads(
                                    os.environ.get("EGG_REPO_CHECKS", "{}")
                                )
                            except json.JSONDecodeError:
                                all_repo_checks = {}
                            # Case-insensitive lookup
                            repo_lower = pipeline.repo.lower()
                            for cfg_repo, cfg_checks in all_repo_checks.items():
                                if cfg_repo.lower() == repo_lower:
                                    if isinstance(cfg_checks, list):
                                        repo_checks = validate_checks(cfg_checks) or None
                                    break

                        logger.info(
                            "Spawning combined checker+autofixer",
                            pipeline_id=pipeline_id,
                        )

                        check_fix_prompt = _build_check_and_fix_prompt(
                            pipeline_id,
                            pipeline_mode,
                            repo=pipeline.repo,
                            repo_checks=repo_checks,
                            repo_path=str(worktree_repo_path),
                            issue_number=pipeline.issue_number,
                        )
                        check_fix_command = build_agent_command(check_fix_prompt, max_turns=100)
                        checker_env = {**sandbox_env, "EGG_AGENT_ROLE": "checker"}

                        try:
                            # 45 min: combined check+fix budget, up from
                            # 30 min for the old check-only container.
                            exit_code, _ = _spawn_and_wait(
                                spawner=spawner,
                                pipeline_id=pipeline_id,
                                agent_role=AgentRole.CHECKER,
                                issue_number=pipeline.issue_number,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                phase=current_phase,
                                sandbox_env=checker_env,
                                sandbox_command=check_fix_command,
                                timeout=2700,
                                store=store,
                                certs_volume=certs_volume,
                                branch=pipeline.branch,
                            )
                            if exit_code != 0:
                                logger.warning(
                                    "Checker+autofixer exited non-zero",
                                    pipeline_id=pipeline_id,
                                    exit_code=exit_code,
                                )
                        except ContainerSpawnError as e:
                            logger.warning(
                                "Checker+autofixer failed to spawn, skipping checks",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )

                    # 2.5 Read tester gap findings (multi-agent phases include a tester).
                    # Only read when the phase succeeded — a failed phase may
                    # have left stale output from a previous cycle on disk.
                    if use_multi_agent and not phase_failed:
                        tester_gap_summary = _read_tester_gaps(
                            worktree_repo_path,
                            identifier=_pipeline_identifier(pipeline.issue_number, pipeline_id),
                        )
                        if tester_gap_summary:
                            logger.info(
                                "Tester found gaps",
                                pipeline_id=pipeline_id,
                                phase=current_phase,
                            )

                    # 3. Spawn reviewers and read verdicts (reviewed phases)
                    # Reviewers always run as a separate step after workers +
                    # checker, for both multi-agent and single-agent paths.
                    # For Tier 3, reviewer_code already ran per-phase inside
                    # _run_tier3_implement(), so only run reviewer_contract here
                    # to give it a full-pipeline retry loop.
                    from egg_contracts.agent_roles import (
                        _PHASE_REVIEWERS as _phase_reviewer_roles,
                    )

                    reviewer_roles = _phase_reviewer_roles.get(current_phase.value, [])
                    if use_tier3:
                        reviewer_roles = [r for r in reviewer_roles if r != AgentRole.REVIEWER_CODE]
                    if not reviewer_roles:
                        break  # No reviewers for this phase — advance

                    # Clean stale verdict files
                    for role in reviewer_roles:
                        rtype = role.value.replace("reviewer_", "", 1).replace("_", "-")
                        verdict_rel = _verdict_path_for_type(
                            current_phase.value,
                            rtype,
                            issue_number=pipeline.issue_number,
                            pipeline_id=pipeline_id,
                        )
                        verdict_path = worktree_repo_path / verdict_rel
                        if verdict_path.exists():
                            try:
                                verdict_path.unlink()
                            except OSError:
                                pass

                    # Determine last_reviewed_commit for delta reviews.
                    # If this is a re-review (cycle > 0), use the commit_sha
                    # from the current cycle's start as the baseline.
                    # Note: review_cycle is 0-indexed here; _build_review_prompt
                    # receives _review_cycle + 1 (1-indexed), so cycle > 0 here
                    # corresponds to review_cycle > 1 in the prompt builder.
                    _last_reviewed_commit: str | None = None
                    if review_cycle > 0 and phase_execution.cycle_timings:
                        current_timing = phase_execution.cycle_timings[-1]
                        _last_reviewed_commit = current_timing.commit_sha

                    # Spawn reviewers in parallel (up to max_parallel_agents)
                    from concurrent.futures import ThreadPoolExecutor

                    def _spawn_reviewer(  # type: ignore[no-untyped-def]
                        role,
                        *,
                        _phase=current_phase,
                        _pipeline=pipeline,
                        _review_cycle=review_cycle,
                        _review_feedback=review_feedback,
                        _sandbox_env=sandbox_env,
                        _repos=repos,
                        _repo_path=worktree_repo_path,
                        _last_commit=_last_reviewed_commit,
                    ):
                        role_str = role.value
                        rtype = role_str.replace("reviewer_", "", 1).replace("_", "-")
                        reviewer_prompt = _build_review_prompt(
                            phase=_phase.value,
                            pipeline_id=pipeline_id,
                            pipeline_mode=pipeline_mode,
                            reviewer_type=rtype,
                            issue_number=_pipeline.issue_number,
                            review_cycle=_review_cycle + 1,
                            prior_feedback=_review_feedback,
                            repo_path=str(_repo_path),
                            last_reviewed_commit=_last_commit,
                        )
                        reviewer_command = build_agent_command(reviewer_prompt, max_turns=50)
                        reviewer_env = {
                            **_sandbox_env,
                            "EGG_AGENT_ROLE": role_str,
                        }
                        try:
                            orch_role = AgentRole(role_str)
                        except ValueError:
                            return
                        try:
                            _spawn_and_wait(
                                spawner=spawner,
                                pipeline_id=pipeline_id,
                                agent_role=orch_role,
                                issue_number=_pipeline.issue_number,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=_repos,
                                phase=_phase.value,
                                sandbox_env=reviewer_env,
                                sandbox_command=reviewer_command,
                                timeout=1800,
                                store=store,
                                certs_volume=certs_volume,
                                branch=_pipeline.branch,
                            )
                        except Exception as e:
                            logger.warning(
                                "Reviewer failed, skipping",
                                pipeline_id=pipeline_id,
                                reviewer=role_str,
                                error=str(e),
                            )

                    max_workers = min(
                        len(reviewer_roles),
                        pipeline.config.max_parallel_agents,
                    )
                    with ThreadPoolExecutor(max_workers=max_workers) as rev_executor:
                        futures = [
                            rev_executor.submit(_spawn_reviewer, role) for role in reviewer_roles
                        ]
                        for future in futures:
                            future.result()

                    all_verdicts: dict[str, ReviewVerdict | None] = {}
                    for role in reviewer_roles:
                        rtype = role.value.replace("reviewer_", "", 1).replace("_", "-")
                        all_verdicts[rtype] = _read_review_verdict(
                            worktree_repo_path,
                            current_phase.value,
                            reviewer_type=rtype,
                            pipeline_mode=pipeline_mode,
                            issue_number=pipeline.issue_number,
                            pipeline_id=pipeline_id,
                        )

                    agg_result = _aggregate_review_verdicts(all_verdicts)

                    if agg_result.advisory_content:
                        logger.info(
                            "Review advisory content (non-blocking)",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            advisory_preview=agg_result.advisory_content[:500],
                        )

                    if agg_result.verdict == "approved":
                        logger.info(
                            "All reviewers approved",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            review_cycle=review_cycle + 1,
                        )
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            if phase_execution.cycle_timings:
                                phase_execution.cycle_timings[-1].completed_at = datetime.utcnow()
                                store.save_pipeline(pipeline)
                        break  # Advance to next phase

                    # needs_revision — check circuit breaker
                    max_cycles = pipeline.config.max_review_cycles
                    if review_cycle + 1 >= max_cycles:
                        logger.warning(
                            "Review circuit breaker — advancing despite needs_revision",
                            pipeline_id=pipeline_id,
                            phase=current_phase,
                            review_cycles=review_cycle + 1,
                            max_review_cycles=max_cycles,
                        )
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            if phase_execution.cycle_timings:
                                phase_execution.cycle_timings[-1].completed_at = datetime.utcnow()
                                store.save_pipeline(pipeline)
                        break

                    # Store feedback and loop — merge tester gaps with reviewer
                    # feedback so the coder sees both on the next cycle.
                    if tester_gap_summary and agg_result.blocking_feedback:
                        review_feedback = f"{agg_result.blocking_feedback}\n\n{tester_gap_summary}"
                    elif tester_gap_summary:
                        review_feedback = tester_gap_summary
                    else:
                        review_feedback = agg_result.blocking_feedback
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        if phase_execution.cycle_timings:
                            phase_execution.cycle_timings[-1].completed_at = datetime.utcnow()
                        phase_execution.review_cycles = review_cycle + 1
                        store.save_pipeline(pipeline)

                    logger.info(
                        "Review needs revision — looping",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        review_cycle=review_cycle + 1,
                        feedback_preview=review_feedback[:200] if review_feedback else "",
                    )
                    continue  # Re-run while loop with feedback

            # If the phase failed, emit the failure event so the SSE stream
            # terminates, then break out of the outer loop.
            if phase_failed:
                # report_pipeline_status is a stub (no-op) unless status_reporter
                # is installed.  The actual SSE emission is _emit_pipeline_event
                # below.  Kept for consistency with the except block at the
                # bottom of this function.
                report_pipeline_status(
                    pipeline,
                    event_type="pipeline.failed",
                    message=f"Pipeline failed: {(pipeline.error or 'unknown')[:100]}",
                )
                _emit_pipeline_event(pipeline, "pipeline.failed")

                # Best-effort: push worktree branch to remote so work is backed up
                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                        )
                    except Exception as push_err:
                        logger.warning(
                            "Best-effort push on failure failed",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

                break

            # Phase succeeded — mark complete and advance
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = PipelineStatus.COMPLETE
                phase_execution.completed_at = datetime.utcnow()

                # Check for short-circuit signal after refine phase.
                # Reset first so a HITL revision that removes the signal
                # correctly clears a previously-detected short-circuit.
                if current_phase.value == "refine":
                    # Detect complexity tier from refine analysis.
                    # Reset parallel flag first so a HITL revision that
                    # downgrades complexity correctly clears a previously-
                    # detected parallel_phases signal.
                    pipeline.config.enable_parallel_phases = False
                    tier, parallel_phases = _check_high_complexity_signal(
                        worktree_repo_path,
                        pipeline_mode,
                        pipeline.issue_number,
                        pipeline_id,
                    )
                    pipeline.complexity_tier = ComplexityTier(tier)
                    if parallel_phases:
                        pipeline.config.enable_parallel_phases = True
                    logger.info(
                        "Complexity tier detected",
                        pipeline_id=pipeline_id,
                        tier=tier,
                        parallel_phases=parallel_phases,
                    )

                    # Check for short-circuit signal (Tier 1 / low complexity)
                    if pipeline.config.allow_short_circuit:
                        pipeline.short_circuit = False
                        if _check_short_circuit_signal(
                            worktree_repo_path,
                            pipeline_mode,
                            pipeline.issue_number,
                            pipeline_id,
                        ):
                            pipeline.short_circuit = True
                            pipeline.complexity_tier = ComplexityTier.LOW
                            logger.info("Short-circuit detected", pipeline_id=pipeline_id)

                store.save_pipeline(pipeline)  # Persist phase completion before HITL gate

            # Report phase completion to collaborator
            report_pipeline_status(
                pipeline,
                event_type="phase.completed",
                message=f"Phase {current_phase.value} completed",
            )
            _emit_pipeline_event(pipeline, "phase.completed")

            # After plan phase: populate contract with task structure.
            # NOTE: In short-circuit mode the plan phase is skipped, so the
            # contract will have no task structure.  This is intentional —
            # low-complexity tasks go straight to implement with only the
            # refine analysis as guidance.  The implement agent does not
            # require a populated contract to function.
            # NOTE: worktree_repo_path is used for both draft reads and
            # contract load/save inside _populate_contract_from_plan.
            # The contract was created at worktree_repo_path above, so
            # both operations must use the same path.
            # Called on every successful plan completion (including after
            # HITL revision) so the contract reflects the latest approved
            # plan, not a previously rejected draft.
            if current_phase.value == "plan":
                _populate_contract_from_plan(
                    worktree_repo_path, pipeline_id, pipeline_mode, pipeline.issue_number
                )

            # After refine and plan phases: sync substantive HITL decisions
            # (non-phase-gate) to the contract so implement-phase agents
            # can see what was decided.  Called for both refine and plan
            # phases — refine decisions inform the plan, plan decisions
            # inform the implementation.
            if current_phase.value in _HITL_GATE_PHASES:
                _sync_pipeline_decisions_to_contract(
                    worktree_repo_path, pipeline_id, pipeline_mode, pipeline.issue_number
                )

            # Commit any .egg-state/ files produced during this phase
            # (drafts, reviews, check results, contract updates).  Mirrors
            # the GHA workflow's `git add .egg-state/` at phase boundaries.
            try:
                _commit_statefiles_to_worktree(
                    worktree_repo_path,
                    f"Persist statefiles after {current_phase.value} phase",
                )
            except subprocess.CalledProcessError as git_err:
                logger.warning(
                    "Failed to commit statefiles after phase (continuing)",
                    pipeline_id=pipeline_id,
                    phase=current_phase,
                    error=str(git_err),
                )

            # Push statefiles to remote so the next phase's agents
            # don't have unpushed .egg-state/ files in their diff.
            if pipeline.branch and worktree_repo_path != repo_path:
                try:
                    spawner.gateway.push_worktree_branch(
                        pipeline_id=pipeline_id,
                        repo_path=str(worktree_repo_path),
                        branch=pipeline.branch,
                    )
                except Exception as push_err:
                    logger.warning(
                        "Failed to push statefiles after phase (continuing)",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        error=str(push_err),
                    )

            # --- HITL gate: pause for human approval ---
            if pipeline.config.hitl_gates and current_phase.value in _HITL_GATE_PHASES:
                draft_content = _read_phase_draft(
                    worktree_repo_path,
                    current_phase.value,
                    issue_number=pipeline.issue_number,
                    pipeline_id=pipeline_id,
                )
                phase_label = "analysis" if current_phase.value == "refine" else current_phase.value

                # Warn if draft is missing — the agent may not have written
                # it to the expected path.  See #1016.
                if draft_content is None:
                    logger.warning(
                        "HITL gate: draft not found on work branch",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        worktree_path=str(worktree_repo_path),
                    )
                    draft_content = (
                        f"**Warning**: No {phase_label} draft was found on the "
                        f"work branch. The agent may not have written the output "
                        f"to the expected path."
                    )

                question = (
                    f"The {current_phase.value} phase has completed. "
                    f"Please review the {phase_label} and approve to continue, "
                    f"or provide feedback to request changes."
                )

                dq = get_decision_queue(pipeline_id, repo_path)
                decision = dq.queue_decision(
                    question=question,
                    context=draft_content,
                    options=["approve", "request changes"],
                    decision_type="phase_gate",
                    phase=current_phase,
                )

                # Reload pipeline to pick up the decision persisted by queue_decision(),
                # otherwise the stale local object overwrites it with an empty decisions list.
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.AWAITING_HUMAN
                    # Also mark the phase as awaiting human so the DAG visualization
                    # shows the HITL gate on the correct phase box.
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.AWAITING_HUMAN
                    store.save_pipeline(pipeline)

                # Report HITL gate to collaborator
                report_pipeline_status(
                    pipeline,
                    event_type="decision.created",
                    message=f"Awaiting human approval for {current_phase.value} phase",
                )
                _emit_pipeline_event(pipeline, "decision.created")

                dq.wait_for_decision(decision.id)

                # Check resolution — did the human approve or request changes?
                resolved_decision = dq.get_decision(decision.id)
                resolution = (resolved_decision.resolution or "").strip()

                # JSON-first resolution parsing: try structured payload before
                # falling back to keyword matching for legacy bare-string resolutions.
                _is_approved = False
                _needs_revision = False
                _revision_feedback: str | None = None

                try:
                    payload = json.loads(resolution)
                    if isinstance(payload, dict) and "action" in payload:
                        action = payload["action"]
                        feedback_text = payload.get("feedback", "")

                        if action == "approve":
                            _is_approved = True
                        elif action == "select":
                            # Selection from a choice menu — treat as approval
                            _is_approved = True
                        elif action == "submit_feedback":
                            # Feedback submission — treat as approval (info collected)
                            _is_approved = True
                        elif action in ("request_changes", "change_approach"):
                            if feedback_text:
                                # R-1: Extract readable feedback, not raw JSON
                                _needs_revision = True
                                _revision_feedback = feedback_text
                            else:
                                # JSON request_changes without feedback — same as bare label
                                _needs_revision = True
                                _revision_feedback = None
                        else:
                            # Unknown action — fall through to legacy matching
                            raise json.JSONDecodeError("unknown action", resolution, 0)
                    else:
                        # Valid JSON but no action field — fall through to legacy
                        raise json.JSONDecodeError("no action field", resolution, 0)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    # Legacy bare-string resolution — existing keyword matching
                    if resolution.lower() in _APPROVE_KEYWORDS:
                        _is_approved = True
                    elif resolution.lower() in _BARE_OPTION_LABELS:
                        # Bare "request changes" without feedback
                        _needs_revision = True
                        _revision_feedback = None
                    elif resolution:
                        # Free-text feedback
                        _needs_revision = True
                        _revision_feedback = resolution

                if _needs_revision and _revision_feedback is None:
                    # Bare request without actionable feedback — ask for specifics.
                    # This handles both legacy "request changes" and JSON
                    # {"action":"request_changes"} without feedback text.
                    logger.info(
                        "HITL gate: bare option label without feedback, requesting specifics",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        resolution=resolution,
                    )
                    # Extract a human-friendly label from the resolution for the
                    # follow-up prompt (avoid displaying raw JSON to the user).
                    try:
                        _parsed = json.loads(resolution)
                        display_resolution = (
                            _parsed.get("action", resolution).replace("_", " ")
                            if isinstance(_parsed, dict)
                            else resolution
                        )
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        display_resolution = resolution
                    followup = dq.queue_decision(
                        question=(
                            f'You selected "{display_resolution}" but didn\'t provide specific feedback. '
                            f"Please describe what changes you'd like to see in the {phase_label}, "
                            f"or approve to continue."
                        ),
                        context=draft_content,
                        options=["approve"],
                        decision_type="phase_gate",
                        phase=current_phase,
                    )
                    dq.wait_for_decision(followup.id)
                    resolved_followup = dq.get_decision(followup.id)
                    followup_resolution = (resolved_followup.resolution or "").strip()

                    # Parse follow-up resolution (also JSON-first)
                    try:
                        fp = json.loads(followup_resolution)
                        if isinstance(fp, dict) and "action" in fp:
                            fa = fp["action"]
                            if fa == "approve":
                                _is_approved = True
                                _needs_revision = False
                            elif fa in ("request_changes", "change_approach"):
                                ft = fp.get("feedback", "")
                                if ft:
                                    _revision_feedback = ft
                                else:
                                    _is_approved = True
                                    _needs_revision = False
                            else:
                                raise json.JSONDecodeError("unknown", followup_resolution, 0)
                        else:
                            raise json.JSONDecodeError("no action", followup_resolution, 0)
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        if (
                            followup_resolution.lower() in _APPROVE_KEYWORDS
                            or followup_resolution.lower() in _BARE_OPTION_LABELS
                        ):
                            logger.info(
                                "HITL follow-up: no actionable feedback, treating as approval",
                                pipeline_id=pipeline_id,
                                phase=current_phase,
                            )
                            _is_approved = True
                            _needs_revision = False
                        elif followup_resolution:
                            _revision_feedback = followup_resolution

                if _needs_revision and _revision_feedback:
                    # Human provided feedback — re-run the phase with corrections
                    logger.info(
                        "HITL gate: changes requested, re-running phase",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        feedback_preview=_revision_feedback[:200],
                    )
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        pipeline.status = PipelineStatus.RUNNING
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        phase_execution.status = PipelineStatus.RUNNING
                        phase_execution.completed_at = None  # Reset — phase is re-running
                        phase_execution.hitl_review_cycles += 1

                        # Circuit breaker: don't allow unbounded HITL revision loops.
                        # Uses a dedicated counter so agentic review cycles don't
                        # consume the human's revision budget.
                        max_hitl_cycles = pipeline.config.max_hitl_review_cycles
                        if phase_execution.hitl_review_cycles >= max_hitl_cycles:
                            logger.warning(
                                "HITL revision circuit breaker — advancing despite feedback",
                                pipeline_id=pipeline_id,
                                phase=current_phase,
                                hitl_review_cycles=phase_execution.hitl_review_cycles,
                                max_hitl_review_cycles=max_hitl_cycles,
                            )
                            store.save_pipeline(pipeline)
                            # Fall through to the approval path below
                        else:
                            hitl_revision_feedback = _revision_feedback
                            store.save_pipeline(pipeline)
                            report_pipeline_status(
                                pipeline,
                                event_type="phase.revision_requested",
                                message=f"Human requested changes to {current_phase.value}",
                            )
                            _emit_pipeline_event(pipeline, "phase.revision_requested")
                            continue  # Re-enter outer loop → re-run phase with feedback

                # Approved — resume and advance
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.RUNNING
                    # Restore phase status to COMPLETE now that the HITL gate is cleared
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.COMPLETE
                    if phase_execution.completed_at is None:
                        phase_execution.completed_at = datetime.utcnow()
                    store.save_pipeline(pipeline)

            # Determine next phase
            next_phases = transitions.get(current_phase, [])

            # Short-circuit: skip PLAN phase, advance directly to IMPLEMENT.
            # The transition table in phases.py allows REFINE → IMPLEMENT for
            # the external validation API, but the internal runner uses this
            # manual override to select the next phase.  Both must stay in sync.
            skip_plan = pipeline.short_circuit and current_phase.value == "refine"
            if skip_plan:
                next_phases = [PipelinePhase.IMPLEMENT]
                logger.info("Skipping plan phase (short-circuit)", pipeline_id=pipeline_id)

            if not next_phases:
                # Terminal phase — pipeline complete
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.COMPLETE
                    store.save_pipeline(pipeline, force_commit=(pipeline.issue_number is None))

                # Report pipeline completion to collaborator
                report_pipeline_status(
                    pipeline,
                    event_type="pipeline.completed",
                    message="Pipeline completed successfully",
                )
                _emit_pipeline_event(pipeline, "pipeline.completed")
                logger.info("Pipeline complete", pipeline_id=pipeline_id)
                break

            # Advance to next phase
            next_phase = next_phases[0]
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.current_phase = next_phase
                # Mark plan phase as completed-but-skipped.  We use
                # PipelineStatus.COMPLETE (no SKIPPED status exists) and
                # record a note in the error field so dashboards/audits can
                # distinguish a skipped plan from one that actually ran.
                if skip_plan:
                    plan_execution = pipeline.get_phase_execution(PipelinePhase.PLAN)
                    plan_execution.status = PipelineStatus.COMPLETE
                    plan_execution.completed_at = datetime.utcnow()
                    plan_execution.error = "skipped: short-circuit"
                store.save_pipeline(pipeline, force_commit=(pipeline.issue_number is None))

            logger.info(
                "Phase advanced",
                pipeline_id=pipeline_id,
                from_phase=current_phase.value,
                to_phase=next_phase.value,
            )

    except PipelineNotFoundError:
        # Pipeline was deleted while execution was in progress — exit gracefully
        logger.info(
            "Pipeline was deleted during execution, exiting",
            pipeline_id=pipeline_id,
        )
    except Exception as e:
        logger.error(
            "Pipeline execution failed", pipeline_id=pipeline_id, error=str(e), exc_info=True
        )
        try:
            store = get_state_store(repo_path)
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)

                # Don't corrupt a recreated pipeline's state
                if run_created_at and pipeline.created_at != run_created_at:
                    logger.info(
                        "Pipeline was recreated, not marking new run as failed",
                        pipeline_id=pipeline_id,
                    )
                else:
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.error = str(e)
                    store.save_pipeline(pipeline)

                # Report pipeline failure to collaborator
                report_pipeline_status(
                    pipeline,
                    event_type="pipeline.failed",
                    message=f"Pipeline failed: {str(e)[:100]}",
                )
                _emit_pipeline_event(pipeline, "pipeline.failed")
        except Exception:
            pass
    finally:
        # Clean up pipeline-level worktrees unless the pipeline has been
        # recreated (delete + create with the same ID).  In that case the
        # new run owns the worktrees and we must not remove them.
        try:
            _spawner = get_container_spawner()
            _store = get_state_store(repo_path)
            skip_cleanup = False
            try:
                current = _store.load_pipeline(pipeline_id)
                if run_created_at and current.created_at != run_created_at:
                    skip_cleanup = True
                    logger.info(
                        "Pipeline was recreated, skipping worktree cleanup",
                        pipeline_id=pipeline_id,
                        old_created_at=run_created_at.isoformat(),
                        new_created_at=current.created_at.isoformat(),
                    )
                elif current.status == PipelineStatus.FAILED:
                    skip_cleanup = True
                    logger.info(
                        "Pipeline failed, preserving worktrees for retry",
                        pipeline_id=pipeline_id,
                    )
            except Exception:
                # Pipeline was deleted and not recreated — safe to clean up
                pass

            if not skip_cleanup:
                try:
                    _spawner.gateway.delete_worktrees(
                        container_id=pipeline_id,
                        force=True,
                    )
                    logger.info("Pipeline worktrees cleaned up", pipeline_id=pipeline_id)
                except Exception as pipeline_wt_err:
                    logger.warning(
                        "Failed to clean up pipeline worktrees",
                        pipeline_id=pipeline_id,
                        error=str(pipeline_wt_err),
                    )

                # Also clean up per-agent session worktrees.  Each agent
                # registers a gateway session under container_id
                # "egg-{pipeline_id}-{role}" and session_create creates a
                # worktree keyed to that name.  The per-agent cleanup path
                # calls delete_session_by_container with the Docker container
                # hash (not the session container_id), so those worktrees are
                # never removed via the normal per-container cleanup.  Sweep
                # them here as a safety net.  delete_worktrees is a no-op for
                # container IDs that have no worktree directory.
                for role in AgentRole:
                    agent_container_id = f"egg-{pipeline_id}-{role.value}"
                    try:
                        _spawner.gateway.delete_worktrees(
                            container_id=agent_container_id,
                            force=True,
                        )
                    except Exception as agent_wt_err:
                        logger.warning(
                            "Failed to clean up agent worktrees",
                            pipeline_id=pipeline_id,
                            agent_container_id=agent_container_id,
                            error=str(agent_wt_err),
                        )
        except Exception as wt_err:
            logger.warning(
                "Failed to clean up worktrees",
                pipeline_id=pipeline_id,
                error=str(wt_err),
            )


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
        store, pipeline = _resolve_pipeline(pipeline_id, repo_path)
        # Use the store's repo_path so _run_pipeline operates on the correct directory
        repo_path = store.repo_path

        if pipeline.status == PipelineStatus.RUNNING:
            return make_error_response(
                f"Pipeline {pipeline_id} is already running",
                status_code=409,
            )

        if pipeline.status == PipelineStatus.AWAITING_HUMAN:
            # No pending decisions — the polling thread died (e.g. restart)
            # but the human already resolved everything.  Recover based on
            # the latest phase_gate decision's resolution.
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)

                # Re-validate status after acquiring the lock — another
                # concurrent start_pipeline call may have already recovered
                # this pipeline.
                if pipeline.status != PipelineStatus.AWAITING_HUMAN:
                    return make_error_response(
                        f"Pipeline {pipeline_id} status changed to "
                        f"{pipeline.status.value} (concurrent recovery)",
                        status_code=409,
                    )

                pending = pipeline.get_pending_decisions()
                if len(pending) > 0:
                    return make_error_response(
                        f"Pipeline {pipeline_id} is awaiting human approval "
                        f"({len(pending)} pending decision(s))",
                        status_code=409,
                    )

                # Find the latest resolved phase_gate decision
                phase_gate_decisions = [
                    d
                    for d in reversed(pipeline.decisions)
                    if d.decision_type == "phase_gate" and d.status.value == "resolved"
                ]
                latest_resolution = (
                    phase_gate_decisions[0].resolution if phase_gate_decisions else None
                )

                # Determine if approved or request_changes using the shared
                # parser (handles approve, select, submit_feedback,
                # request_changes, change_approach, and legacy bare strings).
                is_approved, revision_feedback = _parse_resolution(latest_resolution)

                if is_approved:
                    # Mark current phase COMPLETE and advance
                    phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                    phase_execution.status = PipelineStatus.COMPLETE
                    if phase_execution.completed_at is None:
                        phase_execution.completed_at = datetime.utcnow()

                    from routes.phases import PHASE_TRANSITIONS

                    transitions = PHASE_TRANSITIONS
                    current_phase = pipeline.current_phase
                    next_phases = transitions.get(current_phase, [])

                    # Handle short-circuit: refine → implement (skip plan)
                    if pipeline.short_circuit and current_phase.value == "refine":
                        next_phases = [PipelinePhase.IMPLEMENT]

                    if not next_phases:
                        # Terminal phase — pipeline complete.
                        # Bump created_at so any lingering old _run_pipeline
                        # thread (e.g. stuck in its finally block) detects the
                        # recreation and exits without double-cleaning up.
                        pipeline.status = PipelineStatus.COMPLETE
                        pipeline.created_at = datetime.utcnow()
                        store.save_pipeline(pipeline)
                        return make_success_response(
                            "Pipeline recovered and completed",
                            data={
                                "pipeline_id": pipeline_id,
                                "status": "complete",
                                "current_phase": pipeline.current_phase.value,
                            },
                        )

                    # Advance to next phase
                    next_phase = next_phases[0]
                    pipeline.current_phase = next_phase

                    # Mark plan phase as skipped if short-circuit
                    if pipeline.short_circuit and current_phase.value == "refine":
                        plan_execution = pipeline.get_phase_execution(PipelinePhase.PLAN)
                        plan_execution.status = PipelineStatus.COMPLETE
                        plan_execution.completed_at = datetime.utcnow()
                        plan_execution.error = "skipped: short-circuit"

                else:
                    # request_changes/change_approach — reset phase for re-run
                    phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                    if phase_execution.status in (
                        PipelineStatus.COMPLETE,
                        PipelineStatus.FAILED,
                        PipelineStatus.RUNNING,
                        PipelineStatus.AWAITING_HUMAN,
                    ):
                        phase_execution.status = PipelineStatus.PENDING
                        phase_execution.started_at = None
                        phase_execution.work_started_at = None
                        phase_execution.completed_at = None
                        phase_execution.error = None
                        phase_execution.review_cycles = 0
                        phase_execution.hitl_review_cycles = 0
                        phase_execution.containers = []
                        phase_execution.agents = []
                        phase_execution.artifacts = {}

                    # Preserve the reviewer's feedback so the re-launched
                    # _run_pipeline thread can pass it to the agent.
                    if revision_feedback:
                        phase_execution.hitl_feedback = revision_feedback

                pipeline.error = None
                pipeline.created_at = datetime.utcnow()
                pipeline.status = PipelineStatus.RUNNING
                store.save_pipeline(pipeline)

            # Launch runner thread
            thread = threading.Thread(
                target=_run_pipeline,
                args=(pipeline_id, repo_path),
                daemon=True,
                name=f"pipeline-{pipeline_id}",
            )
            thread.start()

            logger.info(
                "Pipeline recovered from AWAITING_HUMAN",
                pipeline_id=pipeline_id,
                recovery_action="advance" if is_approved else "rerun",
            )

            return make_success_response(
                "Pipeline recovered and started",
                data={
                    "pipeline_id": pipeline_id,
                    "status": "running",
                    "current_phase": pipeline.current_phase.value,
                },
            )

        if pipeline.status == PipelineStatus.COMPLETE:
            return make_error_response(
                f"Pipeline {pipeline_id} is already complete",
                status_code=409,
            )

        if pipeline.status == PipelineStatus.CANCELLED:
            return make_error_response(
                f"Pipeline {pipeline_id} is cancelled",
                status_code=409,
            )

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            if pipeline.status == PipelineStatus.FAILED:
                # Reset the failed phase so it can be re-run.
                # Also reset phases stuck in RUNNING — a pipeline-level exception
                # sets the pipeline to FAILED without updating the phase status.
                phase_execution = pipeline.get_phase_execution(pipeline.current_phase)
                if phase_execution.status in (PipelineStatus.FAILED, PipelineStatus.RUNNING):
                    prev_status = phase_execution.status.value
                    phase_execution.status = PipelineStatus.PENDING
                    phase_execution.started_at = None
                    phase_execution.work_started_at = None
                    phase_execution.completed_at = None
                    phase_execution.error = None
                    phase_execution.review_cycles = 0
                    phase_execution.hitl_review_cycles = 0
                    phase_execution.containers = []
                    phase_execution.agents = []
                    phase_execution.artifacts = {}
                    logger.info(
                        "Resetting phase for restart",
                        pipeline_id=pipeline_id,
                        phase=pipeline.current_phase.value,
                        previous_phase_status=prev_status,
                    )
                pipeline.error = None

                # Bump created_at so the old _run_pipeline thread's finally block
                # detects the restart and skips worktree cleanup.
                pipeline.created_at = datetime.utcnow()

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


@pipelines_bp.route("/<pipeline_id>/visualization", methods=["GET"])
def get_pipeline_visualization(pipeline_id: str) -> tuple[Response, int]:
    """
    Get pipeline DAG visualization.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        format: Output format - "full" (default), "compact", "text", "json"
        ascii: Use ASCII-only characters (default: false)

    Response:
        {
            "success": true,
            "data": {
                "pipeline_id": "issue-123",
                "visualization": {
                    "dag": "...",  // Full DAG visualization
                    "compact": "...",  // Single-line status
                    "progress": "..."  // Progress bar
                },
                "phases": {...},  // Phase status summary
                "status": "running",
                "current_phase": "implement"
            }
        }
    """
    # Check if visualization module is available (imported at module level)
    if not _DAG_VISUALIZER_AVAILABLE:
        return make_error_response(
            "Visualization module not available",
            status_code=500,
        )

    repo_path = get_repo_path()
    output_format = request.args.get("format", "full")
    use_ascii = request.args.get("ascii", "false").lower() == "true"

    try:
        _store, pipeline = _resolve_pipeline(pipeline_id, repo_path)

        if output_format == "json":
            # Return structured JSON report
            report = generate_status_report(pipeline, use_ascii=use_ascii)
            return make_success_response(
                "Visualization generated",
                data=report,
            )

        elif output_format == "text":
            # Return plain text DAG
            dag_text = render_pipeline_dag(pipeline, use_ascii=use_ascii)
            return Response(
                dag_text,
                mimetype="text/plain",
                status=200,
            )

        elif output_format == "compact":
            # Return compact single-line status
            compact = render_compact_status(pipeline, use_ascii=use_ascii)
            progress = render_progress_bar(pipeline, use_ascii=use_ascii)
            return make_success_response(
                "Visualization generated",
                data={
                    "pipeline_id": pipeline.id,
                    "compact": compact,
                    "progress": progress,
                    "status": pipeline.status.value,
                    "current_phase": pipeline.current_phase.value,
                },
            )

        else:
            # Full format with all visualizations
            report = generate_status_report(pipeline, use_ascii=use_ascii)
            return make_success_response(
                "Visualization generated",
                data=report,
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


@pipelines_bp.route("/stream", methods=["GET"])
def stream_all_pipelines() -> Response:
    """
    Stream unified events for all pipelines via Server-Sent Events (SSE).

    Provides real-time updates for ALL pipeline state changes in a single
    SSE connection. Unlike the per-pipeline stream, terminal events for
    individual pipelines do not end the stream.

    Query params:
        ascii: Use ASCII-only characters (default: false)
        active_only: Only include active pipelines (default: true)
        full_dag: Include full DAG visualization (default: false)

    Response:
        text/event-stream with the following event types:
        - snapshot: Initial state of all active pipelines
        - pipeline.*: Pipeline lifecycle events
        - phase.*: Phase transition events
        - agent.*: Agent lifecycle events
        - decision.*: HITL decision events
        - done: Stream is ending (timeout)
    """
    if not _UNIFIED_SSE_AVAILABLE:
        return make_error_response(
            "Unified SSE streaming module not available",
            status_code=500,
        )

    use_ascii = request.args.get("ascii", "false").lower() == "true"
    active_only = request.args.get("active_only", "true").lower() == "true"
    full_dag = request.args.get("full_dag", "false").lower() == "true"

    repo_path = get_repo_path()

    return Response(
        stream_with_context(
            create_unified_sse_stream(
                repo_path=repo_path,
                use_ascii=use_ascii,
                active_only=active_only,
                full_dag=full_dag,
            )
        ),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@pipelines_bp.route("/<pipeline_id>/stream", methods=["GET"])
def stream_pipeline(pipeline_id: str) -> Response:
    """
    Stream pipeline events via Server-Sent Events (SSE).

    Provides real-time updates for pipeline state changes including
    phase transitions, agent lifecycle, and DAG visualization.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        ascii: Use ASCII-only characters (default: false)

    Response:
        text/event-stream with the following event types:
        - snapshot: Initial pipeline state
        - pipeline.*: Pipeline lifecycle events
        - phase.*: Phase transition events
        - agent.*: Agent lifecycle events
        - decision.*: HITL decision events
        - done: Stream is ending (terminal state or timeout)
        - error: An error occurred

    The stream automatically closes when the pipeline reaches a
    terminal state (completed, failed, cancelled) or after the
    maximum connection time (1 hour).
    """
    if not _SSE_AVAILABLE:
        return make_error_response(
            "SSE streaming module not available",
            status_code=500,
        )

    use_ascii = request.args.get("ascii", "false").lower() == "true"

    # Validate pipeline exists before starting stream
    repo_path = get_repo_path()
    try:
        _resolve_pipeline(pipeline_id, repo_path)
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

    return Response(
        stream_with_context(
            create_sse_stream(pipeline_id, repo_path=repo_path, use_ascii=use_ascii)
        ),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
