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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from docker.errors import DockerException
from flask import Blueprint, Response, jsonify, request, stream_with_context

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Add config directory to path for repo_config module
_config_path = Path(__file__).parent.parent.parent / "config"
if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


try:
    from repo_config import get_repo_checks
except ImportError:

    def get_repo_checks(repo: str) -> list[dict[str, str]]:  # type: ignore[misc]
        return []


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
        DecisionStatus,
        HITLDecision,
        Pipeline,
        PipelineMode,
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
        ContainerStatus,
        CycleTiming,
        DecisionStatus,
        HITLDecision,
        Pipeline,
        PipelineMode,
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

from egg_git.default_branch import get_default_branch

if TYPE_CHECKING:
    from egg_container import MountSpec

    try:
        from ..container_spawner import ContainerSpawner
    except ImportError:
        from container_spawner import ContainerSpawner  # type: ignore

logger = get_logger("orchestrator.pipelines")


def _check_and_respawn_overseer(
    *,
    spawner: "ContainerSpawner",
    store: "StateStore",
    pipeline_id: str,
    pipeline: "Pipeline",
    overseer_container_id: str | None,
    overseer_respawn_count: int,
    max_overseer_respawns: int,
    gateway_mode: str,
    pipeline_repos: list | None,
    certs_volume: str | None,
) -> tuple[str | None, int]:
    """Check overseer container liveness and respawn if it exited mid-pipeline.

    Returns (updated_container_id, updated_respawn_count).
    """
    if not overseer_container_id or overseer_respawn_count >= max_overseer_respawns:
        return overseer_container_id, overseer_respawn_count

    try:
        info = spawner.docker.get_container_info(overseer_container_id)
        needs_respawn = info.status in (
            ContainerStatus.EXITED,
            ContainerStatus.FAILED,
            ContainerStatus.REMOVED,
        )
        exit_code = info.exit_code
    except ContainerNotFoundError:
        # Container completely deleted from Docker daemon — treat as respawn trigger.
        needs_respawn = True
        exit_code = None
        logger.warning(
            "Overseer container not found in Docker, will check for respawn",
            pipeline_id=pipeline_id,
            container_id=overseer_container_id[:12],
        )
    except Exception as respawn_err:
        logger.warning(
            "Overseer liveness check error",
            pipeline_id=pipeline_id,
            error=str(respawn_err),
        )
        return overseer_container_id, overseer_respawn_count

    if needs_respawn:
        try:
            pipeline_check = store.load_pipeline(pipeline_id)
            if pipeline_check.status in (PipelineStatus.RUNNING, PipelineStatus.AWAITING_HUMAN):
                logger.warning(
                    "Overseer exited mid-pipeline, respawning",
                    pipeline_id=pipeline_id,
                    exit_code=exit_code,
                    respawn_attempt=overseer_respawn_count + 1,
                    max_respawns=max_overseer_respawns,
                )
                new_result = spawner.spawn_overseer_container(
                    pipeline_id=pipeline_id,
                    issue_number=pipeline.issue_number,
                    mode=gateway_mode,
                    poll_interval=pipeline.config.overseer_poll_interval_seconds,
                    decision_model=pipeline.config.overseer_decision_maker_model,
                    repos=pipeline_repos if pipeline_repos else None,
                    certs_volume=certs_volume,
                )
                new_container_id = new_result.container_info.container_id
                overseer_respawn_count += 1
                logger.info(
                    "Overseer respawned successfully",
                    pipeline_id=pipeline_id,
                    container_id=new_container_id[:12],
                    respawn_attempt=overseer_respawn_count,
                )
                return new_container_id, overseer_respawn_count
        except Exception as respawn_err:
            logger.warning(
                "Overseer respawn failed",
                pipeline_id=pipeline_id,
                error=str(respawn_err),
            )

    return overseer_container_id, overseer_respawn_count


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

    Each repo has its own state store and worktree.  This function
    searches all repos under ``base_path`` to find the pipeline.

    Returns:
        (store, pipeline) tuple

    Raises:
        PipelineNotFoundError: if the pipeline cannot be found anywhere
        InvalidPipelineIdError: if the ID format is invalid
    """
    from state_store import discover_repo_paths

    for repo_path in discover_repo_paths(base_path):
        try:
            store = get_state_store(repo_path)
            pipeline = store.load_pipeline(pipeline_id)
            return store, pipeline
        except (PipelineNotFoundError, StateStoreError):
            continue

    raise PipelineNotFoundError(f"Pipeline {pipeline_id} not found") from None


def _collect_all_pipelines(base_path: Path) -> list:
    """Collect pipelines from all git repos under base_path.

    Each repo has its own state store and worktree. Pipelines are
    deduplicated by ID in case of overlapping stores.
    """
    from state_store import discover_repo_paths

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

    for repo_path in discover_repo_paths(base_path):
        try:
            _add_from_store(get_state_store(repo_path))
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
    base_branch = data.get("base_branch")
    prompt = data.get("prompt")
    mode = data.get("mode", "issue")
    pr_number = data.get("pr_number")
    analysis = data.get("analysis")
    plan = data.get("plan")

    # Validate mode
    valid_modes = {m.value for m in PipelineMode}
    if mode not in valid_modes:
        return make_error_response(f"Invalid mode: {mode!r} (must be one of {sorted(valid_modes)})")

    # Babysit mode requires pr_number
    if mode == PipelineMode.BABYSIT:
        if not pr_number:
            return make_error_response("Missing pr_number (required for babysit mode)")
        if not isinstance(pr_number, int) or pr_number < 1:
            return make_error_response("pr_number must be a positive integer")

    if not repo:
        return make_error_response("Missing repo")

    # Issue-driven or explicitly-named pipelines require a branch;
    # prompt-driven ones do not.
    pipeline_id = data.get("pipeline_id")
    if not pipeline_id and mode == PipelineMode.BABYSIT:
        pipeline_id = f"pr-{pr_number}"

    if (issue_number or pipeline_id) and not branch and mode != PipelineMode.BABYSIT:
        return make_error_response("Missing branch")

    repo_path = get_repo_path()

    # Check that the target branch does not already exist on the remote.
    # This catches conflicts early (before spawning agents).
    if branch:
        try:
            gw = get_gateway_client()
            if gw.ls_remote_branch(
                pipeline_id=pipeline_id or f"branch-check-{uuid4().hex[:8]}",
                repo_path=str(repo_path),
                ref=f"refs/heads/{branch}",
            ):
                hint = ""
                if pipeline_id:
                    hint = (
                        f" Use a qualifier to create a separate pipeline"
                        f" (e.g. '{pipeline_id}-<qualifier>')."
                    )
                return make_error_response(
                    f"Branch '{branch}' already exists on remote.{hint}",
                    status_code=409,
                    details={"reason": "branch_exists", "branch": branch},
                )
        except Exception as e:
            # Non-fatal — if we can't reach the gateway, let creation proceed
            # and fail later on push.
            logger.warning(
                "Branch existence check failed, proceeding anyway",
                branch=branch,
                error=str(e),
            )

    # Validate config before creating the pipeline so invalid config
    # returns a 400 instead of bubbling up as a 500.
    config = data.get("config")
    if config is not None:
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError as e:
                return make_error_response(f"Invalid config JSON: {e}")
        try:
            from models import PipelineConfig
            from pydantic import ValidationError

            PipelineConfig.model_validate(config)
        except ValidationError as e:
            errors = [
                {"field": ".".join(str(loc) for loc in err["loc"]), "message": err["msg"]}
                for err in e.errors()
            ]
            return make_error_response(
                f"Invalid pipeline config: {errors}",
                details={"validation_errors": errors},
            )

    # Validate analysis/plan size before creating the pipeline.
    _MAX_DRAFT_LEN = 200_000
    for field_name in ("analysis", "plan"):
        value = data.get(field_name)
        if isinstance(value, str) and len(value) > _MAX_DRAFT_LEN:
            return make_error_response(
                f"{field_name} exceeds maximum length ({len(value)} > {_MAX_DRAFT_LEN})"
            )

    try:
        store = get_state_store(repo_path)
        pipeline = store.create_pipeline(
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            base_branch=base_branch,
            config=config,
            prompt=prompt,
            network_mode=network_mode,
            pipeline_id=pipeline_id,
            mode=PipelineMode(mode) if mode != "issue" else None,
            pr_number=pr_number,
            analysis=analysis,
            plan=plan,
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
            # Include existing pipeline details so callers can decide
            # whether to cancel+resubmit or resume monitoring.
            details: dict[str, Any] = {}
            try:
                # Derive pipeline ID using the same logic as state_store
                pid = pipeline_id or (f"issue-{issue_number}" if issue_number else None)
                if pid:
                    existing = store.load_pipeline(pid)
                    details = {
                        "existing_pipeline_id": existing.id,
                        "existing_status": existing.status.value,
                        "existing_phase": existing.current_phase.value,
                    }
            except Exception:
                pass  # Best-effort enrichment
            return make_error_response(str(e), status_code=409, details=details)
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
    overwriting updates made between the status change and container
    cleanup), marks running records as stopped, and saves.

    Returns the updated pipeline so the caller can use it in the response.
    """
    pipeline = store.load_pipeline(pipeline_id)
    now = datetime.now(UTC)
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

            # Sync pipeline state: reload latest state (agents may have
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
                # Reload pipeline so the response reflects current state
                # rather than the stale pre-cleanup object.
                try:
                    pipeline = store.load_pipeline(pipeline_id)
                except Exception:
                    pass  # Use stale pipeline if reload also fails

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


def _compute_gateway_mode(
    pipeline: "Pipeline",
) -> tuple[Literal["public", "private"], str | None]:
    """Compute gateway session mode from pipeline config and repo visibility.

    Uses the explicit ``network_mode`` if set, otherwise auto-detects from
    repository visibility via the gateway.  Defaults to ``"public"``.

    Returns:
        A ``(mode, visibility)`` tuple.  ``visibility`` is ``None`` when
        ``network_mode`` is explicit, the pipeline has no repo, or the
        gateway query failed.
    """
    if pipeline.network_mode:
        return pipeline.network_mode, None
    if pipeline.repo:
        vis = get_gateway_client().get_repo_visibility(pipeline.repo)
        if vis in ("private", "internal"):
            return "private", vis
        return "public", vis
    return "public", None


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
    mode, _vis = _compute_gateway_mode(pipeline)

    deleted = 0
    for branch in sorted(branches):
        if gateway_client.delete_remote_branch(pipeline_id, repo_path_str, branch, mode=mode):
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

        # Clean up Redis message store keys (stream + counters)
        try:
            from message_store import get_message_store

            get_message_store().clear(pipeline_id)
        except Exception as e:
            logger.warning(
                "Failed to clear message store for deleted pipeline",
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
    try:
        from concurrent_executor import is_concurrent_execution
    except ImportError:
        from ..concurrent_executor import is_concurrent_execution  # type: ignore[no-redef]

    current_phase = pipeline.current_phase.value if pipeline.current_phase else None
    if not is_concurrent_execution(pipeline, phase=current_phase):
        return None

    config = pipeline.config
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
    # BRC peer consensus (preferred) or legacy readiness-based
    try:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[import-not-found]

        tracker = get_peer_consensus_tracker(pipeline.id)
        if not tracker:
            # Attempt lazy reconstruction from message store for concurrent pipelines
            try:
                from review_graph import get_review_graph_for_phase

                from ..concurrent_executor import is_concurrent_execution
                from ..peer_consensus import reconstruct_tracker_from_messages

                if is_concurrent_execution(pipeline, pipeline.current_phase):
                    graph = get_review_graph_for_phase(
                        pipeline.current_phase.value, repo=pipeline.repo
                    )
                    tracker = reconstruct_tracker_from_messages(pipeline.id, graph)
            except ImportError:
                pass  # Fall through to legacy evaluator
            except Exception as e:
                logger.warning(
                    "Tracker reconstruction failed",
                    error=str(e),
                    pipeline_id=pipeline.id,
                )
        if tracker:
            consensus_state = tracker.get_state()
        else:
            from ..consensus import get_consensus_evaluator  # type: ignore[import-not-found]

            evaluator = get_consensus_evaluator()
            consensus_state = evaluator.get_state(pipeline.id)
    except ImportError:
        try:
            from ..consensus import get_consensus_evaluator  # type: ignore[import-not-found]

            evaluator = get_consensus_evaluator()
            consensus_state = evaluator.get_state(pipeline.id)
        except ImportError:
            logger.debug("Consensus evaluator not available for status")
            consensus_state = None

    if consensus_state is not None:
        agents_data = {}
        for role, agent_info in consensus_state.get("agents", {}).items():
            if hasattr(agent_info, "state"):
                # Legacy AgentReadiness object
                agents_data[role] = {
                    "state": agent_info.state.value,
                    "reason": agent_info.reason,
                    "updated_at": agent_info.timestamp.isoformat()
                    if agent_info.timestamp
                    else None,
                }
            else:
                # BRC dict format
                agents_data[role] = agent_info
        result["consensus"] = {
            "agents": agents_data,
            "is_complete": consensus_state.get("is_complete", False),
            "blocking_agents": consensus_state.get("blocking_agents", []),
            "protocol": consensus_state.get("protocol", "readiness"),
        }
    else:
        # Don't populate consensus with empty placeholder — callers (e.g. the
        # MCP get_consensus_status tool) use truthiness to decide whether to
        # fall back to message-based inference.  An empty-but-truthy dict
        # prevents that fallback from triggering (see issue #1229).
        pass

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
) -> str:
    """Return the relative verdict file path for a given reviewer type.

    Uses issue_number as prefix when available, otherwise pipeline_id.
    """
    prefix = _pipeline_identifier(issue_number, pipeline_id or "unknown")
    return f".egg-state/reviews/{prefix}-{phase}-{reviewer_type}-review.json"


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

    Used to give execution agents (tester, documenter) a brief
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

    Execution roles (tester, documenter) receive a brief summary
    with structured task information and pointers to full context.

    Args:
        role_value: Agent role string
        prompt: Original task prompt (full issue body)
        issue_number: GitHub issue number
        phase_obj: Current plan phase object (phase context)
        all_phases: All contract phases (phase context)

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

    # Phase-specific context
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

    if all_phases and phase_obj is not None and role_value in ("tester", "documenter"):
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
        lines.append(
            "6. Research when uncertain — use WebSearch and WebFetch (when available) "
            "to look up library behavior, check official documentation, verify "
            "API usage patterns, and confirm the code follows current best practices"
        )
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
    gateway_mode: Literal["public", "private"] = "public",
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
        mode=gateway_mode,
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
                mode=gateway_mode,
            )
            if push_ok:
                # Push succeeded — local and remote are now in sync.
                # Re-fetch to update the remote tracking ref so that
                # origin/{branch} reflects the pushed commits.
                spawner.gateway.fetch_worktree_branch(
                    pipeline_id=pipeline_id,
                    repo_path=str(worktree_repo_path),
                    mode=gateway_mode,
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


def _ensure_statefiles_on_branch(
    worktree_repo_path: Path,
    pipeline: "Pipeline",
) -> bool:
    """Verify the contract file exists in the worktree and re-create if missing.

    This is a safety net for short-flow pipelines where the initial contract
    push may have failed silently (``contract_synced`` set True despite push
    failure) or where subsequent pushes diverged.

    Returns True if the contract exists (or was successfully restored),
    False if restoration failed.
    """
    identifier = _pipeline_identifier(pipeline.issue_number, pipeline.id)
    contract_path = worktree_repo_path / ".egg-state" / "contracts" / f"{identifier}.json"

    if contract_path.exists():
        return True

    logger.warning(
        "Contract file missing from worktree — attempting restoration",
        pipeline_id=pipeline.id,
        expected_path=str(contract_path),
    )

    try:
        from egg_contracts.loader import create_contract, get_contract_path

        # Double-check using the canonical contract path from the loader to
        # guard against path-construction drift between this function and the
        # contract library.  This prevents data loss if the file exists under
        # a slightly different naming convention.
        canonical_path = get_contract_path(identifier, worktree_repo_path)
        if canonical_path.exists():
            logger.info(
                "Contract file found at canonical path — skipping recreation",
                pipeline_id=pipeline.id,
                canonical_path=str(canonical_path),
            )
            return True

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

        _commit_statefiles_to_worktree(
            worktree_repo_path,
            f"Restore missing contract for {identifier}",
        )
        logger.info(
            "Contract file restored successfully",
            pipeline_id=pipeline.id,
        )
        return True
    except Exception as restore_err:
        logger.error(
            "Failed to restore contract file",
            pipeline_id=pipeline.id,
            error=str(restore_err),
        )
        return False


def _detect_default_branch(worktree_repo_path: Path) -> str:
    """Detect the remote's default branch from a worktree.

    Tries in order:
    1. origin/HEAD symbolic ref (most reliable)
    2. origin/main
    3. origin/master
    4. Fallback to "main"

    Returns:
        The branch name (e.g., "main" or "master"), without the "origin/" prefix.
    """
    # Try origin/HEAD symbolic ref
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            ref = result.stdout.strip()  # e.g. "origin/main"
            return ref.removeprefix("origin/")
    except Exception:
        pass

    # Try origin/main
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return "main"
    except Exception:
        pass

    # Try origin/master
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/master"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return "master"
    except Exception:
        pass

    logger.warning(
        "Could not detect default branch, falling back to 'main'",
        worktree_path=str(worktree_repo_path),
    )
    return "main"


def _handle_pr_creation_failure(
    pipeline_id: str,
    current_phase: str,
    store,
) -> None:
    """Mark a pipeline as FAILED after PR creation returns no URL.

    Extracted from ``_health_monitor_poll`` so this state-transition logic can
    be tested independently of the full polling loop.
    """
    error_msg = "Auto PR creation failed: no PR URL returned"
    logger.error(error_msg, pipeline_id=pipeline_id)
    with get_pipeline_state_lock(pipeline_id):
        pipeline = store.load_pipeline(pipeline_id)
        phase_execution = pipeline.get_phase_execution(current_phase)
        phase_execution.status = PipelineStatus.FAILED
        phase_execution.error = error_msg
        phase_execution.completed_at = datetime.now(UTC)
        pipeline.status = PipelineStatus.FAILED
        pipeline.error = error_msg
        store.save_pipeline(pipeline)


def _build_pr_body(
    pipeline: Pipeline,
    worktree_repo_path: Path,
    default_branch: str | None = None,
) -> tuple[str, str]:
    """Build a PR title and body from contract state and git log.

    Uses the planner-generated PR metadata from the contract when available,
    falling back to the issue title and git log.

    Args:
        pipeline: The pipeline state
        worktree_repo_path: Path to the worktree repo directory
        default_branch: Pre-detected default branch name. If None, will be
            detected automatically.

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

    # Detect default branch for git comparisons
    if default_branch is None:
        default_branch = _detect_default_branch(worktree_repo_path)
    origin_ref = f"origin/{default_branch}"

    # Build commit log
    commit_log = ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"{origin_ref}..HEAD"],
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
            ["git", "diff", "--stat", f"{origin_ref}...HEAD"],
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
    gateway_mode: Literal["public", "private"] = "public",
) -> str | None:
    """Auto-create a PR for a pipeline without spawning an agent.

    Builds the PR title/body from contract state and git log, then
    creates the PR via the gateway.

    Args:
        pipeline: The pipeline state
        worktree_repo_path: Path to the worktree repo directory
        spawner: Container spawner (used to access gateway client)
        gateway_mode: Session mode for the gateway ("public" or "private")

    Returns:
        PR URL if creation succeeded, None otherwise
    """
    if not pipeline.repo or not pipeline.branch:
        logger.warning(
            "Cannot auto-create PR: missing repo or branch",
            pipeline_id=pipeline.id,
        )
        return None

    # Resolve base branch: explicit > auto-detected from repo
    base = pipeline.base_branch
    if not base:
        base = get_default_branch(worktree_repo_path)

    title, body = _build_pr_body(pipeline, worktree_repo_path, default_branch=base)

    try:
        pr_url = spawner.gateway.create_pr(
            pipeline_id=pipeline.id,
            repo=pipeline.repo,
            title=title,
            body=body,
            head=pipeline.branch,
            base=base,
            issue_number=pipeline.issue_number,
            agent_role="orchestrator",
            mode=gateway_mode,
            draft=(gateway_mode == "private"),
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
                "3. Research externally when the task involves third-party libraries, APIs, "
                "or integrations — use WebSearch and WebFetch (when available) to look up "
                "current documentation, best practices, and known issues. Skip external "
                "research for purely internal changes where codebase context is sufficient.",
                "4. Identify constraints and dependencies",
                "5. Consider multiple implementation approaches",
                "6. Recommend an approach with justification",
                "7. Surface **all** questions and uncertainties that need human input "
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
            draft_text = _read_phase_draft(
                Path(repo_path),
                "plan",
                issue_number=issue_number,
                pipeline_id=pipeline_id,
            )
            if draft_text:
                lines.append("## Plan\n")
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
            lines.append("Implement the changes described in the task and plan:")
            lines.append("")

            steps: list[str] = []
            if not draft_embedded:
                steps.append("Review the plan (check `.egg-state/drafts/`)")
            steps.extend(
                [
                    "Implement the required changes — when working with third-party "
                    "libraries or APIs, use WebSearch and WebFetch (when available) to "
                    "look up current documentation, usage examples, and best practices",
                    "Run tests to verify correctness",
                    "Commit with descriptive messages",
                ]
            )
            for i, step in enumerate(steps, 1):
                lines.append(f"{i}. {step}")
            lines.append("")

            lines.append("## Parallel Execution with Subagents\n")
            lines.append(
                "You have access to Claude Code's **Agent tool** for spawning subagents. "
                "Use it to parallelize independent work:\n"
            )
            lines.append(
                "- If the plan has multiple independent phases or task groups that don't touch "
                "overlapping files, implement them in parallel by launching one subagent per "
                "phase/group."
            )
            lines.append(
                "- Each subagent gets a clear, self-contained prompt describing its scope "
                "(files to modify, tasks to complete, acceptance criteria)."
            )
            lines.append(
                "- Subagents share your working directory and git state. Ensure parallel "
                "subagents work on **non-overlapping files** to avoid conflicts."
            )
            lines.append(
                "- Subagents should only edit files — do NOT stage or commit from subagents. "
                "After all subagents complete, stage and commit the combined changes yourself."
            )
            lines.append(
                "- After subagents complete, verify the combined changes compile, pass tests, "
                "and integrate correctly."
            )
            lines.append(
                "- For small or sequential tasks, just implement directly — don't over-parallelize."
            )
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
                "- You CANNOT post comments to the GitHub issue (gh issue comment) — write reviews to `.egg-state/reviews/` instead",
                "- You CANNOT edit the GitHub issue (gh issue edit)",
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
                "- You CANNOT post comments to the GitHub issue (gh issue comment)",
                "- You CANNOT edit the GitHub issue (gh issue edit)",
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
                    "- You CANNOT post comments to the GitHub issue (gh issue comment) — write reviews to `.egg-state/reviews/` instead",
                    "- You CANNOT edit the GitHub issue (gh issue edit)",
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
                    "- You CANNOT post comments to the GitHub issue (gh issue comment)",
                    "- You CANNOT edit the GitHub issue (gh issue edit)",
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


def _build_brc_preamble(role_value: str, phase: str, repo: str | None = None) -> str:
    """Build the BRC consensus lifecycle preamble for an agent.

    Returns a formatted string block that can be appended to any agent prompt
    to inject BRC protocol instructions. Used by both the coder/refiner path
    (which delegates to _build_phase_prompt) and the generic multi-agent path.

    Includes:
    - Agent roster showing all active agents and what they produce
    - Role-specific proactive preparation instructions
    - Full BRC lifecycle steps
    """
    try:
        from review_graph import get_review_graph_for_phase

        graph = get_review_graph_for_phase(phase, repo=repo)
        is_producer = graph.is_producer(role_value)
        is_reviewer = graph.is_reviewer(role_value)
        reviewers = graph.reviewers_for(role_value) if is_producer else []
        producers = graph.producers_for(role_value) if is_reviewer else []
        all_roles = sorted(graph.all_roles())
    except Exception:
        is_producer = role_value in (
            "coder",
            "tester",
            "documenter",
            "refiner",
            "architect",
            "task_planner",
            "risk_analyst",
        )
        is_reviewer = role_value in (
            "reviewer_code",
            "reviewer_contract",
            "tester",
            "reviewer_refine",
            "reviewer_agent_design",
            "reviewer_plan",
        )
        reviewers = []
        producers = []
        all_roles = []

    lines: list[str] = [
        "\n\n## CRITICAL: BRC Consensus Protocol\n",
        "You are running in CONCURRENT mode with the Broadcast-Review-Converge "
        "(BRC) protocol. Your job is NOT just your task — it is the **full "
        "BRC lifecycle**.\n",
    ]

    if is_producer and is_reviewer:
        role_type_desc = "PRODUCER and REVIEWER (dual role)"
    elif is_producer:
        role_type_desc = "PRODUCER"
    elif is_reviewer:
        role_type_desc = "REVIEWER"
    else:
        role_type_desc = "PARTICIPANT"

    lines.append(f"Your role type: **{role_type_desc}**")
    if reviewers:
        lines.append(f"Your reviewers: {', '.join(reviewers)}")
    if producers:
        lines.append(f"Your assigned producers: {', '.join(producers)}")
    lines.append("")

    # Agent roster: show all active agents and what they do
    if all_roles:
        roster = _build_agent_roster(all_roles, role_value, phase)
        if roster:
            lines.append(roster)

    if is_producer:
        lines.extend(
            [
                "### Producer Lifecycle",
                "1. **ORIENT**: Before starting work, "
                + _build_producer_orientation(role_value, phase, reviewers),
                "2. **WORK**: Complete your assigned task (see Your Task below).",
                "3. **PROPOSE**: When done, run: "
                '`egg-orch consensus propose --summary "..." --artifacts "file1" "file2"`',
                "4. **RESPOND TO REVIEWS**: Poll for ACK/NACK from reviewers. "
                "Handle NACKs by fixing issues and re-proposing.",
                "5. **CONFIRM**: When all reviewers ACK: `egg-orch consensus confirmed`",
                "6. **STAY ALIVE**: Keep polling `egg-orch message poll --wait 30` "
                "until the orchestrator stops you.\n",
            ]
        )

    if is_reviewer:
        lines.extend(
            [
                "### Reviewer Lifecycle",
                "1. **PREPARE** (while waiting): " + _build_reviewer_preparation(role_value, phase),
                "2. **POLL**: Wait for `CONSENSUS_PROPOSE` from assigned producers "
                "(`egg-orch message poll --wait 30`). Do NOT inspect producer "
                "artifacts or form judgments before the proposal arrives.",
                "3. **REVIEW**: Once a proposal arrives, form independent judgment from "
                "the referenced code artifacts. Read the actual files — do not rely "
                "solely on the proposal summary.",
                '4. **ACK/NACK**: `egg-orch consensus ack <role> --files-reviewed "f1" "f2"` or '
                '`egg-orch consensus nack <role> --reason "..." --files-reviewed "f1" "f2"`',
                "5. **CONFIRM**: When all assigned producers reviewed: "
                "`egg-orch consensus confirmed`",
                "6. **STAY ALIVE**: Keep polling `egg-orch message poll --wait 30` "
                "until the orchestrator stops you.\n",
            ]
        )

    lines.extend(
        [
            "**If you exit before the orchestrator stops you, you have FAILED your role.** "
            "Completing your task is necessary but NOT sufficient — you must reach "
            "CONFIRMED state and remain alive until consensus.\n",
            "",
        ]
    )

    return "\n".join(lines)


# Role descriptions for agent roster — maps role names to (short description,
# what artifacts they produce).
_ROLE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "coder": (
        "Implements code changes",
        "commits with source files, tests may be included",
    ),
    "tester": (
        "Writes and runs tests (dual role: also reviews coder)",
        "test files, coverage reports, test pass/fail results",
    ),
    "documenter": (
        "Updates documentation for changes",
        "doc files, README updates, inline documentation",
    ),
    "refiner": (
        "Refines implementation based on review feedback",
        "updated source files addressing review concerns",
    ),
    "architect": (
        "Designs architecture and component structure",
        "architecture analysis, component breakdown",
    ),
    "task_planner": (
        "Breaks work into implementation tasks",
        "task list with acceptance criteria",
    ),
    "risk_analyst": (
        "Assesses technical risks",
        "risk assessment with mitigations",
    ),
    "reviewer_code": (
        "Reviews code quality, correctness, and security",
        "ACK/NACK with file-level feedback",
    ),
    "reviewer_contract": (
        "Verifies implementation matches contract/requirements",
        "ACK/NACK with task-level verification",
    ),
    "reviewer_refine": (
        "Reviews refinement changes",
        "ACK/NACK on refined implementation",
    ),
    "reviewer_agent_design": (
        "Reviews agent design and architecture decisions",
        "ACK/NACK on design choices",
    ),
    "reviewer_plan": (
        "Reviews plan phase outputs",
        "ACK/NACK on architecture, tasks, and risk assessment",
    ),
}


def _build_agent_roster(all_roles: list[str], current_role: str, phase: str) -> str:
    """Build a roster of all active agents for the current phase.

    Shows each agent's role, what they do, and what they produce so that
    every agent understands who else is running and what to expect.
    """
    roster_lines = ["### Active Agents in This Phase\n"]
    roster_lines.append(
        "The following agents are running **simultaneously**. "
        "Each must complete their task AND reach CONFIRMED via BRC.\n"
    )
    for role in all_roles:
        desc, artifacts = _ROLE_DESCRIPTIONS.get(
            role, ("Executes assigned role", "role-specific artifacts")
        )
        marker = " **(you)**" if role == current_role else ""
        roster_lines.append(f"- **{role}**{marker}: {desc}. Produces: {artifacts}.")
    roster_lines.append("")
    return "\n".join(roster_lines)


def _build_reviewer_preparation(role_value: str, phase: str) -> str:
    """Build proactive preparation instructions for reviewer agents.

    Tells reviewers what to do while waiting for proposals — e.g., reading
    the contract, familiarizing themselves with the codebase, preparing
    review criteria. This avoids idle waiting and produces better reviews.
    """
    if phase == "implement":
        if role_value == "reviewer_code":
            return (
                "While waiting for proposals, prepare by: "
                "(a) reading the contract with `egg-contract show` to understand "
                "what was planned, "
                "(b) reviewing the issue/PR description for context, "
                "(c) exploring the codebase areas likely to be changed "
                "(grep for relevant classes, read key files), "
                "(d) noting existing test patterns and code conventions. "
                "This background research will make your review faster "
                "and more thorough once proposals arrive. "
                "When reviewing the tester's proposal, check whether tests were "
                "actually executed (look for `tests_run` and `tests_execution_blocked` "
                "in the attestation). If the tester reports `tests_execution_blocked: true`, "
                "this is a blocking concern — NACK unless the limitation is clearly "
                "documented and the tests are syntactically valid. "
                "Also scrutinize low `tests_run` counts relative to change scope — "
                "a multi-file change with only 1 test run warrants investigation."
            )
        elif role_value == "reviewer_contract":
            return (
                "While waiting for proposals, prepare by: "
                "(a) reading the contract with `egg-contract show` to understand "
                "every task and its acceptance criteria, "
                "(b) reviewing the issue description for original requirements, "
                "(c) noting which tasks are marked as must-have vs nice-to-have. "
                "When proposals arrive, you will verify each task's acceptance "
                "criteria is met — prepare a checklist now."
            )
        elif role_value == "tester":
            return (
                "While waiting for the coder's proposal, prepare by: "
                "(a) reading the contract with `egg-contract show` to understand "
                "what's being implemented, "
                "(b) identifying edge cases and boundary conditions from the "
                "requirements, "
                "(c) checking the existing test infrastructure (test frameworks, "
                "fixtures, test utilities). "
                "Start writing test scaffolding for known requirements while "
                "waiting — you can finalize once you see the actual implementation."
            )
    elif phase == "plan":
        if role_value == "reviewer_plan":
            return (
                "While waiting for proposals, prepare by: "
                "(a) reading the issue description to understand the original "
                "request, "
                "(b) exploring the codebase to understand the current architecture "
                "and components that may be affected, "
                "(c) identifying potential risks or constraints the planners "
                "should address. "
                "Form your own mental model of how you would approach this — "
                "then compare against the proposals when they arrive."
            )
    elif phase == "refine":
        if role_value in ("reviewer_refine", "reviewer_agent_design"):
            return (
                "While waiting for the refiner's proposal, prepare by: "
                "(a) reading the prior review feedback that triggered this "
                "refinement cycle, "
                "(b) checking the current state of the code to understand "
                "what was already implemented, "
                "(c) verifying which review concerns are still outstanding. "
                "When the proposal arrives, focus on whether the specific "
                "feedback items were addressed."
            )

    # Generic fallback
    return (
        "While waiting for proposals, read the contract "
        "(`egg-contract show`), explore the codebase for context, "
        "and prepare your review criteria. "
        "Do NOT inspect producer artifacts before proposals arrive."
    )


def _build_producer_orientation(role_value: str, phase: str, reviewers: list[str]) -> str:
    """Build orientation instructions for producer agents.

    Tells producers what to research before starting work — understanding
    context, knowing what reviewers will check, and checking existing code
    patterns. This produces higher-quality first proposals and fewer NACKs.
    """
    reviewer_awareness = ""
    if reviewers:
        reviewer_names = ", ".join(reviewers)
        reviewer_awareness = (
            f" Your work will be reviewed by **{reviewer_names}** — "
            "keep their review criteria in mind as you work."
        )

    if phase == "implement":
        if role_value == "coder":
            return (
                "read the contract (`egg-contract show`) to understand all tasks "
                "and acceptance criteria. Explore the codebase to find existing "
                "patterns, conventions, and the files you will modify. Check for "
                "existing tests that cover the areas you will change — do not "
                "break them." + reviewer_awareness
            )
        elif role_value == "tester":
            return (
                "read the contract (`egg-contract show`) to understand what is "
                "being implemented. Check the existing test infrastructure — "
                "test frameworks, fixtures, conftest files, and naming conventions. "
                "Identify edge cases from the requirements before writing tests."
                + reviewer_awareness
            )
        elif role_value == "documenter":
            return (
                "read the contract (`egg-contract show`) to understand what is "
                "being implemented. Check existing documentation structure — "
                "README files, doc directories, inline documentation patterns. "
                "Identify which docs will need updating once the implementation "
                "is complete." + reviewer_awareness
            )
    elif phase == "plan":
        if role_value == "architect":
            return (
                "read the issue/task description carefully. Explore the codebase "
                "to understand the current architecture, component boundaries, "
                "and dependencies. Identify the areas that will be affected by "
                "the proposed changes." + reviewer_awareness
            )
        elif role_value == "task_planner":
            return (
                "read the issue/task description carefully. Review the codebase "
                "structure to understand the scope of work. Break the work into "
                "tasks with clear acceptance criteria that reviewers can verify."
                + reviewer_awareness
            )
        elif role_value == "risk_analyst":
            return (
                "read the issue/task description carefully. Research the affected "
                "areas of the codebase for potential risks — security, "
                "performance, backwards compatibility, and third-party "
                "dependencies." + reviewer_awareness
            )
    elif phase == "refine":
        if role_value == "refiner":
            return (
                "read the prior review feedback carefully. Understand exactly "
                "what concerns were raised and what changes are expected. Check "
                "the current state of the code before making modifications." + reviewer_awareness
            )

    # Generic fallback
    return (
        "read the contract (`egg-contract show`) and explore the codebase "
        "to understand context, patterns, and conventions before starting." + reviewer_awareness
    )


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
    phase_obj=None,
    all_phases=None,
    concurrent: bool = False,
    network_mode: str | None = None,
) -> str:
    """Build a role-specific prompt for multi-agent execution.

    For the CODER role, delegates to the existing _build_phase_prompt().
    Other roles (TESTER, DOCUMENTER, ARCHITECT, etc.) get
    role-specific instructions.

    Execution roles (tester, documenter) receive a summarized
    background with structured task information instead of the full issue
    body. Analysis roles (architect, task_planner, risk_analyst) receive
    the full issue body.

    Note: Handoff data is passed via the EGG_HANDOFF_DATA environment
    variable, not via the prompt — prompts are built once before
    execution starts.

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
        phase_obj: Current plan phase object (optional)
        all_phases: All contract phases (optional)
        concurrent: Whether agent runs in concurrent multi-agent mode.
            When True, adds consensus lifecycle preamble instructing the
            agent to stay alive, poll messages, and participate in consensus.
        network_mode: Pipeline network mode ("public", "private", or None).
            When "private", injects warnings about blocked package downloads.

    Returns:
        Complete prompt string for the agent
    """
    # CODER and REFINER use the existing phase prompt (phase-specific
    # instructions are already tailored for refine vs implement etc.)
    if role_value in ("coder", "refiner"):
        base_prompt = _build_phase_prompt(
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            prompt=prompt,
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            review_feedback=review_feedback,
            review_cycle=review_cycle,
            repo_path=repo_path,
        )
        # In concurrent mode, inject BRC consensus preamble so the coder/refiner
        # knows to propose, respond to reviews, confirm, and stay alive.
        if concurrent:
            base_prompt += _build_brc_preamble(role_value, phase, repo=repo)
        return base_prompt

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

    # Concurrent mode: add BRC consensus lifecycle preamble so agents understand
    # they must stay alive and participate in Broadcast-Review-Converge consensus.
    if concurrent:
        lines.append(_build_brc_preamble(role_value, phase, repo=repo))

    # Include role-appropriate context instead of the raw issue body.
    # Analysis roles (architect, task_planner, risk_analyst) receive the full
    # issue body. Execution roles (tester, documenter) receive a
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
        # Look up per-repo check commands from repositories.yaml
        repo_checks: list[dict[str, str]] = []
        if repo:
            try:
                repo_checks = get_repo_checks(repo)
            except FileNotFoundError:
                repo_checks = []

        lines.extend(
            [
                "Validate the changes and find gaps in the CODER agent's implementation. "
                "You are responsible for both **testing** and **lint/type-check validation**.",
                "",
                "### Testing",
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
                "### Lint, Type-Check, and Auto-Fix",
                "",
                "After writing tests, run all project checks and fix auto-fixable issues:",
                "",
            ]
        )

        if repo_checks:
            # Inject explicit check commands from repositories.yaml
            lines.extend(
                [
                    "The following check commands are configured for this repository. "
                    "Run them **in order** instead of auto-discovering commands:",
                    "",
                ]
            )
            for i, check in enumerate(repo_checks, 1):
                name = check["name"].replace("\n", " ").strip()
                cmd = check["command"].replace("\n", " ").strip()
                lines.append(f"{i}. **{name}**: `{cmd}`")
            lines.extend(
                [
                    "",
                    "After running these checks:",
                ]
            )
        else:
            # Fall back to auto-discovery
            lines.extend(
                [
                    "1. **Discover commands**: Look for Makefile, pyproject.toml, package.json, "
                    "setup.cfg, tox.ini, or similar build/test configuration files",
                    "2. **Run linters**: Execute linters (ruff, eslint, golangci-lint, etc.)",
                    "3. **Run type checkers**: Execute type checkers (mypy, pyright, tsc, etc.)",
                    "",
                    "After running checks:",
                ]
            )

        lines.extend(
            [
                "- **Auto-fix**: Fix auto-fixable issues (formatting, import order, simple type errors)",
                "- **Repeat**: Re-run checks to verify fixes. Repeat up to 3 times.",
                "- **Commit fixes**: Commit all auto-fixes together with a descriptive message",
                "",
                "Auto-fixable (commit fixes directly):",
                "- Lint errors (formatting, import order, code style)",
                "- Type errors with clear fixes",
                "- Simple test failures with obvious fixes",
                "",
                "Report only (explain what's needed):",
                "- Complex logic errors requiring design decisions",
                "- Security issues requiring architectural changes",
                "",
                "When testing third-party library integrations or unfamiliar frameworks, "
                "use WebSearch and WebFetch (when available) to look up testing patterns, "
                "known edge cases, and recommended test approaches for those libraries.",
                "",
                "Before writing tests, review the coder's session for context on what was changed and why:",
                "`egg-checkpoint list --pipeline $EGG_PIPELINE_ID --agent-type coder --phase implement`",
                "",
                "## Parallel Execution with Subagents\n",
                "If the changes span multiple independent components or modules, you can use "
                "Claude Code's **Agent tool** to parallelize test writing. Launch one subagent "
                "per component to write and run tests concurrently. Each subagent should work "
                "on non-overlapping test files. Subagents should only write files — do NOT "
                "stage or commit from subagents. After all subagents complete, run the full "
                "test suite to verify everything passes together, then stage and commit yourself.",
                "",
            ]
        )

        # Test execution verification — prevents proposing consensus with
        # unverified tests (issue #1359).
        test_verify_lines = [
            "### Test Execution Verification (CRITICAL)\n",
            "You MUST actually execute the test suite (`go test`, `pytest`, `jest`, etc.). "
            "Passing gofmt, syntax checks, or linting alone does NOT count as tests run.\n",
            "If tests cannot run (e.g., dependency downloads blocked in private network mode, "
            "missing build tools), you MUST:",
            "1. Set `tests_execution_blocked: true` and provide `tests_execution_blocked_reason` "
            "in your attestation when proposing consensus",
            '2. Include an explicit **"TESTS UNVERIFIED"** warning in your proposal summary',
            '3. Do NOT claim your work is "complete" — state that tests are written but unverified',
            "",
        ]
        if network_mode == "private":
            test_verify_lines.extend(
                [
                    "**WARNING: Private network mode is active** — external package downloads "
                    "(go mod download, npm install, pip install, etc.) may be blocked. "
                    "If dependency installation fails, you cannot verify tests. "
                    "Follow the instructions above to flag tests as unverified.",
                    "",
                ]
            )
        lines.extend(test_verify_lines)

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
                "When documenting third-party integrations or external APIs, use WebSearch "
                "and WebFetch (when available) to verify current API signatures, link to "
                "official documentation, and confirm usage examples are up to date.",
                "",
                "Find all changed files across agents:",
                "`egg-checkpoint context --pipeline $EGG_PIPELINE_ID --files`",
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
                "3. Research externally when the task involves third-party libraries, APIs, "
                "or frameworks — use WebSearch and WebFetch (when available) to verify "
                "assumptions, check current documentation, review architectural patterns, "
                "and look up current best practices. Skip external research for purely "
                "internal changes.",
                "4. Identify key files, constraints, and dependencies",
                "5. Consider multiple implementation approaches",
                "6. Recommend an approach with justification and document technical decisions",
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
                "3. Research externally when the change involves third-party dependencies — "
                "use WebSearch and WebFetch (when available) to check for known "
                "vulnerabilities, deprecation notices, and compatibility issues. "
                "Skip external research for purely internal changes.",
                "4. Assess impact and likelihood of each risk",
                "5. Propose mitigation strategies and rollback plans",
                "6. Flag areas that need human review",
                "",
                f"Write your risk assessment to `.egg-state/agent-outputs/{_identifier}-risk_analyst-output.json`.",
                "",
            ]
        )
    elif role_value.startswith("reviewer_"):
        # Delegate to the detailed review prompt with criteria and verdict format
        reviewer_type = role_value.replace("reviewer_", "", 1).replace("_", "-")
        review_prompt = _build_review_prompt(
            phase=phase,
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
            reviewer_type=reviewer_type,
            issue_number=issue_number,
            review_cycle=review_cycle + 1,
            prior_feedback=review_feedback,
            repo_path=repo_path,
        )
        if concurrent:
            review_prompt += "\n" + _build_brc_preamble(role_value, phase, repo=repo)
        return review_prompt
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


def _format_nack_summary(nack_details: list[dict]) -> str:
    """Format unresolved NACK details into a human-readable summary string."""
    return "; ".join(
        f"{n['reviewer']} NACKed {n['producer']}: {n.get('reason') or 'no reason given'}"
        for n in nack_details
    )


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
    review_feedback: str | None = None,
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

    # Build per-role prompts for concurrent phase execution.
    from egg_contracts.agent_roles import get_roles_for_phase as _get_roles_for_phase

    roles: list[AgentRole] = []
    for r in _get_roles_for_phase(phase_str, include_reviewers=True, repo=pipeline.repo):
        try:
            roles.append(AgentRole(r.value))
        except ValueError:
            # New roles not yet in orchestrator AgentRole — skip
            continue

    # Build a review graph filtered to only active roles so consensus
    # tracking doesn't wait for unspawned agents.
    from review_graph import ReviewGraph
    from review_graph import get_review_graph_for_phase as _get_graph

    full_graph = _get_graph(phase_str, repo=pipeline.repo)
    active_role_names = {r.value for r in roles}
    filtered_edges = [
        e
        for e in full_graph.edges
        if e.reviewer_role in active_role_names and e.producer_role in active_role_names
    ]
    filtered_graph = ReviewGraph(filtered_edges)

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
            concurrent=True,
            review_feedback=review_feedback,
            network_mode=gateway_mode,
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
        review_graph=filtered_graph,
        roles=roles,
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
                            started_at=datetime.now(UTC),
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
                        started_at=datetime.now(UTC),
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
                            agent.completed_at = datetime.now(UTC)
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
                completed_container_ids: set[str] = set()
                for agent in pe.agents:
                    if agent.status in (StateAgentStatus.RUNNING, StateAgentStatus.FAILED):
                        agent.status = StateAgentStatus.COMPLETE
                        agent.completed_at = datetime.now(UTC)
                        if agent.container_id:
                            completed_container_ids.add(agent.container_id)
                # Also mark containers as exited so the container monitor
                # doesn't find stale RUNNING entries and mark pipeline FAILED.
                # See issue #1294.
                for ci in pe.containers:
                    if (
                        ci.container_id in completed_container_ids
                        and ci.status == ContainerStatus.RUNNING
                    ):
                        ci.status = ContainerStatus.EXITED
                        # Synthetic: container will be stopped next, but 0
                        # reflects successful consensus completion.
                        ci.exit_code = 0
                        ci.exited_at = datetime.now(UTC)
                store.save_pipeline(pip)
        except Exception as track_err:
            logger.warning(
                "Failed to update agents to COMPLETE after consensus",
                pipeline_id=pipeline_id,
                error=str(track_err),
            )

    _demoted_agents: set[str] = set()

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
            # Recover pipeline if externally marked FAILED (issue #1273).
            # The container_monitor reconciliation thread may have marked the
            # pipeline FAILED while we were polling.  Now that consensus is
            # confirmed complete, restore the pipeline to RUNNING so stored
            # state matches the successful outcome.
            #
            # NOTE: consensus staleness is acceptable here.  The `consensus`
            # dict was fetched earlier in this loop iteration and is not
            # re-evaluated under the lock.  If consensus regressed between
            # the outer check and lock acquisition (extremely unlikely), the
            # next iteration of this monitoring loop will re-evaluate and
            # self-correct.
            if store is not None:
                try:
                    _current_pip = store.load_pipeline(pipeline_id)
                    if _current_pip.status == PipelineStatus.FAILED:
                        logger.warning(
                            "Pipeline externally marked FAILED but consensus is complete — recovering",
                            pipeline_id=pipeline_id,
                        )
                        with get_pipeline_state_lock(pipeline_id):
                            _current_pip = store.load_pipeline(pipeline_id)
                            if _current_pip.status == PipelineStatus.FAILED:
                                _current_pip.status = PipelineStatus.RUNNING
                                _current_pip.error = None
                                store.save_pipeline(_current_pip)
                except Exception as recovery_err:
                    logger.warning(
                        "External FAILED recovery check failed",
                        pipeline_id=pipeline_id,
                        error=str(recovery_err),
                    )

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

        # 3b. RC3: Stall demotion for dual-role agents.
        # If a dual-role agent has missed heartbeats for 5+ minutes,
        # demote its reviewer edges to ADVISORY so other agents can proceed.
        try:
            from health_monitor import get_health_monitor

            _hm = get_health_monitor()
            if _hm is not None:
                from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[import-not-found]  # noqa: I001

                _brc_tracker = get_peer_consensus_tracker(pipeline_id)
                if _brc_tracker is not None:
                    heartbeat_actions = _hm.check_heartbeats()
                    for hb_action in heartbeat_actions:
                        stalled_agent = hb_action.get("agent_id", "")
                        stall_elapsed = hb_action.get("elapsed_seconds", 0)
                        if (
                            stall_elapsed >= 300
                            and stalled_agent not in _demoted_agents
                            and _brc_tracker.graph.is_dual_role(stalled_agent)
                        ):
                            try:
                                _brc_tracker.handle_stall_demotion(
                                    stalled_agent,
                                    reason=f"Missed heartbeats for {stall_elapsed}s",
                                )
                                _demoted_agents.add(stalled_agent)
                            except Exception as demote_err:
                                logger.debug(
                                    "Stall demotion skipped",
                                    agent=stalled_agent,
                                    error=str(demote_err),
                                )
        except Exception as stall_err:
            logger.debug(
                "Stall demotion check failed",
                pipeline_id=pipeline_id,
                error=str(stall_err),
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
                    exited_at=datetime.now(UTC),
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

            # Before returning success, check the BRC approval matrix for
            # unresolved NACKs.  If reviewers NACKed but producers exited
            # without iterating, we must NOT report success — escalate to
            # HITL so a human can decide how to proceed.
            if consensus.get("has_unresolved_nacks"):
                nack_details = consensus.get("unresolved_nacks", [])
                nack_summary = _format_nack_summary(nack_details)
                logger.warning(
                    "All containers exited with unresolved NACKs",
                    pipeline_id=pipeline_id,
                    nack_count=len(nack_details),
                    nack_summary=nack_summary,
                )
                try:
                    pipeline.add_decision(
                        question=(
                            f"All agents exited but {len(nack_details)} NACK(s) remain "
                            f"unresolved: {nack_summary}. How to proceed?"
                        ),
                        options=["Retry phase", "Accept current state", "Abort phase"],
                        phase=pipeline.current_phase,
                    )
                except Exception:
                    logger.warning("Failed to create NACK escalation decision", exc_info=True)
                combined_logs += f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
                return 1, combined_logs

            return 0, combined_logs

        # 6. Consensus timeout
        if elapsed >= consensus_timeout:
            logger.warning(
                "Consensus timeout reached, falling back to container exit",
                pipeline_id=pipeline_id,
                timeout_minutes=consensus_timeout / 60,
            )
            # Let the BRC tracker handle the timeout first — it emits
            # role-aware events (CONSENSUS_FAILURE or CONSENSUS_TIMEOUT)
            # and returns whether it handled the situation.  Only fall
            # back to the generic CONSENSUS_TIMEOUT event and HITL
            # decision if BRC did not handle it.
            _brc_handled = False
            _brc_timeout_result = None
            try:
                from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[import-not-found]  # noqa: I001

                _brc_tracker = get_peer_consensus_tracker(pipeline_id)
                if _brc_tracker is not None:
                    _brc_timeout_result = _brc_tracker.handle_timeout()
                    _brc_handled = _brc_tracker.is_timeout_handled()
                    logger.info(
                        "BRC timeout handler result",
                        pipeline_id=pipeline_id,
                        action=_brc_timeout_result.get("action"),
                        brc_handled=_brc_handled,
                    )
            except Exception as e:
                logger.warning(
                    "BRC timeout check failed, falling back to HITL",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

            if (
                _brc_handled
                and _brc_timeout_result is not None
                and _brc_timeout_result.get("action") == "escalate"
            ):
                # BRC handled the timeout but requests escalation —
                # critical reviewers are unconfirmed.  Still need a HITL
                # decision so a human can intervene.
                try:
                    pipeline.add_decision(
                        question=(
                            f"BRC consensus failure: critical reviewers unconfirmed after "
                            f"{int(consensus_timeout / 60)} minutes. How to proceed?"
                        ),
                        options=["Continue waiting", "Accept current state", "Abort phase"],
                        phase=pipeline.current_phase,
                    )
                except Exception:
                    pass
            elif not _brc_handled:
                if _emit_event is not None:
                    _emit_event(
                        EventType.CONSENSUS_TIMEOUT,
                        pipeline_id,
                        data={
                            "timeout_minutes": consensus_timeout / 60,
                            "blocking_agents": consensus.get("blocking_agents", []),
                        },
                    )
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
                                exited_at=datetime.now(UTC),
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

            # After timeout, check the BRC approval matrix for unresolved
            # NACKs before declaring success.  Producers that exited without
            # addressing reviewer feedback should not be treated as passing.
            try:
                _final_consensus = executor.check_consensus()
            except Exception:
                logger.warning("Failed to check consensus at timeout", exc_info=True)
                _final_consensus = {}
            if _final_consensus.get("has_unresolved_nacks"):
                nack_details = _final_consensus.get("unresolved_nacks", [])
                nack_summary = _format_nack_summary(nack_details)
                logger.warning(
                    "Timeout with unresolved NACKs — returning failure",
                    pipeline_id=pipeline_id,
                    nack_count=len(nack_details),
                )
                combined_logs += f"\n--- UNRESOLVED NACKs ({len(nack_details)}) ---\n{nack_summary}"
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
                    started_at=datetime.now(UTC),
                    agent_role=agent_role,
                )
                phase_execution.containers.append(container_info)

                # Track agent execution
                agent_execution = AgentExecution(
                    role=agent_role,
                    status=AgentExecutionStatus.RUNNING,
                    container_id=spawned.container_info.container_id,
                    started_at=datetime.now(UTC),
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
            exited_at=datetime.now(UTC),
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
                        agent.completed_at = datetime.now(UTC)
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
        changed = False

        if contract_phases:
            contract.phases = contract_phases
            changed = True

        # Populate PR metadata from plan if available
        if result.pr_title:
            from egg_contracts.models import PRMetadata

            contract.pr = PRMetadata(
                title=result.pr_title,
                description=result.pr_description or "",
            )
            changed = True

        if changed:
            save_contract(contract, repo_path)
            task_count = sum(len(p.tasks) for p in contract.phases)
            logger.info(
                "Contract populated from plan",
                pipeline_id=pipeline_id,
                phase_count=len(contract.phases),
                task_count=task_count,
                has_pr_metadata=contract.pr is not None,
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


def _persist_phase_gate_resolution(
    repo_path: Path,
    pipeline_id: str,
    decision: HITLDecision,
    phase: str,
    issue_number: int | None = None,
) -> None:
    """Persist a phase-gate resolution to the contract and draft.

    After a human approves a phase gate, the resolution context needs to be
    visible to agents in the next phase.  This function:

    1. Adds the resolution as a HITL decision in the contract so next-phase
       agents see it when they load the contract.
    2. Appends a ``## HITL Resolution`` section to the phase draft file so
       agents reading the draft also see the human's decisions.

    See: #1295
    """
    # Extract structured context from JSON resolution, or use raw string
    resolution_context: str = ""
    raw = (decision.resolution or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                resolution_context = payload.get("context", "") or payload.get("feedback", "")
                if not resolution_context:
                    logger.debug(
                        "Phase gate approved without context, nothing to persist",
                        pipeline_id=pipeline_id,
                        phase=phase,
                    )
                    return
            else:
                resolution_context = raw
        except (json.JSONDecodeError, TypeError):
            resolution_context = raw

    if not resolution_context:
        logger.debug(
            "Phase gate resolution has no context to persist",
            pipeline_id=pipeline_id,
            phase=phase,
        )
        return

    # --- 1. Sync to contract ---
    try:
        from egg_contracts.loader import load_contract, save_contract
        from egg_contracts.models import Decision, DecisionOption, DecisionType

        contract_id: int | str = _pipeline_identifier(issue_number, pipeline_id)
        contract = load_contract(contract_id, repo_path)

        existing_questions = {d.question for d in contract.decisions}
        question_text = f"[Phase gate: {phase}] {decision.question}"

        if question_text not in existing_questions:
            # Determine next decision ID
            max_existing_id = 0
            for d in contract.decisions:
                try:
                    num = int(d.id.split("-")[1])
                    max_existing_id = max(max_existing_id, num)
                except (IndexError, ValueError):
                    pass

            contract_options = [
                DecisionOption(id=f"opt-{i + 1}", label=opt)
                for i, opt in enumerate(decision.options)
            ]

            contract_decision = Decision(
                id=f"decision-{max_existing_id + 1}",
                question=question_text,
                type=DecisionType.HITL,
                options=contract_options,
                resolved=True,
                resolution=resolution_context,
                resolved_by="human",
                resolved_at=decision.resolved_at,
            )
            contract.decisions.append(contract_decision)
            save_contract(contract, repo_path)
            logger.info(
                "Persisted phase gate resolution to contract",
                pipeline_id=pipeline_id,
                phase=phase,
            )
    except ImportError:
        logger.warning("egg_contracts not available, skipping phase gate contract sync")
    except Exception:
        logger.warning(
            "Failed to persist phase gate resolution to contract (continuing)",
            pipeline_id=pipeline_id,
            phase=phase,
            exc_info=True,
        )

    # --- 2. Append to draft ---
    try:
        draft_rel = _get_draft_path(phase, issue_number, pipeline_id)
        if draft_rel:
            draft_path = repo_path / draft_rel
            if draft_path.exists():
                existing = draft_path.read_text(encoding="utf-8")
                if "## HITL Resolution" not in existing:
                    section = (
                        f"\n\n## HITL Resolution\n\n"
                        f"The following was approved by a human reviewer at the "
                        f"{phase} phase gate:\n\n{resolution_context}\n"
                    )
                    draft_path.write_text(existing + section, encoding="utf-8")
                    logger.info(
                        "Appended HITL resolution to draft",
                        pipeline_id=pipeline_id,
                        phase=phase,
                        draft=draft_rel,
                    )
    except Exception:
        logger.warning(
            "Failed to append phase gate resolution to draft (continuing)",
            pipeline_id=pipeline_id,
            phase=phase,
            exc_info=True,
        )


def _run_pipeline(pipeline_id: str, repo_path: Path) -> None:
    """Run a pipeline by spawning containers for each phase.

    This runs in a background thread. For each phase it:
    1. Spawns agent containers via concurrent BRC execution
       (_run_concurrent_phase) for all phases.
    2. For reviewed phases (refine, implement, plan): reviewers participate
       in the BRC consensus protocol alongside workers, then the phase
       loops back with feedback if revision is needed.
    3. Advances to the next phase once approved.

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
    overseer_container_id: str | None = None
    health_monitor_instance = None
    health_monitor_timer: threading.Event | None = None
    poll_thread: threading.Thread | None = None

    try:
        store = get_state_store(repo_path)
        spawner = get_container_spawner()
        pipeline = store.load_pipeline(pipeline_id)
        run_created_at = pipeline.created_at
        pipeline_mode = "issue" if pipeline.issue_number is not None else "prompt"
        transitions = PHASE_TRANSITIONS

        # Map pipeline to gateway session mode.
        gateway_mode, detected_visibility = _compute_gateway_mode(pipeline)
        if not pipeline.network_mode and pipeline.repo:
            if detected_visibility is not None:
                logger.info(
                    "Auto-detected network mode from repo visibility",
                    repo=pipeline.repo,
                    visibility=detected_visibility,
                    gateway_mode=gateway_mode,
                )
            else:
                logger.warning(
                    "Could not detect repo visibility, defaulting to public mode",
                    repo=pipeline.repo,
                )

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
                # When the pipeline specifies a base_branch, pass it through
                # so the worktree is branched from that ref instead of the
                # repo's default branch.  Otherwise let the gateway resolve
                # the remote default branch per-repo (see #860).
                wt_result = spawner.gateway.create_worktrees(
                    container_id=worktree_id,
                    repos=wt_repos,
                    uid=host_uid,
                    gid=host_gid,
                    base_branch=pipeline.base_branch,
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
                gateway_mode=gateway_mode,
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

                # Write pre-generated drafts for short-flow pipelines so the
                # existing plan parser can populate the contract with tasks.
                if pipeline.analysis or pipeline.plan:
                    drafts_dir = worktree_repo_path / ".egg-state" / "drafts"
                    drafts_dir.mkdir(parents=True, exist_ok=True)

                    if pipeline.analysis:
                        analysis_rel = _get_draft_path(
                            "refine",
                            issue_number=pipeline.issue_number,
                            pipeline_id=pipeline_id,
                        )
                        if analysis_rel:
                            (worktree_repo_path / analysis_rel).write_text(pipeline.analysis)
                            logger.info(
                                "Wrote pre-generated analysis draft",
                                pipeline_id=pipeline_id,
                                path=analysis_rel,
                            )

                    if pipeline.plan:
                        plan_rel = _get_draft_path(
                            "plan",
                            issue_number=pipeline.issue_number,
                            pipeline_id=pipeline_id,
                        )
                        if plan_rel:
                            (worktree_repo_path / plan_rel).write_text(pipeline.plan)
                            logger.info(
                                "Wrote pre-generated plan draft",
                                pipeline_id=pipeline_id,
                                path=plan_rel,
                            )

                            # Populate the contract from the plan's yaml-tasks appendix
                            _populate_contract_from_plan(
                                worktree_repo_path,
                                pipeline_id,
                                pipeline_mode,
                                pipeline.issue_number,
                            )

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
                push_succeeded = True
                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        push_succeeded = spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                            mode=gateway_mode,
                        )
                    except Exception as push_err:
                        push_succeeded = False
                        logger.warning(
                            "Failed to push statefiles after contract init (continuing)",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.contract_synced = push_succeeded
                    if push_succeeded:
                        # Clear analysis/plan from pipeline state now that they've
                        # been written to draft files and pushed — avoids re-serializing
                        # potentially large text blobs on every subsequent save.
                        pipeline.analysis = None
                        pipeline.plan = None
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

        # Check for feedback preserved by the recovery path in start_pipeline
        # or by the inline request_changes handler.  When either stores
        # reviewer feedback in phase_execution.hitl_feedback, we read it
        # here so it can be forwarded to the re-running agents.
        _hitl_review_feedback: str | None = None
        try:
            with get_pipeline_state_lock(pipeline_id):
                _recovery_pipeline = store.load_pipeline(pipeline_id)
                _recovery_phase = _recovery_pipeline.get_phase_execution(
                    _recovery_pipeline.current_phase
                )
                if _recovery_phase.hitl_feedback:
                    _hitl_review_feedback = _recovery_phase.hitl_feedback
                    _recovery_phase.hitl_feedback = None
                    store.save_pipeline(_recovery_pipeline)
        except Exception as e:
            logger.debug("Failed to read hitl_feedback from recovery path", error=str(e))

        # Spawn overseer container for pipeline health monitoring.
        # The overseer runs without repo access and monitors health via
        # the orchestrator API.  Spawned early (before first phase) so it
        # can observe the entire pipeline lifecycle.
        if pipeline.config.overseer_enabled:
            try:
                overseer_result = spawner.spawn_overseer_container(
                    pipeline_id=pipeline_id,
                    issue_number=pipeline.issue_number,
                    mode=gateway_mode,
                    poll_interval=pipeline.config.overseer_poll_interval_seconds,
                    decision_model=pipeline.config.overseer_decision_maker_model,
                    repos=pipeline_repos if pipeline_repos else None,
                    certs_volume=certs_volume,
                )
                overseer_container_id = overseer_result.container_info.container_id
                logger.info(
                    "Overseer container spawned",
                    pipeline_id=pipeline_id,
                    container_id=overseer_container_id[:12],
                )
            except ContainerSpawnError as e:
                # Non-fatal: pipeline can run without overseer monitoring
                logger.warning(
                    "Failed to spawn overseer container (continuing without monitoring)",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

        # Initialize the Tier 1 health monitor so deterministic tripwires
        # (heartbeat timeout, container exit, repeated errors, message rate,
        # progress stall) fire during pipeline execution.  The monitor
        # subscribes to EventBus events reactively, but check_heartbeats()
        # and check_progress() need periodic polling.
        try:
            from events import get_event_bus
            from health_monitor import init_health_monitor

            health_monitor_instance = init_health_monitor(
                get_event_bus(), pipeline_id, pipeline.config
            )

            # Start a background polling thread for time-based tripwires
            health_monitor_timer = threading.Event()

            overseer_respawn_count = 0
            max_overseer_respawns = pipeline.config.overseer_max_respawns

            def _health_monitor_poll(monitor, stop_event: threading.Event, interval: float = 30.0):
                nonlocal overseer_container_id, overseer_respawn_count
                while not stop_event.is_set():
                    try:
                        monitor.check_tripwires()
                    except Exception as poll_err:
                        logger.debug(
                            "Health monitor poll error",
                            pipeline_id=pipeline_id,
                            error=str(poll_err),
                        )

                    # Check overseer liveness and respawn if it exited mid-pipeline.
                    # Note: `pipeline` is captured from initial load — config values
                    # (poll_interval, decision_model, etc.) won't reflect mid-run changes.
                    # This is fine because pipeline config is immutable after start.
                    overseer_container_id, overseer_respawn_count = _check_and_respawn_overseer(
                        spawner=spawner,
                        store=store,
                        pipeline_id=pipeline_id,
                        pipeline=pipeline,
                        overseer_container_id=overseer_container_id,
                        overseer_respawn_count=overseer_respawn_count,
                        max_overseer_respawns=max_overseer_respawns,
                        gateway_mode=gateway_mode,
                        pipeline_repos=pipeline_repos,
                        certs_volume=certs_volume,
                    )

                    stop_event.wait(interval)

            poll_thread = threading.Thread(
                target=_health_monitor_poll,
                args=(health_monitor_instance, health_monitor_timer),
                daemon=True,
                name=f"health-monitor-{pipeline_id[:8]}",
            )
            poll_thread.start()
            logger.info(
                "Health monitor initialized",
                pipeline_id=pipeline_id,
            )
        except Exception as hm_err:
            # Non-fatal: pipeline can run without Tier 1 monitoring
            logger.warning(
                "Failed to initialize health monitor (continuing without Tier 1 monitoring)",
                pipeline_id=pipeline_id,
                error=str(hm_err),
            )

        # Honor start_phase config — skip earlier phases
        if pipeline.config.start_phase:
            target_phase = PipelinePhase(pipeline.config.start_phase)
            if target_phase != pipeline.current_phase:
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.current_phase = target_phase
                    store.save_pipeline(pipeline)
                logger.info(
                    "Skipping to start_phase",
                    pipeline_id=pipeline_id,
                    start_phase=target_phase.value,
                )

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
                    phase_execution.started_at = datetime.now(UTC)
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
            else:
                generated_branch = f"egg/{pipeline_id}/work"
                sandbox_env["EGG_BRANCH"] = generated_branch
                # Persist the generated branch so the PR phase can use it
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    if not pipeline.branch:
                        pipeline.branch = generated_branch
                        store.save_pipeline(pipeline)
                        logger.info(
                            "Recorded generated branch on pipeline",
                            pipeline_id=pipeline_id,
                            branch=generated_branch,
                        )
            if pipeline.prompt:
                sandbox_env["EGG_PIPELINE_PROMPT"] = pipeline.prompt

            if pipeline.repo:
                repos = [pipeline.repo]
                sandbox_env["EGG_REPO"] = pipeline.repo
            else:
                repos = []

            phase_failed = False
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
                    phase_execution.work_started_at = datetime.now(UTC)
                    store.save_pipeline(pipeline)

                # Ensure contract and statefiles exist before PR creation
                # (safety net for short-flow pipelines where initial push
                # may have failed).
                if not _ensure_statefiles_on_branch(worktree_repo_path, pipeline):
                    logger.warning(
                        "Contract reconciliation failed — PR may be missing contract",
                        pipeline_id=pipeline_id,
                    )

                # Push latest commits before creating PR
                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                            mode=gateway_mode,
                        )
                    except Exception as push_err:
                        logger.error(
                            "Pre-PR push failed — PR may reference stale code",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

                pr_url = _auto_create_pr(
                    pipeline, worktree_repo_path, spawner, gateway_mode=gateway_mode
                )

                if pr_url:
                    with get_pipeline_state_lock(pipeline_id):
                        pipeline = store.load_pipeline(pipeline_id)
                        phase_execution = pipeline.get_phase_execution(current_phase)
                        phase_execution.artifacts = {"pr_url": pr_url}
                        store.save_pipeline(pipeline)
                else:
                    _handle_pr_creation_failure(pipeline_id, current_phase, store)
                    phase_failed = True

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

                        # Reset status to RUNNING at cycle start so that a
                        # previous cycle's FAILED status doesn't persist and
                        # cause _derive_subphase_status() to misreport (see
                        # issue #1178).
                        phase_execution.status = PipelineStatus.RUNNING
                        pipeline.status = PipelineStatus.RUNNING

                        # Record when actual agent work begins (excludes sandbox setup
                        # and HITL waiting time from the phase duration).
                        phase_execution.work_started_at = datetime.now(UTC)

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

                    # 1. Spawn workers — always use concurrent BRC execution.
                    logger.info(
                        "Spawning concurrent phase execution",
                        pipeline_id=pipeline_id,
                        phase=current_phase,
                        review_cycle=review_cycle,
                        mode=gateway_mode,
                    )

                    # Read HITL feedback stored by the inline request_changes
                    # handler or the AWAITING_HUMAN recovery path, and clear it
                    # so it's only forwarded once.
                    _phase_review_feedback: str | None = None
                    if _hitl_review_feedback:
                        _phase_review_feedback = _hitl_review_feedback
                        _hitl_review_feedback = None
                    else:
                        # Re-read from persisted state in case the inline path
                        # stored feedback and looped back via continue.
                        try:
                            with get_pipeline_state_lock(pipeline_id):
                                _fb_pipeline = store.load_pipeline(pipeline_id)
                                _fb_phase = _fb_pipeline.get_phase_execution(current_phase)
                                if _fb_phase.hitl_feedback:
                                    _phase_review_feedback = _fb_phase.hitl_feedback
                                    _fb_phase.hitl_feedback = None
                                    store.save_pipeline(_fb_pipeline)
                        except Exception as e:
                            logger.debug("Failed to read hitl_feedback for phase", error=str(e))

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
                            review_feedback=_phase_review_feedback,
                        )
                    except ContainerSpawnError as e:
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
                            if phase_execution.cycle_timings:
                                phase_execution.cycle_timings[-1].completed_at = datetime.now(UTC)
                            phase_execution.status = PipelineStatus.FAILED
                            phase_execution.error = str(e)
                            phase_execution.completed_at = datetime.now(UTC)
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
                                phase_execution.cycle_timings[-1].completed_at = datetime.now(UTC)
                            phase_execution.status = PipelineStatus.FAILED
                            phase_execution.error = error_msg
                            phase_execution.completed_at = datetime.now(UTC)
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

                    # 2. Read tester gap findings (concurrent phases include a tester).
                    # Only read when the phase succeeded — a failed phase may
                    # have left stale output from a previous cycle on disk.
                    if not phase_failed:
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

                    # Reviewers are handled within the BRC consensus protocol
                    # (see issue #1178) — advance to next phase.
                    break

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
                            mode=gateway_mode,
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
                phase_execution.completed_at = datetime.now(UTC)

                store.save_pipeline(pipeline)  # Persist phase completion before HITL gate

            # Report phase completion to collaborator
            report_pipeline_status(
                pipeline,
                event_type="phase.completed",
                message=f"Phase {current_phase.value} completed",
            )
            _emit_pipeline_event(pipeline, "phase.completed")

            # Sync worktree with remote before post-phase modifications
            # so that agent-pushed commits (including plan drafts) are
            # incorporated.  This must run BEFORE _populate_contract_from_plan
            # and _sync_pipeline_decisions_to_contract — otherwise
            # git reset --hard in _sync_worktree_with_remote would revert
            # their on-disk modifications.  Running the sync first also
            # ensures _populate_contract_from_plan can read agent-produced
            # draft files that only exist on the remote.
            if pipeline.branch and worktree_repo_path != repo_path:
                _sync_worktree_with_remote(
                    spawner, pipeline_id, worktree_repo_path, gateway_mode=gateway_mode
                )

            # After plan phase: populate contract with task structure.
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
                        mode=gateway_mode,
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
                # Check for an existing pending phase_gate decision for this
                # phase.  A prior agent-exit event may
                # have already created one — creating a duplicate confuses the
                # human reviewer.  See #1152.
                existing_pending_gate = any(
                    d.decision_type == "phase_gate"
                    and d.phase == current_phase
                    and d.status == DecisionStatus.PENDING
                    for d in pipeline.decisions
                )

                if existing_pending_gate:
                    logger.info(
                        "HITL gate: reusing existing pending phase_gate decision",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                    )
                    # Find the existing decision to wait on
                    dq = get_decision_queue(pipeline_id, repo_path)
                    decision = next(
                        d
                        for d in reversed(pipeline.decisions)
                        if d.decision_type == "phase_gate"
                        and d.phase == current_phase
                        and d.status == DecisionStatus.PENDING
                    )
                else:
                    draft_content = _read_phase_draft(
                        worktree_repo_path,
                        current_phase.value,
                        issue_number=pipeline.issue_number,
                        pipeline_id=pipeline_id,
                    )
                    phase_label = (
                        "analysis" if current_phase.value == "refine" else current_phase.value
                    )

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

                    # Detect whether the draft changed compared to the
                    # previous phase_gate decision for this phase (if any).
                    _content_changed: bool | None = None
                    _prev_gate = next(
                        (
                            d
                            for d in reversed(pipeline.decisions)
                            if d.decision_type == "phase_gate"
                            and d.phase == current_phase
                            and d.status == DecisionStatus.RESOLVED
                        ),
                        None,
                    )
                    if _prev_gate is not None:
                        _content_changed = draft_content != _prev_gate.context

                    dq = get_decision_queue(pipeline_id, repo_path)
                    decision = dq.queue_decision(
                        question=question,
                        context=draft_content,
                        options=["approve", "request changes"],
                        decision_type="phase_gate",
                        phase=current_phase,
                        content_changed=_content_changed,
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
                            # Store feedback so the re-running agents receive it.
                            phase_execution.hitl_feedback = _revision_feedback

                            # Reset containers/agents/artifacts so the re-run
                            # starts clean, resetting the same container/agent/
                            # artifact fields that the recovery path resets.
                            phase_execution.containers = []
                            phase_execution.agents = []
                            phase_execution.artifacts = {}
                            phase_execution.review_cycles = 0

                            # Clear message store and consensus tracker so the
                            # re-run doesn't short-circuit on stale CONSENSUS_CONFIRMED
                            # messages from the previous run (issue #1296).
                            from routes.phases import _clear_concurrent_state

                            _clear_concurrent_state(pipeline_id)

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
                        phase_execution.completed_at = datetime.now(UTC)
                    store.save_pipeline(pipeline)

                # Persist phase gate resolution to contract and draft so
                # next-phase agents can see the human's decisions.  #1295
                _persist_phase_gate_resolution(
                    worktree_repo_path,
                    pipeline_id,
                    resolved_decision,
                    current_phase.value,
                    pipeline.issue_number,
                )

                # Commit and push updated statefiles (contract + draft with resolution)
                try:
                    _commit_statefiles_to_worktree(
                        worktree_repo_path,
                        f"Persist HITL resolution after {current_phase.value} phase gate",
                    )
                except subprocess.CalledProcessError as git_err:
                    logger.warning(
                        "Failed to commit statefiles after phase gate resolution (continuing)",
                        pipeline_id=pipeline_id,
                        error=str(git_err),
                    )

                if pipeline.branch and worktree_repo_path != repo_path:
                    try:
                        spawner.gateway.push_worktree_branch(
                            pipeline_id=pipeline_id,
                            repo_path=str(worktree_repo_path),
                            branch=pipeline.branch,
                            mode=gateway_mode,
                        )
                    except Exception as push_err:
                        logger.warning(
                            "Failed to push statefiles after phase gate resolution (continuing)",
                            pipeline_id=pipeline_id,
                            error=str(push_err),
                        )

            # Determine next phase
            next_phases = transitions.get(current_phase, [])

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
        # Stop health monitor polling and unsubscribe from events
        if health_monitor_timer is not None:
            health_monitor_timer.set()
        if poll_thread is not None:
            poll_thread.join(timeout=5)
        if health_monitor_instance is not None:
            try:
                health_monitor_instance.stop()
                logger.info("Health monitor stopped", pipeline_id=pipeline_id)
            except Exception as hm_stop_err:
                logger.debug(
                    "Failed to stop health monitor",
                    pipeline_id=pipeline_id,
                    error=str(hm_stop_err),
                )

        # Clean up progress store for this pipeline
        try:
            from progress_store import get_progress_store

            progress_store = get_progress_store()
            if progress_store is not None:
                progress_store.clear(pipeline_id)
        except Exception as ps_err:
            logger.debug(
                "Failed to clear progress store",
                pipeline_id=pipeline_id,
                error=str(ps_err),
            )

        # Stop overseer container if it was spawned
        if overseer_container_id:
            try:
                _spawner = get_container_spawner()
                _spawner.stop_agent_container(
                    overseer_container_id,
                    cleanup_session=True,
                    timeout=10,
                )
                logger.info(
                    "Overseer container stopped",
                    pipeline_id=pipeline_id,
                    container_id=overseer_container_id[:12],
                )
            except Exception as overseer_err:
                logger.debug(
                    "Failed to stop overseer container (may have already exited)",
                    pipeline_id=pipeline_id,
                    error=str(overseer_err),
                )

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

        # Compute gateway mode for session operations in the recovery path
        _gw_mode, _gw_vis = _compute_gateway_mode(pipeline)

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
                        phase_execution.completed_at = datetime.now(UTC)

                    # Persist phase gate resolution so next-phase agents see it.  #1295
                    if phase_gate_decisions:
                        _persist_phase_gate_resolution(
                            repo_path,
                            pipeline_id,
                            phase_gate_decisions[0],
                            pipeline.current_phase.value,
                            pipeline.issue_number,
                        )

                        # Commit statefiles so worktrees created by _run_pipeline
                        # include the contract/draft changes.
                        try:
                            _commit_statefiles_to_worktree(
                                repo_path,
                                f"Persist HITL resolution after {pipeline.current_phase.value} phase gate",
                            )
                        except subprocess.CalledProcessError as git_err:
                            logger.warning(
                                "Failed to commit statefiles after phase gate resolution (continuing)",
                                pipeline_id=pipeline_id,
                                error=str(git_err),
                            )

                        # Push if this repo tracks a remote branch
                        if pipeline.branch:
                            try:
                                _spawner = get_container_spawner()
                                _spawner.gateway.push_worktree_branch(
                                    pipeline_id=pipeline_id,
                                    repo_path=str(repo_path),
                                    branch=pipeline.branch,
                                    mode=_gw_mode,
                                )
                            except Exception as push_err:
                                logger.warning(
                                    "Failed to push statefiles after phase gate resolution (continuing)",
                                    pipeline_id=pipeline_id,
                                    error=str(push_err),
                                )

                    from routes.phases import PHASE_TRANSITIONS

                    transitions = PHASE_TRANSITIONS
                    current_phase = pipeline.current_phase
                    next_phases = transitions.get(current_phase, [])

                    if not next_phases:
                        # Terminal phase — pipeline complete.
                        # Bump created_at so any lingering old _run_pipeline
                        # thread (e.g. stuck in its finally block) detects the
                        # recreation and exits without double-cleaning up.
                        pipeline.status = PipelineStatus.COMPLETE
                        pipeline.created_at = datetime.now(UTC)
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

                    # Clear stale consensus state so re-run doesn't
                    # short-circuit (issue #1296).
                    from routes.phases import _clear_concurrent_state

                    _clear_concurrent_state(pipeline_id)

                    # Preserve the reviewer's feedback so the re-launched
                    # _run_pipeline thread can pass it to the agent.
                    if revision_feedback:
                        phase_execution.hitl_feedback = revision_feedback

                pipeline.error = None
                pipeline.created_at = datetime.now(UTC)
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
                pipeline.created_at = datetime.now(UTC)

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
