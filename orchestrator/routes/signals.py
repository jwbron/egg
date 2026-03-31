"""
Signal endpoints for sandbox callbacks.

Provides REST endpoints for sandboxes to report completion,
progress updates, and errors back to the orchestrator.
"""

import re
import subprocess
import sys
from datetime import UTC, datetime
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


from egg_contracts import load_contract, save_contract
from egg_contracts.agent_roles import AgentRole as ContractAgentRole
from egg_contracts.loader import ContractNotFoundError
from egg_contracts.orchestrator import create_orchestrator
from handoffs import AgentOutput, save_agent_output
from models import AgentRole, Pipeline, PipelineStatus
from state_store import InvalidPipelineIdError, PipelineNotFoundError, get_state_store

logger = get_logger("orchestrator.signals")

# Mapping from orchestrator AgentRole to egg_contracts AgentRole.
# Roles not in this mapping (e.g. REFINER, REVIEWER_REFINE) don't
# participate in contract orchestration.
_AGENT_ROLE_TO_CONTRACT_ROLE: dict[AgentRole, ContractAgentRole] = {
    AgentRole.CODER: ContractAgentRole.CODER,
    AgentRole.TESTER: ContractAgentRole.TESTER,
    AgentRole.DOCUMENTER: ContractAgentRole.DOCUMENTER,
    AgentRole.ARCHITECT: ContractAgentRole.ARCHITECT,
    AgentRole.TASK_PLANNER: ContractAgentRole.TASK_PLANNER,
    AgentRole.RISK_ANALYST: ContractAgentRole.RISK_ANALYST,
    AgentRole.REVIEWER_CODE: ContractAgentRole.REVIEWER_CODE,
    AgentRole.REVIEWER_CONTRACT: ContractAgentRole.REVIEWER_CONTRACT,
    AgentRole.REVIEWER_AGENT_DESIGN: ContractAgentRole.REVIEWER_AGENT_DESIGN,
}

_SIGTERM_PATTERN = re.compile(r"\b143\b")


def _is_sigterm_after_completion(pipeline: Pipeline, error_message: str) -> bool:
    """Return True if this error is a SIGTERM exit on an already-complete pipeline."""
    return pipeline.status == PipelineStatus.COMPLETE and bool(
        _SIGTERM_PATTERN.search(error_message)
    )


signals_bp = Blueprint("signals", __name__, url_prefix="/api/v1/pipelines")


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
    status_code: int = 200,
) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), status_code


from routes import (  # noqa: E402 — shared helper
    get_repo_path,
    resolve_repo_path_for_pipeline,
    resolve_worktree_path,
)


@signals_bp.route("/<pipeline_id>/signal", methods=["POST"])
def handle_signal(pipeline_id: str) -> tuple[Response, int]:
    """
    Handle a signal from a sandbox container.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "signal_type": "complete" | "progress" | "error" | "heartbeat",
            "agent_role": "coder",  // required for agent signals
            "container_id": "abc123...",  // optional
            "data": {...}  // signal-specific data
        }

    Signal types:
        - complete: Agent finished execution
        - progress: Progress update
        - error: Error occurred
        - heartbeat: Keep-alive signal

    Response:
        {
            "success": true,
            "message": "Signal received"
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    signal_type = data.get("signal_type")
    if not signal_type:
        return make_error_response("Missing signal_type")

    repo_path = get_repo_path()
    # Signal requests don't include a repo field, so get_repo_path() may
    # return the bare parent directory.  Resolve using the pipeline's
    # stored repo field.
    repo_path = resolve_repo_path_for_pipeline(pipeline_id, repo_path)

    # Route to appropriate handler
    handlers = {
        "complete": handle_complete_signal,
        "progress": handle_progress_signal,
        "error": handle_error_signal,
        "heartbeat": handle_heartbeat_signal,
        "readiness": handle_readiness_signal,
        "consensus_propose": handle_consensus_propose_signal,
        "consensus_ack": handle_consensus_ack_signal,
        "consensus_nack": handle_consensus_nack_signal,
        "consensus_withdraw": handle_consensus_withdraw_signal,
        "consensus_confirmed": handle_consensus_confirmed_signal,
    }

    handler = handlers.get(signal_type)
    if not handler:
        return make_error_response(
            f"Unknown signal type: {signal_type}. Valid types: {list(handlers.keys())}"
        )

    return handler(pipeline_id, data, repo_path)


def _verify_commit_on_branch(
    commit: str,
    branch: str,
    worktree_path: Path,
    pipeline_id: str,
) -> bool | None:
    """Check if a commit exists on the expected branch.

    Returns:
        True if commit is on the branch.
        False if commit is NOT on the branch (hard-block).
        None if verification failed (best-effort, non-blocking).
    """
    try:
        # Fetch first to ensure we have the latest remote state
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "fetch", "origin", "--", branch],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Branch fetch failed during completion verification (non-blocking)",
                pipeline_id=pipeline_id,
                branch=branch,
                error=result.stderr.strip(),
            )
            return None  # Can't verify — don't block

        # Check if commit exists on the branch
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "branch",
                "-r",
                "--contains",
                "--",
                commit,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "git branch --contains failed (non-blocking)",
                pipeline_id=pipeline_id,
                commit=commit,
                error=result.stderr.strip(),
            )
            return None  # Can't verify — don't block

        # Check if origin/{branch} appears in the output
        for line in result.stdout.splitlines():
            if f"origin/{branch}" in line.strip():
                return True

        # Commit not found on expected branch
        logger.warning(
            "Commit not found on expected branch",
            pipeline_id=pipeline_id,
            commit=commit,
            expected_branch=branch,
            branches_containing=result.stdout.strip(),
        )
        return False

    except Exception as e:
        logger.warning(
            "Branch verification failed (non-blocking)",
            pipeline_id=pipeline_id,
            commit=commit,
            branch=branch,
            error=str(e),
        )
        return None  # Don't block on verification errors


def _check_branch_progress(
    branch: str,
    phase_start_sha: str,
    worktree_path: Path,
    pipeline_id: str,
) -> None:
    """Log a warning if no new commits have been pushed since phase start."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "rev-parse",
                f"origin/{branch}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            current_tip = result.stdout.strip()
            if current_tip == phase_start_sha:
                logger.warning(
                    "No new commits on branch since phase start",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    phase_start_sha=phase_start_sha,
                )
    except Exception:
        pass  # Non-fatal — progress check is advisory


def handle_complete_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle agent completion signal.

    Request body data:
        {
            "agent_role": "coder",
            "commit": "abc1234",  // optional
            "files_changed": ["src/main.py"],  // optional
            "handoff_data": {...},  // optional
            "metrics": {...}  // optional
        }
    """
    agent_role_str = data.get("agent_role")
    if not agent_role_str:
        return make_error_response("Missing agent_role")

    try:
        agent_role = AgentRole(agent_role_str)
    except ValueError:
        return make_error_response(f"Invalid agent_role: {agent_role_str}")

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        # Contracts live in per-pipeline worktrees, not the main repo.
        contract_path = resolve_worktree_path(pipeline_id, repo_path)

        commit = data.get("commit")
        outputs = data.get("handoff_data", {})

        # SECURITY: Verify commit exists on the expected branch.
        # When the agent reports a commit SHA, confirm it was actually
        # pushed to the pipeline's assigned branch.  This catches the
        # failure mode where an agent pushes to an improvised branch
        # name and the orchestrator accepts the signal without checking.
        if commit and pipeline.branch:
            branch_verified = _verify_commit_on_branch(
                commit,
                pipeline.branch,
                contract_path,
                pipeline_id,
            )
            if branch_verified is False:
                # Hard-block: commit not found on expected branch.
                return make_error_response(
                    f"Completion rejected: commit {commit} not found on "
                    f"expected branch {pipeline.branch}. The agent may have "
                    f"pushed to a different branch.",
                    status_code=409,
                    details={
                        "commit": commit,
                        "expected_branch": pipeline.branch,
                        "pipeline_id": pipeline_id,
                    },
                )

            # Check for no new commits since phase start (advisory warning).
            # Only run when branch_verified is True — if verification failed
            # (returned None), the fetch didn't succeed and origin/{branch}
            # may be stale, making the progress check unreliable.
            if branch_verified is True:
                current_phase = pipeline.current_phase
                phase_exec = pipeline.phases.get(current_phase.value)
                if phase_exec and phase_exec.phase_start_sha:
                    _check_branch_progress(
                        pipeline.branch,
                        phase_exec.phase_start_sha,
                        contract_path,
                        pipeline_id,
                    )

        # Only interact with the contract for roles that have a contract
        # mapping.  Single-agent roles like REFINER and REVIEWER_REFINE
        # don't participate in contract orchestration.
        contract_role = _AGENT_ROLE_TO_CONTRACT_ROLE.get(agent_role)

        if contract_role is not None:
            identifier: int | str = (
                pipeline.issue_number if pipeline.issue_number is not None else pipeline_id
            )
            contract = load_contract(identifier, contract_path)
            orch = create_orchestrator(contract)
            orch.complete_agent(contract_role, commit=commit, outputs=outputs)
            updated_contract = orch.apply_to_contract()
            save_contract(updated_contract, contract_path)
            decision = orch.get_next_dispatch()
            is_complete = decision.all_complete
        else:
            is_complete = True

        # Save agent output (independent of contract — used for phase handoffs)
        if data.get("handoff_data") or data.get("files_changed"):
            output = AgentOutput(
                role=agent_role,
                commit=commit,
                files_changed=data.get("files_changed", []),
                handoff_data=outputs,
                metrics=data.get("metrics", {}),
            )
            # Derive pipeline identifier matching _pipeline_identifier() convention
            identifier: int | str = (
                pipeline.issue_number if pipeline.issue_number is not None else pipeline_id
            )
            save_agent_output(contract_path, output, identifier=identifier)

        logger.info(
            "Agent completed",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            commit=commit,
        )

        return make_success_response(
            "Completion recorded",
            data={
                "agent_role": agent_role.value,
                "commit": commit,
                "all_complete": is_complete,
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
    except ContractNotFoundError:
        # Contract may not exist yet (e.g. first phase still initializing)
        # or may have been cleaned up.  This is non-fatal — the multi-agent
        # executor tracks completion independently.
        logger.warning(
            "Contract not found for completion signal (non-fatal)",
            pipeline_id=pipeline_id,
            role=agent_role_str,
            commit=data.get("commit"),
        )
        return make_success_response(
            "Completion acknowledged (contract not found)",
            data={
                "agent_role": agent_role_str,
                "contract_missing": True,
                "commit": data.get("commit"),
            },
        )
    except Exception as e:
        logger.error(
            "Failed to record completion",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return make_error_response(f"Failed to record completion: {e}", status_code=500)


def handle_progress_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle progress update signal.

    Request body data:
        {
            "agent_role": "coder",
            "progress_percent": 50,
            "current_task": "Writing tests",
            "message": "..."
        }
    """
    agent_role_str = data.get("agent_role")

    logger.info(
        "Progress update",
        pipeline_id=pipeline_id,
        role=agent_role_str,
        progress=data.get("progress_percent"),
        task=data.get("current_task"),
    )

    return make_success_response(
        "Progress recorded",
        data={
            "agent_role": agent_role_str,
            "progress_percent": data.get("progress_percent"),
        },
    )


def handle_error_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle error signal.

    Request body data:
        {
            "agent_role": "coder",
            "error": "Error message",
            "recoverable": false
        }
    """
    agent_role_str = data.get("agent_role")
    if not agent_role_str:
        return make_error_response("Missing agent_role")

    try:
        agent_role = AgentRole(agent_role_str)
    except ValueError:
        return make_error_response(f"Invalid agent_role: {agent_role_str}")

    error_message = data.get("error", "Unknown error")
    recoverable = data.get("recoverable", False)

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        # SIGTERM (exit code 143) is expected when the orchestrator stops
        # agents after pipeline completion.  Suppress the error to avoid
        # noisy warnings on every successful run.
        if _is_sigterm_after_completion(pipeline, error_message):
            logger.info(
                "Agent stopped after pipeline completion (SIGTERM — expected)",
                pipeline_id=pipeline_id,
                role=agent_role.value,
            )
            return make_success_response(
                "Clean shutdown acknowledged",
                data={
                    "agent_role": agent_role.value,
                    "clean_shutdown": True,
                },
            )

        # Contracts live in per-pipeline worktrees, not the main repo.
        contract_path = resolve_worktree_path(pipeline_id, repo_path)

        # Only interact with the contract for roles that have a contract
        # mapping.  Single-agent roles (REFINER, REVIEWER_REFINE, etc.)
        # don't participate in contract orchestration.
        contract_role = _AGENT_ROLE_TO_CONTRACT_ROLE.get(agent_role)

        if contract_role is not None:
            identifier: int | str = (
                pipeline.issue_number if pipeline.issue_number is not None else pipeline_id
            )
            contract = load_contract(identifier, contract_path)
            orch = create_orchestrator(contract)
            orch.fail_agent(contract_role, error_message)
            updated_contract = orch.apply_to_contract()
            save_contract(updated_contract, contract_path)

        logger.error(
            "Agent failed",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            error=error_message,
            recoverable=recoverable,
        )

        return make_success_response(
            "Error recorded",
            data={
                "agent_role": agent_role.value,
                "error": error_message,
                "recoverable": recoverable,
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
    except ContractNotFoundError:
        logger.warning(
            "Contract not found for error signal (non-fatal)",
            pipeline_id=pipeline_id,
            role=agent_role_str,
            error=error_message,
            recoverable=recoverable,
        )
        return make_success_response(
            "Error acknowledged (contract not found)",
            data={
                "agent_role": agent_role_str,
                "contract_missing": True,
                "error": error_message,
                "recoverable": recoverable,
            },
        )
    except Exception as e:
        logger.error(
            "Failed to record error",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return make_error_response(f"Failed to record error: {e}", status_code=500)


def handle_heartbeat_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle heartbeat signal.

    Request body data:
        {
            "agent_role": "coder",
            "container_id": "abc123..."
        }
    """
    agent_role_str = data.get("agent_role")
    container_id = data.get("container_id")

    logger.debug(
        "Heartbeat",
        pipeline_id=pipeline_id,
        role=agent_role_str,
        container_id=container_id[:12] if container_id else None,
    )

    return make_success_response(
        "Heartbeat acknowledged",
        data={
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def handle_readiness_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle readiness signal for concurrent phase consensus.

    Request body data:
        {
            "agent_role": "coder",
            "state": "READY" | "WORKING" | "BLOCKED" | "OBJECTING",
            "reason": "Optional reason text"
        }
    """
    logger.warning(
        "Readiness signal is deprecated. Use consensus protocol signals instead.",
        pipeline_id=pipeline_id,
        role=data.get("agent_role"),
    )
    agent_role_str = data.get("agent_role")
    if not agent_role_str:
        return make_error_response("Missing agent_role")

    state_str = data.get("state")
    if not state_str:
        return make_error_response("Missing state")

    valid_states = {"WORKING", "READY", "BLOCKED", "OBJECTING"}
    if state_str not in valid_states:
        return make_error_response(
            f"Invalid state: {state_str}. Valid states: {sorted(valid_states)}"
        )

    reason = data.get("reason")

    try:
        from consensus import ReadinessState, get_consensus_evaluator
    except ImportError:
        from ..consensus import ReadinessState, get_consensus_evaluator  # type: ignore[no-redef]

    try:
        from events import EventType, emit_event
    except ImportError:
        from ..events import EventType, emit_event  # type: ignore[no-redef]

    try:
        store = get_state_store(repo_path)
        store.load_pipeline(pipeline_id)
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

    try:
        evaluator = get_consensus_evaluator()
        readiness = evaluator.update_readiness(
            pipeline_id,
            agent_role_str,
            ReadinessState(state_str),
            reason=reason,
        )

        emit_event(
            EventType.READINESS_CHANGED,
            pipeline_id,
            data={
                "role": agent_role_str,
                "readiness_state": state_str,
                "reason": reason,
            },
        )

        # Check if consensus has been reached
        consensus = evaluator.evaluate(pipeline_id)

        logger.info(
            "Readiness signal",
            pipeline_id=pipeline_id,
            role=agent_role_str,
            state=state_str,
            consensus_complete=consensus["is_complete"],
        )

        return make_success_response(
            f"Readiness updated: {agent_role_str} -> {state_str}",
            data={
                "readiness": {
                    "role": readiness.role,
                    "state": readiness.state.value,
                    "reason": readiness.reason,
                },
                "consensus": {
                    "is_complete": consensus["is_complete"],
                    "blocking_agents": consensus["blocking_agents"],
                },
            },
        )
    except Exception as e:
        logger.error(
            "Failed to process readiness signal",
            pipeline_id=pipeline_id,
            role=agent_role_str,
            state=state_str,
            error=str(e),
        )
        return make_error_response(
            f"Failed to process readiness signal: {e}",
            status_code=500,
        )


def _validate_tester_check_coverage(pipeline_id: str, payload: dict[str, Any]) -> None:
    """Validate that tester proposals report all configured repo checks as passed.

    Compares the ``checks_passed`` list in the tester's attestation against the
    checks configured in ``repositories.yaml``.  Raises ``ValueError`` if any
    configured check is missing (i.e. did not pass), which prevents the proposal
    from being recorded (issues #1459, #1467).
    """
    attestation = payload.get("attestation", {})

    # Skip validation when tests were blocked — mirrors attestation-level
    # behaviour in attestation_schemas.py (issue #1459).
    if attestation.get("tests_execution_blocked"):
        return

    checks_passed = {name.lower() for name in attestation.get("checks_passed", [])}
    if not checks_passed:
        # Empty checks_passed is already caught by strict attestation validation,
        # but guard here for completeness.
        return

    # Load configured checks for the pipeline's repo.
    try:
        from pipeline_state import get_pipeline_state_store

        store = get_pipeline_state_store()
        pip = store.load_pipeline(pipeline_id)
        repo = getattr(pip.config, "repo", None)
    except Exception:
        # If we can't determine the repo, skip coverage validation
        # (strict attestation validation still enforces checks_passed non-empty).
        return

    if not repo:
        return

    try:
        from config.repo_config import get_repo_checks
    except ImportError:
        try:
            from repo_config import get_repo_checks  # type: ignore[no-redef]
        except ImportError:
            return

    try:
        configured_checks = get_repo_checks(repo)
    except Exception:
        return

    if not configured_checks:
        return

    configured_names = {check["name"].lower() for check in configured_checks}
    missing = configured_names - checks_passed
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(
            f"Tester proposal is missing passing checks: {missing_list}. "
            f"All checks from repositories.yaml must pass before proposing "
            f"consensus. Fix failing checks and re-propose."
        )


def handle_consensus_propose_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_PROPOSE signal from a producer agent."""
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    payload = data.get("payload", {})
    if not payload:
        return make_error_response("Missing payload")

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id)
    if not tracker:
        return make_error_response(f"No consensus tracker for pipeline {pipeline_id}", 404)

    # Verify commit SHA exists on the expected branch before accepting
    # the proposal (#1473).  Reuses _verify_commit_on_branch() from the
    # completion handler — graceful degradation on network errors (None).
    commit_sha = payload.get("commit_sha", "")
    if commit_sha:
        try:
            store_mod = get_state_store(repo_path)
            pipeline = store_mod.load_pipeline(pipeline_id)
            if pipeline.branch:
                worktree_path = resolve_worktree_path(pipeline_id, repo_path)
                branch_verified = _verify_commit_on_branch(
                    commit_sha, pipeline.branch, worktree_path, pipeline_id,
                )
                if branch_verified is False:
                    return make_error_response(
                        f"Proposal rejected: commit {commit_sha} not found on "
                        f"expected branch {pipeline.branch}. Push your work before "
                        f"proposing consensus.",
                        status_code=409,
                        details={
                            "commit_sha": commit_sha,
                            "expected_branch": pipeline.branch,
                            "pipeline_id": pipeline_id,
                        },
                    )
        except Exception as e:
            logger.warning(
                "Could not verify commit on branch (non-blocking)",
                pipeline_id=pipeline_id,
                commit_sha=commit_sha,
                error=str(e),
            )

    try:
        # Validate tester proposals cover all configured repo checks (#1459).
        # Must run BEFORE handle_propose to avoid mutating tracker state on
        # rejected proposals.
        if agent_role == "tester":
            _validate_tester_check_coverage(pipeline_id, payload)

        # Check if this is a re-proposal
        changed_artifacts = data.get("changed_artifacts")
        if changed_artifacts:
            result = tracker.handle_re_propose(agent_role, payload, changed_artifacts)
        else:
            result = tracker.handle_propose(agent_role, payload)

        # Write consensus message to message bus
        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=agent_role,
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject=f"Proposal from {agent_role}",
                body=payload.get("summary", ""),
                metadata={
                    "payload": payload,
                    "version": result.get("version"),
                    "commit_sha": commit_sha,
                },
            )
        )

        # Notify stale reviewers that they need to re-review.  Includes
        # both reviewers who confirmed on a prior version and reviewers
        # whose pre-proposal ACKs (version 0) were invalidated.
        for stale_reviewer in result.get("stale_reviewers", []):
            store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role="orchestrator",
                    to_role=stale_reviewer,
                    message_type=MessageType.CONSENSUS_RE_REVIEW,
                    subject=f"Re-review required: {agent_role} submitted new proposal v{result.get('version')}",
                    body=(
                        f"Producer {agent_role} has submitted a new proposal "
                        f"(version {result.get('version')}) after withdrawal. "
                        f"Your previous confirmation was on an earlier version. "
                        f"Please re-review and ACK/NACK the new proposal."
                    ),
                    metadata={
                        "producer_role": agent_role,
                        "version": result.get("version"),
                    },
                )
            )

        return make_success_response(
            f"Proposal recorded for {agent_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        logger.error(
            "Failed to process consensus propose",
            pipeline_id=pipeline_id,
            role=agent_role,
            error=str(e),
        )
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def handle_consensus_ack_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_ACK signal from a reviewer agent."""
    reviewer_role = data.get("agent_role")
    producer_role = data.get("producer_role")
    if not reviewer_role:
        return make_error_response("Missing agent_role")
    if not producer_role:
        return make_error_response("Missing producer_role")

    payload = data.get("payload", {})

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id)
    if not tracker:
        return make_error_response(f"No consensus tracker for pipeline {pipeline_id}", 404)

    try:
        result = tracker.handle_ack(reviewer_role, producer_role, payload)

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=reviewer_role,
                to_role=producer_role,
                message_type=MessageType.CONSENSUS_ACK,
                subject=f"ACK from {reviewer_role} for {producer_role}",
                body=payload.get("reason", ""),
                metadata={"payload": payload, "version": result.get("version")},
            )
        )

        # Notify the producer when all reviewers have ACKed so it can confirm
        if result.get("fully_acked"):
            store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role="orchestrator",
                    to_role=producer_role,
                    message_type=MessageType.STATUS,
                    subject="All reviewers have ACKed — ready to confirm",
                    body=f"All assigned reviewers have ACKed your proposal (version {result.get('version')}). "
                    "Run `egg-orch consensus confirmed` to confirm.",
                    metadata={"fully_acked": True, "version": result.get("version")},
                )
            )

        return make_success_response(
            f"ACK recorded: {reviewer_role} -> {producer_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        logger.error(
            "Failed to process consensus ACK",
            pipeline_id=pipeline_id,
            reviewer=reviewer_role,
            producer=producer_role,
            error=str(e),
        )
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def handle_consensus_nack_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_NACK signal from a reviewer agent."""
    reviewer_role = data.get("agent_role")
    producer_role = data.get("producer_role")
    if not reviewer_role:
        return make_error_response("Missing agent_role")
    if not producer_role:
        return make_error_response("Missing producer_role")

    payload = data.get("payload", {})

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id)
    if not tracker:
        return make_error_response(f"No consensus tracker for pipeline {pipeline_id}", 404)

    try:
        result = tracker.handle_nack(reviewer_role, producer_role, payload)

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=reviewer_role,
                to_role=producer_role,
                message_type=MessageType.CONSENSUS_NACK,
                subject=f"NACK from {reviewer_role} for {producer_role}",
                body=payload.get("reason", ""),
                metadata={
                    "payload": payload,
                    "reason": result.get("reason"),
                    "revision_count": result.get("revision_count"),
                },
            )
        )

        return make_success_response(
            f"NACK recorded: {reviewer_role} -> {producer_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        logger.error("Failed to process consensus NACK", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def handle_consensus_withdraw_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_WITHDRAW signal from a producer agent."""
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    reason = data.get("reason", "")

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id)
    if not tracker:
        return make_error_response(f"No consensus tracker for pipeline {pipeline_id}", 404)

    try:
        result = tracker.handle_withdraw(agent_role, reason)

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=agent_role,
                to_role="all",
                message_type=MessageType.CONSENSUS_WITHDRAW,
                subject=f"Withdrawal by {agent_role}",
                body=reason,
            )
        )

        return make_success_response(
            f"Withdrawal recorded for {agent_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        logger.error("Failed to process consensus withdraw", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def _write_consensus_confirmed_marker(
    pipeline_id: str, agent_role: str, repo_path: Path
) -> None:
    """Write a marker file so auto-commit skips push after BRC confirmation (#1473)."""
    try:
        worktree_path = resolve_worktree_path(pipeline_id, repo_path)
        marker_dir = worktree_path / ".egg-state" / "agent-outputs"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_file = marker_dir / "consensus-confirmed"
        marker_file.write_text(f"{agent_role}\n", encoding="utf-8")
    except Exception as e:
        logger.warning(
            "Failed to write consensus-confirmed marker (non-blocking)",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            error=str(e),
        )


def handle_consensus_confirmed_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_CONFIRMED signal from an agent."""
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id)
    if not tracker:
        # Defaults must be outside the try block so the message-bus fallback
        # (second try block) can reference them even if reconstruction fails.
        _phase = "implement"
        _repo = None

        # Attempt reconstruction from message store before returning 404
        try:
            from peer_consensus import reconstruct_tracker_from_messages
            from review_graph import get_review_graph_for_phase

            # Determine phase and repo from pipeline state if available
            try:
                from pipeline_state import get_pipeline_state_store

                _store = get_pipeline_state_store()
                _pip = _store.load_pipeline(pipeline_id)
                _phase = _pip.current_phase.value
                _repo = getattr(_pip.config, "repo", None)
            except Exception:
                pass

            graph = get_review_graph_for_phase(_phase, repo=_repo)
            tracker = reconstruct_tracker_from_messages(pipeline_id, graph)
        except Exception as recon_err:
            logger.warning(
                "Tracker reconstruction failed in confirmed handler",
                pipeline_id=pipeline_id,
                error=str(recon_err),
            )

        if not tracker:
            # Message-bus authoritative fallback: if all expected roles have
            # CONSENSUS_CONFIRMED messages, accept the confirmation directly.
            try:
                from message_store import Message, MessageType, get_message_store
                from review_graph import get_review_graph_for_phase

                store = get_message_store()
                messages = store.get_messages(pipeline_id, limit=10000)
                confirmed_roles = {
                    m.from_role for m in messages if m.message_type == "CONSENSUS_CONFIRMED"
                }
                # Agent sending this signal is also confirming
                confirmed_roles.add(agent_role)

                graph = get_review_graph_for_phase(_phase, repo=_repo)
                all_roles = graph.all_roles()

                if all_roles and all_roles.issubset(confirmed_roles):
                    logger.info(
                        "All roles confirmed via message bus (tracker lost)",
                        pipeline_id=pipeline_id,
                        confirmed_roles=sorted(confirmed_roles),
                    )
                    # Write the CONSENSUS_CONFIRMED message
                    store.add_message(
                        Message(
                            pipeline_id=pipeline_id,
                            from_role=agent_role,
                            to_role="all",
                            message_type=MessageType.CONSENSUS_CONFIRMED,
                            subject=f"Confirmed by {agent_role}",
                            body="",
                            metadata={"consensus_reached": True, "fallback": "message_bus"},
                        )
                    )
                    _write_consensus_confirmed_marker(pipeline_id, agent_role, repo_path)
                    return make_success_response(
                        f"Confirmation recorded for {agent_role} (message-bus fallback)",
                        data={
                            "status": "confirmed",
                            "consensus_reached": True,
                            "fallback": "message_bus",
                        },
                    )
            except Exception as fallback_err:
                logger.warning(
                    "Message-bus fallback failed in confirmed handler",
                    pipeline_id=pipeline_id,
                    error=str(fallback_err),
                )

            return make_error_response(f"No consensus tracker for pipeline {pipeline_id}", 404)

    try:
        result = tracker.handle_confirmed(agent_role)

        # If the producer is waiting for reviewer re-ACKs (e.g. after a
        # re-proposal invalidated stale ACKs), return 202 so the agent
        # knows to retry later instead of treating it as an error.
        # Note: we intentionally skip writing a CONSENSUS_CONFIRMED message
        # to the message store here — the agent hasn't actually confirmed,
        # so peers polling for CONSENSUS_CONFIRMED won't see a premature one.
        if result.get("status") == "pending_acks":
            return make_success_response(result["message"], data=result, status_code=202)

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=agent_role,
                to_role="all",
                message_type=MessageType.CONSENSUS_CONFIRMED,
                subject=f"Confirmed by {agent_role}",
                body="",
                metadata={"consensus_reached": result.get("consensus_reached", False)},
            )
        )

        # Write consensus-confirmed marker so auto-commit can detect that
        # BRC review is complete and skip pushing unreviewed WIP (#1473).
        _write_consensus_confirmed_marker(pipeline_id, agent_role, repo_path)

        return make_success_response(
            f"Confirmation recorded for {agent_role}",
            data=result,
        )
    except ValueError as e:
        logger.error("Failed to process consensus confirmed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 400)
    except Exception as e:
        logger.error("Failed to process consensus confirmed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 500)


@signals_bp.route("/<pipeline_id>/signal/batch", methods=["POST"])
def handle_batch_signals(pipeline_id: str) -> tuple[Response, int]:
    """
    Handle multiple signals in a batch.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "signals": [
                {"signal_type": "complete", "agent_role": "coder", ...},
                {"signal_type": "complete", "agent_role": "tester", ...}
            ]
        }

    Response:
        {
            "success": true,
            "data": {
                "processed": 2,
                "results": [...]
            }
        }
    """
    data = request.get_json()
    if not data or "signals" not in data:
        return make_error_response("Missing signals array")

    signals = data["signals"]
    if not isinstance(signals, list):
        return make_error_response("signals must be an array")

    repo_path = get_repo_path()
    repo_path = resolve_repo_path_for_pipeline(pipeline_id, repo_path)
    results = []

    for signal in signals:
        signal_type = signal.get("signal_type", "unknown")

        try:
            # Re-use single signal handling
            handlers = {
                "complete": handle_complete_signal,
                "progress": handle_progress_signal,
                "error": handle_error_signal,
                "heartbeat": handle_heartbeat_signal,
                "readiness": handle_readiness_signal,
                "consensus_propose": handle_consensus_propose_signal,
                "consensus_ack": handle_consensus_ack_signal,
                "consensus_nack": handle_consensus_nack_signal,
                "consensus_withdraw": handle_consensus_withdraw_signal,
                "consensus_confirmed": handle_consensus_confirmed_signal,
            }

            handler = handlers.get(signal_type)
            if handler:
                response, status = handler(pipeline_id, signal, repo_path)
                results.append(
                    {
                        "signal_type": signal_type,
                        "success": status in (200, 202),
                        "pending": status == 202,
                        "response": response.get_json(),
                    }
                )
            else:
                results.append(
                    {
                        "signal_type": signal_type,
                        "success": False,
                        "error": f"Unknown signal type: {signal_type}",
                    }
                )

        except Exception as e:
            results.append(
                {
                    "signal_type": signal_type,
                    "success": False,
                    "error": str(e),
                }
            )

    return make_success_response(
        f"Processed {len(results)} signal(s)",
        data={
            "processed": len(results),
            "results": results,
        },
    )
