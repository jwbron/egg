"""
Contract API endpoints for SDLC pipeline.

Provides REST endpoints for contract state management:
- GET /api/v1/contract/{issue} - Retrieve contract state
- POST /api/v1/contract/mutate - Validate role and apply mutation
- POST /api/v1/contract/create - Create new contract
- POST /api/v1/phase/advance - Advance pipeline phase

Role is determined from GitHub Actions workflow context (not agent env vars)
to prevent privilege escalation.
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, g, jsonify, request

# Add shared directory to path for egg_contracts
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str) -> logging.Logger:
        return logging.getLogger(name)


try:
    from egg_contracts import (
        Contract,
        ContractValidator,
        Decision,
        DecisionType,
        Issue,
        Phase,
        PhaseStatus,
        PipelinePhase,
        Role,
        Task,
        TaskStatus,
        ValidationError,
        create_audit_entry,
        get_contract_path,
        load_contract,
        save_contract,
    )
    from egg_contracts.audit import log_blocked_operation, log_mutation
    from egg_contracts.models import AuditAction
except ImportError:
    # Contracts not yet installed - provide stubs for tests
    Contract = None  # type: ignore
    Role = None  # type: ignore


logger = get_logger("gateway.contract_api")

# Blueprint for contract endpoints
contract_bp = Blueprint("contract", __name__, url_prefix="/api/v1")


def get_role_from_context() -> Role | None:
    """
    Get the agent role from workflow context.

    Role is determined from GitHub Actions environment, not from agent env vars,
    to prevent privilege escalation.

    Returns:
        Role enum value or None if not in workflow context
    """
    # In GitHub Actions, role comes from the job's environment
    # This is set by the workflow, not the agent
    role_str = os.environ.get("EGG_AGENT_ROLE", "").lower()

    # Also check for workflow role override (trusted source)
    workflow_role = os.environ.get("EGG_WORKFLOW_ROLE", "").lower()
    if workflow_role:
        role_str = workflow_role

    if not role_str:
        return None

    try:
        return Role(role_str)
    except ValueError:
        logger.warning("Invalid role in context", role=role_str)
        return None


def get_repo_root_from_request() -> Path | None:
    """Get repo root from request data or session."""
    data = request.get_json() or {}
    repo_path = data.get("repo_path")

    if repo_path:
        return Path(repo_path)

    # Try to get from session
    session = getattr(g, "session", None)
    if session and hasattr(session, "repo_path"):
        return Path(session.repo_path)

    return None


def make_error(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create an error response."""
    response: dict[str, Any] = {"success": False, "message": message}
    if details:
        response["details"] = details
    return jsonify(response), status_code


@contract_bp.route("/contract/<int:issue_number>", methods=["GET"])
def get_contract(issue_number: int) -> tuple[Response, int] | Response:
    """
    Retrieve contract state for an issue.

    Returns the full contract JSON.
    """
    repo_root = get_repo_root_from_request()
    if not repo_root:
        # Try default locations
        default_paths = [
            Path.cwd(),
            Path("/home/egg/repos"),
            Path(os.environ.get("GITHUB_WORKSPACE", "")),
        ]
        for path in default_paths:
            if path.exists() and (path / ".egg" / "contracts").exists():
                repo_root = path
                break

    if not repo_root:
        return make_error("Could not determine repository root")

    contract = load_contract(repo_root, issue_number)
    if not contract:
        return make_error(f"Contract not found for issue #{issue_number}", status_code=404)

    return jsonify(
        {
            "success": True,
            "contract": contract.model_dump(mode="json", exclude_none=True),
        }
    )


@contract_bp.route("/contract/create", methods=["POST"])
def create_contract() -> tuple[Response, int] | Response:
    """
    Create a new contract for an issue.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "issue": {
                "number": 123,
                "title": "Issue title",
                "url": "https://github.com/owner/repo/issues/123"
            },
            "branch": "egg/issue-123"
        }
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    issue_data = data.get("issue")
    branch = data.get("branch")

    if not repo_path:
        return make_error("Missing repo_path")
    if not issue_data:
        return make_error("Missing issue data")

    repo_root = Path(repo_path)
    if not repo_root.exists():
        return make_error(f"Repository path does not exist: {repo_path}")

    # Check if contract already exists
    existing = load_contract(repo_root, issue_data.get("number", 0))
    if existing:
        return make_error(
            f"Contract already exists for issue #{issue_data['number']}",
            status_code=409,
        )

    # Create the contract
    try:
        issue = Issue(
            number=issue_data["number"],
            title=issue_data["title"],
            url=issue_data["url"],
        )
        contract = Contract(
            schemaVersion="1.0",
            issue=issue,
            currentPhase=PipelinePhase.REFINE,
            branch=branch,
            phases=[],
            decisions=[],
            audit_log=[],
        )

        # Add creation audit entry
        log_mutation(
            contract,
            actor="system",
            role="human",
            field_path=".",
            new_value="created",
        )

        path = save_contract(contract, repo_root)
        logger.info(
            "Contract created",
            issue=issue.number,
            path=str(path),
        )

        return jsonify(
            {
                "success": True,
                "message": f"Contract created for issue #{issue.number}",
                "path": str(path),
            }
        )
    except Exception as e:
        logger.error("Failed to create contract", error=str(e))
        return make_error(f"Failed to create contract: {e}")


@contract_bp.route("/contract/mutate", methods=["POST"])
def mutate_contract() -> tuple[Response, int] | Response:
    """
    Apply a mutation to a contract with role-based validation.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "issue_number": 123,
            "mutations": [
                {
                    "path": "phases.0.tasks.0.commit",
                    "value": "abc1234"
                }
            ]
        }

    Role is determined from workflow context, not request body.
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    issue_number = data.get("issue_number")
    mutations = data.get("mutations", [])

    if not repo_path:
        return make_error("Missing repo_path")
    if not issue_number:
        return make_error("Missing issue_number")
    if not mutations:
        return make_error("Missing mutations")

    # Get role from trusted context
    role = get_role_from_context()
    if not role:
        return make_error(
            "Agent role not configured. Set EGG_AGENT_ROLE in workflow context.",
            status_code=403,
        )

    repo_root = Path(repo_path)
    contract = load_contract(repo_root, issue_number)
    if not contract:
        return make_error(f"Contract not found for issue #{issue_number}", status_code=404)

    # Validate all mutations
    validator = ContractValidator(role)
    blocked = []
    allowed = []

    for mutation in mutations:
        path = mutation.get("path")
        value = mutation.get("value")

        if not path:
            return make_error("Mutation missing 'path' field")

        result = validator.validate_mutation(path, value)
        if result.allowed:
            allowed.append(mutation)
        else:
            blocked.append(
                {
                    "path": path,
                    "message": result.message,
                    "owner": result.owner,
                }
            )
            # Log blocked attempt
            log_blocked_operation(
                contract,
                actor=role.value,
                role=role,
                field_path=path,
                attempted_value=value,
                reason=result.message,
            )

    if blocked:
        # Save contract with blocked operation logs
        save_contract(contract, repo_root)
        return make_error(
            f"Role '{role.value}' cannot modify these fields",
            status_code=403,
            details={"blocked": blocked},
        )

    # Apply all mutations
    for mutation in allowed:
        path = mutation.get("path")
        value = mutation.get("value")

        # Apply the mutation (simplified - real implementation would use jsonpath)
        old_value = apply_mutation(contract, path, value)

        # Log successful mutation
        log_mutation(
            contract,
            actor=role.value,
            role=role,
            field_path=path,
            new_value=value,
            old_value=old_value,
        )

    # Save updated contract
    save_contract(contract, repo_root)

    logger.info(
        "Contract mutated",
        issue=issue_number,
        role=role.value,
        mutations=len(allowed),
    )

    return jsonify(
        {
            "success": True,
            "message": f"Applied {len(allowed)} mutations",
            "mutations": [m.get("path") for m in allowed],
        }
    )


def apply_mutation(contract: Contract, path: str, value: Any) -> Any:
    """
    Apply a mutation to the contract.

    This is a simplified implementation that handles common paths.
    A full implementation would use a proper JSON path library.

    Returns the old value for audit logging.
    """
    parts = path.split(".")
    old_value = None

    # Handle common mutation patterns
    if len(parts) >= 4 and parts[0] == "phases" and parts[2] == "tasks":
        # phases.N.tasks.M.field
        phase_idx = int(parts[1])
        task_idx = int(parts[3])
        field = parts[4] if len(parts) > 4 else None

        if phase_idx < len(contract.phases):
            phase = contract.phases[phase_idx]
            if task_idx < len(phase.tasks):
                task = phase.tasks[task_idx]
                if field:
                    old_value = getattr(task, field, None)
                    setattr(task, field, value)

    elif len(parts) >= 3 and parts[0] == "phases":
        # phases.N.field
        phase_idx = int(parts[1])
        field = parts[2]

        if phase_idx < len(contract.phases):
            phase = contract.phases[phase_idx]
            old_value = getattr(phase, field, None)
            setattr(phase, field, value)

    elif len(parts) >= 3 and parts[0] == "decisions":
        # decisions.N.field
        dec_idx = int(parts[1])
        field = parts[2]

        if contract.decisions and dec_idx < len(contract.decisions):
            decision = contract.decisions[dec_idx]
            old_value = getattr(decision, field, None)
            setattr(decision, field, value)

    elif len(parts) == 1:
        # Top-level field
        old_value = getattr(contract, parts[0], None)
        setattr(contract, parts[0], value)

    return old_value


@contract_bp.route("/phase/advance", methods=["POST"])
def advance_phase() -> tuple[Response, int] | Response:
    """
    Advance the pipeline to the next phase.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "issue_number": 123,
            "target_phase": "plan"
        }

    Only humans can advance phases (enforced by role check).
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    issue_number = data.get("issue_number")
    target_phase = data.get("target_phase")

    if not repo_path:
        return make_error("Missing repo_path")
    if not issue_number:
        return make_error("Missing issue_number")
    if not target_phase:
        return make_error("Missing target_phase")

    # Get role from context
    role = get_role_from_context()
    if not role:
        return make_error(
            "Agent role not configured",
            status_code=403,
        )

    # Only humans can advance phases
    if role != Role.HUMAN:
        return make_error(
            f"Role '{role.value}' cannot advance phases. Only humans can transition between phases.",
            status_code=403,
        )

    repo_root = Path(repo_path)
    contract = load_contract(repo_root, issue_number)
    if not contract:
        return make_error(f"Contract not found for issue #{issue_number}", status_code=404)

    # Validate phase transition
    try:
        new_phase = PipelinePhase(target_phase)
    except ValueError:
        return make_error(f"Invalid phase: {target_phase}")

    old_phase = contract.currentPhase

    # Update phase
    contract.currentPhase = new_phase

    # Log transition
    log_mutation(
        contract,
        actor=role.value,
        role=role,
        field_path="currentPhase",
        new_value=new_phase.value,
        old_value=old_phase.value,
    )

    save_contract(contract, repo_root)

    logger.info(
        "Phase advanced",
        issue=issue_number,
        from_phase=old_phase.value,
        to_phase=new_phase.value,
    )

    return jsonify(
        {
            "success": True,
            "message": f"Advanced from {old_phase.value} to {new_phase.value}",
            "previous_phase": old_phase.value,
            "current_phase": new_phase.value,
        }
    )


@contract_bp.route("/contract/<int:issue_number>/decision", methods=["POST"])
def add_decision(issue_number: int) -> tuple[Response, int] | Response:
    """
    Add a HITL decision point to the contract.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "question": "Approve the implementation plan?",
            "options": [
                {"id": "approve", "label": "Approve"},
                {"id": "reject", "label": "Reject"}
            ]
        }
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    question = data.get("question")
    options = data.get("options", [])

    if not repo_path:
        return make_error("Missing repo_path")
    if not question:
        return make_error("Missing question")

    repo_root = Path(repo_path)
    contract = load_contract(repo_root, issue_number)
    if not contract:
        return make_error(f"Contract not found for issue #{issue_number}", status_code=404)

    # Create decision
    decision_id = contract.next_decision_id()
    decision = Decision(
        id=decision_id,
        question=question,
        type=DecisionType.HITL,
        options=[
            {"id": opt.get("id", f"opt-{i}"), "label": opt.get("label", "")}
            for i, opt in enumerate(options)
        ]
        if options
        else None,
        resolved=False,
    )

    if contract.decisions is None:
        contract.decisions = []
    contract.decisions.append(decision)

    # Log creation
    role = get_role_from_context() or Role.HUMAN
    log_mutation(
        contract,
        actor=role.value if isinstance(role, Role) else str(role),
        role=role,
        field_path=f"decisions.{len(contract.decisions) - 1}",
        new_value=decision.model_dump(mode="json"),
    )

    save_contract(contract, repo_root)

    logger.info(
        "Decision added",
        issue=issue_number,
        decision_id=decision_id,
        question=question,
    )

    return jsonify(
        {
            "success": True,
            "message": "Decision point added",
            "decision_id": decision_id,
        }
    )


def register_contract_routes(app: Any) -> None:
    """Register contract API routes with Flask app."""
    app.register_blueprint(contract_bp)
