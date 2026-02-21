#!/usr/bin/env python3
"""
Gateway Sidecar - REST API for policy-enforced git/gh operations.

Provides a REST API that egg containers call to perform git push and gh operations.
The gateway holds GitHub credentials and enforces ownership policies.

Security:
    - Authentication via launcher secret (EGG_LAUNCHER_SECRET) and session tokens
    - Listens on all interfaces (containers access via host.docker.internal)

Endpoints:
    POST /api/v1/git/push       - Push to remote (policy: branch_ownership or trusted_user)
    POST /api/v1/git/fetch      - Fetch from remote (no policy - read operations allowed)
    POST /api/v1/gh/pr/create   - Create PR (policy: blocked in user mode)
    POST /api/v1/gh/pr/comment  - Comment on PR (policy: none - allowed on any PR)
    POST /api/v1/gh/pr/edit     - Edit PR (policy: pr_ownership)
    POST /api/v1/gh/pr/close    - Close PR (policy: pr_ownership)
    POST /api/v1/gh/execute     - Generic gh command (policy: filtered)
    GET  /api/v1/health         - Health check (no auth required)

Usage:
    gateway.py [--host HOST] [--port PORT] [--debug]
"""

import argparse
import functools
import json
import os
import re
import secrets
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

import httpx
from flask import Flask, Response, g, jsonify, request, stream_with_context
from waitress import serve

# Add shared directory to path for egg_logging
# In container, egg_logging is at /app/egg_logging
# On host, it's at ../../shared/egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

# Import gateway modules - try relative import first (module mode),
# fall back to absolute import (standalone script mode in container)
try:
    from .anthropic_credentials import get_credentials_manager
    from .checkpoint_handler import (
        _get_checkpoint_repo_for_path,
        capture_and_store_checkpoint,
        capture_and_store_checkpoints_for_push,
        get_checkpoint_handler,
    )
    from .git_client import (
        GIT_ALLOWED_COMMANDS,
        cleanup_credential_helper,
        create_credential_helper,
        get_authenticated_remote_target,
        get_changed_files_in_push,
        get_token_for_repo,
        git_cmd,
        is_branch_switch,
        is_branch_switching_operation,
        is_repos_parent_directory,
        resolve_remote_url,
        validate_git_args,
        validate_repo_path,
    )
    from .github_client import (
        BLOCKED_GH_COMMANDS,
        GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE,
        READONLY_GH_COMMANDS,
        extract_repo_from_gh_command,
        get_github_client,
        parse_gh_api_args,
        resolve_gh_api_template_variables,
        validate_gh_api_path,
    )
    from .phase_filter import (
        OperationType,
        check_agent_restrictions,
        check_file_restrictions,
        check_phase_file_restrictions,
        filter_operation,
    )
    from .policy import (
        extract_branch_from_refspec,
        extract_repo_from_remote,
        get_policy_engine,
    )
    from .private_repo_policy import (
        check_private_repo_access,
    )
    from .rate_limiter import (
        check_heartbeat_rate_limit,
        record_failed_lookup,
    )
    from .repo_parser import parse_owner_repo
    from .repo_visibility import get_repo_visibility
    from .session_manager import (
        get_session_manager,
        validate_session_for_request,
    )
    from .transcript_buffer import get_transcript_buffer
    from .worktree_manager import WorktreeManager, get_active_docker_containers, startup_cleanup
except ImportError:
    from anthropic_credentials import get_credentials_manager  # type: ignore[no-redef]
    from checkpoint_handler import (  # type: ignore[no-redef, import-not-found]
        _get_checkpoint_repo_for_path,
        capture_and_store_checkpoint,
        capture_and_store_checkpoints_for_push,
        get_checkpoint_handler,
    )
    from git_client import (  # type: ignore[no-redef, import-not-found]
        GIT_ALLOWED_COMMANDS,
        cleanup_credential_helper,
        create_credential_helper,
        get_authenticated_remote_target,
        get_changed_files_in_push,
        get_token_for_repo,
        git_cmd,
        is_branch_switch,
        is_branch_switching_operation,
        is_repos_parent_directory,
        resolve_remote_url,
        validate_git_args,
        validate_repo_path,
    )
    from github_client import (  # type: ignore[no-redef, import-not-found]
        BLOCKED_GH_COMMANDS,
        GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE,
        READONLY_GH_COMMANDS,
        extract_repo_from_gh_command,
        get_github_client,
        parse_gh_api_args,
        resolve_gh_api_template_variables,
        validate_gh_api_path,
    )
    from phase_filter import (  # type: ignore[no-redef, import-not-found]
        OperationType,
        check_agent_restrictions,
        check_file_restrictions,
        check_phase_file_restrictions,
        filter_operation,
    )
    from policy import (  # type: ignore[no-redef, import-not-found]
        extract_branch_from_refspec,
        extract_repo_from_remote,
        get_policy_engine,
    )
    from private_repo_policy import (  # type: ignore[no-redef]
        check_private_repo_access,
    )
    from rate_limiter import (  # type: ignore[no-redef, import-not-found]
        check_heartbeat_rate_limit,
        record_failed_lookup,
    )
    from repo_parser import parse_owner_repo  # type: ignore[no-redef, import-not-found]
    from repo_visibility import get_repo_visibility  # type: ignore[no-redef]
    from session_manager import (  # type: ignore[no-redef, import-not-found]
        get_session_manager,
        validate_session_for_request,
    )
    from transcript_buffer import get_transcript_buffer  # type: ignore[no-redef, import-not-found]
    from worktree_manager import (  # type: ignore[no-redef, import-not-found]
        WorktreeManager,
        get_active_docker_containers,
        startup_cleanup,
    )

# Import repo_config for user mode support
# Path setup needed because config is in a sibling directory
_config_path = Path(__file__).parent.parent / "config"
if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))
from repo_config import get_auth_mode, get_checkpoint_repo, is_checkpoint_repo

logger = get_logger("gateway")

app = Flask(__name__)

# Register contract API blueprint
try:
    from .contract_api import contract_bp

    app.register_blueprint(contract_bp)
except ImportError:
    from contract_api import contract_bp  # type: ignore[import-not-found, no-redef]

    app.register_blueprint(contract_bp)

# Register phase API blueprint
try:
    from .phase_api import phase_bp

    app.register_blueprint(phase_bp)
except ImportError:
    from phase_api import phase_bp  # type: ignore[import-not-found, no-redef]

    app.register_blueprint(phase_bp)


@app.errorhandler(Exception)
def handle_unhandled_exception(e: Exception) -> tuple[Response, int]:
    """Return JSON for all unhandled exceptions instead of Flask's default HTML."""
    from werkzeug.exceptions import HTTPException

    if isinstance(e, HTTPException):
        # Preserve HTTP status codes for werkzeug exceptions (400, 404, etc.)
        return jsonify(
            {
                "success": False,
                "message": e.description or str(e),
            }
        ), e.code or 500

    logger.error(
        "Unhandled exception in request handler",
        error=str(e),
        error_type=type(e).__name__,
        path=request.path if request else "unknown",
        traceback=traceback.format_exc(),
    )
    return jsonify(
        {
            "success": False,
            "message": "Internal server error",
        }
    ), 500


# Configuration
DEFAULT_HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")  # Listen on all interfaces by default
DEFAULT_PORT = 9848

# Host home directory for path translation
# The gateway container uses /home/egg internally, but needs to return
# host paths to the egg launcher for Docker mount sources
HOST_HOME = os.environ.get("HOST_HOME", "")
CONTAINER_HOME = "/home/egg"


def translate_to_host_path(container_path: str) -> str:
    """
    Translate a container path to the corresponding host path.

    The gateway runs with paths like /home/egg/.egg-worktrees/...
    but the egg launcher needs host paths like /home/user/.egg-worktrees/...
    for Docker mount sources.

    Args:
        container_path: Path inside the gateway container

    Returns:
        The corresponding host path, or original path if translation not possible
    """
    if not HOST_HOME:
        # No host home configured - return as-is (may cause mount issues)
        return container_path

    if container_path.startswith(CONTAINER_HOME):
        return container_path.replace(CONTAINER_HOME, HOST_HOME, 1)

    return container_path


# Import session auth decorator from auth module to avoid circular imports
try:
    from .auth import require_session_auth
except ImportError:
    from auth import require_session_auth  # type: ignore[no-redef, import-not-found]


# Launcher secret for session management and worktree operations
# This is used by the egg launcher to authenticate with the gateway
LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "")
LAUNCHER_SECRET_FILE = Path("/secrets/launcher-secret")


class LauncherSecretNotConfiguredError(Exception):
    """Raised when launcher secret is not configured."""


def get_launcher_secret() -> str:
    """Get the launcher secret from environment or file.

    The launcher secret is used to authenticate the egg launcher when
    registering sessions. It should be generated by 'egg --setup' and
    mounted at /secrets/launcher-secret.

    Raises:
        LauncherSecretNotConfiguredError: If launcher secret is not found.
    """
    global LAUNCHER_SECRET

    if LAUNCHER_SECRET:
        return LAUNCHER_SECRET

    # Try to read from file (mounted from ~/.config/egg/launcher-secret)
    if LAUNCHER_SECRET_FILE.exists():
        LAUNCHER_SECRET = LAUNCHER_SECRET_FILE.read_text().strip()
        return LAUNCHER_SECRET

    raise LauncherSecretNotConfiguredError(
        f"Launcher secret not found at {LAUNCHER_SECRET_FILE} or EGG_LAUNCHER_SECRET env var. "
        "Run 'egg --setup' to generate it."
    )


def check_launcher_auth() -> tuple[bool, str]:
    """
    Check if request has valid launcher authentication.

    Returns:
        Tuple of (is_valid, error_message)
    """
    secret = get_launcher_secret()
    if not secret:
        return False, "Launcher secret not configured"

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False, "Missing or invalid Authorization header"

    provided_token = auth_header[7:]  # Remove "Bearer " prefix

    # Constant-time comparison to prevent timing attacks
    if secrets.compare_digest(provided_token, secret):
        return True, ""

    return False, "Invalid launcher authorization token"


def require_launcher_auth(f: F) -> F:
    """Decorator to require launcher authentication for an endpoint."""

    @functools.wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        is_valid, error = check_launcher_auth()
        if not is_valid:
            logger.warning(
                "Launcher authentication failed",
                endpoint=request.path,
                error=error,
                source_ip=request.remote_addr,
            )
            return make_error(error, status_code=401)
        return f(*args, **kwargs)

    return decorated  # type: ignore[return-value]


def make_response(
    success: bool,
    message: str,
    data: dict[str, Any] | None = None,
    status_code: int = 200,
) -> tuple[Response, int]:
    """Create a standardized JSON response."""
    response = {"success": success, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), status_code


def make_error(
    message: str, status_code: int = 400, details: dict[str, Any] | None = None
) -> tuple[Response, int]:
    """Create an error response."""
    return make_response(False, message, details, status_code)


def make_success(message: str, data: dict[str, Any] | None = None) -> tuple[Response, int]:
    """Create a success response."""
    return make_response(True, message, data, 200)


def audit_log(
    event_type: str,
    operation: str,
    success: bool,
    details: dict[str, Any] | None = None,
) -> None:
    """Log an audit event in structured format."""
    log_data: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "gateway_operation",
        "operation": operation,
        "source_ip": request.remote_addr,
        "success": success,
    }
    if details:
        log_data.update(details)

    if success:
        logger.info(f"Audit: {event_type}", **log_data)
    else:
        logger.warning(f"Audit: {event_type}", **log_data)


def _check_orchestrator_connectivity() -> dict[str, Any]:
    """Check orchestrator connectivity if configured.

    Returns:
        Dictionary with orchestrator status. Contains {"configured": False}
        if orchestrator URL is not set, otherwise includes reachability info.
    """
    orchestrator_url = os.environ.get("EGG_ORCHESTRATOR_URL")
    if not orchestrator_url:
        return {"configured": False}

    try:
        # Use a short timeout for health checks
        import urllib.request

        health_url = f"{orchestrator_url}/api/v1/health"
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode())
            return {
                "configured": True,
                "reachable": True,
                "url": orchestrator_url,
                "status": data.get("status", "unknown"),
            }
    except Exception as e:
        return {
            "configured": True,
            "reachable": False,
            "error": str(e),
        }


@app.route("/api/v1/health", methods=["GET"])
def health_check() -> Response:
    """Health check endpoint (no auth required)."""
    github = get_github_client()
    token_valid = github.is_token_valid()

    # Check launcher secret is configured
    try:
        get_launcher_secret()
        launcher_secret_configured = True
    except LauncherSecretNotConfiguredError:
        launcher_secret_configured = False

    # Get session manager stats
    session_manager = get_session_manager()
    active_sessions = len(session_manager.list_sessions())

    # Check orchestrator connectivity (if configured)
    orchestrator_status = _check_orchestrator_connectivity()

    # Gateway always runs with locked Squid.
    # Per-container mode is enforced at container start via network selection.
    # - Private containers: isolated network + proxy (locked to api.anthropic.com)
    # - Public containers: external network + direct internet (no proxy)
    response_data: dict[str, Any] = {
        "status": "healthy" if (token_valid and launcher_secret_configured) else "degraded",
        "github_token_valid": token_valid,
        "auth_configured": launcher_secret_configured,
        "active_sessions": active_sessions,
        "service": "gateway",
        "client_ip": request.remote_addr,
    }

    # Include orchestrator status if configured
    if orchestrator_status.get("configured"):
        response_data["orchestrator"] = orchestrator_status

    return jsonify(response_data)


@app.route("/api/v1/git/push", methods=["POST"])
@require_session_auth
def git_push() -> tuple[Response, int] | Response:
    """
    Handle git push requests.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "remote": "origin",
            "refspec": "branch-name",
            "force": false
        }

    Policy: branch_ownership
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    remote = data.get("remote", "origin")
    refspec = data.get("refspec", "")
    force = data.get("force", False)
    container_id = data.get("container_id")

    if not repo_path:
        return make_error("Missing repo_path")

    # Validate repo_path to prevent path traversal attacks
    path_valid, path_error = validate_repo_path(repo_path)
    if not path_valid:
        audit_log(
            "push_blocked",
            "git_push",
            success=False,
            details={"repo_path": repo_path, "reason": path_error},
        )
        return make_error(path_error, status_code=403)

    # Map container path to worktree path if container_id is provided
    exec_path = map_container_path_to_worktree(repo_path, container_id, "push")

    # Get remote URL to determine repo
    remote_url, url_error = resolve_remote_url(remote, exec_path)
    if url_error:
        return make_error(url_error)

    # Extract repo from URL
    repo = extract_repo_from_remote(remote_url)
    if not repo:
        return make_error(f"Could not parse repository from URL: {remote_url}")

    # Extract branch from refspec
    branch = extract_branch_from_refspec(refspec)
    if not branch:
        # Try to get current branch
        try:
            result = subprocess.run(
                git_cmd("branch", "--show-current"),
                cwd=exec_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            branch = result.stdout.strip()
        except Exception:
            pass

    if not branch:
        return make_error("Could not determine branch to push")

    # Determine auth mode for this repo
    auth_mode = get_auth_mode(repo)

    # Check Private Repo Mode policy (if enabled)
    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)
    session_phase = getattr(g, "session_phase", None)

    # Checkpoint branch bypass: pushes to the checkpoint branch always succeed
    # regardless of session mode or phase (checkpoints can be created at any time)
    CHECKPOINT_BRANCH = "egg/checkpoints/v2"
    is_checkpoint_push = branch == CHECKPOINT_BRANCH

    repo_info = parse_owner_repo(repo)
    if repo_info:
        # Checkpoint operations are infrastructure — always accessible regardless of
        # session mode. This covers both dedicated checkpoint repos and checkpoint
        # branch pushes to the source repo itself.
        if is_checkpoint_push or is_checkpoint_repo(repo_info.owner, repo_info.repo):
            audit_log(
                "push_checkpoint_exempt",
                "git_push",
                success=True,
                details={
                    "repo": repo,
                    "branch": branch,
                    "reason": "Checkpoint operation exempt from private mode policy",
                    "exempt_type": "checkpoint_repo"
                    if is_checkpoint_repo(repo_info.owner, repo_info.repo)
                    else "checkpoint_branch",
                },
            )
        else:
            priv_result = check_private_repo_access(
                operation="push",
                owner=repo_info.owner,
                repo=repo_info.repo,
                for_write=True,
                session_mode=session_mode,
            )
            if not priv_result.allowed:
                audit_log(
                    "push_denied_private_mode",
                    "git_push",
                    success=False,
                    details={
                        "repo": repo,
                        "branch": branch,
                        "reason": priv_result.reason,
                        "visibility": priv_result.visibility,
                        "auth_mode": auth_mode,
                    },
                )
                return make_error(
                    priv_result.reason,
                    status_code=403,
                    details=priv_result.to_dict(),
                )

    # Check branch ownership policy (pass auth mode for relaxed policy in user mode)
    policy = get_policy_engine()
    policy_result = policy.check_branch_ownership(repo, branch, auth_mode=auth_mode)

    if not policy_result.allowed:
        audit_log(
            "push_denied",
            "git_push",
            success=False,
            details={
                "repo": repo,
                "branch": branch,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            f"Push denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    # SECURITY: Check for protected file modifications based on agent role.
    # File restrictions are configured in phase-permissions.json and enforced
    # by the PhaseFilter module. This prevents certain roles from modifying
    # specific files via git push (e.g., implementers cannot modify contract files).
    #
    # Note: Only configured roles are checked. The SYSTEM role is typically NOT
    # blocked because SYSTEM never makes git pushes - it only initializes contracts
    # via the contract API. The gateway itself runs without a role context.
    session_role = None
    changed_files = None  # May be populated by role check, reused by phase check
    if hasattr(g, "session") and g.session:
        session_role = getattr(g.session, "agent_role", None)

    if session_role:
        # Get the list of files being pushed
        changed_files, check_error = get_changed_files_in_push(exec_path, remote, branch)

        # SECURITY: Fail closed - if we can't determine changed files, block the push.
        # This prevents bypass via git diff manipulation (timeout, corrupt refs, etc.)
        if check_error:
            audit_log(
                "push_denied_file_check_failed",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "role": session_role,
                    "error": check_error,
                },
            )
            return make_error(
                f"Push denied: Could not verify file changes for security check: {check_error}",
                status_code=500,
                details={
                    "role": session_role,
                    "error": check_error,
                    "hint": "This is a security precaution. Try again or contact support.",
                },
            )

        # Check file restrictions using the PhaseFilter configuration
        restriction_result = check_file_restrictions(session_role, changed_files)
        if not restriction_result.allowed:
            audit_log(
                "push_denied_protected_files",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "role": session_role,
                    "blocked_files": restriction_result.blocked_files,
                    "blocked_reason": restriction_result.blocked_reason,
                },
            )
            return make_error(
                f"Push denied: {restriction_result.message}. {restriction_result.blocked_reason}",
                status_code=403,
                details={
                    "role": session_role,
                    "blocked_files": restriction_result.blocked_files,
                    "blocked_reason": restriction_result.blocked_reason,
                    "hint": "Use egg-contract CLI commands to update contract state.",
                },
            )

    # Agent-role file restrictions.
    # Checks agent_restrictions rules (coder vs tester vs documenter file scopes).
    # Default: warn-only (logs but allows push).
    # Set EGG_AGENT_RESTRICTIONS_ENFORCE=true to block pushes that violate
    # agent-role boundaries.
    if session_role and changed_files and not is_checkpoint_push:
        session_complexity_tier = getattr(g.session, "complexity_tier", None)
        agent_result = check_agent_restrictions(
            session_role, changed_files, complexity_tier=session_complexity_tier
        )
        if not agent_result.allowed:
            enforce = os.environ.get("EGG_AGENT_RESTRICTIONS_ENFORCE", "false").lower() in (
                "true",
                "1",
                "yes",
            )

            if enforce:
                audit_log(
                    "push_denied_agent_role_restriction",
                    "git_push",
                    success=False,
                    details={
                        "repo": repo,
                        "branch": branch,
                        "role": session_role,
                        "blocked_files": agent_result.blocked_files,
                        "restriction_message": agent_result.message,
                    },
                )
                return make_error(
                    f"Push denied: agent role '{session_role}' cannot modify "
                    f"these files. {agent_result.message}",
                    status_code=403,
                    details={
                        "role": session_role,
                        "blocked_files": agent_result.blocked_files,
                    },
                )
            else:
                logger.warning(
                    "Agent-role file restriction would block push (warn-only)",
                    event_type="agent_role_restriction_warning",
                    repo=repo,
                    branch=branch,
                    role=session_role,
                    blocked_files=agent_result.blocked_files,
                    restriction_message=agent_result.message,
                )

    # SECURITY: Check phase-based file restrictions for local mode sessions.
    # This replaces the blanket local-mode push block with granular phase-based
    # restrictions. Each phase has specific allowed/blocked file patterns:
    # - refine/plan: Can only push .egg-state/ files (contracts, drafts, checkpoints)
    # - implement: Can push code but not .egg-state/ (except checkpoints)
    # - pr: Can push everything
    #
    # Checkpoint branch pushes always bypass this check (see is_checkpoint_push above).
    if session_phase and not is_checkpoint_push:
        # Get the list of files being pushed (reuse if already fetched for role check)
        if changed_files is None:
            changed_files, check_error = get_changed_files_in_push(exec_path, remote, branch)
            if check_error:
                audit_log(
                    "push_denied_file_check_failed",
                    "git_push",
                    success=False,
                    details={
                        "repo": repo,
                        "branch": branch,
                        "phase": session_phase,
                        "error": check_error,
                    },
                )
                return make_error(
                    f"Push denied: Could not verify file changes for phase check: {check_error}",
                    status_code=500,
                    details={
                        "phase": session_phase,
                        "error": check_error,
                        "hint": "This is a security precaution. Try again or contact support.",
                    },
                )

        # Check phase-based file restrictions
        phase_result = check_phase_file_restrictions(session_phase, changed_files)
        if not phase_result.allowed:
            audit_log(
                "push_denied_phase_restrictions",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "phase": session_phase,
                    "blocked_files": phase_result.blocked_files,
                    "blocked_reason": phase_result.blocked_reason,
                },
            )
            has_non_state_files = any(
                not f.startswith(".egg-state/") for f in phase_result.blocked_files
            )
            if has_non_state_files:
                hint = (
                    "Branch contains files outside .egg-state/ from a previous phase. "
                    "Create a clean branch from origin/main with only your state files."
                )
            else:
                hint = f"Phase '{session_phase}' has file restrictions. Check allowed patterns."
            return make_error(
                f"Push denied: {phase_result.message}",
                status_code=403,
                details={
                    "phase": session_phase,
                    "blocked_files": phase_result.blocked_files,
                    "blocked_reason": phase_result.blocked_reason,
                    "hint": hint,
                },
            )

    # Get authentication token using shared helper
    token_str, auth_mode, token_error = get_token_for_repo(repo)
    if not token_str:
        return make_error(token_error, status_code=503)

    # Build push command with safe.directory for worktree paths
    # Convert SSH URLs to HTTPS since gateway uses token auth
    push_target = get_authenticated_remote_target(remote, remote_url)
    if push_target != remote:
        logger.debug(
            "Converting SSH URL to HTTPS for push",
            original_url=remote_url,
            https_url=push_target,
        )
    # SECURITY: Belt-and-suspenders hook prevention. The primary protection is
    # core.hooksPath=/dev/null in git_cmd() which disables ALL hooks globally.
    # --no-verify is added as defense-in-depth for the pre-push hook. See issue #58.
    push_args = ["push", "--no-verify"]
    if force:
        push_args.append("--force")
    push_args.extend([push_target, refspec] if refspec else [push_target])
    # Clear any http.extraheader from .git/config to ensure the gateway's
    # credential helper (GIT_ASKPASS) is used. actions/checkout@v4 persists
    # GITHUB_TOKEN as an extraheader by default, which takes precedence over
    # GIT_ASKPASS and may lack permissions (e.g., workflows scope).
    cmd = git_cmd("-c", "http.extraheader=", *push_args)

    # NOTE: Git author/committer info is set at COMMIT time, not push time.
    # For user mode, the user must configure their local git:
    #   git config user.name "Your Name"
    #   git config user.email "your@email.com"
    if auth_mode == "user":
        logger.debug("User mode push", repo=repo)

    # Get the remote ref SHA before push (for per-push checkpoint creation)
    # This allows us to identify the range of commits being pushed
    old_ref_sha: str | None = None
    try:
        # Use ls-remote to get the current remote ref
        ls_remote_result = subprocess.run(
            git_cmd("ls-remote", push_target, f"refs/heads/{branch}"),
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if ls_remote_result.returncode == 0 and ls_remote_result.stdout.strip():
            # Output format: "<sha>\trefs/heads/<branch>"
            old_ref_sha = ls_remote_result.stdout.strip().split()[0]
            logger.debug(
                "Got remote ref before push",
                branch=branch,
                old_ref_sha=old_ref_sha[:7] if old_ref_sha else None,
            )
        else:
            # Branch doesn't exist on remote (new branch push)
            old_ref_sha = "0" * 40
            logger.debug("Branch does not exist on remote (new branch)", branch=branch)
    except Exception as e:
        # If we can't get the old ref, we'll fall back to single-commit checkpoint
        logger.debug("Could not get remote ref before push", error=str(e))
        old_ref_sha = None

    # Create credential helper and execute push
    credential_helper_path = None
    try:
        credential_helper_path, env = create_credential_helper(token_str, os.environ.copy())

        result = subprocess.run(
            cmd,
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )

        if result.returncode == 0:
            audit_log(
                "push_success",
                "git_push",
                success=True,
                details={
                    "repo": repo,
                    "branch": branch,
                    "force": force,
                    "auth_mode": auth_mode,
                },
            )

            # Capture per-push checkpoint after successful push (async, non-blocking)
            # Get the HEAD commit SHA (new_sha) from the worktree
            try:
                head_result = subprocess.run(
                    git_cmd("rev-parse", "HEAD"),
                    cwd=exec_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                new_sha = head_result.stdout.strip() if head_result.returncode == 0 else None

                if new_sha:
                    # Get session from request context
                    session = getattr(g, "session", None)

                    # Look up checkpoint repo config (may be a separate repo)
                    ckpt_repo = get_checkpoint_repo(repo) if repo else None

                    # Store on session for session-end checkpoint use
                    if session is not None:
                        session.checkpoint_repo = ckpt_repo  # None clears previous value
                        session.last_repo_path = exec_path
                        session.last_branch = branch

                    if old_ref_sha:
                        # Create single checkpoint for the push tip
                        capture_and_store_checkpoints_for_push(
                            repo_path=exec_path,
                            new_sha=new_sha,
                            branch=branch,
                            session=session,
                            github_token=token_str,
                            async_store=True,  # Don't block push response
                            checkpoint_repo=ckpt_repo,
                        )
                    else:
                        # Fallback: couldn't get old ref, create single checkpoint
                        capture_and_store_checkpoint(
                            repo_path=exec_path,
                            commit_sha=new_sha,
                            branch=branch,
                            session=session,
                            push_sha=new_sha,
                            github_token=token_str,
                            async_store=True,
                            checkpoint_repo=ckpt_repo,
                        )
            except Exception as checkpoint_err:
                # Checkpoint failure should never block push success
                logger.warning(
                    "Checkpoint capture failed (non-blocking)",
                    error=str(checkpoint_err),
                    branch=branch,
                )

            return make_success(
                "Push successful",
                {
                    "repo": repo,
                    "branch": branch,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "auth_mode": auth_mode,
                },
            )
        else:
            audit_log(
                "push_failed",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "returncode": result.returncode,
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                f"Push failed: {result.stderr}",
                status_code=500,
                details={"stdout": result.stdout, "stderr": result.stderr},
            )

    except subprocess.TimeoutExpired:
        return make_error("Push timed out", status_code=504)
    except Exception as e:
        return make_error(f"Push failed: {e}", status_code=500)
    finally:
        cleanup_credential_helper(credential_helper_path)


@app.route("/api/v1/git/execute", methods=["POST"])
@require_session_auth
def git_execute() -> tuple[Response, int] | Response:
    """
    Execute a git command in the gateway's worktree.

    This is the primary endpoint for all git operations in the gateway-managed
    worktree architecture. The container has no direct git access (its .git is
    shadowed by tmpfs), so all git commands route through this endpoint.

    Request body:
        {
            "repo_path": "/home/egg/repos/myrepo",
            "operation": "status",
            "args": ["--porcelain"],
            "container_id": "egg-xxx"  # For path mapping
        }

    Supported operations: status, add, commit, log, diff, show, branch,
    checkout, switch, reset, restore, stash, merge, rebase, cherry-pick,
    tag, clean, config, rev-parse, remote, apply, format-patch

    Network operations (push, fetch, ls-remote) should use dedicated endpoints.
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    operation = data.get("operation")
    args = data.get("args", [])
    container_id = data.get("container_id")

    if not repo_path:
        return make_error("Missing repo_path")
    if not operation:
        return make_error("Missing operation")

    # Validate repo_path
    path_valid, path_error = validate_repo_path(repo_path)
    if not path_valid:
        audit_log(
            "git_execute_blocked",
            operation,
            success=False,
            details={
                "repo_path": repo_path,
                "git_args": args,
                "container_id": container_id,
                "reason": path_error,
            },
        )
        return make_error(path_error, status_code=403)

    # Check if this is a "repos parent" directory (contains repos but isn't one)
    # Git operations in these directories are expected to fail - this is commonly
    # caused by tools like Claude Code running `git rev-parse` to detect if they're
    # in a repo. Return a clear error without logging a warning (since this is
    # expected behavior, not an error condition).
    if is_repos_parent_directory(repo_path):
        logger.debug(
            "Git operation in repos parent directory",
            operation=operation,
            repo_path=repo_path,
            container_id=container_id,
        )
        return make_error(
            f"Path '{repo_path}' is a directory containing repositories, not a git repository. "
            "Run git commands from within a specific repository directory.",
            status_code=400,
            details={
                "hint": "This directory contains repositories but is not itself a git repository.",
                "repo_path": repo_path,
            },
        )

    # Validate operation is in allowlist
    if operation not in GIT_ALLOWED_COMMANDS:
        audit_log(
            "git_execute_blocked",
            operation,
            success=False,
            details={
                "repo_path": repo_path,
                "git_args": args,
                "container_id": container_id,
                "reason": "Operation not allowed",
            },
        )
        return make_error(
            f"Operation '{operation}' not allowed. "
            f"Allowed: {', '.join(sorted(GIT_ALLOWED_COMMANDS.keys()))}",
            status_code=403,
        )

    # Network operations should use dedicated endpoints
    if operation in ("push", "fetch", "ls-remote"):
        return make_error(
            f"Use dedicated endpoint for {operation}: /api/v1/git/{operation}",
            status_code=400,
        )

    # Validate args against allowlist
    args_valid, args_error, validated_args = validate_git_args(operation, args)
    if not args_valid:
        audit_log(
            "git_execute_blocked",
            operation,
            success=False,
            details={
                "repo_path": repo_path,
                "git_args": args,
                "container_id": container_id,
                "reason": args_error,
            },
        )
        return make_error(args_error, status_code=400)

    # SECURITY: Block branch-switching for pipeline sessions.
    # Pipeline containers are locked to their worktree branch to prevent
    # cross-contamination between pipeline tasks.
    if is_branch_switch(operation, validated_args):
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        if isinstance(assigned, str) and assigned:
            audit_log(
                "git_execute_blocked",
                operation,
                success=False,
                details={
                    "repo_path": repo_path,
                    "git_args": validated_args,
                    "container_id": container_id,
                    "assigned_branch": assigned,
                    "reason": "Branch switching blocked in pipeline session",
                },
            )
            return make_error(
                f"Branch switching is not allowed in pipeline sessions. "
                f"You are locked to branch '{assigned}'. "
                f"Use 'git checkout -- <file>' to restore files instead.",
                status_code=403,
            )

    # Map container path to worktree path if container_id is provided
    exec_path = map_container_path_to_worktree(repo_path, container_id, operation)
    is_worktree = exec_path != repo_path

    # SECURITY: Enforce branch isolation in pipeline worktree sessions.
    # Pipeline agents in worktrees must stay on their assigned branch.
    # Interactive sessions are unrestricted even if they use worktrees.
    # We detect pipeline sessions by the presence of pipeline_id on the
    # session (set for both "issue" and "local" pipeline modes), rather
    # than checking session_mode, because issue-mode pipelines use
    # session_mode="public" while local-mode pipelines use "local".
    # See issue #773.
    session = getattr(g, "session", None)
    is_pipeline = session is not None and getattr(session, "pipeline_id", None) is not None
    if is_pipeline and is_worktree and is_branch_switching_operation(operation, validated_args):
        audit_log(
            "git_execute_blocked",
            operation,
            success=False,
            details={
                "repo_path": repo_path,
                "git_args": args,
                "container_id": container_id,
                "pipeline_id": session.pipeline_id,
                "session_mode": getattr(g, "session_mode", None),
                "reason": "Branch switching blocked in pipeline worktree session",
            },
        )
        return make_error(
            "Branch switching is not allowed in pipeline worktree sessions. "
            "You are locked to your assigned branch. "
            "Use 'git restore' for file operations instead of 'git checkout'.",
            status_code=403,
        )

    # SECURITY: Validate staged files at commit time for pipeline sessions.
    # This is an early-catch complement to push-time validation — prevents the
    # agent from building up invalid commits that would only be rejected at push.
    if operation == "commit":
        session = getattr(g, "session", None)
        session_phase = getattr(g, "session_phase", None) if session else None
        if session_phase:
            import subprocess as _sp

            try:
                staged_result = _sp.run(
                    git_cmd("diff", "--cached", "--name-only"),
                    cwd=exec_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if staged_result.returncode == 0:
                    staged_files = [
                        f.strip() for f in staged_result.stdout.strip().split("\n") if f.strip()
                    ]
                    if staged_files:
                        phase_result = check_phase_file_restrictions(session_phase, staged_files)
                        if not phase_result.allowed:
                            audit_log(
                                "git_execute_blocked",
                                operation,
                                success=False,
                                details={
                                    "repo_path": repo_path,
                                    "git_args": validated_args,
                                    "container_id": container_id,
                                    "phase": session_phase,
                                    "blocked_files": phase_result.blocked_files,
                                    "reason": "Staged files violate phase restrictions",
                                },
                            )
                            return make_error(
                                f"Commit blocked: {phase_result.message}. "
                                f"Unstage the blocked files with 'git reset HEAD <file>'.",
                                status_code=403,
                            )
            except Exception:
                # Fail open for commit-time check — push-time check is the
                # authoritative gate and will catch any violations.
                logger.warning(
                    "Staged-file check skipped due to error",
                    operation=operation,
                    container_id=container_id,
                )

    # SECURITY: Belt-and-suspenders hook prevention for operations that support it.
    # The primary protection is core.hooksPath=/dev/null in git_cmd() which disables
    # ALL hooks globally. However, we also add --no-verify for operations that
    # support it as defense-in-depth. See issue #58.
    #
    # Operations that support --no-verify:
    # - commit: pre-commit, prepare-commit-msg, commit-msg, post-commit
    # - merge: pre-merge-commit, prepare-commit-msg, commit-msg, post-merge
    # - am: pre-applypatch, applypatch-msg, post-applypatch
    #
    # Note: cherry-pick is NOT included here. While git 2.36+ added --no-verify
    # for cherry-pick, older versions (including 2.34) reject it with a usage error.
    # The primary protection (core.hooksPath=/dev/null) already covers cherry-pick.
    # See issue #118.
    if operation in ("commit", "merge", "am"):
        validated_args = ["--no-verify", *validated_args]

    # Build command
    cmd = git_cmd(operation, *validated_args)

    # Set GIT_EDITOR=true so operations that need an editor (e.g., rebase
    # --continue after conflict resolution) succeed without a terminal.
    # `true` accepts the default commit message, which is the expected
    # behavior for an agent that always provides messages via -m.
    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"

    try:
        result = subprocess.run(
            cmd,
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )

        if result.returncode == 0:
            audit_log(
                "git_execute_success",
                operation,
                success=True,
                details={
                    "repo_path": repo_path,
                    "git_args": validated_args,
                    "container_id": container_id,
                },
            )
            return make_success(
                f"git {operation} successful",
                {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                },
            )
        else:
            # Check if this is an expected failure (e.g., repo detection queries)
            # These happen when tools check if a directory is a git repo
            is_expected_failure = result.stderr and (
                "not a git repository" in result.stderr
                or "not inside a git repository" in result.stderr
            )

            if is_expected_failure:
                # Log at debug level for expected failures - these are typically
                # from tools probing to detect if they're in a git repo
                logger.debug(
                    "Git operation failed (expected - not a git repository)",
                    operation=operation,
                    repo_path=repo_path,
                    container_id=container_id,
                )
            else:
                # Log at warning level for unexpected failures
                audit_log(
                    "git_execute_failed",
                    operation,
                    success=False,
                    details={
                        "repo_path": repo_path,
                        "git_args": validated_args,
                        "returncode": result.returncode,
                        "container_id": container_id,
                        "stderr": result.stderr[:500] if result.stderr else None,
                    },
                )

            return make_error(
                f"git {operation} failed",
                status_code=500,
                details={
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                },
            )

    except subprocess.TimeoutExpired:
        return make_error(f"git {operation} timed out", status_code=504)
    except Exception as e:
        return make_error(f"git {operation} failed: {e}", status_code=500)


@app.route("/api/v1/git/fetch", methods=["POST"])
@require_session_auth
def git_fetch() -> tuple[Response, int] | Response:
    """
    Handle git fetch requests.

    Required because the container doesn't have direct access to GitHub tokens
    (they are held by the gateway sidecar). This endpoint provides authenticated
    fetch for git fetch, git ls-remote, and similar read operations.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "remote": "origin",
            "args": ["--tags"]  # optional additional args
        }

    For ls-remote:
        {
            "repo_path": "/path/to/repo",
            "operation": "ls-remote",
            "remote": "origin",
            "args": ["HEAD"]  # optional refs to query
        }
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    remote = data.get("remote", "origin")
    operation = data.get("operation", "fetch")  # fetch or ls-remote
    extra_args = data.get("args", [])
    container_id = data.get("container_id")

    if not repo_path:
        return make_error("Missing repo_path")

    # Validate repo_path to prevent path traversal attacks
    path_valid, path_error = validate_repo_path(repo_path)
    if not path_valid:
        audit_log(
            "fetch_blocked",
            "git_fetch",
            success=False,
            details={"repo_path": repo_path, "reason": path_error},
        )
        return make_error(path_error, status_code=403)

    if operation not in ("fetch", "ls-remote"):
        return make_error(f"Unsupported operation: {operation}")

    # Validate extra args against operation-specific allowlist
    args_valid, args_error, validated_args = validate_git_args(operation, extra_args)
    if not args_valid:
        audit_log(
            "fetch_blocked",
            "git_fetch",
            success=False,
            details={"reason": args_error, "operation": operation},
        )
        return make_error(args_error, status_code=400)

    # Map container path to worktree path if container_id is provided
    exec_path = map_container_path_to_worktree(repo_path, container_id, operation)

    # Get remote URL to determine repo
    remote_url, url_error = resolve_remote_url(remote, exec_path)
    if url_error:
        return make_error(url_error)

    # Extract repo from URL
    repo = extract_repo_from_remote(remote_url)
    if not repo:
        return make_error(f"Could not parse repository from URL: {remote_url}")

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        # Checkpoint repos are infrastructure — always accessible regardless of session mode
        if is_checkpoint_repo(repo_info.owner, repo_info.repo):
            audit_log(
                f"{operation}_checkpoint_repo_exempt",
                f"git_{operation}",
                success=True,
                details={
                    "repo": repo,
                    "reason": "Checkpoint repo exempt from private mode policy",
                },
            )
        else:
            priv_result = check_private_repo_access(
                operation=operation,
                owner=repo_info.owner,
                repo=repo_info.repo,
                for_write=False,
                session_mode=session_mode,
            )
            if not priv_result.allowed:
                audit_log(
                    f"{operation}_denied_private_mode",
                    f"git_{operation}",
                    success=False,
                    details={
                        "repo": repo,
                        "reason": priv_result.reason,
                        "visibility": priv_result.visibility,
                    },
                )
                return make_error(
                    priv_result.reason,
                    status_code=403,
                    details=priv_result.to_dict(),
                )

    # Get authentication token using shared helper
    token_str, auth_mode, token_error = get_token_for_repo(repo)
    if not token_str:
        return make_error(token_error, status_code=503)

    # Convert SSH URLs to HTTPS since gateway uses token auth
    fetch_target = get_authenticated_remote_target(remote, remote_url)
    if fetch_target != remote:
        logger.debug(
            f"Converting SSH URL to HTTPS for {operation}",
            original_url=remote_url,
            https_url=fetch_target,
        )

    # Build command using validated args
    if operation == "fetch":
        # Don't include remote when --all is specified (fetches from all remotes)
        if "--all" in validated_args:
            cmd_args = ["fetch"] + validated_args
        else:
            cmd_args = ["fetch", fetch_target] + validated_args
    else:  # ls-remote
        cmd_args = ["ls-remote", fetch_target] + validated_args

    cmd = git_cmd(*cmd_args)

    # Create credential helper and execute operation
    credential_helper_path = None
    try:
        credential_helper_path, env = create_credential_helper(token_str, os.environ.copy())

        result = subprocess.run(
            cmd,
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )

        if result.returncode == 0:
            audit_log(
                f"{operation}_success",
                f"git_{operation}",
                success=True,
                details={
                    "repo": repo,
                    "auth_mode": auth_mode,
                },
            )
            return make_success(
                f"{operation.capitalize()} successful",
                {
                    "repo": repo,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "auth_mode": auth_mode,
                },
            )
        else:
            audit_log(
                f"{operation}_failed",
                f"git_{operation}",
                success=False,
                details={
                    "repo": repo,
                    "returncode": result.returncode,
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                f"{operation.capitalize()} failed: {result.stderr}",
                status_code=500,
                details={"stdout": result.stdout, "stderr": result.stderr},
            )

    except subprocess.TimeoutExpired:
        return make_error(f"{operation.capitalize()} timed out", status_code=504)
    except Exception as e:
        return make_error(f"{operation.capitalize()} failed: {e}", status_code=500)
    finally:
        cleanup_credential_helper(credential_helper_path)


# ---------------------------------------------------------------------------
# Checkpoint read endpoints
# ---------------------------------------------------------------------------


def _int_param(name: str) -> int | None:
    """Parse an optional integer query parameter from the current request."""
    val = request.args.get(name)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


@app.route("/api/v1/checkpoints", methods=["GET"])
@require_session_auth
def checkpoint_list() -> tuple[Response, int] | Response:
    """
    List checkpoint summaries with optional filters.

    Query params:
        repo_path: Repository path (required if not inferable)
        checkpoint_repo: External checkpoint repo (owner/repo), overrides auto-detection
        issue: Filter by issue number
        pr: Filter by PR number
        branch: Filter by branch name
        session: Filter by session ID
        trigger: Filter by trigger type
        status: Filter by session status
        agent_type: Filter by agent type
        phase: Filter by pipeline phase
        pipeline: Filter by pipeline ID
        repo: Filter by source repository (owner/repo)
        limit: Maximum results (default 50)
    """
    from egg_contracts.checkpoint_loader import filter_checkpoints_v2

    repo_path = _resolve_repo_path_for_checkpoints()
    if not repo_path:
        return make_error("Cannot determine repo_path")

    handler = get_checkpoint_handler()
    checkpoint_repo = _resolve_checkpoint_repo(repo_path)

    try:
        index = handler.fetch_and_read_index(repo_path, checkpoint_repo=checkpoint_repo)
    except Exception as e:
        logger.error("Checkpoint index fetch failed", error=str(e))
        return make_error("Failed to fetch checkpoints", status_code=500)

    if not index:
        return make_success("No checkpoints found", {"checkpoints": []})

    # Build filters from query params
    filters: dict[str, Any] = {}

    if request.args.get("issue"):
        filters["issue_number"] = _int_param("issue")
    if request.args.get("pr"):
        filters["pr_number"] = _int_param("pr")
    if request.args.get("branch"):
        filters["branch"] = request.args["branch"]
    if request.args.get("session"):
        filters["session_id"] = request.args["session"]
    if request.args.get("trigger"):
        filters["trigger_type"] = request.args["trigger"]
    if request.args.get("status"):
        filters["session_status"] = request.args["status"]
    if request.args.get("agent_type"):
        filters["agent_type"] = request.args["agent_type"]
    if request.args.get("phase"):
        filters["pipeline_phase"] = request.args["phase"]
    if request.args.get("pipeline"):
        filters["pipeline_id"] = request.args["pipeline"]
    if request.args.get("repo"):
        filters["repo"] = request.args["repo"]

    limit = _int_param("limit")
    filters["limit"] = limit if limit is not None else 50

    summaries = filter_checkpoints_v2(index, **filters)
    data = [s.model_dump(mode="json") for s in summaries]

    return make_success("OK", {"checkpoints": data})


@app.route("/api/v1/checkpoints/cost", methods=["GET"])
@require_session_auth
def checkpoint_cost() -> tuple[Response, int] | Response:
    """
    Get cost breakdown for matching checkpoints.

    Query params:
        repo_path: Repository path
        checkpoint_repo: External checkpoint repo (owner/repo), overrides auto-detection
        pipeline: Filter by pipeline ID
        issue: Filter by issue number
        pr: Filter by PR number
        limit: Maximum checkpoints to load (default 500)
    """
    from egg_contracts.checkpoint_loader import filter_checkpoints_v2
    from egg_contracts.usage import TokenCounts

    repo_path = _resolve_repo_path_for_checkpoints()
    if not repo_path:
        return make_error("Cannot determine repo_path")

    handler = get_checkpoint_handler()
    checkpoint_repo = _resolve_checkpoint_repo(repo_path)

    # fetch_and_read_index does ls-remote + fetch + read index in one pass.
    # We then call ensure_ref to get a ref for read_checkpoint calls below.
    # After the fetch in fetch_and_read_index, ensure_ref's fetch is a no-op
    # (branch already up-to-date), so only the ls-remote is repeated.
    try:
        index = handler.fetch_and_read_index(repo_path, checkpoint_repo=checkpoint_repo)
    except Exception as e:
        logger.error("Checkpoint index fetch failed", error=str(e))
        return make_error("Failed to fetch checkpoint data", status_code=500)

    if not index:
        return make_success(
            "No checkpoints found",
            {
                "checkpoint_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0,
                "breakdown": [],
            },
        )

    try:
        ref = handler.ensure_ref(repo_path, checkpoint_repo=checkpoint_repo)
    except Exception as e:
        logger.error("Checkpoint ref resolution failed", error=str(e))
        return make_error("Failed to fetch checkpoint data", status_code=500)

    if not ref:
        return make_success(
            "No checkpoints found",
            {
                "checkpoint_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0,
                "breakdown": [],
            },
        )

    filters: dict[str, Any] = {}
    if request.args.get("pipeline"):
        filters["pipeline_id"] = request.args["pipeline"]
    if request.args.get("issue"):
        filters["issue_number"] = _int_param("issue")
    if request.args.get("pr"):
        filters["pr_number"] = _int_param("pr")
    limit = _int_param("limit")
    filters["limit"] = limit if limit is not None else 500

    summaries = filter_checkpoints_v2(index, **filters)
    if not summaries:
        return make_success(
            "No checkpoints found",
            {
                "checkpoint_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0,
                "breakdown": [],
            },
        )

    rows: list[dict[str, Any]] = []
    for s in summaries:
        checkpoint = handler.read_checkpoint(repo_path, s.id, ref)
        if not checkpoint or not checkpoint.token_usage:
            continue

        tu = checkpoint.token_usage
        model = checkpoint.session.model if checkpoint.session else None
        tokens = TokenCounts(
            input_tokens=tu.input_tokens,
            output_tokens=tu.output_tokens,
            cache_read_tokens=tu.cache_read_tokens,
            cache_creation_tokens=tu.cache_creation_tokens,
        )
        cost = float(tokens.calculate_cost(model=model))
        phase = checkpoint.pipeline_phase or "(none)"
        agent = checkpoint.agent_type.value if checkpoint.agent_type else "unknown"
        rows.append(
            {
                "phase": phase,
                "agent": agent,
                "input_tokens": tu.input_tokens,
                "output_tokens": tu.output_tokens,
                "cost": cost,
            }
        )

    if not rows:
        return make_success(
            "No cost data",
            {
                "checkpoint_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cost_usd": 0,
                "breakdown": [],
            },
        )

    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["phase"], row["agent"])
        if key not in agg:
            agg[key] = {"input": 0, "output": 0, "cost": 0.0, "count": 0}
        agg[key]["input"] += row["input_tokens"]
        agg[key]["output"] += row["output_tokens"]
        agg[key]["cost"] += row["cost"]
        agg[key]["count"] += 1

    return make_success(
        "OK",
        {
            "checkpoint_count": len(rows),
            "total_input_tokens": sum(v["input"] for v in agg.values()),
            "total_output_tokens": sum(v["output"] for v in agg.values()),
            "total_cost_usd": round(sum(v["cost"] for v in agg.values()), 4),
            "breakdown": [
                {
                    "phase": k[0],
                    "agent": k[1],
                    "input_tokens": v["input"],
                    "output_tokens": v["output"],
                    "cost_usd": round(v["cost"], 4),
                    "checkpoint_count": v["count"],
                }
                for k, v in sorted(agg.items())
            ],
        },
    )


@app.route("/api/v1/checkpoints/<identifier>", methods=["GET"])
@require_session_auth
def checkpoint_show(identifier: str) -> tuple[Response, int] | Response:
    """
    Get a full checkpoint by ID or commit SHA.

    Path params:
        identifier: Checkpoint ID (ckpt-...) or commit SHA

    Query params:
        repo_path: Repository path
        checkpoint_repo: External checkpoint repo (owner/repo), overrides auto-detection
    """
    repo_path = _resolve_repo_path_for_checkpoints()
    if not repo_path:
        return make_error("Cannot determine repo_path")

    handler = get_checkpoint_handler()
    checkpoint_repo = _resolve_checkpoint_repo(repo_path)

    try:
        ref = handler.ensure_ref(repo_path, checkpoint_repo=checkpoint_repo)
    except Exception as e:
        logger.error("Checkpoint ref fetch failed", error=str(e))
        return make_error("Failed to fetch checkpoint data", status_code=500)

    if not ref:
        return make_error("Checkpoint branch not found", status_code=404)

    checkpoint_id = identifier
    if not identifier.startswith("ckpt-"):
        # Look up by commit SHA
        index = handler.fetch_and_read_index(repo_path, checkpoint_repo=checkpoint_repo)
        if index:
            checkpoint_id = index.get_by_commit(identifier)
        if not checkpoint_id:
            return make_error(f"Checkpoint not found: {identifier}", status_code=404)

    checkpoint = handler.read_checkpoint(repo_path, checkpoint_id, ref)
    if not checkpoint:
        return make_error(f"Checkpoint not found: {identifier}", status_code=404)

    return make_success("OK", {"checkpoint": checkpoint.model_dump(mode="json")})


def _resolve_checkpoint_repo(repo_path: str) -> str | None:
    """Resolve checkpoint_repo from query param or auto-detection.

    Accepts an explicit ``checkpoint_repo`` query parameter in
    ``owner/repo`` format.  Falls back to auto-detection via
    ``_get_checkpoint_repo_for_path`` when no explicit value is given.
    """
    explicit = request.args.get("checkpoint_repo")
    if explicit:
        # Basic validation: must look like "owner/repo"
        if re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", explicit):
            return explicit
        logger.warning(
            "Invalid checkpoint_repo format, falling back to auto-detection",
            checkpoint_repo=explicit,
        )
        return None
    return _get_checkpoint_repo_for_path(repo_path)


def _resolve_repo_path_for_checkpoints() -> str | None:
    """Resolve repository path for checkpoint read operations.

    Tries query param, then session's last_repo_path, then EGG_REPO_PATH.
    """
    # Explicit query param — if provided, must be valid; don't silently
    # fall through to fallbacks when the client explicitly requested a path.
    repo_path = request.args.get("repo_path")
    if repo_path:
        path_valid, _err = validate_repo_path(repo_path)
        if path_valid and os.path.isdir(repo_path):
            return repo_path
        return None

    # Session's last known repo path (set during push operations)
    session = getattr(g, "session", None)
    if session and getattr(session, "last_repo_path", None):
        return session.last_repo_path

    # Environment variable
    env_path = os.environ.get("EGG_REPO_PATH")
    if env_path and os.path.isdir(env_path):
        return env_path

    return None


@app.route("/api/v1/gh/pr/create", methods=["POST"])
@require_session_auth
def gh_pr_create() -> tuple[Response, int] | Response:
    """
    Create a pull request.

    Request body:
        {
            "repo": "owner/repo",
            "title": "PR title",
            "body": "PR body",
            "base": "main",
            "head": "feature-branch"
        }

    Policy:
        - Bot mode: allowed (egg can create PRs)
        - User mode: blocked (user must create PRs manually via GitHub UI)
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    title = data.get("title")
    body = data.get("body", "")
    base = data.get("base", "main")
    head = data.get("head")

    if not repo:
        return make_error("Missing repo")
    if not title:
        return make_error("Missing title")
    if not head:
        return make_error("Missing head branch")

    # Determine auth mode for this repo
    auth_mode = get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Get session phase from request context (set by @require_session_auth decorator)
    session_phase = getattr(g, "session_phase", None)

    # Block PR creation in local SDLC mode (except during PR phase, where
    # phase-permissions grant it and the gateway provides push access).
    if session_mode == "local" and session_phase != "pr":
        audit_log(
            "pr_create_blocked_local_mode",
            "gh_pr_create",
            success=False,
            details={"repo": repo, "reason": "PR creation blocked in local SDLC mode"},
        )
        return make_error(
            "Operation blocked in local SDLC mode. Create PR manually when the pipeline completes.",
            status_code=403,
            details={"session_mode": "local"},
        )

    # Check phase restrictions (if session has a phase set)
    if session_phase:
        try:
            phase_result = filter_operation(
                phase=session_phase,
                operation_type=OperationType.GH,
                command="pr create",
            )
            if not phase_result.allowed:
                audit_log(
                    "pr_create_blocked_phase",
                    "gh_pr_create",
                    success=False,
                    details={
                        "repo": repo,
                        "phase": session_phase,
                        "reason": phase_result.blocked_reason,
                    },
                )
                return make_error(
                    phase_result.message,
                    status_code=403,
                    details={
                        "phase": session_phase,
                        "blocked_reason": phase_result.blocked_reason,
                    },
                )
        except ValueError as e:
            # Invalid phase value - log warning and allow (backward compat)
            logger.warning(
                "Invalid session phase value",
                phase=session_phase,
                error=str(e),
            )
    else:
        # No phase set - allow by default for backward compatibility
        # Log a warning to track sessions without phase
        logger.debug(
            "PR create request from session without phase (backward compat)",
            repo=repo,
        )

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = check_private_repo_access(
            operation="pr_create",
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            audit_log(
                "pr_create_denied_private_mode",
                "gh_pr_create",
                success=False,
                details={
                    "repo": repo,
                    "reason": priv_result.reason,
                    "visibility": priv_result.visibility,
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                priv_result.reason,
                status_code=403,
                details=priv_result.to_dict(),
            )

    # Policy check: PR creation may be blocked in user mode
    policy = get_policy_engine()
    policy_result = policy.check_pr_create_allowed(repo, auth_mode=auth_mode)
    if not policy_result.allowed:
        audit_log(
            "pr_create_blocked",
            "gh_pr_create",
            success=False,
            details={
                "repo": repo,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            policy_result.reason,
            status_code=403,
            details=policy_result.details,
        )

    try:
        github = get_github_client(mode=auth_mode)
        args = [
            "pr",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
            "--base",
            base,
            "--head",
            head,
        ]

        result = github.execute(args, timeout=60, mode=auth_mode)

        if result.success:
            audit_log(
                "pr_created",
                "gh_pr_create",
                success=True,
                details={
                    "repo": repo,
                    "title": title,
                    "base": base,
                    "head": head,
                    "auth_mode": auth_mode,
                },
            )
            return make_success(
                "PR created",
                {"stdout": result.stdout, "stderr": result.stderr, "auth_mode": auth_mode},
            )
        else:
            error_msg = result.stderr or "Unknown error"
            audit_log(
                "pr_create_failed",
                "gh_pr_create",
                success=False,
                details={
                    "repo": repo,
                    "error": error_msg[:200] if error_msg else "",
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                f"Failed to create PR: {error_msg}",
                status_code=500,
                details=result.to_dict(),
            )
    except Exception as e:
        logger.exception("Unexpected error in gh_pr_create")
        return make_error(f"Internal error: {e}", status_code=500)


@app.route("/api/v1/gh/pr/comment", methods=["POST"])
@require_session_auth
def gh_pr_comment() -> tuple[Response, int] | Response:
    """
    Add a comment to a PR.

    Request body:
        {
            "repo": "owner/repo",
            "pr_number": 123,
            "body": "Comment text"
        }

    Policy: pr_comment (allowed on any PR)
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    pr_number = data.get("pr_number")
    body = data.get("body")

    if not repo:
        return make_error("Missing repo")
    if not pr_number:
        return make_error("Missing pr_number")
    if not body:
        return make_error("Missing body")

    # Determine auth mode for this repo
    auth_mode = get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Block PR comment in local SDLC mode (except during PR phase)
    session_phase = getattr(g, "session_phase", None)
    if session_mode == "local" and session_phase != "pr":
        audit_log(
            "pr_comment_blocked_local_mode",
            "gh_pr_comment",
            success=False,
            details={"repo": repo, "reason": "PR comment blocked in local SDLC mode"},
        )
        return make_error(
            "Operation blocked in local SDLC mode. Interact with PRs manually when the pipeline completes.",
            status_code=403,
            details={"session_mode": "local"},
        )

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = check_private_repo_access(
            operation="pr_comment",
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            audit_log(
                "pr_comment_denied_private_mode",
                "gh_pr_comment",
                success=False,
                details={
                    "repo": repo,
                    "pr_number": pr_number,
                    "reason": priv_result.reason,
                    "visibility": priv_result.visibility,
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                priv_result.reason,
                status_code=403,
                details=priv_result.to_dict(),
            )

    # Check if commenting is allowed (allowed on any PR)
    policy = get_policy_engine()
    policy_result = policy.check_pr_comment_allowed(repo, pr_number)

    if not policy_result.allowed:
        audit_log(
            "pr_comment_denied",
            "gh_pr_comment",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            f"Comment denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    github = get_github_client(mode=auth_mode)
    args = [
        "pr",
        "comment",
        str(pr_number),
        "--repo",
        repo,
        "--body",
        body,
    ]

    result = github.execute(args, timeout=30, mode=auth_mode)

    if result.success:
        audit_log(
            "pr_comment_added",
            "gh_pr_comment",
            success=True,
            details={"repo": repo, "pr_number": pr_number, "auth_mode": auth_mode},
        )
        return make_success("Comment added", {"stdout": result.stdout, "auth_mode": auth_mode})
    else:
        return make_error(
            f"Failed to add comment: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )


@app.route("/api/v1/gh/pr/edit", methods=["POST"])
@require_session_auth
def gh_pr_edit() -> tuple[Response, int] | Response:
    """
    Edit a PR title or body.

    Request body:
        {
            "repo": "owner/repo",
            "pr_number": 123,
            "title": "New title",  # optional
            "body": "New body"      # optional
        }

    Policy: pr_ownership
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    pr_number = data.get("pr_number")
    title = data.get("title")
    body = data.get("body")

    if not repo:
        return make_error("Missing repo")
    if not pr_number:
        return make_error("Missing pr_number")
    if not title and not body:
        return make_error("Must provide title or body to edit")

    # Determine auth mode for this repo
    auth_mode = get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Block PR edit in local SDLC mode (except during PR phase)
    session_phase = getattr(g, "session_phase", None)
    if session_mode == "local" and session_phase != "pr":
        audit_log(
            "pr_edit_blocked_local_mode",
            "gh_pr_edit",
            success=False,
            details={"repo": repo, "reason": "PR edit blocked in local SDLC mode"},
        )
        return make_error(
            "Operation blocked in local SDLC mode. Edit PRs manually when the pipeline completes.",
            status_code=403,
            details={"session_mode": "local"},
        )

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = check_private_repo_access(
            operation="pr_edit",
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            audit_log(
                "pr_edit_denied_private_mode",
                "gh_pr_edit",
                success=False,
                details={
                    "repo": repo,
                    "pr_number": pr_number,
                    "reason": priv_result.reason,
                    "visibility": priv_result.visibility,
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                priv_result.reason,
                status_code=403,
                details=priv_result.to_dict(),
            )

    # Check PR ownership (pass auth mode for relaxed policy in user mode)
    policy = get_policy_engine()
    policy_result = policy.check_pr_ownership(repo, pr_number, auth_mode=auth_mode)

    if not policy_result.allowed:
        audit_log(
            "pr_edit_denied",
            "gh_pr_edit",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            f"Edit denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    github = get_github_client(mode=auth_mode)
    args = ["pr", "edit", str(pr_number), "--repo", repo]
    if title:
        args.extend(["--title", title])
    if body:
        args.extend(["--body", body])

    result = github.execute(args, timeout=30, mode=auth_mode)

    if result.success:
        audit_log(
            "pr_edited",
            "gh_pr_edit",
            success=True,
            details={"repo": repo, "pr_number": pr_number, "auth_mode": auth_mode},
        )
        return make_success("PR edited", {"stdout": result.stdout, "auth_mode": auth_mode})
    else:
        return make_error(
            f"Failed to edit PR: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )


@app.route("/api/v1/gh/pr/close", methods=["POST"])
@require_session_auth
def gh_pr_close() -> tuple[Response, int] | Response:
    """
    Close a PR.

    Request body:
        {
            "repo": "owner/repo",
            "pr_number": 123
        }

    Policy: pr_ownership
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    pr_number = data.get("pr_number")

    if not repo:
        return make_error("Missing repo")
    if not pr_number:
        return make_error("Missing pr_number")

    # Determine auth mode for this repo
    auth_mode = get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Block PR close in local SDLC mode (except during PR phase)
    session_phase = getattr(g, "session_phase", None)
    if session_mode == "local" and session_phase != "pr":
        audit_log(
            "pr_close_blocked_local_mode",
            "gh_pr_close",
            success=False,
            details={"repo": repo, "reason": "PR close blocked in local SDLC mode"},
        )
        return make_error(
            "Operation blocked in local SDLC mode. Close PRs manually when the pipeline completes.",
            status_code=403,
            details={"session_mode": "local"},
        )

    # Check Private Repo Mode policy (if enabled)
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = check_private_repo_access(
            operation="pr_close",
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            audit_log(
                "pr_close_denied_private_mode",
                "gh_pr_close",
                success=False,
                details={
                    "repo": repo,
                    "pr_number": pr_number,
                    "reason": priv_result.reason,
                    "visibility": priv_result.visibility,
                    "auth_mode": auth_mode,
                },
            )
            return make_error(
                priv_result.reason,
                status_code=403,
                details=priv_result.to_dict(),
            )

    # Check PR ownership (pass auth mode for relaxed policy in user mode)
    policy = get_policy_engine()
    policy_result = policy.check_pr_ownership(repo, pr_number, auth_mode=auth_mode)

    if not policy_result.allowed:
        audit_log(
            "pr_close_denied",
            "gh_pr_close",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
                "reason": policy_result.reason,
                "auth_mode": auth_mode,
            },
        )
        return make_error(
            f"Close denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    github = get_github_client(mode=auth_mode)
    args = ["pr", "close", str(pr_number), "--repo", repo]

    result = github.execute(args, timeout=30, mode=auth_mode)

    if result.success:
        audit_log(
            "pr_closed",
            "gh_pr_close",
            success=True,
            details={"repo": repo, "pr_number": pr_number, "auth_mode": auth_mode},
        )
        return make_success("PR closed", {"stdout": result.stdout, "auth_mode": auth_mode})
    else:
        return make_error(
            f"Failed to close PR: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )


@app.route("/api/v1/gh/execute", methods=["POST"])
@require_session_auth
def gh_execute() -> tuple[Response, int] | Response:
    """
    Execute a generic gh command.

    Request body:
        {
            "args": ["pr", "view", "123"],
            "cwd": "/path/to/repo"  # optional
        }

    Policy: Filtered - only read-only operations allowed by default.
    Blocked commands return 403.
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    args = data.get("args", [])
    cwd = data.get("cwd")
    # Repo passed from container - container can detect repo from worktree,
    # but gateway can't (different git structure)
    payload_repo = data.get("repo")

    if not args:
        return make_error("Missing args")

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Block gh commands in local SDLC mode.
    # During PR phase, only allow PR-scoped operations through.
    # All other gh commands remain blocked.
    session_phase = getattr(g, "session_phase", None)
    if session_mode == "local":
        allowed = False
        if session_phase == "pr":
            cmd_prefix = " ".join(args[:2]) if len(args) >= 2 else args[0] if args else ""
            allowed_pr_phase_prefixes = (
                "pr create",
                "pr edit",
                "pr view",
                "pr list",
                "pr comment",
                "pr close",
                "pr diff",
                "pr checks",
                "pr status",
            )
            allowed = any(cmd_prefix.startswith(p) for p in allowed_pr_phase_prefixes)

        if not allowed:
            audit_log(
                "gh_command_blocked_local_mode",
                "gh_execute",
                success=False,
                details={"command_args": args, "reason": "gh commands blocked in local SDLC mode"},
            )
            return make_error(
                "Operation blocked in local SDLC mode. Run gh commands manually when the pipeline completes.",
                status_code=403,
                details={"session_mode": "local"},
            )

    # Check for commands blocked entirely in private mode (too broad to filter by repo)
    if session_mode == "private" and args and args[0] in GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE:
        audit_log(
            "gh_command_blocked_private_mode",
            "gh_execute",
            success=False,
            details={
                "command": args[0],
                "reason": "Command blocked in private mode (too broad)",
            },
        )
        return make_error(
            f"Command 'gh {args[0]}' is not allowed in private mode",
            status_code=403,
            details={"command": args[0], "session_mode": "private"},
        )

    # Check for blocked commands
    cmd_str = " ".join(args[:2]) if len(args) >= 2 else args[0] if args else ""

    for blocked in BLOCKED_GH_COMMANDS:
        if cmd_str.startswith(blocked):
            audit_log(
                "blocked_command",
                "gh_execute",
                success=False,
                details={"command_args": args, "blocked_command": blocked},
            )
            return make_error(
                f"Command '{blocked}' is not allowed through the gateway. "
                f"Allowed read-only commands: {', '.join(sorted(READONLY_GH_COMMANDS))}",
                status_code=403,
                details={"blocked_command": blocked, "command_args": args},
            )

    # For 'gh api' commands, validate the path against allowlist
    if args and args[0] == "api" and len(args) > 1:
        # Parse arguments to find the actual API path (skip flags like -X, --method, etc.)
        api_path, method = parse_gh_api_args(args[1:])
        if api_path is None:
            audit_log(
                "api_path_missing",
                "gh_execute",
                success=False,
                details={"command_args": args},
            )
            return make_error("No API path provided in gh api command", status_code=400)

        # Resolve {owner} and {repo} template variables if present
        # The gh CLI resolves these from the current repo's git remote
        resolved_api_path = resolve_gh_api_template_variables(api_path, cwd)
        if resolved_api_path is None:
            audit_log(
                "api_path_template_resolution_failed",
                "gh_execute",
                success=False,
                details={
                    "api_path": api_path,
                    "cwd": cwd,
                    "reason": "Could not resolve template variables",
                },
            )
            return make_error(
                "Could not resolve {owner}/{repo} template variables. "
                "Ensure you are in a git repository with an 'origin' remote.",
                status_code=400,
            )

        # If template variables were resolved, update the args to use resolved path
        if resolved_api_path != api_path:
            # Find and replace the API path in args
            args = list(args)  # Make a mutable copy
            for i, arg in enumerate(args):
                if arg == api_path:
                    args[i] = resolved_api_path
                    break
            api_path = resolved_api_path

        path_valid, path_error = validate_gh_api_path(api_path, method)
        if not path_valid:
            audit_log(
                "api_path_blocked",
                "gh_execute",
                success=False,
                details={"api_path": api_path, "method": method, "reason": path_error},
            )
            return make_error(path_error, status_code=403)

    # Extract repo using comprehensive extractor (handles --repo, gh repo *, gh api paths)
    repo = extract_repo_from_gh_command(args)

    # Fall back to payload_repo if command doesn't contain repo
    if not repo and payload_repo:
        repo = payload_repo
        # Inject --repo into args so gh command uses it
        # NOTE: Don't inject for commands that don't support --repo flag:
        # - 'gh repo' commands - they take repo as positional arg
        # - 'gh auth' commands - global commands, no repo context
        # - 'gh config' commands - global commands, no repo context
        # - 'gh api' commands - repo is in the API path, not a flag
        commands_without_repo_flag = {"repo", "auth", "config", "api"}
        if args and args[0] not in commands_without_repo_flag:
            args = ["--repo", payload_repo] + list(args)

    # Determine auth mode (default to bot if repo not specified)
    auth_mode = get_auth_mode(repo) if repo else "bot"

    # Check Private Repo Mode policy (if enabled and repo is known)
    if repo:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            priv_result = check_private_repo_access(
                operation="gh_execute",
                owner=repo_info.owner,
                repo=repo_info.repo,
                for_write=False,  # Assume read for generic gh execute
                session_mode=session_mode,
            )
            if not priv_result.allowed:
                audit_log(
                    "gh_execute_denied_private_mode",
                    "gh_execute",
                    success=False,
                    details={
                        "repo": repo,
                        "command_args": args[:3] if len(args) > 3 else args,
                        "reason": priv_result.reason,
                        "visibility": priv_result.visibility,
                        "auth_mode": auth_mode,
                    },
                )
                return make_error(
                    priv_result.reason,
                    status_code=403,
                    details=priv_result.to_dict(),
                )

    # Use reviewer token for PR reviews when available. This allows the
    # reviewer bot (a separate GitHub App) to post approve/request-changes
    # on PRs authored by the main bot — something the bot can't do on its own PRs.
    # This applies to both bot and user modes since the reviewer token is a
    # separate identity specifically for reviews.
    # Note: args may have "--repo owner/repo" prepended, so we check if "pr" and "review"
    # appear in sequence anywhere in the args (not just at positions 0 and 1).
    def is_pr_review_command(cmd_args: list[str]) -> bool:
        for i in range(len(cmd_args) - 1):
            if cmd_args[i] == "pr" and cmd_args[i + 1] == "review":
                return True
        return False

    if is_pr_review_command(args) and auth_mode in ("bot", "user"):
        try:
            from token_refresher import is_reviewer_token_available

            if is_reviewer_token_available():
                auth_mode = "reviewer"
                logger.info("Using reviewer token for pr review command")
            else:
                logger.debug(
                    "Reviewer token not available, using %s token for pr review", auth_mode
                )
        except ImportError:
            pass

    # Execute the command
    github = get_github_client(mode=auth_mode)
    result = github.execute(args, timeout=60, cwd=cwd, mode=auth_mode)

    if result.success:
        response_data = result.to_dict()
        response_data["auth_mode"] = auth_mode
        return make_success("Command executed", response_data)
    else:
        return make_error(
            f"Command failed: {result.stderr}",
            status_code=500,
            details=result.to_dict(),
        )


# =============================================================================
# Worktree Lifecycle Endpoints
# =============================================================================

# Global WorktreeManager instance
_worktree_manager: WorktreeManager | None = None


def get_worktree_manager() -> WorktreeManager:
    """Get or create the global WorktreeManager instance."""
    global _worktree_manager
    if _worktree_manager is None:
        _worktree_manager = WorktreeManager()
    return _worktree_manager


def map_container_path_to_worktree(
    repo_path: str, container_id: str | None, operation: str = "git"
) -> str:
    """
    Map a container's repo path to the corresponding worktree path.

    Container sends paths like /home/egg/repos/{repo} or subdirectories like
    /home/egg/repos/{repo}/src/foo, but the gateway needs to run git in the
    worktree at /home/egg/.egg-worktrees/{container_id}/{repo}[/subdir].

    Args:
        repo_path: The path sent by the container (e.g., /home/egg/repos/myrepo/src)
        container_id: The container's unique identifier
        operation: Name of the operation for logging purposes

    Returns:
        The worktree path if mapping succeeds, otherwise the original repo_path.
    """
    if not container_id:
        return repo_path

    # Extract repo name and any subdirectory from paths like:
    # /home/egg/repos/myrepo -> repo_name=myrepo, subdir=""
    # /home/egg/repos/myrepo/src/foo -> repo_name=myrepo, subdir="src/foo"
    repos_prefix = "/home/egg/repos/"
    if not repo_path.startswith(repos_prefix):
        return repo_path

    # Get the path relative to /home/egg/repos/
    relative_path = repo_path[len(repos_prefix) :].rstrip("/")
    if not relative_path:
        # Path is exactly /home/egg/repos/ - not a repo
        return repo_path

    # Split into repo name and subdirectory
    parts = relative_path.split("/", 1)
    repo_name = parts[0]
    subdir = parts[1] if len(parts) > 1 else ""

    if not repo_name:
        return repo_path

    manager = get_worktree_manager()
    try:
        worktree_path, _main_repo = manager.get_worktree_paths(container_id, repo_name)
        if worktree_path.exists():
            # Append subdirectory if present
            final_path = worktree_path / subdir if subdir else worktree_path
            logger.debug(
                f"Mapped container path to worktree for {operation}",
                container_path=repo_path,
                worktree_path=str(final_path),
                container_id=container_id,
            )
            return str(final_path)
    except ValueError as e:
        logger.debug(
            f"Failed to map container path to worktree for {operation}",
            error=str(e),
            container_id=container_id,
            repo_name=repo_name,
        )

    return repo_path


@app.route("/api/v1/worktree/create", methods=["POST"])
@require_launcher_auth
def worktree_create() -> tuple[Response, int] | Response:
    """
    Create worktrees for a container.

    Called by the egg launcher before starting a container. Creates isolated
    worktrees for each repository the container needs access to.

    Request body:
        {
            "container_id": "egg-xxx-yyy",
            "repos": ["owner/repo1", "owner/repo2"],
            "uid": 1000,  // optional, defaults to 1000 (egg user)
            "gid": 1000   // optional, defaults to 1000 (egg group)
        }

    Returns:
        {
            "success": true,
            "message": "Worktrees created",
            "data": {
                "worktrees": {
                    "repo1": "/home/user/.egg-worktrees/egg-xxx-yyy/repo1",
                    "repo2": "/home/user/.egg-worktrees/egg-xxx-yyy/repo2"
                }
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    repos = data.get("repos", [])
    base_branch = data.get("base_branch", "HEAD")
    # UID/GID for worktree ownership (default: 1000 for egg user)
    uid = data.get("uid")
    gid = data.get("gid")

    if not container_id:
        return make_error("Missing container_id")
    if not repos:
        return make_error("Missing repos list")

    # Validate uid/gid if provided
    if uid is not None and (not isinstance(uid, int) or uid < 0):
        return make_error("Invalid uid: must be a non-negative integer")
    if gid is not None and (not isinstance(gid, int) or gid < 0):
        return make_error("Invalid gid: must be a non-negative integer")

    manager = get_worktree_manager()
    worktrees = {}
    errors = []

    for repo in repos:
        # Extract repo name from owner/repo format
        if "/" in repo:
            repo_name = repo.split("/")[-1]
        else:
            repo_name = repo

        try:
            info = manager.create_worktree(
                repo_name=repo_name,
                container_id=container_id,
                base_branch=base_branch,
                uid=uid,
                gid=gid,
            )
            # Translate container path to host path for egg launcher mount sources
            worktrees[repo_name] = translate_to_host_path(str(info.worktree_path))
        except ValueError as e:
            errors.append(f"{repo_name}: {e}")
        except RuntimeError as e:
            errors.append(f"{repo_name}: {e}")
        except Exception as e:
            errors.append(f"{repo_name}: unexpected error - {e}")

    if errors and not worktrees:
        return make_error(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": errors},
        )

    audit_log(
        "worktrees_created",
        "worktree_create",
        success=True,
        details={
            "container_id": container_id,
            "repos": list(worktrees.keys()),
            "errors": errors,
        },
    )

    return make_success(
        "Worktrees created",
        {
            "worktrees": worktrees,
            "errors": errors if errors else None,
        },
    )


@app.route("/api/v1/worktree/delete", methods=["POST"])
@require_launcher_auth
def worktree_delete() -> tuple[Response, int] | Response:
    """
    Delete worktrees for a container.

    Called by the egg launcher when a container exits. Removes the worktrees
    and associated branches.

    Request body:
        {
            "container_id": "egg-xxx-yyy",
            "force": false  # optional, force remove even with uncommitted changes
        }

    Returns:
        {
            "success": true,
            "message": "Worktrees deleted",
            "data": {
                "deleted": ["repo1", "repo2"],
                "warnings": ["repo1: had uncommitted changes"]
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    force = data.get("force", False)

    if not container_id:
        return make_error("Missing container_id")

    manager = get_worktree_manager()

    # Get list of worktrees for this container
    worktree_dir = manager.worktree_base / container_id
    if not worktree_dir.exists():
        return make_success("No worktrees to delete", {"deleted": []})

    deleted = []
    errors = []
    warnings = []

    # Iterate through worktree directories
    for repo_dir in list(worktree_dir.iterdir()):
        if not repo_dir.is_dir():
            continue

        repo_name = repo_dir.name

        try:
            result = manager.remove_worktree(
                container_id=container_id,
                repo_name=repo_name,
                force=force,
            )

            if result.success:
                deleted.append(repo_name)
                if result.warning:
                    warnings.append(f"{repo_name}: {result.warning}")
            elif result.uncommitted_changes and not force:
                errors.append(f"{repo_name}: has uncommitted changes (use force=true)")
            elif result.error:
                errors.append(f"{repo_name}: {result.error}")
            else:
                errors.append(f"{repo_name}: removal failed")
        except Exception as e:
            errors.append(f"{repo_name}: unexpected error - {e}")

    audit_log(
        "worktrees_deleted",
        "worktree_delete",
        success=True,
        details={
            "container_id": container_id,
            "deleted": deleted,
            "errors": errors,
        },
    )

    return make_success(
        "Worktrees deleted",
        {
            "deleted": deleted,
            "errors": errors if errors else None,
            "warnings": warnings if warnings else None,
        },
    )


@app.route("/api/v1/worktree/list", methods=["GET"])
@require_launcher_auth
def worktree_list() -> tuple[Response, int] | Response:
    """
    List all active worktrees.

    Returns information about all worktrees managed by the gateway.
    """
    manager = get_worktree_manager()
    worktrees = manager.list_worktrees()
    return make_success("Worktrees listed", {"worktrees": worktrees})


# =============================================================================
# Session Management Endpoints (Per-Container Repository Mode)
# =============================================================================


@app.route("/api/v1/sessions/create", methods=["POST"])
@require_launcher_auth
def session_create() -> tuple[Response, int] | Response:
    """
    Create a session with atomic visibility query, filtering, worktree creation.

    This is the primary endpoint for session registration. It performs:
    1. Query repository visibility for all requested repos
    2. Filter repos based on mode (private keeps private/internal, public keeps public)
    3. Create worktrees for filtered repos
    4. Register session with the filtered repo list

    This atomic operation prevents TOCTOU race conditions between visibility
    check and session registration.

    Request body:
        {
            "container_id": "egg-xxx",
            "container_ip": "172.18.0.3",
            "mode": "private"|"public",
            "repos": ["owner/repo1", "owner/repo2"],
            "uid": 1000,
            "gid": 1000
        }

    Response:
        {
            "success": true,
            "session_token": "tok_...",
            "filtered_repos": ["owner/repo1"],
            "worktrees": {
                "repo1": "/path/to/worktree"
            }
        }

    Auth: Bearer {launcher_secret}
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    container_ip = data.get("container_ip")
    mode = data.get("mode")
    repos = data.get("repos", [])
    uid = data.get("uid")
    gid = data.get("gid")
    phase = data.get("phase")  # Optional SDLC pipeline phase
    pipeline_id = data.get("pipeline_id")  # Optional pipeline run ID
    issue_number = data.get("issue_number")  # Optional GitHub issue number
    pr_number = data.get("pr_number")  # Optional GitHub PR number
    agent_role = data.get("agent_role")  # Optional agent role
    claude_code_version = data.get("claude_code_version")  # Optional Claude Code version
    branch = data.get("branch")  # Optional git branch for non-pushing sessions
    complexity_tier = data.get("complexity_tier")  # Optional complexity tier for Tier 3 dispatch

    # Validate required fields
    if not container_id:
        return make_error("Missing container_id")
    if not container_ip:
        return make_error("Missing container_ip")
    if mode not in ("private", "public", "local"):
        return make_error("Invalid mode: must be 'private', 'public', or 'local'")
    if not repos:
        return make_error("Missing repos list")

    # Validate uid/gid if provided
    if uid is not None and (not isinstance(uid, int) or uid < 0):
        return make_error("Invalid uid: must be a non-negative integer")
    if gid is not None and (not isinstance(gid, int) or gid < 0):
        return make_error("Invalid gid: must be a non-negative integer")

    # Validate phase if provided
    if phase is not None and phase not in VALID_PIPELINE_PHASES:
        return make_error(
            f"Invalid phase: {phase}. Must be one of: {', '.join(sorted(VALID_PIPELINE_PHASES))}"
        )

    # Validate pipeline_id if provided
    if pipeline_id is not None:
        if not isinstance(pipeline_id, str):
            return make_error("Invalid pipeline_id: must be a string")
        if not pipeline_id:
            return make_error("Invalid pipeline_id: must be a non-empty string")
        if len(pipeline_id) > 256:
            return make_error("Invalid pipeline_id: must be 256 characters or fewer")

    # Validate issue_number if provided
    if issue_number is not None and (not isinstance(issue_number, int) or issue_number < 1):
        return make_error("Invalid issue_number: must be a positive integer")

    # Validate pr_number if provided
    if pr_number is not None and (not isinstance(pr_number, int) or pr_number < 1):
        return make_error("Invalid pr_number: must be a positive integer")

    # Validate agent_role if provided
    if agent_role is not None:
        if not isinstance(agent_role, str):
            return make_error("Invalid agent_role: must be a string")
        if len(agent_role) > 64:
            return make_error("Invalid agent_role: must be 64 characters or fewer")

    # Validate claude_code_version if provided
    if claude_code_version is not None:
        if not isinstance(claude_code_version, str):
            return make_error("Invalid claude_code_version: must be a string")
        if len(claude_code_version) > 64:
            return make_error("Invalid claude_code_version: must be 64 characters or fewer")

    # Validate branch if provided
    if branch is not None:
        if not isinstance(branch, str):
            return make_error("Invalid branch: must be a string")
        if len(branch) > 256:
            return make_error("Invalid branch: must be 256 characters or fewer")

    # Step 1: Query visibility for all repos
    repo_visibilities = {}
    for repo in repos:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            visibility = get_repo_visibility(repo_info.owner, repo_info.repo)
            repo_visibilities[repo] = visibility
        else:
            # Can't parse repo - skip it
            logger.warning(
                "Could not parse repository for visibility check",
                repo=repo,
                container_id=container_id,
            )

    # Step 2: Filter repos based on mode
    # private mode: keep private and internal repos
    # public mode: keep only public repos
    filtered_repos = []
    for repo, visibility in repo_visibilities.items():
        if visibility is None:
            # Unknown visibility - fail closed, don't include
            logger.warning(
                "Unknown visibility for repo, excluding",
                repo=repo,
                mode=mode,
                container_id=container_id,
            )
            continue

        if mode == "private":
            # Private mode: include private and internal repos only
            if visibility in ("private", "internal"):
                filtered_repos.append(repo)
            else:
                logger.debug(
                    "Excluding public repo in private mode",
                    repo=repo,
                    visibility=visibility,
                    container_id=container_id,
                )
        # Public mode: include only public repos
        elif visibility == "public":
            filtered_repos.append(repo)
        else:
            logger.debug(
                "Excluding non-public repo in public mode",
                repo=repo,
                visibility=visibility,
                container_id=container_id,
            )

    # Step 3: Create worktrees for filtered repos
    manager = get_worktree_manager()
    worktrees = {}
    worktree_errors = []
    first_worktree_path: str | None = None  # Gateway-side path for checkpoint context
    first_repo: str | None = None  # First filtered repo in "owner/repo" format
    worktree_branch: str | None = None  # Worktree branch name for branch lock

    for repo in filtered_repos:
        # Extract repo name from owner/repo format
        if "/" in repo:
            repo_name = repo.split("/")[-1]
        else:
            repo_name = repo

        try:
            # For pipeline sessions, use the remote default branch (e.g., origin/main)
            # instead of HEAD.  HEAD may point to a feature branch in the main repo,
            # which would pollute the worktree with commits outside the current phase's
            # allowed scope and cause push rejections.  See #860.
            if pipeline_id:
                worktree_base_branch = manager.resolve_default_branch(repo_name)
            else:
                worktree_base_branch = "HEAD"

            info = manager.create_worktree(
                repo_name=repo_name,
                container_id=container_id,
                base_branch=worktree_base_branch,
                uid=uid,
                gid=gid,
            )
            # Capture the first worktree's gateway-side path for checkpoint context
            if first_worktree_path is None:
                first_worktree_path = str(info.worktree_path)
                first_repo = repo
                worktree_branch = info.branch
            # Translate container path to host path for egg launcher mount sources
            worktrees[repo_name] = translate_to_host_path(str(info.worktree_path))
        except ValueError as e:
            worktree_errors.append(f"{repo_name}: {e}")
        except RuntimeError as e:
            worktree_errors.append(f"{repo_name}: {e}")
        except Exception as e:
            worktree_errors.append(f"{repo_name}: unexpected error - {e}")

    # If no worktrees could be created, fail
    if not worktrees and filtered_repos:
        return make_error(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": worktree_errors},
        )

    # Step 4: Register session
    session_manager = get_session_manager()
    token, _session = session_manager.register_session(
        container_id=container_id,
        container_ip=container_ip,
        mode=mode,
        phase=phase,
        pipeline_id=pipeline_id,
        issue_number=issue_number,
        pr_number=pr_number,
        agent_role=agent_role,
        claude_code_version=claude_code_version,
        branch=branch,
        complexity_tier=complexity_tier,
    )

    # Pre-populate checkpoint context so non-pushing sessions (reviewers,
    # architects, etc.) have a repo_path and checkpoint_repo for session-end
    # checkpoint storage. These fields are also set on git push, but pipeline
    # agents that never push would otherwise have None values.
    if first_worktree_path is not None:
        _session.last_repo_path = first_worktree_path
    if first_repo is not None:
        _session.checkpoint_repo = get_checkpoint_repo(first_repo)

    # Lock pipeline sessions to their assigned worktree branch
    if worktree_branch and pipeline_id:
        _session.assigned_branch = worktree_branch

    audit_log(
        "session_created",
        "session_create",
        success=True,
        details={
            "container_id": container_id,
            "container_ip": container_ip,
            "mode": mode,
            "phase": phase,
            "pipeline_id": pipeline_id,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "agent_role": agent_role,
            "filtered_repos": filtered_repos,
            "worktree_count": len(worktrees),
            "worktree_errors": worktree_errors if worktree_errors else None,
        },
    )

    return make_success(
        "Session created",
        {
            "session_token": token,
            "filtered_repos": filtered_repos,
            "worktrees": worktrees,
            "errors": worktree_errors if worktree_errors else None,
        },
    )


def _cleanup_container_worktrees(
    container_id: str,
) -> tuple[list[str], list[str]]:
    """Clean up all worktrees for a container.

    Returns:
        Tuple of (deleted_repo_names, errors).
    """
    manager = get_worktree_manager()
    worktree_dir = manager.worktree_base / container_id
    deleted_worktrees: list[str] = []
    errors: list[str] = []
    if worktree_dir.exists():
        for repo_dir in list(worktree_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            repo_name = repo_dir.name
            try:
                result = manager.remove_worktree(
                    container_id=container_id,
                    repo_name=repo_name,
                    force=True,
                )
                if result.success:
                    deleted_worktrees.append(repo_name)
                elif result.error:
                    errors.append(f"{repo_name}: {result.error}")
                else:
                    errors.append(f"{repo_name}: removal failed")
            except Exception as e:
                errors.append(f"{repo_name}: unexpected error - {e}")
    return deleted_worktrees, errors


@app.route("/api/v1/sessions/<session_token>", methods=["DELETE"])
@require_launcher_auth
def session_delete(session_token: str) -> tuple[Response, int] | Response:
    """
    Delete a session.

    Only the launcher (with launcher_secret) can delete sessions.
    Containers CANNOT delete sessions.

    Also cleans up associated worktrees.

    Args:
        session_token: The session token to delete

    Auth: Bearer {launcher_secret}
    """
    session_manager = get_session_manager()

    # Get session info for worktree cleanup
    session = session_manager.get_session(session_token)
    container_id = session.container_id if session else None

    # Delete the session
    deleted = session_manager.delete_session(session_token)

    if not deleted:
        return make_error("Session not found", status_code=404)

    # Clean up worktrees for this container
    deleted_worktrees, worktree_errors = (
        _cleanup_container_worktrees(container_id) if container_id else ([], [])
    )

    audit_log(
        "session_deleted",
        "session_delete",
        success=True,
        details={
            "container_id": container_id,
            "worktrees_deleted": deleted_worktrees,
            "errors": worktree_errors if worktree_errors else None,
        },
    )

    return make_success("Session deleted")


@app.route("/api/v1/sessions/by-container/<container_id>", methods=["DELETE"])
@require_launcher_auth
def session_delete_by_container(container_id: str) -> tuple[Response, int] | Response:
    """
    Delete a session by container ID.

    Used by the orchestrator for cleanup when the session token is not available.

    Args:
        container_id: The container ID whose session to delete

    Auth: Bearer {launcher_secret}
    """
    session_manager = get_session_manager()
    deleted = session_manager.delete_session_by_container(container_id)

    if not deleted:
        return make_error("Session not found for container", status_code=404)

    # Clean up worktrees for this container
    deleted_worktrees, worktree_errors = _cleanup_container_worktrees(container_id)

    audit_log(
        "session_deleted",
        "session_delete_by_container",
        success=True,
        details={
            "container_id": container_id,
            "worktrees_deleted": deleted_worktrees,
            "errors": worktree_errors if worktree_errors else None,
        },
    )

    return make_success("Session deleted")


@app.route("/api/v1/sessions/<session_token>", methods=["GET"])
@require_launcher_auth
def session_get(session_token: str) -> tuple[Response, int] | Response:
    """
    Get session information and validate if it exists.

    Args:
        session_token: The session token

    Auth: Bearer {launcher_secret}

    Response:
        {
            "valid": true,
            "mode": "private"|"public",
            "container_id": "...",
            "expires_at": "...",
        }
    """
    session_manager = get_session_manager()
    result = session_manager.validate_session(session_token)

    if not result.valid:
        return jsonify({"valid": False, "error": result.error or "Session not found"}), 404

    return jsonify(
        {
            "valid": True,
            "mode": result.session.mode if result.session else None,
            "container_id": result.session.container_id if result.session else None,
            "expires_at": result.session.expires_at.isoformat() if result.session else None,
        }
    )


@app.route("/api/v1/sessions/<session_token>/heartbeat", methods=["POST"])
@require_launcher_auth
def session_heartbeat(session_token: str) -> tuple[Response, int] | Response:
    """
    Explicit session heartbeat to extend TTL.

    Note: Heartbeats are also triggered implicitly on any successful
    session-authenticated request. This endpoint exists for edge cases
    where long-running operations need TTL extension without git/gh activity.

    Args:
        session_token: The session token

    Auth: Bearer {session_token}

    Rate limit: 100 per hour per session
    """
    # Validate the session
    result = validate_session_for_request(session_token, request.remote_addr)
    if not result.valid:
        # Record failed lookup for rate limiting
        record_failed_lookup(request.remote_addr or "")
        return make_error(result.error or "Invalid session", status_code=401)

    # Check heartbeat rate limit (100 per hour per session)
    if result.session:
        rate_limit = check_heartbeat_rate_limit(result.session.session_token_hash)
        if not rate_limit.allowed:
            return make_error(
                f"Heartbeat rate limit exceeded. Retry after {rate_limit.retry_after_seconds}s",
                status_code=429,
            )

    # Session validation already extends TTL, just return success
    return make_success(
        "Heartbeat recorded",
        {
            "expires_at": result.session.expires_at.isoformat() if result.session else None,
        },
    )


@app.route("/api/v1/sessions/<session_token>", methods=["PATCH"])
@require_launcher_auth
def session_update(session_token: str) -> tuple[Response, int] | Response:
    """
    Update session container binding (container_id and/or container_ip).

    Used by the orchestrator to bind a session to the real container
    after pre-registering with a placeholder ID before container creation.

    Request body:
        {
            "container_id": "abc123...",  # Optional
            "container_ip": "172.32.0.10"  # Optional
        }

    At least one of container_id or container_ip must be provided.

    Args:
        session_token: The session token to update

    Auth: Bearer {launcher_secret}
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    container_ip = data.get("container_ip")

    if not container_id and not container_ip:
        return make_error("Must provide container_id and/or container_ip")

    session_manager = get_session_manager()
    success = session_manager.update_session(
        session_token,
        container_id=container_id,
        container_ip=container_ip,
    )

    if not success:
        return make_error("Session not found or expired", status_code=404)

    audit_log(
        "session_container_updated",
        "session_update",
        success=True,
        details={
            "container_id": container_id,
            "container_ip": container_ip,
        },
    )

    return make_success(
        "Session updated",
        {
            "container_id": container_id,
            "container_ip": container_ip,
        },
    )


# Valid SDLC pipeline phases
VALID_PIPELINE_PHASES = frozenset({"refine", "plan", "implement", "pr"})


@app.route("/api/v1/sessions/<session_token>/phase", methods=["PATCH"])
@require_launcher_auth
def session_update_phase(session_token: str) -> tuple[Response, int] | Response:
    """
    Update the SDLC pipeline phase for a session.

    This endpoint allows the launcher/workflow to update the phase as
    the pipeline progresses. Phase restrictions are enforced by the
    gateway for operations like PR creation.

    Request body:
        {
            "phase": "refine"|"plan"|"implement"|"pr"
        }

    Args:
        session_token: The session token to update

    Auth: Bearer {launcher_secret}
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    phase = data.get("phase")
    if not phase:
        return make_error("Missing phase")

    if phase not in VALID_PIPELINE_PHASES:
        return make_error(
            f"Invalid phase: {phase}. Must be one of: {', '.join(sorted(VALID_PIPELINE_PHASES))}"
        )

    session_manager = get_session_manager()
    success = session_manager.update_phase(session_token, phase)

    if not success:
        return make_error("Session not found or expired", status_code=404)

    audit_log(
        "session_phase_updated",
        "session_update_phase",
        success=True,
        details={"phase": phase},
    )

    return make_success("Phase updated", {"phase": phase})


@app.route("/api/v1/repos/visibility", methods=["GET"])
@require_launcher_auth
def repos_visibility() -> tuple[Response, int] | Response:
    """
    Query visibility for multiple repositories.

    Used by launcher for informational queries. For atomic session+worktree
    creation, use POST /api/v1/sessions/create instead.

    Query params:
        repos: Comma-separated list of owner/repo strings

    Response:
        {
            "visibilities": {
                "owner/repo1": "public",
                "owner/repo2": "private",
                "owner/repo3": "internal"
            }
        }

    Auth: Bearer {launcher_secret}
    """
    repos_param = request.args.get("repos", "")
    if not repos_param:
        return make_error("Missing repos query parameter")

    repos = [r.strip() for r in repos_param.split(",") if r.strip()]
    if not repos:
        return make_error("No valid repos provided")

    visibilities = {}
    for repo in repos:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            visibility = get_repo_visibility(repo_info.owner, repo_info.repo)
            visibilities[repo] = visibility
        else:
            visibilities[repo] = None

    return make_success("Visibility queried", {"visibilities": visibilities})


@app.route("/api/v1/sessions", methods=["GET"])
@require_launcher_auth
def sessions_list() -> tuple[Response, int] | Response:
    """
    List all active sessions.

    Auth: Bearer {launcher_secret}
    """
    session_manager = get_session_manager()
    sessions = session_manager.list_sessions()
    return make_success("Sessions listed", {"sessions": sessions})


# =============================================================================
# Anthropic API Proxy Endpoints
# =============================================================================

# Singleton httpx client with connection pooling for Anthropic API
_anthropic_client: httpx.Client | None = None


def get_anthropic_client() -> httpx.Client:
    """Get or create the singleton Anthropic API client."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = httpx.Client(
            base_url="https://api.anthropic.com",
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _anthropic_client


# Headers to block - forward everything else for maximum compatibility
ANTHROPIC_BLOCKED_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "authorization",
    "x-api-key",
    "connection",
}


def _get_forwarded_headers(request_headers: Any) -> dict[str, str]:
    """Forward all headers except blocked ones (blocklist approach)."""
    return {k: v for k, v in request_headers if k.lower() not in ANTHROPIC_BLOCKED_HEADERS}


def _filter_response_headers(headers: Any) -> dict[str, str]:
    """Filter response headers for passthrough."""
    # Preserve important headers like x-request-id for debugging
    skip = {"content-encoding", "transfer-encoding", "connection"}
    return {k: v for k, v in headers.items() if k.lower() not in skip}


def _inject_anthropic_credentials(
    headers: dict[str, str],
) -> tuple[dict[str, str], tuple[Any, int] | None]:
    """
    Inject Anthropic credentials into headers.

    Returns:
        (headers, None) on success
        (headers, error_response_tuple) on failure - caller should return this
    """
    credentials_manager = get_credentials_manager()
    cred = credentials_manager.get_credential()

    if cred:
        # Credential includes header_name (x-api-key or Authorization)
        # and header_value (raw key or "Bearer <token>")
        headers[cred.header_name] = cred.header_value
        return headers, None

    # No gateway-managed credentials - check if client sent auth
    # This allows OAuth mode where Claude Code manages its own tokens
    client_auth = headers.get("Authorization")
    client_api_key = headers.get("x-api-key")
    if client_auth or client_api_key:
        return headers, None

    logger.warning(
        "No Anthropic credentials available for proxy request",
        has_gateway_cred=False,
        has_client_auth=bool(client_auth),
        has_client_api_key=bool(client_api_key),
    )
    return headers, (
        jsonify(
            {
                "error": {
                    "type": "authentication_error",
                    "message": "No Anthropic credentials available",
                }
            }
        ),
        401,
    )


# Tools blocked in private mode to prevent data exfiltration
# These tools route through Anthropic's infrastructure, bypassing container network controls
# See PR #686 security findings and PR #702 analysis
BLOCKED_TOOLS_PRIVATE_MODE = {"web_search", "WebSearch", "web_fetch", "WebFetch"}

# Maximum size (in characters) for preserving raw tool input when JSON parsing fails.
# Used for debugging incomplete streaming responses without bloating the buffer.
RAW_INPUT_TRUNCATE_SIZE = 1000


def _filter_blocked_tools(request_body: bytes, session_mode: str | None) -> bytes:
    """
    Remove blocked tools from API request when in private mode.

    In private mode, WebSearch and WebFetch bypass container network controls
    because they're processed by Anthropic's infrastructure. This creates a
    data exfiltration risk where a compromised agent could encode sensitive
    data in search queries.

    By filtering these tools at the gateway, we enforce the restriction at
    the infrastructure level where the container cannot bypass it.

    Args:
        request_body: Raw JSON request body
        session_mode: The session's mode ("private" or "public"), or None

    Returns:
        Modified request body with blocked tools removed (if in private mode),
        or original body unchanged (if in public mode or on parse error)
    """
    if session_mode != "private":
        return request_body

    try:
        body = json.loads(request_body)
        if "tools" not in body:
            return request_body

        original_tools = body["tools"]
        filtered_tools = [
            t for t in original_tools if t.get("name") not in BLOCKED_TOOLS_PRIVATE_MODE
        ]

        removed_count = len(original_tools) - len(filtered_tools)
        if removed_count > 0:
            removed_names = [
                t.get("name") for t in original_tools if t.get("name") in BLOCKED_TOOLS_PRIVATE_MODE
            ]
            logger.info(
                "Filtered blocked tools in private mode",
                removed_count=removed_count,
                removed_tools=removed_names,
            )
            body["tools"] = filtered_tools
            return json.dumps(body).encode()

    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Failed to parse request body for tool filtering", error=str(e))

    return request_body


def _is_streaming_request(request_body: bytes) -> bool:
    """
    Check if request body indicates streaming mode.

    Parses JSON properly to avoid false positives from byte string matching.
    """
    try:
        body_json = json.loads(request_body)
        return body_json.get("stream", False) is True
    except (json.JSONDecodeError, TypeError):
        return False


def _capture_non_streaming_response(
    container_id: str,
    request_json: dict[str, Any],
    response_body: bytes,
    start_time: float,
    status_code: int = 200,
) -> None:
    """
    Capture a non-streaming API response to the transcript buffer.

    Args:
        container_id: Container ID for buffer lookup
        request_json: Parsed request body
        response_body: Raw response bytes
        start_time: Request start time for duration calculation
        status_code: HTTP status code of the response
    """
    duration_ms = (time.time() - start_time) * 1000

    try:
        response_json = json.loads(response_body)
    except (json.JSONDecodeError, TypeError):
        # For non-JSON responses (error pages, malformed responses), capture basic info
        # This is important for debugging failed API calls
        if status_code >= 400:
            response_json = {
                "error": {
                    "type": "api_error",
                    "status_code": status_code,
                    "message": response_body.decode("utf-8", errors="replace")[:500],
                }
            }
        else:
            logger.debug("Could not parse response body for transcript capture")
            return

    try:
        buffer = get_transcript_buffer(container_id)
        # Check if this is an error response
        error_info = response_json.get("error")
        if error_info:
            # Capture error as content block for visibility
            buffer.write_api_turn(
                request_body=request_json,
                response_content=[{"type": "error", "error": error_info}],
                response_usage=response_json.get("usage"),
                response_model=response_json.get("model"),
                stop_reason="error",
                duration_ms=duration_ms,
                streaming=False,
            )
        else:
            buffer.write_api_turn(
                request_body=request_json,
                response_content=response_json.get("content"),
                response_usage=response_json.get("usage"),
                response_model=response_json.get("model"),
                stop_reason=response_json.get("stop_reason"),
                duration_ms=duration_ms,
                streaming=False,
            )
    except Exception as e:
        logger.warning(
            "Failed to capture non-streaming response to transcript buffer",
            container_id=container_id,
            error=str(e),
        )


def _capture_streaming_response(
    container_id: str,
    request_json: dict[str, Any],
    chunks: list[bytes],
    start_time: float,
) -> None:
    """
    Capture a streaming API response to the transcript buffer.

    Reassembles the SSE chunks to extract the final message content and usage.

    Args:
        container_id: Container ID for buffer lookup
        request_json: Parsed request body
        chunks: List of SSE response chunks
        start_time: Request start time for duration calculation
    """
    duration_ms = (time.time() - start_time) * 1000

    # Reassemble SSE response to get content and usage
    try:
        response_content, response_usage, response_model, stop_reason = _parse_sse_response(chunks)
    except Exception as e:
        logger.debug(
            "Failed to parse SSE response for transcript capture",
            container_id=container_id,
            error=str(e),
        )
        return

    try:
        buffer = get_transcript_buffer(container_id)
        buffer.write_api_turn(
            request_body=request_json,
            response_content=response_content,
            response_usage=response_usage,
            response_model=response_model,
            stop_reason=stop_reason,
            duration_ms=duration_ms,
            streaming=True,
        )
    except Exception as e:
        logger.warning(
            "Failed to capture streaming response to transcript buffer",
            container_id=container_id,
            error=str(e),
        )


def _parse_sse_response(
    chunks: list[bytes],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, str | None, str | None]:
    """
    Parse SSE response chunks to extract message content and usage.

    Returns:
        Tuple of (content, usage, model, stop_reason)
    """
    # Combine chunks and parse SSE events
    full_response = b"".join(chunks).decode("utf-8", errors="replace")

    content_blocks: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    model: str | None = None
    stop_reason: str | None = None

    # Track content blocks being built (keyed by index)
    content_by_index: dict[int, dict[str, Any]] = {}

    for line in full_response.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue

        data_str = line[6:]  # Remove "data: " prefix
        if data_str == "[DONE]":
            continue

        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        # Extract model and input_tokens from message_start
        if event_type == "message_start":
            message = event.get("message", {})
            model = message.get("model")
            # Capture input_tokens from message_start (message_delta only has output_tokens)
            message_usage = message.get("usage", {})
            if message_usage:
                if usage is None:
                    usage = {}
                # input_tokens comes from message_start, not message_delta
                if "input_tokens" in message_usage:
                    usage["input_tokens"] = message_usage["input_tokens"]
                if "cache_read_input_tokens" in message_usage:
                    usage["cache_read_input_tokens"] = message_usage["cache_read_input_tokens"]
                if "cache_creation_input_tokens" in message_usage:
                    usage["cache_creation_input_tokens"] = message_usage[
                        "cache_creation_input_tokens"
                    ]

        # Handle error events from streaming API
        elif event_type == "error":
            error_info = event.get("error", {})
            # Add error as a content block so it's captured in transcript
            content_blocks.append(
                {
                    "type": "error",
                    "error": error_info,
                }
            )
            stop_reason = "error"

        # Track content block starts
        elif event_type == "content_block_start":
            index = event.get("index", 0)
            content_block = event.get("content_block", {})
            content_by_index[index] = content_block.copy()

        # Accumulate content block deltas
        elif event_type == "content_block_delta":
            index = event.get("index", 0)
            delta = event.get("delta", {})
            delta_type = delta.get("type")

            if index not in content_by_index:
                content_by_index[index] = {
                    "type": delta_type.replace("_delta", "") if delta_type else "unknown"
                }

            block = content_by_index[index]

            if delta_type == "text_delta":
                text = delta.get("text", "")
                block["text"] = block.get("text", "") + text
            elif delta_type == "input_json_delta":
                # For tool_use blocks
                partial_json = delta.get("partial_json", "")
                block["partial_input"] = block.get("partial_input", "") + partial_json

        # Extract output_tokens and stop_reason from message_delta
        elif event_type == "message_delta":
            delta = event.get("delta", {})
            stop_reason = delta.get("stop_reason")
            event_usage = event.get("usage")
            if event_usage:
                if usage is None:
                    usage = {}
                # message_delta contains output_tokens
                usage.update(event_usage)

    # Build final content blocks
    for index in sorted(content_by_index.keys()):
        block = content_by_index[index]
        # For tool_use blocks, parse the accumulated JSON
        if block.get("type") == "tool_use" and "partial_input" in block:
            partial_input = block.pop("partial_input")
            try:
                block["input"] = json.loads(partial_input)
            except json.JSONDecodeError:
                # Log warning but still include the block with parse failure noted.
                # Preserve the raw partial_input for debugging (truncated to avoid bloat).
                logger.debug(
                    "Failed to parse tool_use input JSON",
                    tool_id=block.get("id"),
                )
                block["input"] = {}
                block["input_parse_error"] = True
                # Include truncated raw input for debugging incomplete streaming responses
                block["raw_partial_input"] = (
                    partial_input[:RAW_INPUT_TRUNCATE_SIZE]
                    if len(partial_input) > RAW_INPUT_TRUNCATE_SIZE
                    else partial_input
                )
        content_blocks.append(block)

    return content_blocks or None, usage, model, stop_reason


@app.route("/v1/messages", methods=["POST"])
def proxy_anthropic_messages() -> tuple[Response, int] | Response:
    """
    Proxy messages API with credential injection, streaming support, and transcript capture.

    This endpoint allows Claude Code to use ANTHROPIC_BASE_URL to route
    API traffic through the gateway for credential injection.

    Uses IP-based session lookup for mode detection (Claude Code doesn't send session tokens).
    API request/response pairs are captured to a per-session buffer for checkpoint creation.
    """
    start_time = time.time()

    # Build headers with injected auth
    headers = _get_forwarded_headers(request.headers)
    headers, error = _inject_anthropic_credentials(headers)
    if error:
        return error

    request_body = request.get_data()

    # Look up session by IP to determine mode (Claude Code doesn't send session tokens)
    session_manager = get_session_manager()
    session = session_manager.get_session_by_ip(request.remote_addr or "")
    session_mode = session.mode if session else None
    container_id = session.container_id if session else None
    request_body = _filter_blocked_tools(
        request_body, session_mode
    )  # Remove web tools in private mode
    is_streaming = _is_streaming_request(request_body)

    # Parse request body for transcript capture
    try:
        request_json = json.loads(request_body)
    except (json.JSONDecodeError, TypeError):
        request_json = {}

    client = get_anthropic_client()

    try:
        if is_streaming:
            # Stream SSE response using httpx's send() with stream=True
            # This gives us direct control over the response lifecycle
            http_request = client.build_request(
                "POST",
                "/v1/messages",
                headers=headers,
                content=request_body,
            )
            upstream = client.send(http_request, stream=True)
            response_headers = _filter_response_headers(upstream.headers)
            # Forward actual Content-Type from upstream (usually text/event-stream)
            content_type = upstream.headers.get("content-type", "text/event-stream")

            # For streaming, we need to capture the full response while forwarding chunks
            # Cap memory usage at 10MB to prevent resource exhaustion
            MAX_CAPTURE_SIZE = 10 * 1024 * 1024  # 10MB
            collected_chunks: list[bytes] = []
            collected_size = 0
            capture_truncated = False

            def generate() -> Any:
                nonlocal collected_size, capture_truncated
                try:
                    for chunk in upstream.iter_bytes():
                        # Only collect chunks until we hit the size limit
                        if not capture_truncated:
                            if collected_size + len(chunk) <= MAX_CAPTURE_SIZE:
                                collected_chunks.append(chunk)
                                collected_size += len(chunk)
                            else:
                                capture_truncated = True
                                logger.debug(
                                    "Streaming capture truncated due to size limit",
                                    container_id=container_id,
                                    size_limit=MAX_CAPTURE_SIZE,
                                )
                        yield chunk
                finally:
                    upstream.close()
                    # Capture to transcript buffer after streaming completes
                    if container_id:
                        _capture_streaming_response(
                            container_id=container_id,
                            request_json=request_json,
                            chunks=collected_chunks,
                            start_time=start_time,
                        )

            return Response(
                stream_with_context(generate()),
                status=upstream.status_code,
                headers=response_headers,
                content_type=content_type,
            )
        else:
            # Non-streaming: simple request/response
            response = client.post(
                "/v1/messages",
                headers=headers,
                content=request_body,
            )

            # Capture to transcript buffer
            if container_id:
                _capture_non_streaming_response(
                    container_id=container_id,
                    request_json=request_json,
                    response_body=response.content,
                    start_time=start_time,
                    status_code=response.status_code,
                )

            return Response(
                response.content,
                status=response.status_code,
                headers=_filter_response_headers(response.headers),
            )

    except httpx.ConnectError as e:
        logger.error("Anthropic API connection failed", error=str(e))
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"Failed to connect to Anthropic API: {e}",
                }
            }
        ), 502

    except httpx.TimeoutException as e:
        logger.error("Anthropic API request timed out", error=str(e))
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"Anthropic API request timed out: {e}",
                }
            }
        ), 504

    except Exception as e:
        logger.exception("Anthropic API proxy error")
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"Anthropic API proxy error: {e}",
                }
            }
        ), 502


@app.route("/v1/messages/count_tokens", methods=["POST"])
def proxy_count_tokens() -> tuple[Response, int] | Response:
    """
    Proxy token counting API (non-streaming).

    This endpoint allows Claude Code to use ANTHROPIC_BASE_URL to route
    token counting requests through the gateway.
    """
    headers = _get_forwarded_headers(request.headers)
    headers, error = _inject_anthropic_credentials(headers)
    if error:
        return error

    client = get_anthropic_client()

    try:
        response = client.post(
            "/v1/messages/count_tokens",
            headers=headers,
            content=request.get_data(),
        )
        return Response(
            response.content,
            status=response.status_code,
            headers=_filter_response_headers(response.headers),
        )

    except httpx.ConnectError as e:
        logger.error("Anthropic API connection failed", error=str(e))
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"Failed to connect to Anthropic API: {e}",
                }
            }
        ), 502

    except httpx.TimeoutException as e:
        logger.error("Anthropic API request timed out", error=str(e))
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"Anthropic API request timed out: {e}",
                }
            }
        ), 504

    except Exception as e:
        logger.exception("Anthropic API proxy error")
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"Anthropic API proxy error: {e}",
                }
            }
        ), 502


def main() -> None:
    """Run the gateway server."""
    # Safety check: refuse to run as root to prevent permission issues
    # When the gateway runs as root, git objects are created with root:root ownership,
    # which breaks git operations on the host (permission denied on .git/objects).
    if os.getuid() == 0:
        print(
            "ERROR: gateway must not run as root.\n"
            "\n"
            "Running as root causes git objects to be created with root:root ownership,\n"
            "which breaks git operations on the host with 'permission denied' errors.\n"
            "\n"
            "To fix this:\n"
            "  1. Check the service file path in gateway.service\n"
            "  2. Ensure the gateway is started via 'egg' or 'bin/egg-deploy up'\n"
            "  3. Restart the gateway and try again\n"
            "  4. Verify the gateway is running as your user: ps aux | grep gateway\n"
            "\n"
            "If .git/objects already has root-owned files, fix with:\n"
            "  sudo chown -R $(id -u):$(id -g) ~/repos/*/.git",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Gateway Sidecar REST API")
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Host to listen on (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )

    args = parser.parse_args()

    # Initialize token refresher for in-memory token management
    try:
        from token_refresher import initialize_token_refresher

        refresher = initialize_token_refresher()
        if refresher:
            logger.info("Token refresher initialized (in-memory token refresh enabled)")
        else:
            logger.warning("Token refresher not configured - GitHub operations will fail")
    except ImportError:
        logger.error("Token refresher module not available - GitHub operations will fail")
    except Exception as e:
        logger.error("Token refresher initialization failed", error=str(e))

    # Initialize reviewer token refresher (optional — for posting reviews with
    # approve/request-changes using a separate GitHub App identity)
    try:
        from token_refresher import initialize_reviewer_token_refresher

        reviewer_refresher = initialize_reviewer_token_refresher()
        if reviewer_refresher:
            logger.info("Reviewer token refresher initialized")
        else:
            logger.debug("Reviewer token refresher not configured (optional)")
    except ImportError:
        pass  # Already logged above
    except Exception as e:
        logger.warning("Reviewer token refresher initialization failed", error=str(e))

    # Validate user mode config if configured
    github = get_github_client()
    is_valid, validation_msg = github.validate_user_mode_config()
    if not is_valid:
        logger.warning("User mode config validation failed", reason=validation_msg)
    else:
        logger.info("User mode config", status=validation_msg)

    # Load sessions BEFORE worktree cleanup so we know which containers are active.
    # After a gateway restart, Docker CLI may not be available inside the container,
    # so we derive the active container set from persisted sessions instead.
    active_container_ids: set[str] = set()
    try:
        session_manager = get_session_manager()
        pruned = session_manager.prune_expired_sessions()
        if pruned > 0:
            logger.info(f"Startup session cleanup pruned {pruned} expired session(s)")
        # Extract active container IDs from surviving sessions
        for session_info in session_manager.list_sessions():
            container_id = session_info.get("container_id")
            if container_id:
                active_container_ids.add(container_id)
        if active_container_ids:
            logger.info(
                "Active containers from sessions",
                count=len(active_container_ids),
            )
    except Exception as e:
        logger.warning("Startup session cleanup failed", error=str(e))

    # Also check Docker directly as safety net — sessions may be
    # pruned but containers still running.
    try:
        docker_containers = get_active_docker_containers()
        active_container_ids |= docker_containers
    except Exception as e:
        logger.warning("Could not query Docker containers", error=str(e))

    # Clean up orphaned worktrees from crashed containers
    try:
        orphans_removed = startup_cleanup(
            active_containers=active_container_ids,
            session_manager=get_session_manager(),
        )
        if orphans_removed > 0:
            logger.info(f"Startup cleanup removed {orphans_removed} orphaned worktree(s)")
    except Exception as e:
        logger.warning("Startup worktree cleanup failed", error=str(e))

    # Ensure launcher secret is configured - fail startup if not
    try:
        get_launcher_secret()
    except LauncherSecretNotConfiguredError as e:
        logger.error("Startup failed: launcher secret not configured", error=str(e))
        sys.exit(1)

    logger.info(
        "Starting Gateway Sidecar",
        host=args.host,
        port=args.port,
        debug=args.debug,
    )
    logger.info("Session authentication required for all container operations")

    # Run with production server in production, debug server in debug mode
    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
    else:
        # Use waitress for production
        serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
