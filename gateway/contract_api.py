"""
Contract API endpoints for the gateway.

Provides REST endpoints for contract mutations with role-based enforcement.
Role is determined from GitHub Actions workflow context, not agent environment.
"""

import os
import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, g, jsonify, request

# Import gateway authentication - try relative import first (module mode),
# fall back to absolute import (standalone script mode in container)
try:
    from .auth import require_session_auth
except ImportError:
    from auth import require_session_auth  # type: ignore[no-redef, import-not-found]

# Add shared directory to path for egg_contracts
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_contracts import (
    ContractNotFoundError,
    ContractValidationError,
    Role,
    apply_mutation,
    contract_exists,
    export_contract,
    load_contract,
    save_contract,
    validate_mutation,
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


logger = get_logger("gateway.contract")

# Blueprint for contract endpoints
contract_bp = Blueprint("contract", __name__, url_prefix="/api/v1/contract")


def get_role_from_context() -> Role | None:
    """
    Get the agent role from workflow context.

    Role source priority (highest to lowest):
    1. Session metadata (production path - set by launcher, most secure)
    2. X-Egg-Role header (for gateway testing only)
    3. EGG_AGENT_ROLE environment variable (development fallback)

    In GitHub Actions, the role is passed via workflow inputs and set
    in the session metadata by the launcher, NOT by the agent.
    This prevents privilege escalation.

    Returns:
        The Role if valid, None otherwise
    """
    # Production path: role comes from workflow context via session metadata
    # The session is set by the launcher when starting the container
    # This takes precedence to prevent header-based privilege escalation
    if hasattr(g, "session") and g.session:
        session_role = getattr(g.session, "agent_role", None)
        if session_role:
            try:
                return Role(session_role.lower())
            except ValueError:
                return None

    # Testing path: role can be passed in request header for gateway testing
    # This is lower priority than session to prevent bypassing production auth
    header_role = request.headers.get("X-Egg-Role")
    if header_role:
        try:
            return Role(header_role.lower())
        except ValueError:
            return None

    # Fallback: check environment (least secure, used in development only)
    env_role = os.environ.get("EGG_AGENT_ROLE")
    if env_role:
        try:
            return Role(env_role.lower())
        except ValueError:
            return None

    return None


def get_repo_path_from_request(from_query: bool = False) -> Path | None:
    """Get the repository path from the request.

    Args:
        from_query: If True, look for repo_path in query parameters (for GET requests).
                   If False, look in JSON body (for POST requests).
    """
    if from_query:
        # For GET requests, use query parameters
        repo_path = request.args.get("repo_path")
    else:
        # For POST requests, use JSON body
        data = request.get_json() or {}
        repo_path = data.get("repo_path")

    if repo_path:
        return Path(repo_path)

    # Try to get from session
    if hasattr(g, "session") and g.session:
        session_repo = getattr(g.session, "repo_path", None)
        if session_repo:
            return Path(session_repo)

    return None


def make_contract_error(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a contract error response."""
    response: dict[str, Any] = {"success": False, "message": message}
    if details:
        response["details"] = details
    return jsonify(response), status_code


def make_contract_success(
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a contract success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


@contract_bp.route("/<int:issue_number>", methods=["GET"])
@require_session_auth
def get_contract(issue_number: int) -> tuple[Response, int]:
    """
    Get contract state for an issue.

    URL params:
        issue_number: GitHub issue number

    Query params:
        repo_path: Path to the repository (optional)
        include_audit_log: Whether to include audit log (default: false)
    """
    repo_path = get_repo_path_from_request(from_query=True)
    if not repo_path:
        repo_path = Path.cwd()

    include_audit = request.args.get("include_audit_log", "false").lower() == "true"

    try:
        contract = load_contract(issue_number, repo_path)
        data = export_contract(contract, include_audit_log=include_audit)
        return make_contract_success("Contract retrieved", data=data)
    except ContractNotFoundError:
        return make_contract_error(
            f"Contract for issue #{issue_number} not found",
            status_code=404,
        )
    except ContractValidationError as e:
        return make_contract_error(
            f"Contract validation failed: {e}",
            status_code=500,
        )


@contract_bp.route("/mutate", methods=["POST"])
@require_session_auth
def mutate_contract() -> tuple[Response, int]:
    """
    Apply a mutation to a contract.

    Request body:
        {
            "issue_number": 123,
            "repo_path": "/path/to/repo",  // optional
            "field_path": "phases.0.tasks.0.commit",
            "new_value": "abc1234",
            "actor": "egg",  // optional, defaults to "agent"
            "reason": "Implementation complete"  // optional
        }

    The role is determined from workflow context, not the request body.
    This prevents agents from escalating their privileges.

    Returns:
        Success: {"success": true, "message": "...", "data": {"contract": {...}}}
        Error: {"success": false, "message": "...", "details": {...}}
    """
    data = request.get_json()
    if not data:
        return make_contract_error("Missing request body")

    # Required fields
    issue_number = data.get("issue_number")
    field_path = data.get("field_path")
    new_value = data.get("new_value")

    if not issue_number:
        return make_contract_error("Missing issue_number")
    if not field_path:
        return make_contract_error("Missing field_path")
    if new_value is None:
        return make_contract_error("Missing new_value")

    # Optional fields
    repo_path = Path(data["repo_path"]) if data.get("repo_path") else Path.cwd()
    actor = data.get("actor", "agent")
    reason = data.get("reason")

    # Get role from context (NOT from request body)
    role = get_role_from_context()
    if not role:
        return make_contract_error(
            "Cannot determine agent role. Role must be set via workflow context.",
            status_code=403,
            details={"hint": "Set EGG_AGENT_ROLE via workflow inputs, not agent env vars"},
        )

    # Load the contract
    try:
        contract = load_contract(issue_number, repo_path)
    except ContractNotFoundError:
        return make_contract_error(
            f"Contract for issue #{issue_number} not found",
            status_code=404,
        )
    except ContractValidationError as e:
        return make_contract_error(
            f"Contract validation failed: {e}",
            status_code=500,
        )

    # Apply the mutation
    result = apply_mutation(
        contract=contract,
        role=role,
        actor=actor,
        field_path=field_path,
        new_value=new_value,
        reason=reason,
    )

    if not result.success:
        logger.warning(
            "Contract mutation rejected",
            issue=issue_number,
            role=role.value,
            field_path=field_path,
            error=result.message,
        )
        return make_contract_error(
            result.message,
            status_code=403,
            details={
                "role": role.value,
                "field_path": field_path,
            },
        )

    # Save the updated contract
    # Type assertion: contract is always set when success is True
    assert result.contract is not None
    try:
        save_contract(result.contract, repo_path)
    except Exception as e:
        logger.error(
            "Failed to save contract",
            issue=issue_number,
            error=str(e),
        )
        return make_contract_error(
            f"Failed to save contract: {e}",
            status_code=500,
        )

    logger.info(
        "Contract mutation applied",
        issue=issue_number,
        role=role.value,
        actor=actor,
        field_path=field_path,
    )

    return make_contract_success(
        "Mutation applied successfully",
        data={"contract": export_contract(result.contract, include_audit_log=False)},
    )


@contract_bp.route("/validate", methods=["POST"])
@require_session_auth
def validate_contract_mutation() -> tuple[Response, int]:
    """
    Validate a mutation without applying it.

    Request body:
        {
            "field_path": "phases.0.tasks.0.status",
            "new_value": "complete"
        }

    The role is determined from workflow context.

    Returns:
        {"success": true, "message": "Mutation allowed"}
        or
        {"success": false, "message": "...", "details": {...}}
    """
    data = request.get_json()
    if not data:
        return make_contract_error("Missing request body")

    field_path = data.get("field_path")
    new_value = data.get("new_value")

    if not field_path:
        return make_contract_error("Missing field_path")
    if new_value is None:
        return make_contract_error("Missing new_value")

    # Get role from context
    role = get_role_from_context()
    if not role:
        return make_contract_error(
            "Cannot determine agent role",
            status_code=403,
        )

    # Validate the mutation
    result = validate_mutation(role, field_path, new_value)

    if result.valid:
        return make_contract_success("Mutation allowed")
    else:
        return make_contract_error(
            result.message,
            status_code=403,
            details={
                "role": role.value,
                "field_path": result.field_path,
                "required_role": result.required_role,
            },
        )


@contract_bp.route("/exists/<int:issue_number>", methods=["GET"])
@require_session_auth
def check_contract_exists(issue_number: int) -> tuple[Response, int]:
    """Check if a contract exists for an issue.

    Query params:
        repo_path: Path to the repository (optional)
    """
    repo_path = get_repo_path_from_request(from_query=True)
    if not repo_path:
        repo_path = Path.cwd()

    exists = contract_exists(issue_number, repo_path)
    return make_contract_success(
        "Contract exists" if exists else "Contract does not exist",
        data={"exists": exists},
    )
