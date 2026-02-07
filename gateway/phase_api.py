"""
Phase API endpoints for the gateway.

Provides REST endpoints for phase transitions and operation filtering
in the SDLC pipeline.
"""

import os
import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, g, jsonify, request

# Try relative imports first (module mode), fall back to absolute (script mode)
try:
    from .auth import require_session_auth
    from .phase_filter import (
        OperationType,
        PipelinePhase,
        filter_operation,
        get_phase_filter,
    )
    from .phase_transition import (
        TransitionRole,
        can_transition_to,
        get_next_phase,
    )
except ImportError:
    from auth import require_session_auth  # type: ignore[no-redef, import-not-found]
    from phase_filter import (  # type: ignore[no-redef, import-not-found]
        OperationType,
        PipelinePhase,
        filter_operation,
        get_phase_filter,
    )
    from phase_transition import (  # type: ignore[no-redef, import-not-found]
        TransitionRole,
        can_transition_to,
        get_next_phase,
    )

# Add shared directory to path for egg_contracts
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_contracts import (
    ContractNotFoundError,
    ContractValidationError,
    Role,
    apply_mutation,
    load_contract,
    save_contract,
)

# Import gateway logging
try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(  # type: ignore[misc]
        name: str,
        level: int | str = logging.INFO,
        component: str | None = None,
    ) -> logging.Logger:
        return logging.getLogger(name)


logger = get_logger("gateway.phase")

# Blueprint for phase endpoints
phase_bp = Blueprint("phase", __name__, url_prefix="/api/v1/phase")


def get_role_from_context() -> TransitionRole | None:
    """Get the agent role from workflow context.

    Role source priority (highest to lowest):
    1. Session metadata (production path - set by launcher)
    2. X-Egg-Role header (for gateway testing only)
    3. EGG_AGENT_ROLE environment variable (development fallback)

    Returns:
        The TransitionRole if valid, None otherwise
    """
    # Production path: role from session metadata
    if hasattr(g, "session") and g.session:
        session_role = getattr(g.session, "agent_role", None)
        if session_role:
            try:
                return TransitionRole(session_role.lower())
            except ValueError:
                return None

    # Testing path: role from header (only when enabled)
    if os.environ.get("EGG_ENABLE_TEST_ROLE_HEADER") == "1":
        header_role = request.headers.get("X-Egg-Role")
        if header_role:
            try:
                return TransitionRole(header_role.lower())
            except ValueError:
                return None

    # Fallback: environment variable
    env_role = os.environ.get("EGG_AGENT_ROLE")
    if env_role:
        try:
            return TransitionRole(env_role.lower())
        except ValueError:
            return None

    return None


def make_phase_error(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a phase error response."""
    response: dict[str, Any] = {"success": False, "message": message}
    if details:
        response["details"] = details
    return jsonify(response), status_code


def make_phase_success(
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a phase success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


@phase_bp.route("/advance", methods=["POST"])
@require_session_auth
def advance_phase() -> tuple[Response, int]:
    """
    Advance the pipeline to the next phase.

    Request body:
        {
            "issue_number": 123,
            "repo_path": "/path/to/repo",  // optional
            "reason": "All tasks complete"  // optional
        }

    The role is determined from workflow context.
    The current phase is read from the contract.
    The next phase is determined by the transition graph.

    Returns:
        Success: {"success": true, "message": "...", "data": {"from_phase": "...", "to_phase": "..."}}
        Error: {"success": false, "message": "...", "details": {...}}
    """
    data = request.get_json()
    if not data:
        return make_phase_error("Missing request body")

    issue_number = data.get("issue_number")
    if not issue_number:
        return make_phase_error("Missing issue_number")

    repo_path = Path(data.get("repo_path", "."))
    reason = data.get("reason")
    actor = data.get("actor", "agent")

    # Get role from context
    role = get_role_from_context()
    if not role:
        return make_phase_error(
            "Cannot determine agent role. Role must be set via workflow context.",
            status_code=403,
            details={"hint": "Set EGG_AGENT_ROLE via workflow inputs"},
        )

    # Load the contract to get current phase
    try:
        contract = load_contract(issue_number, repo_path)
    except ContractNotFoundError:
        return make_phase_error(
            f"Contract for issue #{issue_number} not found",
            status_code=404,
        )
    except ContractValidationError as e:
        return make_phase_error(
            f"Contract validation failed: {e}",
            status_code=500,
        )

    # Get current phase and determine next phase
    current_phase = PipelinePhase(contract.current_phase.value)
    next_phase = get_next_phase(current_phase)

    if next_phase is None:
        return make_phase_error(
            f"Cannot advance from phase '{current_phase.value}': terminal state",
            status_code=400,
            details={"current_phase": current_phase.value},
        )

    # Validate the transition
    result = can_transition_to(current_phase, next_phase, role, actor)

    if not result.success:
        logger.warning(
            "Phase transition denied",
            issue=issue_number,
            role=role.value,
            from_phase=current_phase.value,
            to_phase=next_phase.value,
            error=result.message,
        )
        return make_phase_error(
            result.message,
            status_code=403,
            details={
                "role": role.value,
                "from_phase": current_phase.value,
                "to_phase": next_phase.value,
            },
        )

    # Apply the phase transition to the contract
    # Use the contract's mutation system for audit trail
    mutation_result = apply_mutation(
        contract=contract,
        role=Role.HUMAN if role == TransitionRole.HUMAN else Role.REVIEWER,
        actor=actor,
        field_path="current_phase",
        new_value=next_phase.value,
        reason=reason or f"Transition from {current_phase.value} to {next_phase.value}",
    )

    if not mutation_result.success:
        return make_phase_error(
            f"Failed to update contract: {mutation_result.message}",
            status_code=500,
        )

    # Save the updated contract
    assert mutation_result.contract is not None
    try:
        save_contract(mutation_result.contract, repo_path)
    except Exception as e:
        logger.error(
            "Failed to save contract after phase transition",
            issue=issue_number,
            error=str(e),
        )
        return make_phase_error(
            f"Failed to save contract: {e}",
            status_code=500,
        )

    logger.info(
        "Phase transition completed",
        issue=issue_number,
        role=role.value,
        actor=actor,
        from_phase=current_phase.value,
        to_phase=next_phase.value,
    )

    return make_phase_success(
        f"Advanced from '{current_phase.value}' to '{next_phase.value}'",
        data={
            "from_phase": current_phase.value,
            "to_phase": next_phase.value,
            "transitioned_by": actor,
        },
    )


@phase_bp.route("/filter", methods=["POST"])
@require_session_auth
def filter_phase_operation() -> tuple[Response, int]:
    """
    Check if an operation is allowed in the current phase.

    Request body:
        {
            "issue_number": 123,
            "repo_path": "/path/to/repo",  // optional
            "operation_type": "git",  // "git", "gh", or "egg-contract"
            "command": "push origin main"
        }

    Returns:
        Success: {"success": true, "message": "Operation allowed", "data": {"allowed": true}}
        Blocked: {"success": false, "message": "...", "data": {"allowed": false, "reason": "..."}}
    """
    data = request.get_json()
    if not data:
        return make_phase_error("Missing request body")

    issue_number = data.get("issue_number")
    operation_type = data.get("operation_type")
    command = data.get("command")

    if not issue_number:
        return make_phase_error("Missing issue_number")
    if not operation_type:
        return make_phase_error("Missing operation_type")
    if not command:
        return make_phase_error("Missing command")

    repo_path = Path(data.get("repo_path", "."))

    # Validate operation type
    try:
        op_type = OperationType(operation_type)
    except ValueError:
        return make_phase_error(
            f"Invalid operation_type: {operation_type}",
            details={"valid_types": [t.value for t in OperationType]},
        )

    # Load contract to get current phase
    try:
        contract = load_contract(issue_number, repo_path)
    except ContractNotFoundError:
        return make_phase_error(
            f"Contract for issue #{issue_number} not found",
            status_code=404,
        )
    except ContractValidationError as e:
        return make_phase_error(
            f"Contract validation failed: {e}",
            status_code=500,
        )

    current_phase = PipelinePhase(contract.current_phase.value)

    # Filter the operation
    result = filter_operation(current_phase, op_type, command)

    if result.allowed:
        return make_phase_success(
            result.message,
            data={"allowed": True, "phase": current_phase.value},
        )
    else:
        logger.info(
            "Operation blocked by phase filter",
            issue=issue_number,
            phase=current_phase.value,
            operation_type=operation_type,
            command=command,
            reason=result.blocked_reason,
        )
        return make_phase_error(
            result.message,
            status_code=403,
            details={
                "allowed": False,
                "phase": current_phase.value,
                "operation_type": operation_type,
                "reason": result.blocked_reason,
            },
        )


@phase_bp.route("/current/<int:issue_number>", methods=["GET"])
@require_session_auth
def get_current_phase(issue_number: int) -> tuple[Response, int]:
    """
    Get the current phase for an issue.

    URL params:
        issue_number: GitHub issue number

    Query params:
        repo_path: Path to the repository (optional)

    Returns:
        {"success": true, "data": {"phase": "implement", "exit_requires": "reviewer"}}
    """
    repo_path = Path(request.args.get("repo_path", "."))

    try:
        contract = load_contract(issue_number, repo_path)
    except ContractNotFoundError:
        return make_phase_error(
            f"Contract for issue #{issue_number} not found",
            status_code=404,
        )
    except ContractValidationError as e:
        return make_phase_error(
            f"Contract validation failed: {e}",
            status_code=500,
        )

    current_phase = PipelinePhase(contract.current_phase.value)
    phase_filter = get_phase_filter()
    exit_requires = phase_filter.get_exit_requirement(current_phase)
    next_phase = get_next_phase(current_phase)

    return make_phase_success(
        f"Current phase: {current_phase.value}",
        data={
            "phase": current_phase.value,
            "exit_requires": exit_requires,
            "next_phase": next_phase.value if next_phase else None,
        },
    )


@phase_bp.route("/permissions/<phase>", methods=["GET"])
@require_session_auth
def get_phase_permissions(phase: str) -> tuple[Response, int]:
    """
    Get the permissions for a specific phase.

    URL params:
        phase: Phase name (refine, plan, implement, pr)

    Returns:
        {"success": true, "data": {"allowed": [...], "blocked": [...], "exit_requires": "..."}}
    """
    try:
        pipeline_phase = PipelinePhase(phase)
    except ValueError:
        return make_phase_error(
            f"Invalid phase: {phase}",
            details={"valid_phases": [p.value for p in PipelinePhase]},
        )

    phase_filter = get_phase_filter()
    permissions = phase_filter.get_permissions(pipeline_phase)

    if not permissions:
        return make_phase_error(
            f"No permissions configured for phase: {phase}",
            status_code=404,
        )

    return make_phase_success(
        f"Permissions for phase: {phase}",
        data={
            "phase": phase,
            "allowed_operations": [
                {"type": op.type.value, "pattern": op.pattern, "description": op.description}
                for op in permissions.allowed_operations
            ],
            "blocked_operations": [
                {"type": op.type.value, "pattern": op.pattern, "description": op.description}
                for op in permissions.blocked_operations
            ],
            "exit_requires": permissions.exit_requires,
        },
    )
