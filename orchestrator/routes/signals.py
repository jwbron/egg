"""
Signal endpoints for sandbox callbacks.

Provides REST endpoints for sandboxes to report completion,
progress updates, and errors back to the orchestrator.
"""

import subprocess
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


from dispatch import create_dispatcher, map_agent_role_to_contract_role
from egg_contracts.loader import ContractNotFoundError
from handoffs import AgentOutput, save_agent_output
from models import AgentRole
from state_store import InvalidPipelineIdError, PipelineNotFoundError, get_state_store

logger = get_logger("orchestrator.signals")

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
) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


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

        # Only interact with the contract dispatcher for roles that have
        # a contract mapping (multi-agent phases: plan, implement).
        # Single-agent roles like REFINER and REVIEWER_REFINE don't
        # participate in contract orchestration.
        has_contract_role = map_agent_role_to_contract_role(agent_role) is not None

        if has_contract_role:
            dispatcher = create_dispatcher(pipeline, contract_path)
            dispatcher.complete_agent(agent_role, commit=commit, outputs=outputs)
            dispatcher.save_contract()
            is_complete = dispatcher.is_complete()
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
            # Derive pipeline identifier matching PipelineDispatcher.contract_key
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

        # Contracts live in per-pipeline worktrees, not the main repo.
        contract_path = resolve_worktree_path(pipeline_id, repo_path)

        # Only interact with the contract dispatcher for roles that have
        # a contract mapping.  Single-agent roles (REFINER, REVIEWER_REFINE,
        # etc.) don't participate in contract orchestration.
        has_contract_role = map_agent_role_to_contract_role(agent_role) is not None

        if has_contract_role:
            dispatcher = create_dispatcher(pipeline, contract_path)
            dispatcher.fail_agent(agent_role, error_message)
            dispatcher.save_contract()

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
            "timestamp": datetime.utcnow().isoformat(),
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

    try:
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
                metadata={"payload": payload, "version": result.get("version")},
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
        return make_error_response(f"No consensus tracker for pipeline {pipeline_id}", 404)

    try:
        result = tracker.handle_confirmed(agent_role)

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

        return make_success_response(
            f"Confirmation recorded for {agent_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        logger.error("Failed to process consensus confirmed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


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
                        "success": status == 200,
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
