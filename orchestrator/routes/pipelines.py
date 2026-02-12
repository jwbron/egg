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


def _build_phase_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    prompt: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
) -> str:
    """Build a phase-specific prompt for the sandbox Claude invocation.

    Follows the same structure as action/build-sdlc-prompt.sh:
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

    # --- Task description ---
    if prompt:
        lines.append("## Task Description\n")
        lines.append(prompt)
        lines.append("")

    # --- Phase-specific instructions ---
    lines.append("## Your Task\n")

    if phase == "refine":
        lines.extend(
            [
                "Analyze the task and produce a structured analysis:",
                "",
                "1. Understand the problem or feature request",
                "2. Research the current codebase to understand existing patterns",
                "3. Identify constraints and dependencies",
                "4. Consider multiple implementation approaches",
                "5. Recommend an approach with justification",
                "",
            ]
        )
        if is_local:
            lines.extend(
                [
                    "Write your analysis to `.egg-state/drafts/analysis.md`.",
                    "Commit the draft when done.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"Write your analysis to `.egg-state/drafts/{issue_number}-analysis.md`.",
                    "Commit and push the draft when done.",
                    "",
                ]
            )

    elif phase == "plan":
        lines.extend(
            [
                "Create a detailed implementation plan:",
                "",
                "1. Review any prior analysis",
                "2. Break down the work into phases with discrete tasks",
                "3. Define clear acceptance criteria for each task",
                "4. Identify test strategy",
                "5. Consider rollback and risks",
                "",
            ]
        )
        if is_local:
            lines.extend(
                [
                    "Write your plan to `.egg-state/drafts/plan.md`.",
                    "Commit the draft when done.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"Write your plan to `.egg-state/drafts/{issue_number}-plan.md`.",
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
        if not is_local:
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
    if is_local:
        lines.extend(
            [
                "This is a **local** pipeline — no GitHub operations:",
                "- You CANNOT push code (git push)",
                "- You CANNOT create PRs (gh pr create)",
                "- You CANNOT post issue comments",
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
    lines.append(
        "When you have completed your work for this phase, "
        "ensure everything is committed and exit successfully."
    )

    return "\n".join(lines)


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

        # Determine host repos path for volume mount.  When the
        # orchestrator runs inside Docker, EGG_REPO_PATH is the
        # *container* path but volume mounts need the *host* path
        # (since the Docker socket operates on the host daemon).
        # EGG_HOST_REPOS_DIR provides that; fall back to EGG_REPO_PATH
        # when running natively (not in Docker).
        host_repos_dir = os.environ.get(
            "EGG_HOST_REPOS_DIR",
            os.environ.get("EGG_REPO_PATH"),
        )

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

            # Build sandbox environment.  The sandbox entrypoint needs
            # GATEWAY_URL for health checks and Anthropic API routing,
            # plus proxy vars for network access in private mode.
            gateway_url = os.environ.get("GATEWAY_URL", "http://172.32.0.2:9848")
            sandbox_env: dict[str, str] = {
                # Pipeline identity
                "EGG_PIPELINE_ID": pipeline_id,
                "EGG_PIPELINE_PHASE": current_phase.value,
                "EGG_PIPELINE_MODE": pipeline_mode,
                # Gateway connection (sandbox entrypoint health check + API routing)
                "GATEWAY_URL": gateway_url,
                "EGG_GATEWAY_URL": gateway_url,
                # Host UID/GID for file permission alignment
                "RUNTIME_UID": os.environ.get("HOST_UID", "1000"),
                "RUNTIME_GID": os.environ.get("HOST_GID", "1000"),
            }
            if pipeline.prompt:
                sandbox_env["EGG_PIPELINE_PROMPT"] = pipeline.prompt

            # Build the claude --print command for the sandbox entrypoint.
            # The entrypoint detects args and runs them via gosu as the
            # egg user instead of launching interactive mode.
            phase_prompt = _build_phase_prompt(
                phase=current_phase.value,
                pipeline_id=pipeline_id,
                pipeline_mode=pipeline_mode,
                prompt=pipeline.prompt,
                issue_number=pipeline.issue_number,
                repo=pipeline.repo,
                branch=pipeline.branch,
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

            try:
                spawned = spawner.spawn_agent_container(
                    pipeline_id=pipeline_id,
                    agent_role=AgentRole.CODER,
                    issue_number=pipeline.issue_number,
                    repo_mount=host_repos_dir,
                    mode=gateway_mode,
                    wait_for_gateway=False,
                    repos=[pipeline.repo] if pipeline.repo else [],
                    phase=current_phase.value,
                    extra_env=sandbox_env,
                    command=sandbox_command,
                )

                # Wait for the container to finish
                docker_client = spawner.docker
                final_info = docker_client.wait_for_container(
                    spawned.container_info.container_id,
                    timeout=3600,
                )

                if final_info.exit_code != 0:
                    # Capture container logs BEFORE cleanup for diagnostics
                    container_logs = ""
                    try:
                        container_logs = spawner.docker.get_container_logs(
                            spawned.container_info.container_id,
                            tail=50,
                        )
                    except Exception:
                        pass

                    # Clean up the failed container
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

                    # Build error message with log tail for debugging
                    error_msg = f"Container exited with code {final_info.exit_code}"
                    if container_logs:
                        # Include last few lines in the error for visibility
                        log_lines = container_logs.strip().splitlines()
                        tail = "\n".join(log_lines[-10:])
                        error_msg += f"\n--- container logs (last 10 lines) ---\n{tail}"

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
                        exit_code=final_info.exit_code,
                        container_logs=container_logs[-2000:] if container_logs else "",
                    )
                    break

                # Phase succeeded — clean up the container so the next phase can reuse the name
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
