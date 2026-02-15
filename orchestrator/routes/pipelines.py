"""
Pipeline CRUD endpoints for egg-orchestrator.
"""

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

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
    from ..decision_queue import DecisionTimeoutError, get_decision_queue
    from ..docker_client import DockerClientError
    from ..models import AgentRole, Pipeline, PipelineStatus, ReviewVerdict
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
    from decision_queue import DecisionTimeoutError, get_decision_queue  # type: ignore
    from docker_client import DockerClientError  # type: ignore
    from models import AgentRole, Pipeline, PipelineStatus, ReviewVerdict  # type: ignore
    from state_store import (  # type: ignore
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStore,
        StateStoreError,
        StateValidationError,
        get_pipeline_state_lock,
        get_state_store,
    )

logger = get_logger("orchestrator.pipelines")

# Base directory where the gateway creates per-pipeline worktrees.
# Must match the gateway's WORKTREE_BASE_DIR and docker-compose volume mounts.
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")

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

    mode = data.get("mode", "issue")

    network_mode = data.get("network_mode")
    if network_mode is not None and network_mode not in ("public", "private"):
        return make_error_response(
            f"Invalid network_mode: {network_mode!r} (must be 'public' or 'private')"
        )

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
                network_mode=network_mode,
            )

            # Contract creation is deferred to _run_pipeline so it writes
            # into the per-pipeline worktree instead of the main repo.

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
        if pipeline.status in (PipelineStatus.CANCELLED, PipelineStatus.FAILED):
            spawner = get_container_spawner()
            try:
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
        store, _pipeline = _resolve_pipeline(pipeline_id, repo_path)

        # Clean up any running containers for this pipeline
        spawner = get_container_spawner()
        try:
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


def _get_unified_criteria(phase: str) -> str:
    """Return unified review criteria for the given phase."""
    if phase == "refine":
        return (
            "### 1. Problem Understanding\n"
            "- Does the analysis correctly identify the core problem or feature request?\n"
            "- Is the current behavior (if applicable) accurately described?\n"
            "- Are the goals and desired outcomes clear?\n\n"
            "### 2. Research Quality\n"
            "- Has the agent explored the relevant parts of the codebase?\n"
            "- Are existing patterns and conventions identified?\n"
            "- Is the technical context accurate?\n\n"
            "### 3. Options Analysis\n"
            "- Are the options meaningfully different?\n"
            "- Are trade-offs clearly articulated for each option?\n"
            "- Is the reasoning logical and well-founded?\n\n"
            "### 4. Constraints and Dependencies\n"
            "- Are technical constraints identified (performance, compatibility, etc.)?\n"
            "- Are dependencies on other code or systems noted?\n"
            "- Are potential risks or complications surfaced?\n\n"
            "### 5. Open Questions\n"
            "- Are open questions specific enough for a human to answer?\n"
            "- Do questions address genuine ambiguities?\n"
            "- Are questions actionable?\n\n"
            "### 6. Recommendation Quality\n"
            "- Is there a clear recommended approach?\n"
            "- Is the recommendation justified with specific reasons?\n"
            "- Does the recommendation align with the analysis findings?\n"
        )
    elif phase == "plan":
        return (
            "### 1. Alignment with Analysis\n"
            "- Does the plan implement the recommended approach from the analysis?\n"
            "- Are all requirements addressed?\n"
            "- If the plan deviates from the analysis, is the reason explained?\n\n"
            "### 2. Task Breakdown\n"
            "- Are tasks specific and actionable?\n"
            "- Are tasks appropriately sized (not too large, not too granular)?\n"
            "- Is the order of tasks logical?\n\n"
            "### 3. Acceptance Criteria\n"
            "- Does each task have clear, verifiable acceptance criteria?\n"
            "- Are acceptance criteria specific (not vague)?\n"
            "- Can the criteria be objectively verified?\n\n"
            "### 4. Dependencies\n"
            "- Are dependencies between tasks identified?\n"
            "- Is the phase structure logical?\n"
            "- Are external dependencies (libraries, APIs, etc.) noted?\n\n"
            "### 5. Test Strategy\n"
            "- Is there a test strategy section?\n"
            "- Does it cover unit tests, integration tests as appropriate?\n"
            "- Are edge cases and error scenarios considered?\n\n"
            "### 6. Risk Assessment\n"
            "- Are potential risks identified?\n"
            "- Is there a rollback plan or mitigation strategy?\n"
            "- Are technical challenges acknowledged?\n"
        )
    else:
        return (
            "### 1. Task Completion\n"
            "- Are all tasks from the plan implemented?\n"
            "- Does each implementation match its acceptance criteria?\n"
            "- Are all files listed in the plan modified or created?\n\n"
            "### 2. Code Quality\n"
            "- Does the code follow existing patterns in the codebase?\n"
            "- Is the code readable and maintainable?\n\n"
            "### 3. Security\n"
            "- Are there any injection vulnerabilities (SQL, command, XSS)?\n"
            "- Is input validation present at trust boundaries?\n"
            "- Are credentials properly handled (not hardcoded)?\n\n"
            "### 4. Error Handling\n"
            "- Are errors handled gracefully?\n"
            "- Are failure paths considered?\n\n"
            "### 5. Testing\n"
            "- Are there tests for new functionality?\n"
            "- Do existing tests still pass?\n"
            "- Are edge cases covered?\n\n"
            "### 6. Documentation\n"
            "- Are significant changes documented?\n"
        )


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
        "- Resource leaks (connections, file handles, memory)\n\n"
        "### Robustness\n"
        "- Missing input validation at trust boundaries\n"
        "- Unhandled exceptions that could crash the system\n"
        "- Missing retry logic for transient failures\n"
        "- Inadequate timeouts for external calls\n\n"
        "### Design\n"
        "- Violations of existing codebase patterns\n"
        "- Breaking changes to public interfaces\n"
        "- Tight coupling that will hinder future changes\n"
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
        "- Are questions actionable?\n\n"
        "### 6. Recommendation Quality\n"
        "- Is there a clear recommended approach?\n"
        "- Is the recommendation justified with specific reasons?\n"
        "- Does the recommendation align with the analysis findings?\n"
    )


def _get_plan_review_criteria() -> str:
    """Return review criteria for the dedicated plan reviewer."""
    return (
        "### 1. Task Breakdown\n"
        "- Are tasks discrete, actionable, and properly scoped?\n"
        "- Is each task small enough to implement in a single pass?\n"
        "- Are task boundaries clear (no overlapping responsibilities)?\n\n"
        "### 2. Acceptance Criteria\n"
        "- Does each task have clear, testable acceptance criteria?\n"
        "- Are criteria specific enough to verify completion?\n"
        "- Do criteria cover both happy path and edge cases?\n\n"
        "### 3. Dependency Ordering\n"
        "- Are task dependencies correctly identified?\n"
        "- Is the ordering logical (foundations before features)?\n"
        "- Are there opportunities for parallelism that are missed?\n\n"
        "### 4. Risk Assessment\n"
        "- Are technical risks identified (security, performance, compatibility)?\n"
        "- Are mitigation strategies concrete and actionable?\n"
        "- Is the rollback plan realistic?\n\n"
        "### 5. Test Strategy\n"
        "- Is the test strategy appropriate for the scope of changes?\n"
        "- Are both unit and integration tests considered?\n"
        "- Are test scenarios aligned with acceptance criteria?\n\n"
        "### 6. Completeness\n"
        "- Does the plan cover all aspects of the original request?\n"
        "- Are documentation updates included where needed?\n"
        "- Are there any obvious gaps or missing tasks?\n"
    )


def _get_review_criteria_for_type(
    reviewer_type: str, phase: str, repo_path: str | None = None
) -> str:
    """Dispatch to the correct criteria function based on reviewer type."""
    if reviewer_type == "unified":
        return _get_unified_criteria(phase)
    elif reviewer_type == "agent-design":
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
        return _get_unified_criteria(phase)


def _get_reviewer_scope_preamble(reviewer_type: str, phase: str) -> str:
    """Return a scope preamble that tells the reviewer what to focus on."""
    if reviewer_type == "unified":
        return f"This is a **unified review** of the {phase} phase output."
    elif reviewer_type == "agent-design":
        return (
            "This is a specialized **agent-mode design review**. Focus ONLY on "
            "agent-mode design principles. Do NOT review general code quality, "
            "security, or correctness — other reviewers handle those.\n\n"
            "**Only flag issues if you find clear agent-mode design anti-patterns.** "
            "If the output has no agent-mode concerns, approve it."
        )
    elif reviewer_type == "code":
        return (
            "This is a **comprehensive code review**. Focus on security, correctness, "
            "and robustness. Agent-mode design alignment is handled by another reviewer."
        )
    elif reviewer_type == "contract":
        return (
            "This is a **contract verification review**. Verify that the implementation "
            "matches the contract and all acceptance criteria are met. Do NOT review "
            "general code quality or security — other reviewers handle those."
        )
    elif reviewer_type == "refine":
        return (
            "This is a **refine phase review**. Focus on the quality and completeness "
            "of the analysis produced during the refine phase. Evaluate problem "
            "understanding, codebase research, options analysis, and the recommended "
            "approach. Agent-mode design alignment is handled by another reviewer."
        )
    elif reviewer_type == "plan":
        return (
            "This is a **plan phase review**. Focus on the quality and completeness "
            "of the implementation plan. Evaluate task breakdown, acceptance criteria, "
            "dependency ordering, risk assessment, and test strategy. Agent-mode "
            "design alignment is handled by another reviewer."
        )
    return ""


def _verdict_path_for_type(
    phase: str,
    reviewer_type: str,
    pipeline_mode: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str:
    """Return the relative verdict file path for a given reviewer type.

    For issue mode, uses issue number as prefix (e.g., 123-refine-unified-review.json).
    For local mode, uses pipeline_id as prefix (e.g., local-abc12345-refine-unified-review.json).
    """
    if pipeline_mode == "local":
        prefix = pipeline_id if pipeline_id else "local"
        return f".egg-state/reviews/{prefix}-{phase}-{reviewer_type}-review.json"
    else:
        return f".egg-state/reviews/{issue_number}-{phase}-{reviewer_type}-review.json"


def _get_draft_path(
    phase: str,
    pipeline_mode: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str | None:
    """Return relative path to the draft file for a phase.

    For issue mode, uses issue number as prefix (e.g., 123-analysis.md).
    For local mode, uses pipeline_id as prefix (e.g., local-abc12345-analysis.md).
    """
    is_local = pipeline_mode == "local"
    if is_local:
        prefix = pipeline_id if pipeline_id else "local"
        if phase == "refine":
            return f".egg-state/drafts/{prefix}-analysis.md"
        elif phase == "implement":
            return None
        else:
            return f".egg-state/drafts/{prefix}-{phase}.md"
    else:
        if phase == "refine":
            return f".egg-state/drafts/{issue_number}-analysis.md"
        elif phase == "implement":
            return None
        else:
            return f".egg-state/drafts/{issue_number}-{phase}.md"


def _read_phase_draft(
    repo_path: Path,
    phase: str,
    pipeline_mode: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
    max_chars: int = 32000,
) -> str:
    """Read draft file contents. Truncates at max_chars."""
    draft_rel = _get_draft_path(phase, pipeline_mode, issue_number, pipeline_id)
    if not draft_rel:
        return f"(No draft file for {phase} phase)"
    draft_path = repo_path / draft_rel
    if not draft_path.exists():
        return f"(Draft file not found: {draft_rel})"
    content = draft_path.read_text(encoding="utf-8")
    if len(content) > max_chars:
        return content[:max_chars] + f"\n\n... (truncated, {len(content)} chars total)"
    return content


def _build_review_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    reviewer_type: str = "unified",
    issue_number: int | None = None,
    review_cycle: int = 1,
    prior_feedback: str | None = None,
    repo_path: str | None = None,
) -> str:
    """Build a review prompt for the reviewer agent.

    Tells the reviewer to evaluate the draft for the given phase and write
    a typed verdict JSON file to .egg-state/reviews/.
    """
    draft_path = _get_draft_path(phase, pipeline_mode, issue_number, pipeline_id)

    verdict_path = _verdict_path_for_type(
        phase, reviewer_type, pipeline_mode, issue_number, pipeline_id
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

    if draft_path:
        lines.append(f"1. Read the draft at `{draft_path}`")
    else:
        lines.append(
            "1. Review the implementation using `git log --oneline -10` "
            "and `git diff HEAD~10..HEAD`"
        )
    lines.append("2. Evaluate it against the criteria below")
    lines.append(f"3. Write your verdict to `{verdict_path}` as JSON")
    lines.append("4. Commit the verdict file")
    lines.append("")

    # Review criteria
    lines.append("## Review Criteria\n")
    lines.append(_get_review_criteria_for_type(reviewer_type, phase, repo_path=repo_path))
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
    lines.append('  "summary": "Brief summary of findings",')
    lines.append('  "feedback": "Detailed feedback if needs_revision, empty if approved",')
    lines.append('  "timestamp": "ISO 8601 timestamp"')
    lines.append("}")
    lines.append("```\n")
    lines.append(
        "If the work meets all criteria, set verdict to `approved`. "
        "If significant issues remain, set verdict to `needs_revision` "
        "and provide actionable feedback."
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
    reviewer_type: str = "unified",
    pipeline_mode: str = "local",
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> ReviewVerdict | None:
    """Read a typed review verdict JSON from the repo.

    Returns None if the file is missing or malformed (treated as approved
    for graceful degradation).
    """
    verdict_rel = _verdict_path_for_type(
        phase, reviewer_type, pipeline_mode, issue_number, pipeline_id
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


def _aggregate_review_verdicts(
    verdicts: dict[str, ReviewVerdict | None],
) -> tuple[str, str]:
    """Aggregate multiple typed review verdicts into an overall result.

    Returns:
        (overall_verdict, combined_feedback) where overall_verdict is
        "approved" or "needs_revision". Any needs_revision → overall
        needs_revision. Missing/None verdicts are treated as approved.
    """
    overall = "approved"
    feedback_sections: list[str] = []

    for reviewer_type, verdict in verdicts.items():
        if verdict is None:
            continue
        if verdict.verdict == "needs_revision":
            overall = "needs_revision"
            section = f"### {reviewer_type} reviewer\n"
            if verdict.feedback:
                section += verdict.feedback
            elif verdict.summary:
                section += verdict.summary
            feedback_sections.append(section)

    combined = "\n\n".join(feedback_sections) if feedback_sections else ""
    return overall, combined


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
) -> str:
    """Build a phase-specific prompt for the sandbox Claude invocation.

    Follows a structured prompt format:
    Context → Task → Restrictions → Completion.  Adapted for the
    orchestrator (local mode has no GitHub issue, contract, or PR).
    """
    is_local = pipeline_mode == "local"

    # --- Context header ---
    lines = [f"You are in the **{phase}** phase of the SDLC pipeline.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append(f"Phase: {phase}")
    lines.append(f"Mode: {pipeline_mode}")
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
        lines.append(
            "The reviewer found issues with your previous draft. "
            "Address the feedback below and revise your draft **in-place** "
            "(overwrite the same file).\n"
        )
        lines.append(review_feedback)
        lines.append("")

    # --- Task description ---
    if prompt:
        lines.append("## Task Description\n")
        lines.append(prompt)
        lines.append("")

    # --- Phase-specific instructions ---
    lines.append("## Your Task\n")

    # Get the correct draft path based on mode
    analysis_path = _get_draft_path("refine", pipeline_mode, issue_number, pipeline_id)
    plan_path = _get_draft_path("plan", pipeline_mode, issue_number, pipeline_id)

    if phase == "refine":
        lines.extend(
            [
                "Analyze this issue and produce a structured analysis document. Your goal is to:\n",
                "1. Understand the problem or feature request",
                "2. Research the current codebase to understand existing patterns",
                "3. Identify constraints and dependencies",
                "4. Consider multiple implementation approaches",
                "5. Recommend an approach with justification",
                "6. Surface any questions that need human input",
                "",
                "**IMPORTANT**: Do NOT create an implementation plan, task breakdown, "
                "or phased rollout. That is the **plan** phase's job. Stay focused on "
                "**analysis**: understanding the problem, researching the codebase, "
                "evaluating options, and surfacing decisions for the human.",
                "",
                "## Output Format\n",
                "Create an analysis document following this template:\n",
                "```markdown",
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
                "[Questions that require human input before proceeding.]\n",
                "---\n",
                "*Authored-by: egg*",
                "```\n",
                "## HITL Decisions\n",
                "For questions that require human input before proceeding:\n",
                "**Multiple-choice questions** (use formal HITL decisions):",
                "```bash",
                'egg-contract add-decision --question "Which approach should we use?" \\',
                '  --options "Option A" "Option B" "Option C" --format markdown',
                "```",
                "Copy the markdown output into your analysis. The human can check "
                'a checkbox to select an option. An "Other (explain in reply)" '
                "option is auto-appended.\n",
                "**Open-ended questions** (use dedicated feedback comment):",
                "```bash",
                "egg-contract add-feedback \\",
                '  --question "What is the expected request volume?" \\',
                '  --question "Are there any constraints on third-party dependencies?" \\',
                "  --format markdown",
                "```",
                "This creates a dedicated comment for the human to fill in answers. "
                'They edit the comment to add their responses and check "Submit '
                'feedback" when done. The pipeline will resume with the feedback '
                "available in the contract.",
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
        lines.extend(
            [
                "Implement the changes described in the task and plan:",
                "",
                "1. Review the plan (check `.egg-state/drafts/`)",
                "2. Implement the required changes",
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

    elif phase == "pr":
        lines.extend(
            [
                "Create a pull request for this implementation:",
                "",
                "1. Ensure all commits are pushed",
                "2. Create the PR with a descriptive title and body",
                f"3. Reference the issue (#{issue_number}) in the PR description"
                if issue_number
                else "3. Create the PR with a clear summary",
                "4. Wait for human review and approval",
                "",
            ]
        )

    else:
        lines.append(f"Execute the {phase} phase.\n")

    # --- Phase restrictions ---
    lines.append("## Phase Restrictions\n")
    if is_local and phase in ("refine", "plan"):
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
    elif is_local and phase == "implement":
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
    elif is_local and phase == "pr":
        lines.extend(
            [
                "This is a **local** pipeline entering the PR phase.",
                "PR operations are enabled for this phase.",
                "- You CAN push code (git push)",
                "- You CAN create and edit PRs (gh pr create, gh pr edit)",
                "- You CANNOT merge PRs (human must merge)",
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
        elif phase == "pr":
            lines.extend(
                [
                    "- You CAN create and edit PRs (gh pr create, gh pr edit)",
                    "- You CAN push additional commits",
                    "- You CANNOT merge PRs (human must merge)",
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
) -> str:
    """Build a role-specific prompt for multi-agent execution.

    For the CODER role, delegates to the existing _build_phase_prompt().
    Other roles (TESTER, DOCUMENTER, INTEGRATOR, ARCHITECT, etc.) get
    role-specific instructions.

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

    # Include the original task prompt so agents know what they're working on
    if prompt:
        lines.append("## Task Description\n")
        lines.append(prompt)
        lines.append("")

    # Review feedback from prior cycles
    if review_feedback:
        lines.append("## Review Feedback\n")
        lines.append(review_feedback)
        lines.append("")

    # Role-specific instructions
    lines.append("## Your Task\n")

    if role_value == "tester":
        lines.extend(
            [
                "Write and run tests for the changes made by the CODER agent:",
                "",
                "1. Review the changed files (available in handoff data or via git diff)",
                "2. Write or update tests covering the new/changed code",
                "3. Run all tests to ensure they pass",
                "4. Report test coverage for the new code",
                "5. Commit test files with descriptive messages",
                "",
                "Focus on:",
                "- Unit tests for new functions and methods",
                "- Edge cases and error handling",
                "- Integration tests where appropriate",
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
                "Write your integration report to `.egg-state/agent-outputs/integrator-output.json`.",
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
                "Write your analysis to `.egg-state/agent-outputs/architect-output.json`.",
                "",
            ]
        )
    elif role_value == "task_planner":
        draft_path = _get_draft_path("plan", pipeline_mode, issue_number, pipeline_id)
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
                "Write your risk assessment to `.egg-state/agent-outputs/risk_analyst-output.json`.",
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
    lines.append(
        "When you have completed your work, ensure everything is committed and exit successfully."
    )

    return "\n".join(lines)


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
    pipeline_mode = pipeline.mode or "issue"

    # Build agent-specific prompts for all roles in this phase
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

        sandbox_command = [
            "claude",
            "--dangerously-skip-permissions",
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--model",
            "opus",
            "--max-turns",
            "200",
            prompt_text,
        ]

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
    from egg_contracts.loader import contract_exists, create_contract, create_local_contract

    contract_key: int | str = (
        pipeline.issue_number if pipeline.issue_number is not None else pipeline_id
    )
    if not contract_exists(contract_key, worktree_repo_path):
        logger.warning(
            "Contract missing from worktree, recreating for multi-agent phase",
            pipeline_id=pipeline_id,
            phase=phase,
            contract_key=contract_key,
        )
        if pipeline_mode == "local":
            create_local_contract(
                pipeline_id=str(contract_key),
                title=(pipeline.prompt or "")[:100],
                repo_root=worktree_repo_path,
            )
        else:
            if pipeline.issue_number is None:
                logger.error(
                    "Cannot recreate contract: issue-mode pipeline has no issue_number",
                    pipeline_id=pipeline_id,
                )
                # Fall through — dispatcher will raise ContractNotFoundError
                # which is handled by the existing catch in the completion handler
            else:
                issue_url = f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
                create_contract(
                    issue_number=pipeline.issue_number,
                    title=f"Issue #{pipeline.issue_number}",
                    url=issue_url,
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
    final_info = docker_client.wait_for_container(
        spawned.container_info.container_id,
        timeout=timeout,
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
                        ci.status = ContainerStatus.EXITED
                        ci.exited_at = datetime.utcnow()
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


def _build_checker_prompt(
    pipeline_id: str,
    pipeline_mode: str,
    repo: str | None = None,
    repo_checks: list[dict] | None = None,
) -> str:
    """Build a prompt for the checker agent that runs tests/lint.

    The checker discovers and runs project test/lint commands, then
    writes structured results to .egg-state/checks/implement-results.json.

    Args:
        pipeline_id: Pipeline identifier.
        pipeline_mode: Pipeline mode (e.g. "local", "issue").
        repo: Target repository in "owner/repo" format.
        repo_checks: Pre-configured check commands from repositories.yaml.
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

    lines.extend(
        [
            "After running checks, **write results** to `.egg-state/checks/implement-results.json`:\n",
            "```json",
            "{",
            '  "all_passed": true/false,',
            '  "checks": [',
            '    {"name": "pytest", "passed": true/false, "output": "summary of output"},',
            '    {"name": "lint", "passed": true/false, "output": "summary of output"}',
            "  ]",
            "}",
            "```\n",
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
) -> str:
    """Build a prompt for the autofixer agent.

    Modeled on action/build-autofixer-prompt.sh. Tells the agent to read
    check failures, fix auto-fixable issues, and commit fixes.
    """
    failures = []
    for check in check_results.get("checks", []):
        if not check.get("passed", True):
            failures.append(
                f"- **{check.get('name', 'unknown')}**: {check.get('output', 'failed')}"
            )

    failure_summary = "\n".join(failures) if failures else "No specific failures recorded."

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
    lines.extend(
        [
            "1. **Read the check results** at `.egg-state/checks/implement-results.json`",
            "2. **Investigate all failures**: Examine test output, lint errors, etc.",
            "3. **Fix without committing yet**: For each auto-fixable issue "
            "(lint errors, formatting, simple type errors, obvious test fixes), make the fix",
            "4. **Verify locally**: Run the same checks again to confirm fixes work",
            "5. **Commit all fixes together** with a descriptive message\n",
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
                "## Auto-fixable vs Report-only\n",
                "**Auto-fixable (commit fixes directly):**",
                "- Lint errors (formatting, import order, code style)",
                "- Type errors with clear fixes",
                "- Simple test failures with obvious fixes\n",
                "**Report only (note in commit message):**",
                "- Complex logic errors requiring design decisions",
                "- Security issues requiring architectural changes",
                "- Test failures from unclear requirements",
            ]
        )

    return "\n".join(lines)


def _read_check_results(repo_path: Path) -> dict | None:
    """Read checker output from .egg-state/checks/implement-results.json.

    Returns None if the file is missing or malformed.
    """
    check_file = repo_path / ".egg-state/checks/implement-results.json"
    if not check_file.exists():
        logger.warning("Check results file not found", path=str(check_file))
        return None
    try:
        raw = check_file.read_text()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Failed to parse check results", path=str(check_file), error=str(e))
        return None


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
    draft_rel = _get_draft_path("plan", pipeline_mode, issue_number, pipeline_id)
    if not draft_rel:
        logger.debug(
            "No draft path for plan phase, skipping synthesis",
            pipeline_id=pipeline_id,
            pipeline_mode=pipeline_mode,
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

    sections: list[str] = []
    agent_files = [
        ("architect-output.json", "Architecture Analysis"),
        ("risk_analyst-output.json", "Risk Assessment"),
    ]

    for filename, heading in agent_files:
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

    # Guard against issue-mode pipelines missing issue_number — _get_draft_path
    # would produce a path containing literal "None" (e.g. .egg-state/drafts/None-plan.md).
    if pipeline_mode != "local" and not issue_number:
        logger.warning(
            "Issue-mode pipeline missing issue_number, skipping contract population",
            pipeline_id=pipeline_id,
        )
        return

    # Resolve draft path based on pipeline mode
    draft_rel = _get_draft_path("plan", pipeline_mode, issue_number, pipeline_id)
    if not draft_rel:
        logger.warning("No draft path for plan phase", pipeline_id=pipeline_id)
        return

    plan_path = repo_path / draft_rel
    if not plan_path.exists():
        logger.warning("Plan draft not found, skipping contract population", path=str(plan_path))
        return

    # For issue mode, use issue number as the contract identifier
    contract_id: int | str = pipeline_id
    if pipeline_mode != "local" and issue_number:
        contract_id = issue_number

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
    from routes.phases import get_phase_transitions

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
        pipeline_mode = getattr(pipeline, "mode", "issue")
        transitions = get_phase_transitions(pipeline_mode)

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

        # Map pipeline mode to gateway session mode.
        # If the pipeline has an explicit network_mode (e.g. "private"), use it;
        # otherwise fall back to the default mapping.
        if pipeline.network_mode:
            gateway_mode = pipeline.network_mode
        else:
            gateway_mode = "local" if pipeline_mode == "local" else "public"

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
        repo_volumes = dict(host_repo_map)  # fallback: raw host paths
        worktree_repo_path = repo_path  # default; overridden when worktrees exist
        host_uid = int(os.environ.get("HOST_UID", 1000))
        host_gid = int(os.environ.get("HOST_GID", 1000))
        pipeline_repos = [pipeline.repo] if pipeline.repo else []

        if host_repo_map:
            try:
                # Request repos in owner/repo format if available, else bare names
                wt_repos = pipeline_repos if pipeline_repos else list(host_repo_map.keys())
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
                    logger.warning(
                        "Worktree creation returned no worktrees, using raw host paths",
                        pipeline_id=pipeline_id,
                        errors=wt_result.errors,
                    )

                if wt_result.errors:
                    for err in wt_result.errors:
                        logger.warning("Worktree error", pipeline_id=pipeline_id, error=err)

            except Exception as wt_err:
                logger.warning(
                    "Failed to create worktrees, falling back to raw host paths",
                    pipeline_id=pipeline_id,
                    error=str(wt_err),
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
                if pipeline_mode == "local":
                    from egg_contracts.loader import create_local_contract

                    create_local_contract(
                        pipeline_id=pipeline.id,
                        title=(pipeline.prompt or "")[:100],
                        repo_root=worktree_repo_path,
                    )
                else:
                    from egg_contracts.loader import create_contract

                    issue_url = f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
                    create_contract(
                        issue_number=pipeline.issue_number,
                        title=f"Issue #{pipeline.issue_number}",
                        url=issue_url,
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
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.RUNNING
                    phase_execution.started_at = datetime.utcnow()
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
            if gateway_mode in ("private", "local"):
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
            if pipeline.prompt:
                sandbox_env["EGG_PIPELINE_PROMPT"] = pipeline.prompt

            repos = [pipeline.repo] if pipeline.repo else []

            phase_failed = False
            review_feedback: str | None = hitl_revision_feedback
            hitl_revision_feedback = None

            # --- Inner review cycle ---
            while True:
                # Reload to get latest review_cycles count
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    review_cycle = phase_execution.review_cycles

                    # Record when actual agent work begins (excludes sandbox setup
                    # and HITL waiting time from the phase duration).
                    phase_execution.work_started_at = datetime.utcnow()
                    store.save_pipeline(pipeline)

                # 1. Spawn worker(s)
                # Use multi-agent wave-based execution when enabled for
                # implement and plan phases; single-CODER path otherwise.
                use_multi_agent = pipeline.config.multi_agent and current_phase.value in {
                    "implement",
                    "plan",
                }

                if use_multi_agent:
                    logger.info(
                        "Spawning multi-agent wave execution for phase",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        review_cycle=review_cycle,
                        mode=gateway_mode,
                    )

                    try:
                        exit_code, container_logs = _run_multi_agent_phase(
                            pipeline_id=pipeline_id,
                            pipeline=pipeline,
                            phase=current_phase.value,
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
                        phase=current_phase.value,
                        review_cycle=review_cycle,
                        mode=gateway_mode,
                    )

                    phase_prompt = _build_phase_prompt(
                        phase=current_phase.value,
                        pipeline_id=pipeline_id,
                        pipeline_mode=pipeline_mode,
                        prompt=pipeline.prompt,
                        issue_number=pipeline.issue_number,
                        repo=pipeline.repo,
                        branch=pipeline.branch,
                        review_feedback=review_feedback,
                        review_cycle=review_cycle,
                    )

                    sandbox_command = [
                        "claude",
                        "--dangerously-skip-permissions",
                        "--print",
                        "--verbose",
                        "--output-format",
                        "stream-json",
                        "--model",
                        "opus",
                        "--max-turns",
                        "200",
                        phase_prompt,
                    ]

                    # Use the REFINER role for the refine phase,
                    # CODER for all other single-agent phases.
                    single_agent_role = (
                        AgentRole.REFINER if current_phase.value == "refine" else AgentRole.CODER
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
                            phase=current_phase.value,
                            sandbox_env=sandbox_env,
                            sandbox_command=sandbox_command,
                            store=store,
                            certs_volume=certs_volume,
                        )
                    except ContainerSpawnError as e:
                        with get_pipeline_state_lock(pipeline_id):
                            pipeline = store.load_pipeline(pipeline_id)
                            phase_execution = pipeline.get_phase_execution(current_phase)
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
                        phase_execution.status = PipelineStatus.FAILED
                        phase_execution.error = error_msg
                        phase_execution.completed_at = datetime.utcnow()
                        pipeline.status = PipelineStatus.FAILED
                        pipeline.error = error_msg
                        store.save_pipeline(pipeline)
                    logger.error(
                        "Phase failed",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        exit_code=exit_code,
                        container_logs=container_logs[-2000:] if container_logs else "",
                    )
                    phase_failed = True
                    break

                # 2. Checker + autofix loop (implement phase only)
                if current_phase.value == "implement":
                    # Look up configured check commands for this repo
                    repo_checks: list[dict] | None = None
                    if pipeline.repo:
                        try:
                            all_repo_checks = json.loads(os.environ.get("EGG_REPO_CHECKS", "{}"))
                        except json.JSONDecodeError:
                            all_repo_checks = {}
                        # Case-insensitive lookup
                        repo_lower = pipeline.repo.lower()
                        for cfg_repo, cfg_checks in all_repo_checks.items():
                            if cfg_repo.lower() == repo_lower:
                                if isinstance(cfg_checks, list):
                                    repo_checks = validate_checks(cfg_checks) or None
                                break

                    max_autofix = 3
                    for autofix_attempt in range(max_autofix):
                        logger.info(
                            "Spawning checker",
                            pipeline_id=pipeline_id,
                            autofix_attempt=autofix_attempt + 1,
                        )

                        checker_prompt = _build_checker_prompt(
                            pipeline_id,
                            pipeline_mode,
                            repo=pipeline.repo,
                            repo_checks=repo_checks,
                        )
                        checker_command = [
                            "claude",
                            "--dangerously-skip-permissions",
                            "--print",
                            "--verbose",
                            "--output-format",
                            "stream-json",
                            "--model",
                            "opus",
                            "--max-turns",
                            "50",
                            checker_prompt,
                        ]
                        checker_env = {**sandbox_env, "EGG_AGENT_ROLE": "checker"}

                        try:
                            checker_exit, _ = _spawn_and_wait(
                                spawner=spawner,
                                pipeline_id=pipeline_id,
                                agent_role=AgentRole.CHECKER,
                                issue_number=pipeline.issue_number,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                phase=current_phase.value,
                                sandbox_env=checker_env,
                                sandbox_command=checker_command,
                                timeout=1800,
                                store=store,
                                certs_volume=certs_volume,
                            )
                        except ContainerSpawnError as e:
                            logger.warning(
                                "Checker failed to spawn, skipping checks",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )
                            break

                        check_results = _read_check_results(worktree_repo_path)
                        if check_results is None or check_results.get("all_passed"):
                            logger.info(
                                "All checks passed",
                                pipeline_id=pipeline_id,
                                attempt=autofix_attempt + 1,
                            )
                            break  # Checks pass — proceed to review

                        # Last attempt — don't autofix, just proceed
                        if autofix_attempt >= max_autofix - 1:
                            logger.warning(
                                "Max autofix attempts reached, proceeding to review",
                                pipeline_id=pipeline_id,
                                attempts=max_autofix,
                            )
                            break

                        # Spawn autofix worker
                        logger.info(
                            "Spawning autofixer",
                            pipeline_id=pipeline_id,
                            autofix_attempt=autofix_attempt + 1,
                        )

                        autofix_prompt = _build_autofix_prompt(
                            pipeline_id,
                            pipeline_mode,
                            check_results,
                            repo=pipeline.repo,
                            repo_path=str(worktree_repo_path),
                        )
                        autofix_command = [
                            "claude",
                            "--dangerously-skip-permissions",
                            "--print",
                            "--verbose",
                            "--output-format",
                            "stream-json",
                            "--model",
                            "opus",
                            "--max-turns",
                            "100",
                            autofix_prompt,
                        ]

                        try:
                            _spawn_and_wait(
                                spawner=spawner,
                                pipeline_id=pipeline_id,
                                agent_role=AgentRole.CODER,
                                issue_number=pipeline.issue_number,
                                repo_volumes=repo_volumes,
                                gateway_mode=gateway_mode,
                                repos=repos,
                                phase=current_phase.value,
                                sandbox_env=sandbox_env,
                                sandbox_command=autofix_command,
                                store=store,
                                certs_volume=certs_volume,
                            )
                        except ContainerSpawnError as e:
                            logger.warning(
                                "Autofixer failed to spawn, proceeding to review",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )
                            break

                # 3. Spawn reviewers and read verdicts (reviewed phases)
                # Reviewers always run as a separate step after workers +
                # checker, for both multi-agent and single-agent paths.
                from egg_contracts.agent_roles import (
                    _PHASE_REVIEWERS as _phase_reviewer_roles,
                )

                reviewer_roles = _phase_reviewer_roles.get(current_phase.value, [])
                if not reviewer_roles:
                    break  # No reviewers for this phase — advance

                # Clean stale verdict files
                for role in reviewer_roles:
                    rtype = role.value.replace("reviewer_", "", 1).replace("_", "-")
                    verdict_rel = _verdict_path_for_type(
                        current_phase.value,
                        rtype,
                        pipeline_mode,
                        pipeline.issue_number,
                        pipeline_id,
                    )
                    verdict_path = worktree_repo_path / verdict_rel
                    if verdict_path.exists():
                        try:
                            verdict_path.unlink()
                        except OSError:
                            pass

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
                    )
                    reviewer_command = [
                        "claude",
                        "--dangerously-skip-permissions",
                        "--print",
                        "--verbose",
                        "--output-format",
                        "stream-json",
                        "--model",
                        "opus",
                        "--max-turns",
                        "50",
                        reviewer_prompt,
                    ]
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

                overall_verdict, combined_feedback = _aggregate_review_verdicts(all_verdicts)

                if overall_verdict == "approved":
                    logger.info(
                        "All reviewers approved",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        review_cycle=review_cycle + 1,
                    )
                    break  # Advance to next phase

                # needs_revision — check circuit breaker
                max_cycles = pipeline.config.max_review_cycles
                if review_cycle + 1 >= max_cycles:
                    logger.warning(
                        "Review circuit breaker — advancing despite needs_revision",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        review_cycles=review_cycle + 1,
                        max_review_cycles=max_cycles,
                    )
                    break

                # Store feedback and loop
                review_feedback = combined_feedback
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.review_cycles = review_cycle + 1
                    store.save_pipeline(pipeline)

                logger.info(
                    "Review needs revision — looping",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                    review_cycle=review_cycle + 1,
                    feedback_preview=review_feedback[:200] if review_feedback else "",
                )
                continue  # Re-run while loop with feedback

            # If the phase failed, the outer loop should also break
            if phase_failed:
                break

            # Phase succeeded — mark complete and advance
            with get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.status = PipelineStatus.COMPLETE
                phase_execution.completed_at = datetime.utcnow()
                store.save_pipeline(pipeline)  # Persist phase completion before HITL gate

            # Report phase completion to collaborator
            report_pipeline_status(
                pipeline,
                event_type="phase.completed",
                message=f"Phase {current_phase.value} completed",
            )
            _emit_pipeline_event(pipeline, "phase.completed")

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
                    phase=current_phase.value,
                    error=str(git_err),
                )

            # --- HITL gate: pause for human approval ---
            if pipeline.config.hitl_gates and current_phase.value in _HITL_GATE_PHASES:
                draft_content = _read_phase_draft(
                    worktree_repo_path,
                    current_phase.value,
                    pipeline_mode,
                    pipeline.issue_number,
                    pipeline_id,
                )
                phase_label = "analysis" if current_phase.value == "refine" else current_phase.value
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
                    timeout_seconds=pipeline.config.decision_timeout,
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

                try:
                    dq.wait_for_decision(decision.id)
                except DecisionTimeoutError:
                    logger.warning(
                        "HITL gate timed out, advancing",
                        pipeline_id=pipeline_id,
                        decision_id=decision.id,
                    )

                # Check resolution — did the human approve or request changes?
                resolved_decision = dq.get_decision(decision.id)
                resolution = (resolved_decision.resolution or "").strip()

                if resolution.lower() not in _APPROVE_KEYWORDS:
                    # If the resolution is just a bare option label (e.g. "request changes")
                    # with no actionable feedback, re-queue asking for specifics.
                    if resolution.lower() in _BARE_OPTION_LABELS:
                        logger.info(
                            "HITL gate: bare option label without feedback, requesting specifics",
                            pipeline_id=pipeline_id,
                            phase=current_phase.value,
                            resolution=resolution,
                        )
                        followup = dq.queue_decision(
                            question=(
                                f'You selected "{resolution}" but didn\'t provide specific feedback. '
                                f"Please describe what changes you'd like to see in the {phase_label}, "
                                f"or approve to continue."
                            ),
                            context=draft_content,
                            options=["approve"],
                            timeout_seconds=pipeline.config.decision_timeout,
                        )
                        try:
                            dq.wait_for_decision(followup.id)
                        except DecisionTimeoutError:
                            # Timeout: resolution will be None, so
                            # (resolution or "").strip() → "", which is in
                            # _APPROVE_KEYWORDS — intentionally treating
                            # timeout as approval (same as no-text approve).
                            logger.warning(
                                "HITL follow-up timed out, advancing",
                                pipeline_id=pipeline_id,
                                decision_id=followup.id,
                            )
                        resolved_followup = dq.get_decision(followup.id)
                        resolution = (resolved_followup.resolution or "").strip()
                        # If the follow-up is also bare or an approval, just approve
                        if (
                            resolution.lower() in _APPROVE_KEYWORDS
                            or resolution.lower() in _BARE_OPTION_LABELS
                        ):
                            logger.info(
                                "HITL follow-up: no actionable feedback, treating as approval",
                                pipeline_id=pipeline_id,
                                phase=current_phase.value,
                            )
                            # Fall through to approval path below
                        else:
                            # Got real feedback on the follow-up — proceed to revision
                            pass  # Fall through to the revision block below

                    # Re-check: resolution may have been updated by the follow-up path
                    if (
                        resolution.lower() not in _APPROVE_KEYWORDS
                        and resolution.lower() not in _BARE_OPTION_LABELS
                    ):
                        # Human provided feedback — re-run the phase with corrections
                        logger.info(
                            "HITL gate: changes requested, re-running phase",
                            pipeline_id=pipeline_id,
                            phase=current_phase.value,
                            feedback_preview=resolution[:200],
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
                                    phase=current_phase.value,
                                    hitl_review_cycles=phase_execution.hitl_review_cycles,
                                    max_hitl_review_cycles=max_hitl_cycles,
                                )
                                store.save_pipeline(pipeline)
                                # Fall through to the approval path below
                            else:
                                hitl_revision_feedback = resolution
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
            if not next_phases:
                # Terminal phase — pipeline complete
                with get_pipeline_state_lock(pipeline_id):
                    pipeline = store.load_pipeline(pipeline_id)
                    pipeline.status = PipelineStatus.COMPLETE
                    is_local = pipeline_mode == "local"
                    store.save_pipeline(pipeline, force_commit=is_local)

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
                is_local = pipeline_mode == "local"
                store.save_pipeline(pipeline, force_commit=is_local)

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
            except Exception:
                # Pipeline was deleted and not recreated — safe to clean up
                pass

            if not skip_cleanup:
                _spawner.gateway.delete_worktrees(
                    container_id=pipeline_id,
                    force=True,
                )
                logger.info("Pipeline worktrees cleaned up", pipeline_id=pipeline_id)
        except Exception as wt_err:
            logger.warning(
                "Failed to clean up pipeline worktrees",
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
            return make_error_response(
                f"Pipeline {pipeline_id} is awaiting human approval",
                status_code=409,
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
