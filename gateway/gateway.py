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
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        get_github_client,
        validate_gh_api_path,
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
        check_registration_rate_limit,
        record_failed_lookup,
    )
    from .repo_parser import parse_owner_repo
    from .repo_visibility import get_repo_visibility
    from .session_manager import (
        get_session_manager,
        validate_session_for_request,
    )
    from .worktree_manager import WorktreeManager, startup_cleanup
except ImportError:
    from anthropic_credentials import get_credentials_manager
    from git_client import (
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
    from github_client import (
        BLOCKED_GH_COMMANDS,
        GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE,
        READONLY_GH_COMMANDS,
        extract_repo_from_gh_command,
        get_github_client,
        parse_gh_api_args,
        validate_gh_api_path,
    )
    from policy import (
        extract_branch_from_refspec,
        extract_repo_from_remote,
        get_policy_engine,
    )
    from private_repo_policy import (
        check_private_repo_access,
    )
    from rate_limiter import (
        check_heartbeat_rate_limit,
        check_registration_rate_limit,
        record_failed_lookup,
    )
    from repo_parser import parse_owner_repo
    from repo_visibility import get_repo_visibility
    from session_manager import (
        get_session_manager,
        validate_session_for_request,
    )
    from worktree_manager import WorktreeManager, startup_cleanup

# Import repo_config for user mode support
# Path setup needed because config is in a sibling directory
_config_path = Path(__file__).parent.parent / "config"
if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))
from repo_config import get_auth_mode

logger = get_logger("gateway")

app = Flask(__name__)

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


def require_session_auth(f):
    """
    Decorator that validates session tokens in request handlers.

    - Extracts session token from Authorization header
    - Validates token via session_manager
    - Stores validated session and mode in Flask's g object for handler use
    - Returns 401 on validation failure

    All containers must have a valid session. There is no legacy fallback.
    """

    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning(
                "Session auth failed - missing Authorization header",
                endpoint=request.path,
                source_ip=request.remote_addr,
            )
            return make_error("Missing or invalid Authorization header", status_code=401)

        token = auth_header[7:]  # Remove "Bearer " prefix
        source_ip = request.remote_addr

        # Validate session via session_manager
        result = validate_session_for_request(token, source_ip)
        if not result.valid:
            # Record failed lookup for rate limiting
            record_failed_lookup(source_ip)
            logger.warning(
                "Session auth failed - invalid token",
                endpoint=request.path,
                source_ip=source_ip,
                error=result.error,
            )
            return make_error(result.error or "Invalid or expired session token", status_code=401)

        # Set session context from validation result
        g.session = result.session
        g.session_mode = result.session.mode if result.session else None

        return f(*args, **kwargs)

    return decorated


# Launcher secret for session management and worktree operations
# This is used by the egg launcher to authenticate with the gateway
LAUNCHER_SECRET = os.environ.get("EGG_LAUNCHER_SECRET", "")
LAUNCHER_SECRET_FILE = Path("/secrets/launcher-secret")


class LauncherSecretNotConfiguredError(Exception):
    """Raised when launcher secret is not configured."""


def get_launcher_secret() -> str:
    """Get the launcher secret from environment or file.

    The launcher secret is used to authenticate the egg launcher when
    registering sessions. It should be generated by setup.sh and mounted
    at /secrets/launcher-secret.

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
        "Run gateway/setup.sh to generate it."
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


def require_launcher_auth(f):
    """Decorator to require launcher authentication for an endpoint."""

    @functools.wraps(f)
    def decorated(*args, **kwargs):
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

    return decorated


def make_response(
    success: bool,
    message: str,
    data: dict[str, Any] | None = None,
    status_code: int = 200,
):
    """Create a standardized JSON response."""
    response = {"success": success, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), status_code


def make_error(message: str, status_code: int = 400, details: dict[str, Any] | None = None):
    """Create an error response."""
    return make_response(False, message, details, status_code)


def make_success(message: str, data: dict[str, Any] | None = None):
    """Create a success response."""
    return make_response(True, message, data, 200)


def audit_log(
    event_type: str,
    operation: str,
    success: bool,
    details: dict[str, Any] | None = None,
) -> None:
    """Log an audit event in structured format."""
    log_data = {
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


@app.route("/api/v1/health", methods=["GET"])
def health_check():
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

    # Gateway always runs with locked Squid.
    # Per-container mode is enforced at container start via network selection.
    # - Private containers: isolated network + proxy (locked to api.anthropic.com)
    # - Public containers: external network + direct internet (no proxy)
    return jsonify(
        {
            "status": "healthy" if (token_valid and launcher_secret_configured) else "degraded",
            "github_token_valid": token_valid,
            "auth_configured": launcher_secret_configured,
            "active_sessions": active_sessions,
            "service": "gateway",
        }
    )


@app.route("/api/v1/git/push", methods=["POST"])
@require_session_auth
def git_push():
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
    cmd = git_cmd(*push_args)

    # NOTE: Git author/committer info is set at COMMIT time, not push time.
    # For user mode, the user must configure their local git:
    #   git config user.name "Your Name"
    #   git config user.email "your@email.com"
    if auth_mode == "user":
        logger.debug("User mode push", repo=repo)

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
def git_execute():
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
    tag, clean, config, rev-parse, remote

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

    # Map container path to worktree path if container_id is provided
    exec_path = map_container_path_to_worktree(repo_path, container_id, operation)

    # SECURITY: Belt-and-suspenders hook prevention for operations that support it.
    # The primary protection is core.hooksPath=/dev/null in git_cmd() which disables
    # ALL hooks globally. However, we also add --no-verify for operations that
    # support it as defense-in-depth. See issue #58.
    #
    # Operations that support --no-verify:
    # - commit: pre-commit, prepare-commit-msg, commit-msg, post-commit
    # - merge: pre-merge-commit, prepare-commit-msg, commit-msg, post-merge
    # - cherry-pick: prepare-commit-msg, commit-msg
    # - am: pre-applypatch, applypatch-msg, post-applypatch
    if operation in ("commit", "merge", "cherry-pick", "am"):
        validated_args = ["--no-verify", *validated_args]

    # Build command
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
def git_fetch():
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

    # Extract repo from URL
    repo = extract_repo_from_remote(remote_url)
    if not repo:
        return make_error(f"Could not parse repository from URL: {remote_url}")

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Check Private Repo Mode policy (if enabled)
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


@app.route("/api/v1/gh/pr/create", methods=["POST"])
@require_session_auth
def gh_pr_create():
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
def gh_pr_comment():
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
def gh_pr_edit():
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
def gh_pr_close():
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
def gh_execute():
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
def worktree_create():
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
def worktree_delete():
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
def worktree_list():
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
def session_create():
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
    Rate limit: 10 registrations per minute per source IP
    """
    # Rate limit check
    rate_result = check_registration_rate_limit(request.remote_addr)
    if not rate_result.allowed:
        return make_error(
            "Rate limit exceeded for session registration",
            status_code=429,
            details={
                "retry_after_seconds": rate_result.retry_after_seconds,
            },
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

    # Validate required fields
    if not container_id:
        return make_error("Missing container_id")
    if not container_ip:
        return make_error("Missing container_ip")
    if mode not in ("private", "public"):
        return make_error("Invalid mode: must be 'private' or 'public'")
    if not repos:
        return make_error("Missing repos list")

    # Validate uid/gid if provided
    if uid is not None and (not isinstance(uid, int) or uid < 0):
        return make_error("Invalid uid: must be a non-negative integer")
    if gid is not None and (not isinstance(gid, int) or gid < 0):
        return make_error("Invalid gid: must be a non-negative integer")

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

    for repo in filtered_repos:
        # Extract repo name from owner/repo format
        if "/" in repo:
            repo_name = repo.split("/")[-1]
        else:
            repo_name = repo

        try:
            info = manager.create_worktree(
                repo_name=repo_name,
                container_id=container_id,
                base_branch="HEAD",
                uid=uid,
                gid=gid,
            )
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


@app.route("/api/v1/sessions/<session_token>", methods=["DELETE"])
@require_launcher_auth
def session_delete(session_token: str):
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

            audit_log(
                "session_deleted",
                "session_delete",
                success=True,
                details={
                    "container_id": container_id,
                    "worktrees_deleted": deleted_worktrees,
                },
            )
        else:
            audit_log(
                "session_deleted",
                "session_delete",
                success=True,
                details={"container_id": container_id},
            )

    return make_success("Session deleted")


@app.route("/api/v1/sessions/<session_token>/heartbeat", methods=["POST"])
@require_launcher_auth
def session_heartbeat(session_token: str):
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
        record_failed_lookup(request.remote_addr)
        return make_error(result.error, status_code=401)

    # Check heartbeat rate limit (100 per hour per session)
    if result.session:
        rate_limit = check_heartbeat_rate_limit(result.session.session_id)
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
def repos_visibility():
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
def sessions_list():
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


def _get_forwarded_headers(request_headers) -> dict[str, str]:
    """Forward all headers except blocked ones (blocklist approach)."""
    return {k: v for k, v in request_headers if k.lower() not in ANTHROPIC_BLOCKED_HEADERS}


def _filter_response_headers(headers) -> dict[str, str]:
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


@app.route("/v1/messages", methods=["POST"])
def proxy_anthropic_messages():
    """
    Proxy messages API with credential injection and streaming support.

    This endpoint allows Claude Code to use ANTHROPIC_BASE_URL to route
    API traffic through the gateway for credential injection.

    Uses IP-based session lookup for mode detection (Claude Code doesn't send session tokens).
    """
    # Build headers with injected auth
    headers = _get_forwarded_headers(request.headers)
    headers, error = _inject_anthropic_credentials(headers)
    if error:
        return error

    request_body = request.get_data()

    # Look up session by IP to determine mode (Claude Code doesn't send session tokens)
    session_manager = get_session_manager()
    session = session_manager.get_session_by_ip(request.remote_addr)
    session_mode = session.mode if session else None
    request_body = _filter_blocked_tools(
        request_body, session_mode
    )  # Remove web tools in private mode
    is_streaming = _is_streaming_request(request_body)

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

            def generate():
                try:
                    yield from upstream.iter_bytes()
                finally:
                    upstream.close()

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
def proxy_count_tokens():
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


def main():
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
            "  2. Ensure ExecStart points to the correct start-gateway.sh location\n"
            "  3. Restart the service: systemctl --user restart gateway\n"
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

    # Validate user mode config if configured
    github = get_github_client()
    is_valid, validation_msg = github.validate_user_mode_config()
    if not is_valid:
        logger.warning("User mode config validation failed", reason=validation_msg)
    else:
        logger.info("User mode config", status=validation_msg)

    # Clean up orphaned worktrees from crashed containers
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
        serve(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
