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
from models import AgentExecutionStatus, AgentRole, Pipeline, PipelineStatus

# Slice-aware consensus routing (#2403): per-slice agents tag their
# signals with a ``slice_id`` field on the request body so the
# orchestrator routes each ``CONSENSUS_*`` to the per-slice tracker
# (``peer_consensus._tracker_key`` composes ``{pipeline_id}/{slice_id}``).
# The canonical extractor lives in ``slice_id_validation`` so the
# operator-triggered restart route (#2410) and the gateway-bound branch
# builders in ``concurrent_executor`` validate against the same shape.
# The alias below preserves the existing private name for the many
# handler call sites in this file.
from slice_id_validation import (
    extract_slice_id as _extract_slice_id,
)
from state_store import (
    InvalidPipelineIdError,
    PipelineNotFoundError,
    StateStoreError,
    get_state_store,
)

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


# --- BRC content validation (#1716) ---
_BRC_MIN_CONTENT_LEN = 50
# Pre-merge conditions are imperative instructions (e.g. "git mv X Y"),
# not full rationale, so they have a lower minimum length (#2005).
_BRC_CONDITION_MIN_LEN = 10
_BRC_BOILERPLATE = frozenset({"lgtm", "looks good", "no issues", "approved", "ok"})

# Kinds whose content is an imperative instruction rather than rationale.
_BRC_CONDITION_KINDS = frozenset({"pre-merge condition"})


def _validate_brc_content(body: str, kind: str) -> str | None:
    """Validate that BRC message content is substantive.

    Returns an error message string if validation fails, or None if content
    is acceptable.  ``kind`` is a human-readable label for the message type
    (e.g. "proposal summary", "ACK reason") used in error messages.

    Content kinds whose lowercase form appears in ``_BRC_CONDITION_KINDS``
    use a shorter minimum length because they are imperative instructions
    (e.g. "git mv X Y") rather than full rationale.
    """
    stripped = (body or "").strip()
    if not stripped:
        return f"{kind} must not be empty"
    if stripped.lower() in _BRC_BOILERPLATE:
        return (
            f"{kind} is boilerplate ('{stripped}'). Provide substantive rationale: "
            f"what was read/built, what was checked/tested, why the verdict follows"
        )
    min_length = (
        _BRC_CONDITION_MIN_LEN if kind.lower() in _BRC_CONDITION_KINDS else _BRC_MIN_CONTENT_LEN
    )
    if len(stripped) < min_length:
        return (
            f"{kind} is too short ({len(stripped)} chars, minimum {min_length}). "
            f"Provide substantive rationale: what was read/built, what was checked/tested, "
            f"why the verdict follows"
        )
    return None


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
        "consensus_producer_push": handle_consensus_producer_push_signal,
        "consensus_excuse_producer": handle_consensus_excuse_producer_signal,
        "consensus_resolve_obligation": handle_consensus_resolve_obligation_signal,
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
            # Contracts are keyed by pipeline_id (includes any qualifier) so
            # qualified pipelines resolve to the correct contract file. The
            # loader's compat shim handles legacy pre-unification paths.
            contract = load_contract(pipeline_id, contract_path)
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

    # Slice-scope the "already COMPLETE" suppression check below — without
    # this, a slice-2 coder finishing would silently swallow a slice-3
    # coder's error because both records share ``phase_execution.agents``
    # (#2422). The sandbox attaches ``slice_id`` on per-slice agents via
    # ``progress._maybe_attach_slice_id``; pipeline-level agents send no
    # ``slice_id`` and this resolves to ``None`` (matches non-sliced
    # records). ``_extract_slice_id`` rejects malformed values the same
    # way the BRC handlers do.
    try:
        signal_slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(f"Invalid slice_id: {exc}")

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

        # Suppress errors from agents already marked COMPLETE by the
        # consensus path.  _update_agents_complete() runs before containers
        # are stopped, so the agent is COMPLETE by the time this error
        # signal arrives.  See issue #1495.
        phase_key = pipeline.current_phase.value
        phase_execution = pipeline.phases.get(phase_key)
        if phase_execution is not None:
            for agent in phase_execution.agents:
                if (
                    agent.role == agent_role
                    and getattr(agent, "slice_id", None) == signal_slice_id
                    and agent.status == AgentExecutionStatus.COMPLETE
                ):
                    logger.info(
                        "Agent already COMPLETE, suppressing error signal (consensus path)",
                        pipeline_id=pipeline_id,
                        role=agent_role.value,
                        slice_id=signal_slice_id,
                    )
                    return make_success_response(
                        "Error suppressed (agent already complete)",
                        data={
                            "agent_role": agent_role.value,
                            "already_complete": True,
                        },
                    )

        # Contracts live in per-pipeline worktrees, not the main repo.
        contract_path = resolve_worktree_path(pipeline_id, repo_path)

        # Only interact with the contract for roles that have a contract
        # mapping.  Single-agent roles (REFINER, REVIEWER_REFINE, etc.)
        # don't participate in contract orchestration.
        contract_role = _AGENT_ROLE_TO_CONTRACT_ROLE.get(agent_role)

        if contract_role is not None:
            # Contracts are keyed by pipeline_id (includes any qualifier) so
            # qualified pipelines resolve to the correct contract file. The
            # loader's compat shim handles legacy pre-unification paths.
            contract = load_contract(pipeline_id, contract_path)
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


def _validate_tester_check_coverage(
    pipeline_id: str, payload: dict[str, Any], repo_path: Path
) -> None:
    """Validate that tester proposals report all configured repo checks as passed.

    Compares the ``checks_passed`` list in the tester's attestation against the
    checks configured in ``repositories.yaml``.  Raises ``ValueError`` if any
    configured check is missing (i.e. did not pass), which prevents the proposal
    from being recorded (issues #1459, #1467, #1966).
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

    try:
        pipeline = get_state_store(repo_path).load_pipeline(pipeline_id)
    except StateStoreError:
        return

    repo = pipeline.repo
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
        logger.warning(
            "Failed to load repo checks config, skipping coverage validation",
            pipeline_id=pipeline_id,
            repo=repo,
        )
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


def _resolve_pipeline_phase(pipeline_id: str, repo_path: Path) -> str:
    """Resolve the current phase for a pipeline, with graceful fallback.

    Loads the pipeline from the state store and returns the current phase
    name as a string.  Falls back to ``"implement"`` (the most common BRC
    phase) if loading fails for any reason — this keeps Message creation
    from silently dropping the phase field.
    """
    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        return pipeline.current_phase.value
    except Exception:
        return "implement"


def _emit_ready_to_confirm_nudges(
    pipeline_id: str,
    phase: str,
    newly_ready: list[dict[str, Any]],
    tracker: Any = None,
) -> None:
    """Emit a STATUS to each producer that newly became ready to confirm.

    The tracker returns producers whose ``check_confirm_guard`` now passes —
    not just those that are fully ACKed by their critical reviewers.  This
    closes the gap where a documenter (advisory-only) was nudged the moment
    its single ADVISORY ACK arrived, even though the global zero-proposal
    guard still rejected confirm (#2078).

    If ``add_message`` raises for a given producer and a ``tracker`` is
    supplied, the per-version memo entry is rolled back so the producer
    can be re-nudged on the next state change.  Other producers in the
    batch are still attempted.
    """
    if not newly_ready:
        return
    from message_store import Message, MessageType, get_message_store

    store = get_message_store()
    for entry in newly_ready:
        producer = entry["role"]
        version = entry["version"]
        try:
            store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role="orchestrator",
                    to_role=producer,
                    message_type=MessageType.STATUS,
                    subject="Ready to confirm — all confirm preconditions satisfied",
                    body=(
                        f"Your proposal (version {version}) is ready to confirm — "
                        f"all blocking reviews are clear and global confirm "
                        f"preconditions are met. Run "
                        f"`egg-orch consensus confirmed` to confirm."
                    ),
                    phase=phase,
                    metadata={"ready_to_confirm": True, "version": version},
                )
            )
        except Exception as exc:
            if tracker is not None:
                tracker.release_nudge(producer, version)
            logger.error(
                "Failed to emit ready-to-confirm nudge",
                pipeline_id=pipeline_id,
                role=producer,
                version=version,
                error=str(exc),
            )


def _stale_version_rejection(
    tracker: Any,
    producer_role: str,
    err_message: str,
    reviewer_role: str,
    verdict: str,
) -> tuple[Response, int] | None:
    """Build a structured 409 for stale-version ACK / NACK rejections (#2142).

    Returns the (response, status) tuple when ``err_message`` came from the
    version-match guard, or ``None`` to let the caller raise normally.  The
    rejection inlines the producer's current proposal snapshot so the
    reviewer can re-review the latest version without a separate fetch.
    """
    if "version mismatch" not in err_message.lower():
        return None
    snapshot = tracker.get_current_proposal_snapshot(producer_role)
    logger.warning(
        f"{verdict} rejected: stale proposal version",
        reviewer=reviewer_role,
        producer=producer_role,
        current_version=snapshot.get("version"),
    )
    return make_error_response(
        err_message,
        status_code=409,
        details={
            "status": "stale_version",
            "reviewer": reviewer_role,
            "verdict": verdict,
            "current_proposal": snapshot,
        },
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

    # Validate proposal summary content (#1716)
    summary_error = _validate_brc_content(payload.get("summary", ""), "Proposal summary")
    if summary_error:
        return make_error_response(summary_error, 400)

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

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
                    commit_sha,
                    pipeline.branch,
                    worktree_path,
                    pipeline_id,
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
            _validate_tester_check_coverage(pipeline_id, payload, repo_path)

        # Check if this is a re-proposal
        changed_artifacts = data.get("changed_artifacts")
        if changed_artifacts:
            result = tracker.handle_re_propose(agent_role, payload, changed_artifacts)
        else:
            result = tracker.handle_propose(agent_role, payload)

        # Open-NACK barrier rejection (#2142): re_propose returned a
        # structured rejection because NACKs against the current version
        # had not yet been delivered to the producer.  Surface every NACK
        # inline (full reason text + artifact refs) so the producer can
        # aggregate them into one re-propose without a separate fetch.
        if isinstance(result, dict) and result.get("status") == "open_nacks_blocked":
            logger.warning(
                "re_propose blocked by open NACKs",
                pipeline_id=pipeline_id,
                role=agent_role,
                version=result.get("current_version"),
                nacking_reviewers=result.get("nacking_reviewers"),
            )
            return make_error_response(
                result.get("message", "Re-propose blocked: unresolved NACKs"),
                status_code=409,
                details=result,
            )

        # Write consensus message to message bus
        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        phase = _resolve_pipeline_phase(pipeline_id, repo_path)
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=agent_role,
                to_role="all",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject=f"Proposal from {agent_role}",
                body=payload.get("summary", ""),
                phase=phase,
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
                    phase=phase,
                    metadata={
                        "producer_role": agent_role,
                        "version": result.get("version"),
                    },
                )
            )

        # A new proposal can unblock the global zero-proposal guard for
        # producers that were previously fully ACKed but unable to confirm.
        _emit_ready_to_confirm_nudges(pipeline_id, phase, result.get("newly_ready", []), tracker)

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

    # Forward ack_version from signal data into the payload so the
    # version-match guard can detect stale ACKs.
    if "ack_version" in data and "ack_version" not in payload:
        payload["ack_version"] = int(data["ack_version"])

    # Validate ACK reason content (#1716)
    reason_error = _validate_brc_content(payload.get("reason", ""), "ACK reason")
    if reason_error:
        return make_error_response(reason_error, 400)

    # Validate pre-merge condition content when present (#2005). An empty
    # or whitespace-only condition is a plain ACK, not a conditional ACK,
    # and must pass through unaffected.
    pre_merge_condition = (payload.get("pre_merge_condition") or "").strip()
    if pre_merge_condition:
        condition_error = _validate_brc_content(pre_merge_condition, "Pre-merge condition")
        if condition_error:
            return make_error_response(condition_error, 400)

    # A resolution SHA without an obligation has nothing to resolve (#2336);
    # reject at the boundary so downstream code can assume the invariant.
    pre_merge_condition_resolved_in_diff = (
        payload.get("pre_merge_condition_resolved_in_diff") or ""
    ).strip()
    if pre_merge_condition_resolved_in_diff and not pre_merge_condition:
        return make_error_response(
            "pre_merge_condition_resolved_in_diff requires a non-empty "
            "pre_merge_condition; a resolution SHA has nothing to resolve "
            "on a plain ACK",
            400,
        )

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        try:
            result = tracker.handle_ack(reviewer_role, producer_role, payload)
        except ValueError as ack_err:
            stale_response = _stale_version_rejection(
                tracker, producer_role, str(ack_err), reviewer_role, "ACK"
            )
            if stale_response is not None:
                return stale_response
            raise

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        phase = _resolve_pipeline_phase(pipeline_id, repo_path)
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=reviewer_role,
                to_role=producer_role,
                message_type=MessageType.CONSENSUS_ACK,
                subject=f"ACK from {reviewer_role} for {producer_role}",
                body=payload.get("reason", ""),
                phase=phase,
                metadata={"payload": payload, "version": result.get("version")},
            )
        )

        # Nudge any producer that the tracker says is now ready to confirm —
        # i.e. ``check_confirm_guard`` actually passes, not just the
        # critical-reviewer ACK predicate.  Replaces the prior ``fully_acked``
        # gate which fired before global guards (e.g. zero-proposal) cleared
        # and could mislead an advisory-only producer like documenter (#2078).
        _emit_ready_to_confirm_nudges(pipeline_id, phase, result.get("newly_ready", []), tracker)

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

    # Forward nack_version from signal data into the payload so the
    # version-match guard can detect stale NACKs (#2142).
    if "nack_version" in data and "nack_version" not in payload:
        payload["nack_version"] = int(data["nack_version"])

    # Validate NACK reason content (#1716)
    reason_error = _validate_brc_content(payload.get("reason", ""), "NACK reason")
    if reason_error:
        return make_error_response(reason_error, 400)

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        try:
            result = tracker.handle_nack(reviewer_role, producer_role, payload)
        except ValueError as nack_err:
            stale_response = _stale_version_rejection(
                tracker, producer_role, str(nack_err), reviewer_role, "NACK"
            )
            if stale_response is not None:
                return stale_response
            raise

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
                phase=_resolve_pipeline_phase(pipeline_id, repo_path),
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

    # Validate withdrawal reason content (#1716)
    reason_error = _validate_brc_content(reason, "Withdrawal reason")
    if reason_error:
        return make_error_response(reason_error, 400)

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

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
                phase=_resolve_pipeline_phase(pipeline_id, repo_path),
            )
        )

        return make_success_response(
            f"Withdrawal recorded for {agent_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        logger.error("Failed to process consensus withdraw", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def _write_consensus_confirmed_marker(pipeline_id: str, agent_role: str, repo_path: Path) -> None:
    """Write a marker file so auto-commit skips push after BRC confirmation (#1473)."""
    try:
        worktree_path = resolve_worktree_path(pipeline_id, repo_path)
        marker_dir = worktree_path / ".egg-state" / "agent-outputs"
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_file = marker_dir / "consensus-confirmed"
        marker_file.touch()
    except Exception as e:
        logger.warning(
            "Failed to write consensus-confirmed marker (non-blocking)",
            pipeline_id=pipeline_id,
            agent_role=agent_role,
            error=str(e),
        )


def _existing_confirmed_for_role(
    pipeline_id: str,
    agent_role: str,
    phase: str | None,
) -> tuple[bool, bool]:
    """Return (has_final, has_pending_acks) for prior CONFIRMED messages.

    Scans the message store for prior ``CONSENSUS_CONFIRMED`` messages
    from ``agent_role`` in ``phase``.  Used for idempotency so that
    repeated ``egg-orch consensus confirmed`` invocations don't pollute
    the bus with duplicate messages (see #1890).

    - ``has_final``: a non-pending_acks CONFIRMED message already exists.
    - ``has_pending_acks``: a pending_acks CONFIRMED message exists.
    """
    try:
        from message_store import get_message_store
    except ImportError:
        try:
            from ..message_store import get_message_store  # type: ignore[no-redef]
        except ImportError:
            return (False, False)

    try:
        store = get_message_store()
        # Fetch a generous window of recent messages.  get_messages returns
        # the *newest* N, so an extremely old CONFIRMED in a >10k-message
        # pipeline could be missed — but that's the safe failure direction
        # (a duplicate write, not a lost write).  Don't lower this limit
        # without understanding that tradeoff.
        messages = store.get_messages(pipeline_id, limit=10000)
    except Exception:
        return (False, False)

    has_final = False
    has_pending = False
    for m in messages:
        if getattr(m, "from_role", None) != agent_role:
            continue
        if str(getattr(m, "message_type", "")) != "CONSENSUS_CONFIRMED":
            continue
        msg_phase = getattr(m, "phase", None)
        # A null msg_phase is treated as matching any phase.  In practice
        # all CONSENSUS_CONFIRMED writes set a phase, but if one somehow
        # doesn't, counting it as a match is the conservative choice
        # (prevents a duplicate rather than allowing one).
        if phase is not None and msg_phase is not None and msg_phase != phase:
            continue
        metadata = getattr(m, "metadata", None) or {}
        if metadata.get("pending_acks"):
            has_pending = True
        else:
            has_final = True
    return (has_final, has_pending)


def handle_consensus_confirmed_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_CONFIRMED signal from an agent.

    Idempotent with respect to the message store: repeated invocations
    from the same agent in the same phase do not pollute the bus with
    duplicate CONFIRMED messages (see #1890).  The underlying consensus
    tracker still observes each call so its own state stays in sync.
    """
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        # Defaults must be outside the try block so the message-bus fallback
        # (second try block) can reference them even if reconstruction fails.
        _phase = "implement"
        _repo = None

        # Attempt reconstruction from message store before returning 404.
        # Slice-scoped trackers are NOT reconstructed today: the message
        # store keys messages by bare pipeline_id only, so a per-slice
        # replay would mingle other slices' messages and reach false
        # consensus. Tracked in #2409 (orchestrator-restart recovery for
        # slice-scoped trackers; needs a slice_id field on Message and a
        # filtered replay). For pipeline-level (slice_id is None) requests
        # the existing replay path is unchanged.
        try:
            from peer_consensus import reconstruct_tracker_from_messages
            from review_graph import get_review_graph_for_phase

            # Determine phase and repo from pipeline state if available
            try:
                _pip = get_state_store(repo_path).load_pipeline(pipeline_id)
                _phase = _pip.current_phase.value
                _repo = _pip.repo
            except StateStoreError:
                pass

            if slice_id is None:
                graph = get_review_graph_for_phase(_phase, repo=_repo)
                tracker = reconstruct_tracker_from_messages(pipeline_id, graph)
        except Exception as recon_err:
            logger.warning(
                "Tracker reconstruction failed in confirmed handler",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(recon_err),
            )

        if not tracker and slice_id is not None:
            # Slice-scoped: pipeline-wide message-bus fallback would mingle
            # other slices' CONFIRMs and reach false consensus the moment a
            # fresh slice spawns roles matching an already-confirmed prior
            # slice (#2535). Per-slice trackers are recreated by the slice
            # scheduler on the next iteration; surface the missing tracker
            # rather than guessing from sibling-slice state.
            scope = f"{pipeline_id}/{slice_id}"
            return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

        if not tracker:
            # Message-bus authoritative fallback: if all expected roles have
            # CONSENSUS_CONFIRMED messages, accept the confirmation directly.
            try:
                from message_store import Message, MessageType, get_message_store
                from review_graph import get_review_graph_for_phase

                store = get_message_store()
                messages = store.get_messages(pipeline_id, limit=10000)
                # Count ANY CONSENSUS_CONFIRMED message — when the
                # tracker is lost we can't cross-reference _confirmed,
                # so be lenient.  Matches the consensus_stall health
                # check which also doesn't filter pending_acks (#1671).
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
                    # Idempotency: only write the CONFIRMED message if this
                    # role hasn't already emitted a final one in this phase.
                    has_final, _ = _existing_confirmed_for_role(pipeline_id, agent_role, _phase)
                    if not has_final:
                        store.add_message(
                            Message(
                                pipeline_id=pipeline_id,
                                from_role=agent_role,
                                to_role="all",
                                message_type=MessageType.CONSENSUS_CONFIRMED,
                                subject=f"Confirmed by {agent_role}",
                                body="",
                                phase=_phase,
                                metadata={
                                    "consensus_reached": True,
                                    "fallback": "message_bus",
                                },
                            )
                        )
                        _write_consensus_confirmed_marker(pipeline_id, agent_role, repo_path)
                    return make_success_response(
                        f"Confirmation recorded for {agent_role} (message-bus fallback)",
                        data={
                            "status": "confirmed",
                            "consensus_reached": True,
                            "fallback": "message_bus",
                            "idempotent": has_final,
                        },
                    )
            except Exception as fallback_err:
                logger.warning(
                    "Message-bus fallback failed in confirmed handler",
                    pipeline_id=pipeline_id,
                    error=str(fallback_err),
                )

            scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
            return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        result = tracker.handle_confirmed(agent_role)

        # If the producer is waiting for reviewer re-ACKs (e.g. after a
        # re-proposal invalidated stale ACKs), return 202 so the agent
        # knows to retry later instead of treating it as an error.
        # We still write a CONSENSUS_CONFIRMED message to the store (with
        # pending_acks=True metadata) so the message-bus fallback in
        # check_consensus() can detect when all agents have *attempted*
        # confirmation even if the tracker rejected some (#1615).
        current_phase = _resolve_pipeline_phase(pipeline_id, repo_path)
        has_final, has_pending = _existing_confirmed_for_role(
            pipeline_id, agent_role, current_phase
        )

        if result.get("status") == "pending_acks":
            # Dedupe pending_acks writes once an agent has already emitted one
            # (or a final) in this phase — the fallback check only needs one
            # to detect "attempted confirmation" (#1615).
            if not has_pending and not has_final:
                from message_store import Message, MessageType, get_message_store

                store = get_message_store()
                store.add_message(
                    Message(
                        pipeline_id=pipeline_id,
                        from_role=agent_role,
                        to_role="all",
                        message_type=MessageType.CONSENSUS_CONFIRMED,
                        subject=f"Confirmed by {agent_role} (pending_acks)",
                        body=result.get("message", ""),
                        phase=current_phase,
                        metadata={"pending_acks": True},
                    )
                )
            return make_success_response(result["message"], data=result, status_code=202)

        # Final CONFIRMED: skip if this role has already emitted one in this
        # phase.  Prevents the ``egg-orch consensus confirmed`` retry-loop
        # from spraying the bus with duplicates (#1890).
        if not has_final:
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
                    phase=current_phase,
                    metadata={"consensus_reached": result.get("consensus_reached", False)},
                )
            )

            # Write consensus-confirmed marker so auto-commit can detect that
            # BRC review is complete and skip pushing unreviewed WIP (#1473).
            _write_consensus_confirmed_marker(pipeline_id, agent_role, repo_path)

        payload = dict(result)
        if has_final:
            payload["idempotent"] = True
        return make_success_response(
            f"Confirmation recorded for {agent_role}",
            data=payload,
        )
    except ValueError as e:
        logger.error("Failed to process consensus confirmed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 400)
    except Exception as e:
        logger.error("Failed to process consensus confirmed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(str(e), 500)


def handle_consensus_excuse_producer_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle EXCUSE_PRODUCER signal (HITL-gated).

    Removes a non-delivering producer from the review graph so that
    reviewers can proceed without its deliverable.  Requires a resolved
    HITL decision — the ``decision_id`` field must reference a RESOLVED
    decision to prevent unauthorized producer removal.

    Request data:
        producer_role: The producer role to excuse.
        reason: Why the producer is being excused.
        decision_id: ID of the resolved HITL decision authorizing this action.
    """
    producer_role = data.get("producer_role")
    if not producer_role:
        return make_error_response("Missing producer_role")

    reason = data.get("reason", "")

    # --- HITL gate: require a resolved decision ---
    decision_id = data.get("decision_id")
    if not decision_id:
        return make_error_response(
            "Missing decision_id. consensus_excuse_producer requires a "
            "resolved HITL decision. Create a decision via the decisions "
            "API and resolve it before calling this signal.",
            status_code=403,
        )

    try:
        from decision_queue import DecisionNotFoundError, get_decision_queue
    except ImportError:
        from ..decision_queue import (  # type: ignore[no-redef]
            DecisionNotFoundError,
            get_decision_queue,
        )

    try:
        from models import DecisionStatus
    except ImportError:
        from ..models import DecisionStatus  # type: ignore[no-redef]

    try:
        queue = get_decision_queue(pipeline_id, repo_path)
        decision = queue.get_decision(decision_id)
        if decision.status != DecisionStatus.RESOLVED:
            return make_error_response(
                f"Decision {decision_id} is not resolved "
                f"(status: {decision.status.value}). Only resolved HITL "
                f"decisions can authorize producer excusal.",
                status_code=403,
            )

        # Scope validation: the decision must be specifically about
        # excusing *this* producer, not just any resolved decision.
        # Mirrors the excuse_reviewer pattern in decisions.py.
        expected_context = f"failed_role:{producer_role}"
        if decision.context != expected_context:
            return make_error_response(
                f"Decision {decision_id} is not authorized for excusing "
                f"producer {producer_role} (expected context: "
                f"'{expected_context}', got: '{decision.context}').",
                status_code=403,
            )
    except DecisionNotFoundError:
        return make_error_response(
            f"Decision {decision_id} not found. A valid resolved HITL "
            f"decision is required to excuse a producer.",
            status_code=404,
        )

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        result = tracker.excuse_producer(producer_role, reason)

        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        phase = _resolve_pipeline_phase(pipeline_id, repo_path)

        # Notify all agents that the producer has been excused
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.STATUS,
                subject=f"Producer {producer_role} excused from consensus",
                body=(
                    f"Producer {producer_role} has been excused from the consensus "
                    f"protocol (reason: {reason or 'HITL decision'}). Reviewers "
                    f"assigned to this producer are no longer blocked by it."
                ),
                phase=phase,
                metadata={
                    "excuse_producer": True,
                    "producer_role": producer_role,
                    "reason": reason,
                    "affected_reviewers": result.get("affected_reviewers", []),
                },
            )
        )

        return make_success_response(
            f"Producer {producer_role} excused from consensus",
            data=result,
        )
    except (ValueError, Exception) as e:
        logger.error(
            "Failed to excuse producer",
            pipeline_id=pipeline_id,
            producer_role=producer_role,
            error=str(e),
        )
        return make_error_response(str(e), 400 if isinstance(e, ValueError) else 500)


def handle_consensus_resolve_obligation_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle CONSENSUS_RESOLVE_OBLIGATION signal (#2338).

    The caller (typically the tester after cherry-picking the conditioning
    commit) marks a reviewer's conditional-ACK obligation as satisfied
    in-cycle. The matrix keeps the obligation text for audit but
    ``get_pre_merge_conditions`` filters out resolved entries, so the PR
    body and HITL gate stop surfacing the obligation.

    Resolution is per-version: any later ``record_ack`` / ``record_nack`` /
    ``invalidate_ack`` resets the resolved flag. If the same obligation
    re-appears on a later proposal, the satisfier must call this signal
    again (or the reviewer should drop it on re-ACK per the prompt
    guidance in ``code-review-criteria.md``).

    Request data:
        agent_role: Caller's role (the resolver — recorded for audit).
        reviewer_role: Reviewer whose conditional-ACK is being resolved.
        producer_role: Producer the conditional-ACK was attached to.
        commit_sha: Optional commit SHA that satisfies the obligation.
        note: Optional free-form note for the audit log.
    """
    resolver_role = data.get("agent_role")
    reviewer_role = data.get("reviewer_role")
    producer_role = data.get("producer_role")
    if not resolver_role:
        return make_error_response("Missing agent_role")
    if not reviewer_role:
        return make_error_response("Missing reviewer_role")
    if not producer_role:
        return make_error_response("Missing producer_role")

    commit_sha = (data.get("commit_sha") or "").strip()
    note = (data.get("note") or "").strip()

    # Validate the optional resolution note for parity with summary / reason
    # validation on other BRC verbs. Notes are short-form imperatives, not
    # rationale, so they share the relaxed minimum-length bucket with
    # pre-merge conditions (#2338).
    if note:
        note_error = _validate_brc_content(note, "Pre-merge condition")
        if note_error:
            return make_error_response(note_error, 400)

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        result = tracker.handle_resolve_obligation(
            resolver_role=resolver_role,
            reviewer_role=reviewer_role,
            producer_role=producer_role,
            commit_sha=commit_sha,
            note=note,
        )

        # Persist the resolution so ``reconstruct_tracker_from_messages``
        # can replay it after an orchestrator restart (#2338). Without
        # this, a satisfied obligation re-emerges from replay and the
        # HITL gate fires for work that was already done.
        from message_store import Message, MessageType, get_message_store

        store = get_message_store()
        phase = _resolve_pipeline_phase(pipeline_id, repo_path)
        store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role=resolver_role,
                to_role=producer_role,
                message_type=MessageType.CONSENSUS_OBLIGATION_RESOLVED,
                subject=(
                    f"Obligation resolved: {reviewer_role} -> {producer_role} by {resolver_role}"
                ),
                body=note,
                phase=phase,
                metadata={
                    "reviewer_role": reviewer_role,
                    "producer_role": producer_role,
                    "resolver_role": resolver_role,
                    "commit_sha": commit_sha,
                    "note": note,
                    "version": result.get("version"),
                    "condition": result.get("condition", ""),
                },
            )
        )

        return make_success_response(
            f"Obligation resolved: {reviewer_role} -> {producer_role} by {resolver_role}",
            data=result,
        )
    except ValueError as e:
        return make_error_response(str(e), 400)
    except Exception as e:
        logger.error(
            "Failed to resolve obligation",
            pipeline_id=pipeline_id,
            resolver=resolver_role,
            reviewer=reviewer_role,
            producer=producer_role,
            error=str(e),
        )
        return make_error_response(str(e), 500)


def handle_consensus_producer_push_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Handle a producer push/commit that should trigger auto re-proposal.

    When a producer pushes new commits after having already proposed, this
    signal triggers an automatic re-proposal in the consensus tracker.
    Existing ACKs are invalidated and reviewers are notified to re-review.

    Request data:
        agent_role: The producer role that pushed.
        commit_sha: The new commit SHA.
        changed_files: Optional list of changed file paths for scoped
            re-evaluation.
    """
    agent_role = data.get("agent_role")
    if not agent_role:
        return make_error_response("Missing agent_role")

    commit_sha = data.get("commit_sha", "")
    if not commit_sha:
        return make_error_response("Missing commit_sha")

    changed_files = data.get("changed_files")

    try:
        slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(str(exc), 400)

    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[no-redef]

    tracker = get_peer_consensus_tracker(pipeline_id, slice_id)
    if not tracker:
        scope = f"{pipeline_id}/{slice_id}" if slice_id else pipeline_id
        return make_error_response(f"No consensus tracker for pipeline {scope}", 404)

    try:
        result = tracker.handle_producer_push(agent_role, commit_sha, changed_files)

        # If auto re-propose happened, write a message and notify reviewers
        if result.get("auto_re_propose"):
            from message_store import Message, MessageType, get_message_store

            store = get_message_store()
            phase = _resolve_pipeline_phase(pipeline_id, repo_path)
            store.add_message(
                Message(
                    pipeline_id=pipeline_id,
                    from_role=agent_role,
                    to_role="all",
                    message_type=MessageType.CONSENSUS_PROPOSE,
                    subject=f"Auto re-proposal from {agent_role} (push)",
                    body=(
                        f"Producer {agent_role} pushed new commit {commit_sha}. "
                        f"Existing ACKs invalidated; re-review required."
                    ),
                    phase=phase,
                    metadata={
                        "auto_re_propose": True,
                        "trigger": "auto_push",
                        "commit_sha": commit_sha,
                        "version": result.get("version"),
                        "changed_files": changed_files,
                    },
                )
            )

            # Notify invalidated reviewers (deduplicate in case a reviewer
            # appears in both lists)
            notified_reviewers = set(
                result.get("stale_reviewers", []) + result.get("invalidated_reviewers", [])
            )
            for reviewer in notified_reviewers:
                store.add_message(
                    Message(
                        pipeline_id=pipeline_id,
                        from_role="orchestrator",
                        to_role=reviewer,
                        message_type=MessageType.CONSENSUS_RE_REVIEW,
                        subject=(
                            f"Re-review required: {agent_role} pushed new changes "
                            f"(v{result.get('version')})"
                        ),
                        body=(
                            f"Producer {agent_role} has pushed new commits after "
                            f"proposing. Your previous review is invalidated. "
                            f"Please re-review and ACK/NACK the updated work."
                        ),
                        phase=phase,
                        metadata={
                            "producer_role": agent_role,
                            "version": result.get("version"),
                            "commit_sha": commit_sha,
                        },
                    )
                )

            # Auto re-propose runs the same propose path that surfaces
            # newly-ready producers; emit nudges for symmetry with the
            # explicit propose/re-propose handlers.  Today no peer's
            # readiness depends on this producer's version bump (the
            # producer themselves cannot be newly ready since their own
            # ACKs were just invalidated), but skipping the call would
            # silently regress if a future guard depends on peer versions.
            _emit_ready_to_confirm_nudges(
                pipeline_id, phase, result.get("newly_ready", []), tracker
            )

        return make_success_response(
            f"Producer push processed for {agent_role}",
            data=result,
        )
    except (ValueError, Exception) as e:
        logger.error(
            "Failed to process consensus producer push",
            pipeline_id=pipeline_id,
            role=agent_role,
            error=str(e),
        )
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
                "consensus_producer_push": handle_consensus_producer_push_signal,
                "consensus_excuse_producer": handle_consensus_excuse_producer_signal,
                "consensus_resolve_obligation": handle_consensus_resolve_obligation_signal,
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
