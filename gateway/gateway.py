#!/usr/bin/env python3
"""
Gateway Sidecar - REST API for policy-enforced git/gh operations.

Provides a REST API that sandbox containers call to perform git push and gh operations.
The gateway holds GitHub credentials and enforces ownership policies.

Security:
    - Authentication via launcher secret (EGG_LAUNCHER_SECRET) and session tokens
    - Listens on all interfaces (containers access via host.docker.internal)

Endpoints:
    POST /api/v1/git/push       - Push to remote (policy: branch_ownership or trusted_user)
    POST /api/v1/git/fetch      - Fetch from remote (no policy - read operations allowed)
    POST /api/v1/git/execute    - Local git commands (status, commit, etc.)
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
import os
import secrets
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from flask import Flask, Response, g, jsonify, request
from waitress import serve

from shared.egg_logging import get_logger

from .git_client import (
    GIT_ALLOWED_COMMANDS,
    cleanup_credential_helper,
    create_credential_helper,
    get_authenticated_remote_target,
    get_token_for_repo,
    git_cmd,
    is_repos_parent_directory,
    validate_git_args,
    validate_repo_path,
)
from .github_client import (
    BLOCKED_GH_COMMANDS,
    READONLY_GH_COMMANDS,
    extract_repo_from_gh_command,
    get_github_client,
    parse_gh_api_args,
    validate_gh_api_path,
)
from .policy import (
    extract_branch_from_refspec,
    extract_repo_from_remote,
    get_policy_engine,
)
from .private_repo_policy import check_private_repo_access
from .rate_limiter import (
    check_heartbeat_rate_limit,
    check_registration_rate_limit,
    record_failed_lookup,
)
from .repo_config import get_auth_mode
from .repo_parser import parse_owner_repo
from .repo_visibility import get_repo_visibility
from .session_manager import (
    get_session_manager,
    validate_session_for_request,
)
from .worktree_manager import WorktreeManager, startup_cleanup

# Type variables for decorator typing
P = ParamSpec("P")
R = TypeVar("R")

logger = get_logger("gateway")

app = Flask(__name__)

# Configuration
DEFAULT_HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")  # nosec B104 - intentional for container
DEFAULT_PORT = int(os.environ.get("GATEWAY_PORT", "9847"))

# Host home directory for path translation
HOST_HOME = os.environ.get("HOST_HOME", "")
CONTAINER_HOME = os.environ.get("CONTAINER_HOME", "/home/user")

# Commands blocked in private mode (too broad to filter by repo)
GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE = frozenset({"search", "browse", "gist"})


def translate_to_host_path(container_path: str) -> str:
    """Translate a container path to the corresponding host path."""
    if not HOST_HOME:
        return container_path

    if container_path.startswith(CONTAINER_HOME):
        return container_path.replace(CONTAINER_HOME, HOST_HOME, 1)

    return container_path


def require_session_auth(f: Callable[P, R]) -> Callable[P, R]:
    """Decorator that validates session tokens in request handlers."""

    @functools.wraps(f)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                "Session auth failed - missing Authorization header",
                endpoint=request.path,
                source_ip=request.remote_addr,
            )
            return make_error("Missing or invalid Authorization header", status_code=401)  # type: ignore[return-value]

        token = auth_header[7:]
        source_ip = request.remote_addr or "unknown"

        result = validate_session_for_request(token, source_ip)
        if not result.valid:
            record_failed_lookup(source_ip)
            logger.warning(
                "Session auth failed - invalid token",
                endpoint=request.path,
                source_ip=source_ip,
                error=result.error,
            )
            return make_error(result.error or "Invalid or expired session token", status_code=401)  # type: ignore[return-value]

        g.session = result.session
        g.session_mode = result.session.mode if result.session else None

        return f(*args, **kwargs)

    return decorated


# Launcher secret for session management
LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "")
LAUNCHER_SECRET_FILE = Path("/secrets/launcher-secret")


class LauncherSecretNotConfiguredError(Exception):
    """Raised when launcher secret is not configured."""


def get_launcher_secret() -> str:
    """Get the launcher secret from environment or file."""
    global LAUNCHER_SECRET

    if LAUNCHER_SECRET:
        return LAUNCHER_SECRET

    if LAUNCHER_SECRET_FILE.exists():
        LAUNCHER_SECRET = LAUNCHER_SECRET_FILE.read_text().strip()
        return LAUNCHER_SECRET

    raise LauncherSecretNotConfiguredError(
        f"Launcher secret not found at {LAUNCHER_SECRET_FILE} or EGG_LAUNCHER_SECRET env var."
    )


def check_launcher_auth() -> tuple[bool, str]:
    """Check if request has valid launcher authentication."""
    try:
        secret = get_launcher_secret()
    except LauncherSecretNotConfiguredError:
        return False, "Launcher secret not configured"

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False, "Missing or invalid Authorization header"

    provided_token = auth_header[7:]

    if secrets.compare_digest(provided_token, secret):
        return True, ""

    return False, "Invalid launcher authorization token"


def require_launcher_auth(f: Callable[P, R]) -> Callable[P, R]:
    """Decorator to require launcher authentication for an endpoint."""

    @functools.wraps(f)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        is_valid, error = check_launcher_auth()
        if not is_valid:
            logger.warning(
                "Launcher authentication failed",
                endpoint=request.path,
                error=error,
                source_ip=request.remote_addr,
            )
            return make_error(error, status_code=401)  # type: ignore[return-value]
        return f(*args, **kwargs)

    return decorated


def make_response(
    success: bool,
    message: str,
    data: dict[str, Any] | None = None,
    status_code: int = 200,
) -> tuple[Response, int]:
    """Create a standardized JSON response."""
    response: dict[str, Any] = {"success": success, "message": message}
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


@dataclass
class PROperationContext:
    """Context for PR operations after access checks pass."""

    repo: str
    auth_mode: str
    session_mode: str | None


def check_pr_operation_access(
    repo: str,
    operation: str,
    endpoint_name: str,
    pr_number: int | None = None,
) -> tuple[PROperationContext | None, tuple[Response, int] | None]:
    """
    Common access checks for PR operations.

    Performs:
    - Auth mode determination
    - Session mode extraction
    - Private repo access policy check

    Returns:
        (context, None) if access is allowed
        (None, error_response) if access is denied
    """
    auth_mode = get_auth_mode(repo)
    session_mode = getattr(g, "session_mode", None)

    # Check Private Repo Mode policy
    repo_info = parse_owner_repo(repo)
    if repo_info:
        priv_result = check_private_repo_access(
            operation=operation,
            owner=repo_info.owner,
            repo=repo_info.repo,
            for_write=True,
            session_mode=session_mode,
        )
        if not priv_result.allowed:
            details: dict[str, Any] = {
                "repo": repo,
                "reason": priv_result.reason,
                "visibility": priv_result.visibility,
                "auth_mode": auth_mode,
            }
            if pr_number is not None:
                details["pr_number"] = pr_number

            audit_log(
                f"{operation}_denied_private_mode",
                endpoint_name,
                success=False,
                details=details,
            )
            return None, make_error(
                priv_result.reason,
                status_code=403,
                details=priv_result.to_dict(),
            )

    return PROperationContext(repo=repo, auth_mode=auth_mode, session_mode=session_mode), None


def execute_pr_operation(
    operation_name: str,
    endpoint_name: str,
    repo: str,
    auth_mode: str,
    gh_args: list[str],
    audit_details: dict[str, Any],
    success_message: str,
    timeout: int = 30,
) -> tuple[Response, int]:
    """
    Execute a PR operation with consistent error handling and audit logging.

    Args:
        operation_name: Name of the operation for logging (e.g., "pr_comment")
        endpoint_name: Flask endpoint name (e.g., "gh_pr_comment")
        repo: Repository in owner/repo format
        auth_mode: Authentication mode ("bot" or "user")
        gh_args: Arguments for gh CLI
        audit_details: Details to include in audit log
        success_message: Message to return on success
        timeout: Timeout for gh command in seconds

    Returns:
        Flask response tuple
    """
    try:
        github = get_github_client(mode=auth_mode)
        result = github.execute(gh_args, timeout=timeout, mode=auth_mode)

        if result.success:
            audit_log(
                operation_name,
                endpoint_name,
                success=True,
                details=audit_details,
            )
            return make_success(
                success_message,
                {"stdout": result.stdout, "auth_mode": auth_mode},
            )
        else:
            error_msg = result.stderr or "Unknown error"
            audit_log(
                f"{operation_name}_failed",
                endpoint_name,
                success=False,
                details={
                    **audit_details,
                    "error": error_msg[:200] if error_msg else "",
                },
            )
            return make_error(
                f"Failed to {operation_name.replace('_', ' ')}: {error_msg}",
                status_code=500,
                details=result.to_dict(),
            )
    except Exception as e:
        logger.exception(f"Unexpected error in {endpoint_name}")
        return make_error(f"Internal error: {e}", status_code=500)


@app.route("/api/v1/health", methods=["GET"])
def health_check() -> Response:
    """Health check endpoint (no auth required)."""
    github = get_github_client()
    token_valid = github.is_token_valid()

    try:
        get_launcher_secret()
        launcher_secret_configured = True
    except LauncherSecretNotConfiguredError:
        launcher_secret_configured = False

    session_manager = get_session_manager()
    active_sessions = len(session_manager.list_sessions())

    return jsonify(
        {
            "status": "healthy" if (token_valid and launcher_secret_configured) else "degraded",
            "github_token_valid": token_valid,
            "auth_configured": launcher_secret_configured,
            "active_sessions": active_sessions,
            "service": "egg-gateway",
        }
    )


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
    """Map a container's repo path to the corresponding worktree path."""
    if not container_id:
        return repo_path

    repos_prefix = f"{CONTAINER_HOME}/repos/"
    if not repo_path.startswith(repos_prefix):
        return repo_path

    relative_path = repo_path[len(repos_prefix) :].rstrip("/")
    if not relative_path:
        return repo_path

    parts = relative_path.split("/", 1)
    repo_name = parts[0]
    subdir = parts[1] if len(parts) > 1 else ""

    if not repo_name:
        return repo_path

    manager = get_worktree_manager()
    try:
        worktree_path, _main_repo = manager.get_worktree_paths(container_id, repo_name)
        if worktree_path.exists():
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


@app.route("/api/v1/git/push", methods=["POST"])
@require_session_auth
def git_push() -> tuple[Response, int]:
    """Handle git push requests."""
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

    path_valid, path_error = validate_repo_path(repo_path)
    if not path_valid:
        audit_log(
            "push_blocked",
            "git_push",
            success=False,
            details={"repo_path": repo_path, "reason": path_error},
        )
        return make_error(path_error, status_code=403)

    exec_path = map_container_path_to_worktree(repo_path, container_id, "push")

    try:
        result = subprocess.run(
            git_cmd("remote", "get-url", remote),
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return make_error(f"Failed to get remote URL: {result.stderr}")
        remote_url = result.stdout.strip()
    except Exception as e:
        return make_error(f"Failed to get remote URL: {e}")

    repo = extract_repo_from_remote(remote_url)
    if not repo:
        return make_error(f"Could not parse repository from URL: {remote_url}")

    branch = extract_branch_from_refspec(refspec)
    if not branch:
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

    auth_mode = get_auth_mode(repo)
    session_mode = getattr(g, "session_mode", None)

    repo_info = parse_owner_repo(repo)
    if repo_info:
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

    policy = get_policy_engine()
    policy_result = policy.check_branch_ownership(repo, branch)

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

    token_str, auth_mode, token_error = get_token_for_repo(repo, get_auth_mode, get_github_client)
    if not token_str:
        return make_error(token_error, status_code=503)

    push_target = get_authenticated_remote_target(remote, remote_url)
    push_args = ["push"]
    if force:
        push_args.append("--force")
    push_args.extend([push_target, refspec] if refspec else [push_target])
    cmd = git_cmd(*push_args)

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
def git_execute() -> tuple[Response, int]:
    """Execute a git command in the gateway's worktree."""
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

    if operation in ("push", "fetch", "ls-remote"):
        return make_error(
            f"Use dedicated endpoint for {operation}: /api/v1/git/{operation}",
            status_code=400,
        )

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

    exec_path = map_container_path_to_worktree(repo_path, container_id, operation)
    cmd = git_cmd(operation, *validated_args)

    try:
        result = subprocess.run(
            cmd,
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
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
            is_expected_failure = result.stderr and (
                "not a git repository" in result.stderr
                or "not inside a git repository" in result.stderr
            )

            if not is_expected_failure:
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
def git_fetch() -> tuple[Response, int]:
    """Handle git fetch requests."""
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    remote = data.get("remote", "origin")
    operation = data.get("operation", "fetch")
    extra_args = data.get("args", [])
    container_id = data.get("container_id")

    if not repo_path:
        return make_error("Missing repo_path")

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

    args_valid, args_error, validated_args = validate_git_args(operation, extra_args)
    if not args_valid:
        audit_log(
            "fetch_blocked",
            "git_fetch",
            success=False,
            details={"reason": args_error, "operation": operation},
        )
        return make_error(args_error, status_code=400)

    exec_path = map_container_path_to_worktree(repo_path, container_id, operation)

    try:
        result = subprocess.run(
            git_cmd("remote", "get-url", remote),
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return make_error(f"Failed to get remote URL: {result.stderr}")
        remote_url = result.stdout.strip()
    except Exception as e:
        return make_error(f"Failed to get remote URL: {e}")

    repo = extract_repo_from_remote(remote_url)
    if not repo:
        return make_error(f"Could not parse repository from URL: {remote_url}")

    session_mode = getattr(g, "session_mode", None)

    repo_info = parse_owner_repo(repo)
    if repo_info:
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

    token_str, auth_mode, token_error = get_token_for_repo(repo, get_auth_mode, get_github_client)
    if not token_str:
        return make_error(token_error, status_code=503)

    fetch_target = get_authenticated_remote_target(remote, remote_url)

    if operation == "fetch":
        if "--all" in validated_args:
            cmd_args = ["fetch"] + validated_args
        else:
            cmd_args = ["fetch", fetch_target] + validated_args
    else:
        cmd_args = ["ls-remote", fetch_target] + validated_args

    cmd = git_cmd(*cmd_args)

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
                details={"repo": repo, "auth_mode": auth_mode},
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


@app.route("/api/v1/gh/execute", methods=["POST"])
@require_session_auth
def gh_execute() -> tuple[Response, int]:
    """Execute a generic gh command."""
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    args = data.get("args", [])
    cwd = data.get("cwd")
    payload_repo = data.get("repo")

    if not args:
        return make_error("Missing args")

    session_mode = getattr(g, "session_mode", None)

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

    if args and args[0] == "api" and len(args) > 1:
        api_path, method = parse_gh_api_args(args[1:])
        if api_path is None:
            audit_log(
                "api_path_missing",
                "gh_execute",
                success=False,
                details={"command_args": args},
            )
            return make_error("No API path provided in gh api command", status_code=400)

        path_valid, path_error = validate_gh_api_path(api_path, method)
        if not path_valid:
            audit_log(
                "api_path_blocked",
                "gh_execute",
                success=False,
                details={"api_path": api_path, "method": method, "reason": path_error},
            )
            return make_error(path_error, status_code=403)

    repo = extract_repo_from_gh_command(args)

    if not repo and payload_repo:
        repo = payload_repo
        if args and args[0] != "repo":
            args = ["--repo", payload_repo] + list(args)

    auth_mode = get_auth_mode(repo) if repo else "bot"

    if repo:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            priv_result = check_private_repo_access(
                operation="gh_execute",
                owner=repo_info.owner,
                repo=repo_info.repo,
                for_write=False,
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


@app.route("/api/v1/gh/pr/create", methods=["POST"])
@require_session_auth
def gh_pr_create() -> tuple[Response, int]:
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

    # Common access checks (auth mode, session mode, private repo policy)
    ctx, error = check_pr_operation_access(repo, "pr_create", "gh_pr_create")
    if error:
        return error

    # Policy check: PR creation may be blocked in user mode
    policy = get_policy_engine()
    policy_result = policy.check_pr_create_allowed(repo, auth_mode=ctx.auth_mode)
    if not policy_result.allowed:
        audit_log(
            "pr_create_blocked",
            "gh_pr_create",
            success=False,
            details={
                "repo": repo,
                "reason": policy_result.reason,
                "auth_mode": ctx.auth_mode,
            },
        )
        return make_error(
            policy_result.reason,
            status_code=403,
            details=policy_result.details,
        )

    # Execute with consistent error handling
    return execute_pr_operation(
        operation_name="pr_create",
        endpoint_name="gh_pr_create",
        repo=repo,
        auth_mode=ctx.auth_mode,
        gh_args=[
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
        ],
        audit_details={
            "repo": repo,
            "title": title,
            "base": base,
            "head": head,
            "auth_mode": ctx.auth_mode,
        },
        success_message="PR created",
        timeout=60,
    )


@app.route("/api/v1/gh/pr/comment", methods=["POST"])
@require_session_auth
def gh_pr_comment() -> tuple[Response, int]:
    """
    Add a comment to a PR.

    Request body:
        {
            "repo": "owner/repo",
            "pr_number": 123,
            "body": "Comment text"
        }

    Policy: Comments are allowed on any PR.
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

    # Common access checks (auth mode, session mode, private repo policy)
    ctx, error = check_pr_operation_access(repo, "pr_comment", "gh_pr_comment", pr_number)
    if error:
        return error

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
                "auth_mode": ctx.auth_mode,
            },
        )
        return make_error(
            f"Comment denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    # Execute with consistent error handling
    return execute_pr_operation(
        operation_name="pr_comment",
        endpoint_name="gh_pr_comment",
        repo=repo,
        auth_mode=ctx.auth_mode,
        gh_args=["pr", "comment", str(pr_number), "--repo", repo, "--body", body],
        audit_details={"repo": repo, "pr_number": pr_number, "auth_mode": ctx.auth_mode},
        success_message="Comment added",
    )


@app.route("/api/v1/gh/pr/edit", methods=["POST"])
@require_session_auth
def gh_pr_edit() -> tuple[Response, int]:
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

    # Common access checks (auth mode, session mode, private repo policy)
    ctx, error = check_pr_operation_access(repo, "pr_edit", "gh_pr_edit", pr_number)
    if error:
        return error

    # Check PR ownership
    policy = get_policy_engine()
    policy_result = policy.check_pr_ownership(repo, pr_number)

    if not policy_result.allowed:
        audit_log(
            "pr_edit_denied",
            "gh_pr_edit",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
                "reason": policy_result.reason,
                "auth_mode": ctx.auth_mode,
            },
        )
        return make_error(
            f"Edit denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    # Build args
    args = ["pr", "edit", str(pr_number), "--repo", repo]
    if title:
        args.extend(["--title", title])
    if body:
        args.extend(["--body", body])

    # Execute with consistent error handling
    return execute_pr_operation(
        operation_name="pr_edit",
        endpoint_name="gh_pr_edit",
        repo=repo,
        auth_mode=ctx.auth_mode,
        gh_args=args,
        audit_details={"repo": repo, "pr_number": pr_number, "auth_mode": ctx.auth_mode},
        success_message="PR edited",
    )


@app.route("/api/v1/gh/pr/close", methods=["POST"])
@require_session_auth
def gh_pr_close() -> tuple[Response, int]:
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

    # Common access checks (auth mode, session mode, private repo policy)
    ctx, error = check_pr_operation_access(repo, "pr_close", "gh_pr_close", pr_number)
    if error:
        return error

    # Check PR ownership
    policy = get_policy_engine()
    policy_result = policy.check_pr_ownership(repo, pr_number)

    if not policy_result.allowed:
        audit_log(
            "pr_close_denied",
            "gh_pr_close",
            success=False,
            details={
                "repo": repo,
                "pr_number": pr_number,
                "reason": policy_result.reason,
                "auth_mode": ctx.auth_mode,
            },
        )
        return make_error(
            f"Close denied: {policy_result.reason}",
            status_code=403,
            details=policy_result.details,
        )

    # Execute with consistent error handling
    return execute_pr_operation(
        operation_name="pr_close",
        endpoint_name="gh_pr_close",
        repo=repo,
        auth_mode=ctx.auth_mode,
        gh_args=["pr", "close", str(pr_number), "--repo", repo],
        audit_details={"repo": repo, "pr_number": pr_number, "auth_mode": ctx.auth_mode},
        success_message="PR closed",
    )


# Session Management Endpoints


@app.route("/api/v1/sessions/create", methods=["POST"])
@require_launcher_auth
def session_create() -> tuple[Response, int]:
    """Create a session with atomic visibility query, filtering, worktree creation."""
    rate_result = check_registration_rate_limit(request.remote_addr or "unknown")
    if not rate_result.allowed:
        return make_error(
            "Rate limit exceeded for session registration",
            status_code=429,
            details={"retry_after_seconds": rate_result.retry_after_seconds},
        )

    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    container_ip = data.get("container_ip")
    mode = data.get("mode")
    repos = data.get("repos", [])
    uid = data.get("uid")
    gid = data.get("gid")

    if not container_id:
        return make_error("Missing container_id")
    if not container_ip:
        return make_error("Missing container_ip")
    if mode not in ("private", "public"):
        return make_error("Invalid mode: must be 'private' or 'public'")
    if not repos:
        return make_error("Missing repos list")

    if uid is not None and (not isinstance(uid, int) or uid < 0):
        return make_error("Invalid uid: must be a non-negative integer")
    if gid is not None and (not isinstance(gid, int) or gid < 0):
        return make_error("Invalid gid: must be a non-negative integer")

    # Query visibility for all repos
    repo_visibilities = {}
    for repo in repos:
        repo_info = parse_owner_repo(repo)
        if repo_info:
            visibility = get_repo_visibility(repo_info.owner, repo_info.repo)
            repo_visibilities[repo] = visibility

    # Filter repos based on mode
    filtered_repos = []
    for repo, visibility in repo_visibilities.items():
        if visibility is None:
            continue

        if mode == "private":
            if visibility in ("private", "internal"):
                filtered_repos.append(repo)
        elif visibility == "public":
            filtered_repos.append(repo)

    # Create worktrees for filtered repos
    manager = get_worktree_manager()
    worktrees = {}
    worktree_errors = []

    for repo in filtered_repos:
        repo_name = repo.split("/")[-1] if "/" in repo else repo

        try:
            info = manager.create_worktree(
                repo_name=repo_name,
                container_id=container_id,
                base_branch="HEAD",
                uid=uid,
                gid=gid,
            )
            worktrees[repo_name] = translate_to_host_path(str(info.worktree_path))
        except (ValueError, RuntimeError) as e:
            worktree_errors.append(f"{repo_name}: {e}")
        except Exception as e:
            worktree_errors.append(f"{repo_name}: unexpected error - {e}")

    if not worktrees and filtered_repos:
        return make_error(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": worktree_errors},
        )

    # Register session
    session_manager = get_session_manager()
    token, _session = session_manager.register_session(
        container_id=container_id,
        container_ip=container_ip,
        mode=mode,
    )

    audit_log(
        "session_created",
        "session_create",
        success=True,
        details={
            "container_id": container_id,
            "container_ip": container_ip,
            "mode": mode,
            "filtered_repos": filtered_repos,
            "worktree_count": len(worktrees),
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


@app.route("/api/v1/sessions/<session_token>", methods=["DELETE"])
@require_launcher_auth
def session_delete(session_token: str) -> tuple[Response, int]:
    """Delete a session."""
    session_manager = get_session_manager()

    session = session_manager.get_session(session_token)
    container_id = session.container_id if session else None

    deleted = session_manager.delete_session(session_token)

    if not deleted:
        return make_error("Session not found", status_code=404)

    if container_id:
        manager = get_worktree_manager()
        worktree_dir = manager.worktree_base / container_id
        if worktree_dir.exists():
            deleted_worktrees = []
            for repo_dir in list(worktree_dir.iterdir()):
                if repo_dir.is_dir():
                    result = manager.remove_worktree(
                        container_id=container_id,
                        repo_name=repo_dir.name,
                        force=True,
                    )
                    if result.success:
                        deleted_worktrees.append(repo_dir.name)

    return make_success("Session deleted")


@app.route("/api/v1/sessions/<session_token>/heartbeat", methods=["POST"])
@require_launcher_auth
def session_heartbeat(session_token: str) -> tuple[Response, int]:
    """
    Explicit session heartbeat to extend TTL.

    Note: Heartbeats are also triggered implicitly on any successful
    session-authenticated request. This endpoint exists for edge cases
    where long-running operations need TTL extension without git/gh activity.

    Args:
        session_token: The session token

    Auth: Bearer {launcher_secret}

    Rate limit: 100 per hour per session
    """
    # Validate the session
    result = validate_session_for_request(session_token, request.remote_addr)
    if not result.valid:
        # Record failed lookup for rate limiting
        record_failed_lookup(request.remote_addr or "unknown")
        return make_error(result.error or "Invalid session", status_code=401)

    # Check heartbeat rate limit (100 per hour per session)
    if result.session:
        rate_limit = check_heartbeat_rate_limit(result.session.container_id)
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


@app.route("/api/v1/repos/visibility", methods=["GET"])
@require_launcher_auth
def repos_visibility() -> tuple[Response, int]:
    """
    Query visibility for multiple repositories.

    Used by launcher for informational queries. For atomic session+worktree
    creation, use POST /api/v1/sessions/create instead.

    Query params:
        repos: Comma-separated list of owner/repo strings

    Response:
        {
            "visibilities": {
                "owner/repo1": {"visibility": "public"},
                "owner/repo2": {"visibility": "private"},
                "owner/repo3": {"visibility": null, "error": "invalid_format"}
            },
            "errors": ["owner/repo3: invalid format"]  # Only if there are errors
        }

    Auth: Bearer {launcher_secret}
    """
    repos_param = request.args.get("repos", "")
    if not repos_param:
        return make_error("Missing repos query parameter")

    repos = [r.strip() for r in repos_param.split(",") if r.strip()]
    if not repos:
        return make_error("No valid repos provided")

    visibilities: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for repo in repos:
        repo_info = parse_owner_repo(repo)
        if not repo_info:
            visibilities[repo] = {"visibility": None, "error": "invalid_format"}
            errors.append(f"{repo}: invalid format (expected owner/repo)")
            continue

        try:
            visibility = get_repo_visibility(repo_info.owner, repo_info.repo)
            if visibility is None:
                visibilities[repo] = {"visibility": None, "error": "not_found_or_no_access"}
                errors.append(f"{repo}: repository not found or no access")
            else:
                visibilities[repo] = {"visibility": visibility}
        except Exception as e:
            visibilities[repo] = {"visibility": None, "error": "api_error"}
            errors.append(f"{repo}: API error ({type(e).__name__})")
            logger.warning(
                "Visibility check failed",
                repo=repo,
                error=str(e),
            )

    response_data: dict[str, Any] = {"visibilities": visibilities}
    if errors:
        response_data["errors"] = errors

    return make_success("Visibility queried", response_data)


@app.route("/api/v1/sessions", methods=["GET"])
@require_launcher_auth
def sessions_list() -> tuple[Response, int]:
    """List all active sessions."""
    session_manager = get_session_manager()
    sessions = session_manager.list_sessions()
    return make_success("Sessions listed", {"sessions": sessions})


# Worktree endpoints


@app.route("/api/v1/worktree/create", methods=["POST"])
@require_launcher_auth
def worktree_create() -> tuple[Response, int]:
    """Create worktrees for a container."""
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    repos = data.get("repos", [])
    base_branch = data.get("base_branch", "HEAD")
    uid = data.get("uid")
    gid = data.get("gid")

    if not container_id:
        return make_error("Missing container_id")
    if not repos:
        return make_error("Missing repos list")

    manager = get_worktree_manager()
    worktrees = {}
    errors = []

    for repo in repos:
        repo_name = repo.split("/")[-1] if "/" in repo else repo

        try:
            info = manager.create_worktree(
                repo_name=repo_name,
                container_id=container_id,
                base_branch=base_branch,
                uid=uid,
                gid=gid,
            )
            worktrees[repo_name] = translate_to_host_path(str(info.worktree_path))
        except (ValueError, RuntimeError) as e:
            errors.append(f"{repo_name}: {e}")
        except Exception as e:
            errors.append(f"{repo_name}: unexpected error - {e}")

    if errors and not worktrees:
        return make_error(
            "Failed to create any worktrees",
            status_code=500,
            details={"errors": errors},
        )

    return make_success(
        "Worktrees created",
        {"worktrees": worktrees, "errors": errors if errors else None},
    )


@app.route("/api/v1/worktree/delete", methods=["POST"])
@require_launcher_auth
def worktree_delete() -> tuple[Response, int]:
    """Delete worktrees for a container."""
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    container_id = data.get("container_id")
    force = data.get("force", False)

    if not container_id:
        return make_error("Missing container_id")

    manager = get_worktree_manager()

    worktree_dir = manager.worktree_base / container_id
    if not worktree_dir.exists():
        return make_success("No worktrees to delete", {"deleted": []})

    deleted = []
    errors = []

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
            elif result.uncommitted_changes and not force:
                errors.append(f"{repo_name}: has uncommitted changes (use force=true)")
            elif result.error:
                errors.append(f"{repo_name}: {result.error}")
        except Exception as e:
            errors.append(f"{repo_name}: unexpected error - {e}")

    return make_success(
        "Worktrees deleted",
        {"deleted": deleted, "errors": errors if errors else None},
    )


@app.route("/api/v1/worktree/list", methods=["GET"])
@require_launcher_auth
def worktree_list() -> tuple[Response, int]:
    """List all active worktrees."""
    manager = get_worktree_manager()
    worktrees = manager.list_worktrees()
    return make_success("Worktrees listed", {"worktrees": worktrees})


def main() -> None:
    """Run the gateway server."""
    if os.getuid() == 0:
        print(
            "ERROR: egg-gateway must not run as root.\n"
            "\n"
            "Running as root causes git objects to be created with root:root ownership,\n"
            "which breaks git operations on the host with 'permission denied' errors.",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Egg Gateway Sidecar REST API")
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

    # Initialize token refresher
    try:
        from .token_refresher import initialize_token_refresher

        refresher = initialize_token_refresher()
        if refresher:
            logger.info("Token refresher initialized (in-memory token refresh enabled)")
        else:
            logger.warning("Token refresher not configured - GitHub operations will fail")
    except ImportError:
        logger.error("Token refresher module not available - GitHub operations will fail")
    except Exception as e:
        logger.error("Token refresher initialization failed", error=str(e))

    # Clean up orphaned worktrees
    try:
        orphans_removed = startup_cleanup()
        if orphans_removed > 0:
            logger.info(f"Startup cleanup removed {orphans_removed} orphaned worktree(s)")
    except Exception as e:
        logger.warning("Startup worktree cleanup failed", error=str(e))

    # Prune expired sessions
    try:
        session_manager = get_session_manager()
        pruned = session_manager.prune_expired_sessions()
        if pruned > 0:
            logger.info(f"Startup session cleanup pruned {pruned} expired session(s)")
    except Exception as e:
        logger.warning("Startup session cleanup failed", error=str(e))

    # Ensure launcher secret is configured
    try:
        get_launcher_secret()
    except LauncherSecretNotConfiguredError as e:
        logger.error("Startup failed: launcher secret not configured", error=str(e))
        sys.exit(1)

    logger.info(
        "Starting Egg Gateway Sidecar",
        host=args.host,
        port=args.port,
        debug=args.debug,
    )

    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)  # nosec B201 - only when explicitly requested
    else:
        serve(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
