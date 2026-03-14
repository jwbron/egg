"""
Coordinator endpoints for dynamic orchestration.

Provides REST endpoints for coordinator-driven pipelines, including
agent spawning, cancellation, phase control, and HITL escalation.
"""

import os
import sys
from datetime import datetime
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


from container_spawner import ContainerSpawnError, get_container_spawner
from decision_queue import get_decision_queue
from events import EventType, emit_event
from gateway_client import GatewayError, get_gateway_client
from models import (
    AgentRole,
    AgentSpawnRecord,
    CoordinatorState,
    Escalation,
    PhaseDecision,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from state_store import (
    InvalidPipelineIdError,
    PipelineNotFoundError,
    get_pipeline_state_lock,
    get_state_store,
)

logger = get_logger("orchestrator.coordinator")

coordinator_bp = Blueprint("coordinator", __name__, url_prefix="/api/v1/pipelines")


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


from routes import get_repo_path  # noqa: E402 — shared helper


def _check_spawn_guardrails(pipeline: Pipeline, role_str: str) -> tuple[bool, str]:
    """Check if spawning is allowed by guardrails.

    Args:
        pipeline: Pipeline state
        role_str: Role string to check

    Returns:
        Tuple of (allowed, reason). If not allowed, reason explains why.
    """
    config = pipeline.config
    state = pipeline.coordinator_state or CoordinatorState()

    # Check total agents
    if state.guardrail_counters.total_agents_spawned >= config.coordinator_max_agents:
        return False, f"Max agents limit reached ({config.coordinator_max_agents})"

    # Check retries per role
    retries = state.guardrail_counters.retries_by_role.get(role_str, 0)
    if retries >= config.coordinator_max_retries_per_role:
        return (
            False,
            f"Max retries for role '{role_str}' reached ({config.coordinator_max_retries_per_role})",
        )

    return True, ""


def _validate_coordinator_enabled(pipeline: Pipeline) -> tuple[Response, int] | None:
    """Validate that coordinator mode is enabled on the pipeline.

    Returns:
        Error response tuple if validation fails, None if OK.
    """
    if not pipeline.config.coordinator_enabled:
        return make_error_response(
            "Coordinator mode is not enabled for this pipeline",
            status_code=403,
            details={"pipeline_id": pipeline.id, "coordinator_enabled": False},
        )
    return None


@coordinator_bp.route("/<pipeline_id>/coordinator/spawn", methods=["POST"])
def spawn_agent(pipeline_id: str) -> tuple[Response, int]:
    """
    Spawn an agent via the coordinator.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "role": "coder",           // required — agent role to spawn
            "task_context": "...",      // optional — task description for the agent
            "extra_env": {"K": "V"}    // optional — additional environment variables
        }

    Response:
        {
            "success": true,
            "message": "Agent spawned",
            "data": {
                "role": "coder",
                "container_id": "abc123...",
                "container_name": "egg-sandbox-issue-496-coder",
                "spawn_record": {...}
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    role_str = data.get("role")
    if not role_str:
        return make_error_response("Missing required field: role")

    # Validate role
    try:
        agent_role = AgentRole(role_str)
    except ValueError:
        valid_roles = [r.value for r in AgentRole]
        return make_error_response(f"Invalid role: {role_str}. Valid roles: {valid_roles}")

    # Prevent coordinator from spawning another coordinator (privilege escalation)
    if agent_role == AgentRole.COORDINATOR:
        return make_error_response(
            "Coordinator cannot spawn another coordinator",
            status_code=403,
        )

    task_context = data.get("task_context", "")
    extra_env = data.get("extra_env") or {}

    if not isinstance(extra_env, dict):
        return make_error_response("extra_env must be a dictionary")

    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            # Validate coordinator is enabled
            err = _validate_coordinator_enabled(pipeline)
            if err is not None:
                return err

            # Check guardrails
            allowed, reason = _check_spawn_guardrails(pipeline, role_str)
            if not allowed:
                return make_error_response(
                    f"Spawn blocked by guardrails: {reason}",
                    status_code=429,
                    details={
                        "role": role_str,
                        "reason": reason,
                        "pipeline_id": pipeline_id,
                    },
                )

            # Calculate retry number for this role
            existing_spawns = [
                s
                for s in (pipeline.coordinator_state or CoordinatorState()).agents_spawned
                if s.role.value == role_str
            ]
            retry_number = len(existing_spawns)

            # Resolve repo_volumes from existing worktrees for this pipeline.
            # The gateway's create_worktrees is idempotent and reuses existing
            # worktrees, so this is safe to call for already-started pipelines.
            repo_volumes: dict[str, str] | None = None
            repos: list[str] | None = None
            mode = pipeline.network_mode or "public"
            if pipeline.repo:
                repos = [pipeline.repo]
                try:
                    gateway = get_gateway_client()
                    host_uid = int(os.environ.get("HOST_UID", 1000))
                    host_gid = int(os.environ.get("HOST_GID", 1000))
                    wt_result = gateway.create_worktrees(
                        container_id=pipeline_id,
                        repos=repos,
                        uid=host_uid,
                        gid=host_gid,
                    )
                    if wt_result.success and wt_result.worktrees:
                        repo_volumes = wt_result.worktrees
                except GatewayError as gw_err:
                    logger.warning(
                        "Failed to resolve repo_volumes from gateway, spawning without repo mounts",
                        pipeline_id=pipeline_id,
                        error=str(gw_err),
                    )

            # Build the agent command with prompt (required for --print mode)
            agent_prompt = task_context or (
                f"You are the {role_str} agent for pipeline {pipeline_id}. "
                f"Execute your role for the {pipeline.current_phase.value} phase. "
                f"Follow the instructions in your CLAUDE.md."
            )
            agent_command = [
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
                agent_prompt,
            ]

            # Spawn the container
            spawner = get_container_spawner()
            spawned = spawner.spawn_agent_container(
                pipeline_id=pipeline_id,
                agent_role=agent_role,
                issue_number=pipeline.issue_number,
                repo_volumes=repo_volumes,
                mode=mode,
                repos=repos,
                extra_env=extra_env,
                phase=pipeline.current_phase.value,
                branch=pipeline.branch,
                command=agent_command,
            )

            # Create spawn record
            spawn_record = AgentSpawnRecord(
                role=agent_role,
                spawned_at=datetime.utcnow(),
                status="running",
                container_id=spawned.container_info.container_id,
                task_context=task_context,
                retry_number=retry_number,
            )

            # Update coordinator state
            if pipeline.coordinator_state is None:
                pipeline.coordinator_state = CoordinatorState()

            pipeline.coordinator_state.agents_spawned.append(spawn_record)
            pipeline.coordinator_state.guardrail_counters.total_agents_spawned += 1

            # Track retries per role
            current_retries = pipeline.coordinator_state.guardrail_counters.retries_by_role.get(
                role_str, 0
            )
            pipeline.coordinator_state.guardrail_counters.retries_by_role[role_str] = (
                current_retries + 1
            )

            store.save_pipeline(pipeline)

        # Emit event outside the lock
        emit_event(
            EventType.COORDINATOR_SPAWN,
            pipeline_id,
            data={
                "role": role_str,
                "container_id": spawned.container_info.container_id,
                "task_context": task_context,
                "retry_number": retry_number,
            },
        )

        logger.info(
            "Coordinator spawned agent",
            pipeline_id=pipeline_id,
            role=role_str,
            container_id=spawned.container_info.container_id[:12],
            retry_number=retry_number,
        )

        return make_success_response(
            "Agent spawned",
            data={
                "role": role_str,
                "container_id": spawned.container_info.container_id,
                "container_name": spawned.container_info.container_name,
                "spawn_record": spawn_record.model_dump(mode="json"),
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
    except ContainerSpawnError as e:
        logger.error(
            "Failed to spawn agent container",
            pipeline_id=pipeline_id,
            role=role_str,
            error=str(e),
        )
        return make_error_response(
            f"Failed to spawn agent container: {e}",
            status_code=500,
        )
    except Exception as e:
        logger.error(
            "Unexpected error spawning agent",
            pipeline_id=pipeline_id,
            role=role_str,
            error=str(e),
            exc_info=True,
        )
        return make_error_response(
            f"Failed to spawn agent: {e}",
            status_code=500,
        )


@coordinator_bp.route("/<pipeline_id>/coordinator/agents/<role>", methods=["DELETE"])
def cancel_agent(pipeline_id: str, role: str) -> tuple[Response, int]:
    """
    Cancel a running agent.

    URL params:
        pipeline_id: Pipeline ID
        role: Agent role to cancel

    Response:
        {
            "success": true,
            "message": "Agent cancelled",
            "data": {
                "role": "coder",
                "container_id": "abc123..."
            }
        }
    """
    # Validate role
    try:
        AgentRole(role)
    except ValueError:
        valid_roles = [r.value for r in AgentRole]
        return make_error_response(f"Invalid role: {role}. Valid roles: {valid_roles}")

    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            # Validate coordinator is enabled
            err = _validate_coordinator_enabled(pipeline)
            if err is not None:
                return err

            # Find the running spawn record for this role
            coord_state = pipeline.coordinator_state or CoordinatorState()
            running_record = None
            for record in reversed(coord_state.agents_spawned):
                if record.role.value == role and record.status == "running":
                    running_record = record
                    break

            if running_record is None:
                return make_error_response(
                    f"No running agent found for role '{role}'",
                    status_code=404,
                    details={"pipeline_id": pipeline_id, "role": role},
                )

            container_id = running_record.container_id

            # Stop the container
            if container_id:
                try:
                    spawner = get_container_spawner()
                    spawner.remove_agent_container(
                        container_id,
                        force=True,
                        cleanup_session=True,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to stop container for cancelled agent",
                        pipeline_id=pipeline_id,
                        role=role,
                        container_id=container_id[:12] if container_id else None,
                        error=str(e),
                    )

            # Update spawn record
            running_record.status = "cancelled"
            running_record.completed_at = datetime.utcnow()

            store.save_pipeline(pipeline)

        logger.info(
            "Coordinator cancelled agent",
            pipeline_id=pipeline_id,
            role=role,
            container_id=container_id[:12] if container_id else None,
        )

        return make_success_response(
            "Agent cancelled",
            data={
                "role": role,
                "container_id": container_id,
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
    except Exception as e:
        logger.error(
            "Failed to cancel agent",
            pipeline_id=pipeline_id,
            role=role,
            error=str(e),
            exc_info=True,
        )
        return make_error_response(
            f"Failed to cancel agent: {e}",
            status_code=500,
        )


@coordinator_bp.route("/<pipeline_id>/coordinator/state", methods=["GET"])
def get_coordinator_state(pipeline_id: str) -> tuple[Response, int]:
    """
    Get comprehensive coordinator state.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "current_phase": "implement",
                "status": "running",
                "running_agents": [...],
                "completed_agents": [...],
                "pending_decisions": [...],
                "coordinator_state": {...},
                "guardrail_counters": {...}
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        coord_state = pipeline.coordinator_state or CoordinatorState()

        # Categorize agents by status
        running_agents = [
            s.model_dump(mode="json") for s in coord_state.agents_spawned if s.status == "running"
        ]
        completed_agents = [
            s.model_dump(mode="json")
            for s in coord_state.agents_spawned
            if s.status in ("complete", "failed", "cancelled")
        ]

        # Get pending decisions
        pending_decisions = [
            d.model_dump(mode="json") for d in pipeline.decisions if d.status == "pending"
        ]

        return make_success_response(
            "Coordinator state retrieved",
            data={
                "current_phase": pipeline.current_phase.value,
                "status": pipeline.status.value,
                "running_agents": running_agents,
                "completed_agents": completed_agents,
                "pending_decisions": pending_decisions,
                "coordinator_state": coord_state.model_dump(mode="json"),
                "guardrail_counters": coord_state.guardrail_counters.model_dump(mode="json"),
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
    except Exception as e:
        logger.error(
            "Failed to get coordinator state",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )
        return make_error_response(
            f"Failed to get coordinator state: {e}",
            status_code=500,
        )


@coordinator_bp.route("/<pipeline_id>/coordinator/phase", methods=["POST"])
def advance_phase(pipeline_id: str) -> tuple[Response, int]:
    """
    Advance or skip to a target phase.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "target_phase": "implement",  // optional — skip to specific phase
            "reason": "All plan tasks approved"  // required
        }

    Response:
        {
            "success": true,
            "message": "Phase advanced",
            "data": {
                "previous_phase": "plan",
                "current_phase": "implement",
                "action": "advance",
                "reason": "..."
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    reason = data.get("reason")
    if not reason:
        return make_error_response("Missing required field: reason")

    target_phase_str = data.get("target_phase")

    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            # Validate coordinator is enabled
            err = _validate_coordinator_enabled(pipeline)
            if err is not None:
                return err

            # Validate pipeline is in a state that allows phase transitions
            if pipeline.status not in (PipelineStatus.RUNNING, PipelineStatus.AWAITING_HUMAN):
                return make_error_response(
                    f"Pipeline status is '{pipeline.status.value}', "
                    "phase transitions require 'running' or 'awaiting_human' status",
                    status_code=409,
                )

            previous_phase = pipeline.current_phase
            # Exclude COORDINATOR pseudo-phase from sequential ordering
            phase_order = [p for p in PipelinePhase if p != PipelinePhase.COORDINATOR]

            # Determine action and target
            if target_phase_str:
                # Validate target phase
                try:
                    target_phase = PipelinePhase(target_phase_str)
                except ValueError:
                    valid_phases = [p.value for p in PipelinePhase]
                    return make_error_response(
                        f"Invalid target_phase: {target_phase_str}. Valid phases: {valid_phases}"
                    )

                if target_phase == PipelinePhase.COORDINATOR:
                    return make_error_response(
                        "Cannot transition to coordinator pseudo-phase",
                        status_code=400,
                    )

                # Determine direction: forward skip or backward loopback
                current_idx = phase_order.index(pipeline.current_phase)
                target_idx = phase_order.index(target_phase)
                if target_idx > current_idx:
                    action = "skip"
                elif target_idx < current_idx:
                    action = "loopback"
                else:
                    return make_error_response(
                        f"Target phase '{target_phase_str}' is the current phase",
                        status_code=400,
                    )
                pipeline.current_phase = target_phase
            else:
                # Advance to next phase in sequence
                current_idx = phase_order.index(pipeline.current_phase)
                if current_idx >= len(phase_order) - 1:
                    return make_error_response(
                        f"Cannot advance beyond final phase '{pipeline.current_phase.value}'",
                        status_code=400,
                    )
                target_phase = phase_order[current_idx + 1]
                action = "advance"
                pipeline.current_phase = target_phase

            # Enforce contract existence before entering implement phase.
            # Every pipeline — simple or complex — must have a contract so
            # reviewers, phase gates, and the reviewer_contract role have a
            # shared source of truth about what is being built.
            if (
                target_phase in (PipelinePhase.IMPLEMENT, PipelinePhase.PR)
                and not pipeline.contract_synced
            ):
                return make_error_response(
                    f"Cannot advance to '{target_phase.value}' phase: no contract exists for this pipeline. "
                    "A contract must be created before implementation can begin. "
                    "The orchestrator creates contracts automatically during pipeline "
                    "startup — if contract_synced is still false, the contract creation "
                    "may have failed. Check pipeline logs for details.",
                    status_code=409,
                )

            # Record the phase decision in coordinator state
            if pipeline.coordinator_state is None:
                pipeline.coordinator_state = CoordinatorState()

            phase_decision = PhaseDecision(
                phase=target_phase.value,
                action=action,
                reason=reason,
                decided_at=datetime.utcnow(),
            )
            pipeline.coordinator_state.phase_decisions.append(phase_decision)

            store.save_pipeline(pipeline)

        # Emit event outside the lock
        emit_event(
            EventType.COORDINATOR_DECISION,
            pipeline_id,
            data={
                "previous_phase": previous_phase.value,
                "current_phase": target_phase.value,
                "action": action,
                "reason": reason,
            },
        )

        logger.info(
            "Coordinator phase decision",
            pipeline_id=pipeline_id,
            previous_phase=previous_phase.value,
            current_phase=target_phase.value,
            action=action,
            reason=reason,
        )

        return make_success_response(
            f"Phase {action}d to {target_phase.value}",
            data={
                "previous_phase": previous_phase.value,
                "current_phase": target_phase.value,
                "action": action,
                "reason": reason,
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
    except Exception as e:
        logger.error(
            "Failed to advance phase",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )
        return make_error_response(
            f"Failed to advance phase: {e}",
            status_code=500,
        )


@coordinator_bp.route("/<pipeline_id>/coordinator/escalate", methods=["POST"])
def escalate(pipeline_id: str) -> tuple[Response, int]:
    """
    Create a HITL escalation from the coordinator.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "question": "Which approach should we use?",  // required
            "escalation_type": "choice",                   // required — "choice" or "feedback"
            "options": ["Option A", "Option B"]            // required for type "choice"
        }

    Response:
        {
            "success": true,
            "message": "Escalation created",
            "data": {
                "question": "...",
                "escalation_type": "choice",
                "decision_id": "d-abc123"
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    question = data.get("question")
    if not question:
        return make_error_response("Missing required field: question")

    escalation_type = data.get("escalation_type")
    if not escalation_type:
        return make_error_response("Missing required field: escalation_type")

    if escalation_type not in ("choice", "feedback"):
        return make_error_response(
            f"Invalid escalation_type: {escalation_type}. Must be 'choice' or 'feedback'"
        )

    options = data.get("options")
    if escalation_type == "choice" and not options:
        return make_error_response(
            "Missing required field: options (required for escalation_type 'choice')"
        )

    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)

        with get_pipeline_state_lock(pipeline_id):
            pipeline = store.load_pipeline(pipeline_id)

            # Validate coordinator is enabled
            err = _validate_coordinator_enabled(pipeline)
            if err is not None:
                return err

            # Create HITL decision via decision queue
            queue = get_decision_queue(pipeline_id, repo_path)
            decision = queue.queue_decision(
                question=question,
                options=options if escalation_type == "choice" else None,
                phase=pipeline.current_phase,
            )

            # Record escalation in coordinator state
            if pipeline.coordinator_state is None:
                pipeline.coordinator_state = CoordinatorState()

            escalation_record = Escalation(
                question=question,
                escalation_type=escalation_type,
                created_at=datetime.utcnow(),
            )
            pipeline.coordinator_state.escalations.append(escalation_record)

            store.save_pipeline(pipeline)

        # Emit event outside the lock
        emit_event(
            EventType.COORDINATOR_ESCALATION,
            pipeline_id,
            data={
                "question": question,
                "escalation_type": escalation_type,
                "options": options,
                "decision_id": decision.id if hasattr(decision, "id") else None,
            },
        )

        logger.info(
            "Coordinator escalation created",
            pipeline_id=pipeline_id,
            question=question[:80],
            escalation_type=escalation_type,
        )

        return make_success_response(
            "Escalation created",
            data={
                "question": question,
                "escalation_type": escalation_type,
                "decision_id": decision.id if hasattr(decision, "id") else None,
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
    except Exception as e:
        logger.error(
            "Failed to create escalation",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )
        return make_error_response(
            f"Failed to create escalation: {e}",
            status_code=500,
        )
