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
    POST /api/v1/jira/ticket/get          - Read Jira issue (policy: private-mode, project allowlist)
    POST /api/v1/jira/search              - JQL search (policy: private-mode, statically project-scoped)
    POST /api/v1/jira/ticket/comments     - Read Jira issue comments (policy: private-mode, project allowlist)
    POST /api/v1/jira/execute             - Generic read-only Jira REST call (policy: private-mode, allowlisted path)
    POST /api/v1/jira/ticket/create       - Create Jira issue (policy: private-mode, project allowlist; #1924)
    POST /api/v1/jira/ticket/edit         - Edit Jira issue (policy: private-mode, project allowlist; #1924)
    POST /api/v1/jira/ticket/comment/add  - Add Jira issue comment (policy: private-mode, project allowlist; #1924)
    POST /api/v1/jira/issue-link/create   - Link two Jira issues (policy: private-mode, both projects allowlisted; #1924)
    POST /api/v1/confluence/page/get             - Read Confluence page (policy: private-mode, space allowlist)
    POST /api/v1/confluence/page/descendants     - List page descendants (policy: private-mode, space allowlist)
    POST /api/v1/confluence/page/footer-comments - Read page footer comments (policy: private-mode, space allowlist)
    POST /api/v1/confluence/page/inline-comments - Read page inline comments (policy: private-mode, space allowlist)
    POST /api/v1/confluence/space/list           - List allowlisted spaces (policy: private-mode)
    POST /api/v1/confluence/space/pages          - List pages in a space (policy: private-mode, space allowlist)
    POST /api/v1/confluence/search               - CQL search (policy: private-mode, statically space-scoped)
    POST /api/v1/confluence/execute              - Generic read-only Confluence REST call (policy: private-mode, allowlisted path)
    GET  /api/v1/health         - Health check (no auth required)

Usage:
    gateway.py [--host HOST] [--port PORT] [--debug]
"""

import argparse
import codecs
import functools
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from flask import Flask, Response, g, has_request_context, jsonify, request, stream_with_context
from waitress import serve

# Add shared directory to path for egg_logging
# In container, egg_logging is at /app/egg_logging
# On host, it's at ../../shared/egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_health import HealthTracker
from egg_logging import get_logger
from egg_restrictions.hints import derive_hint as _derive_push_denied_hint

# Module-level health tracker. Updated every time the /api/v1/health
# endpoint is evaluated so callers can distinguish "healthy since process
# start" from "just came up / recent flapping" (see issue #1855).
_health_tracker = HealthTracker()

# Import gateway modules - try relative import first (module mode),
# fall back to absolute import (standalone script mode in container)
try:
    from .agent_restrictions import (
        check_agent_gh_operation,
        get_agent_pattern,  # noqa: F401 — re-exported for test patching
    )
    from .anthropic_credentials import get_credentials_manager
    from .checkpoint_handler import (
        _get_checkpoint_repo_for_path,
        capture_and_store_checkpoint,
        capture_and_store_checkpoints_for_push,
        get_checkpoint_handler,
    )
    from .confluence_client import (
        DEFAULT_LIMIT as CONFLUENCE_DEFAULT_LIMIT,
    )
    from .confluence_client import (
        HARD_MAX_LIMIT as CONFLUENCE_HARD_MAX_LIMIT,
    )
    from .confluence_client import (
        ConfluenceCredentialsUnavailable,
        ConfluenceResponseTooLarge,
        ConfluenceUpstreamError,
        ConfluenceUpstreamForbidden,
        get_confluence_client,
        redact_response,
        validate_confluence_api_path,
    )
    from .confluence_credentials import reload_confluence_credentials
    from .confluence_policy import (
        allowed_spaces as confluence_allowed_spaces,
    )
    from .confluence_policy import (
        is_space_allowed as is_confluence_space_allowed,
    )
    from .confluence_policy import (
        reload_confluence_policy,
    )
    from .confluence_search import extract_search_spaces
    from .git_client import (
        GIT_ALLOWED_COMMANDS,
        cleanup_credential_helper,
        create_credential_helper,
        extract_reset_target_ref,
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
        GitHubClient,
        extract_comment_edit_info,
        extract_issue_label_info,
        extract_pr_review_info,
        extract_pr_reviewer_info,
        extract_repo_from_gh_command,
        get_github_client,
        parse_gh_api_args,
        resolve_gh_api_template_variables,
        validate_gh_api_path,
    )
    from .jira_client import (
        JiraCredentialsUnavailable,
        JiraUpstreamError,
        get_jira_client,
        validate_jira_api_path,
    )
    from .jira_client import (
        validate_fields as validate_jira_fields,
    )
    from .jira_credentials import reload_jira_credentials
    from .jira_policy import (
        epic_link_field as jira_epic_link_field,
    )
    from .jira_policy import (
        extract_project_key,
        is_project_allowed,
        reload_jira_policy,
    )
    from .jira_policy import (
        link_type_allowed as jira_link_type_allowed,
    )
    from .jira_search import extract_search_projects
    from .mode_gate import require_private_mode
    from .phase_filter import (
        OperationType,
        PipelinePhase,
        check_agent_restrictions,  # noqa: F401 — re-exported for test patching
        check_anchor_write_permission,
        check_phase_file_restrictions,
        filter_operation,
    )
    from .policy import (
        extract_branch_from_refspec,
        extract_repo_from_remote,
        get_policy_engine,
        reload_policy_caches,
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
    from .worktree_manager import (
        REPOS_BASE_DIR,
        WORKTREE_BASE_DIR,
        WorktreeManager,
        get_active_docker_containers,
        startup_cleanup,
        validate_identifier,
    )
except ImportError:
    from agent_restrictions import (  # type: ignore[no-redef, import-untyped]
        check_agent_gh_operation,
        get_agent_pattern,  # noqa: F401 — re-exported for test patching
    )
    from anthropic_credentials import get_credentials_manager  # type: ignore[no-redef]
    from checkpoint_handler import (  # type: ignore[no-redef, import-untyped]
        _get_checkpoint_repo_for_path,
        capture_and_store_checkpoint,
        capture_and_store_checkpoints_for_push,
        get_checkpoint_handler,
    )
    from git_client import (  # type: ignore[no-redef, import-untyped]
        GIT_ALLOWED_COMMANDS,
        cleanup_credential_helper,
        create_credential_helper,
        extract_reset_target_ref,
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
    from github_client import (  # type: ignore[no-redef, import-untyped]
        BLOCKED_GH_COMMANDS,
        GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE,
        READONLY_GH_COMMANDS,
        GitHubClient,
        extract_comment_edit_info,
        extract_issue_label_info,
        extract_pr_review_info,
        extract_pr_reviewer_info,
        extract_repo_from_gh_command,
        get_github_client,
        parse_gh_api_args,
        resolve_gh_api_template_variables,
        validate_gh_api_path,
    )

    # The Jira modules are new in issue #1556 and the flat-module test
    # conftest does not yet preload them.  Make the gateway directory
    # discoverable before the fallback import so standalone / test loading
    # still finds jira_client, jira_credentials, jira_policy, jira_search,
    # and mode_gate by name.  In production (package import), the relative
    # ``from .jira_client import ...`` path above succeeds and this branch
    # never runs.
    _egg_gateway_dir = str(Path(__file__).parent)
    if _egg_gateway_dir not in sys.path:
        sys.path.insert(0, _egg_gateway_dir)
    from confluence_client import (  # type: ignore[no-redef, import-untyped]
        DEFAULT_LIMIT as CONFLUENCE_DEFAULT_LIMIT,
    )
    from confluence_client import (  # type: ignore[no-redef]
        HARD_MAX_LIMIT as CONFLUENCE_HARD_MAX_LIMIT,
    )
    from confluence_client import (  # type: ignore[no-redef]
        ConfluenceCredentialsUnavailable,
        ConfluenceResponseTooLarge,
        ConfluenceUpstreamError,
        ConfluenceUpstreamForbidden,
        get_confluence_client,
        redact_response,
        validate_confluence_api_path,
    )
    from confluence_credentials import (  # type: ignore[no-redef, import-untyped]
        reload_confluence_credentials,
    )
    from confluence_policy import (  # type: ignore[no-redef, import-untyped]
        allowed_spaces as confluence_allowed_spaces,
    )
    from confluence_policy import (  # type: ignore[no-redef]
        is_space_allowed as is_confluence_space_allowed,
    )
    from confluence_policy import (  # type: ignore[no-redef]
        reload_confluence_policy,
    )
    from confluence_search import (  # type: ignore[no-redef, import-untyped]
        extract_search_spaces,
    )
    from jira_client import (  # type: ignore[no-redef, import-untyped]
        JiraCredentialsUnavailable,
        JiraUpstreamError,
        get_jira_client,
        validate_jira_api_path,
    )
    from jira_client import (  # type: ignore[no-redef]
        validate_fields as validate_jira_fields,
    )
    from jira_credentials import (  # type: ignore[no-redef, import-untyped]
        reload_jira_credentials,
    )
    from jira_policy import (  # type: ignore[no-redef, import-untyped]
        epic_link_field as jira_epic_link_field,
    )
    from jira_policy import (  # type: ignore[no-redef]
        extract_project_key,
        is_project_allowed,
        reload_jira_policy,
    )
    from jira_policy import (  # type: ignore[no-redef]
        link_type_allowed as jira_link_type_allowed,
    )
    from jira_search import (  # type: ignore[no-redef, import-untyped]
        extract_search_projects,
    )
    from mode_gate import require_private_mode  # type: ignore[no-redef, import-untyped]
    from phase_filter import (  # type: ignore[no-redef, import-untyped]
        OperationType,
        PipelinePhase,
        check_agent_restrictions,  # noqa: F401 — re-exported for test patching
        check_anchor_write_permission,
        check_phase_file_restrictions,
        filter_operation,
    )
    from policy import (  # type: ignore[no-redef, import-untyped]
        extract_branch_from_refspec,
        extract_repo_from_remote,
        get_policy_engine,
        reload_policy_caches,
    )
    from private_repo_policy import (  # type: ignore[no-redef]
        check_private_repo_access,
    )
    from rate_limiter import (  # type: ignore[no-redef, import-untyped]
        check_heartbeat_rate_limit,
        record_failed_lookup,
    )
    from repo_parser import parse_owner_repo  # type: ignore[no-redef, import-untyped]
    from repo_visibility import get_repo_visibility  # type: ignore[no-redef]
    from session_manager import (  # type: ignore[no-redef, import-untyped]
        get_session_manager,
        validate_session_for_request,
    )
    from transcript_buffer import get_transcript_buffer  # type: ignore[no-redef, import-untyped]
    from worktree_manager import (  # type: ignore[no-redef, import-untyped]
        REPOS_BASE_DIR,
        WORKTREE_BASE_DIR,
        WorktreeManager,
        get_active_docker_containers,
        startup_cleanup,
        validate_identifier,
    )

# Import repo_config for user mode support
# Path setup needed because config is in a sibling directory
_config_path = Path(__file__).parent.parent / "config"
if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))
from repo_config import get_auth_mode, get_checkpoint_repo, is_checkpoint_repo

logger = get_logger("gateway")


try:
    # Production / package mode.
    from ._module_loader import load_sibling_gateway_module as _load_sibling_gateway_module
except ImportError:
    # Standalone-script mode (the test conftest loads gateway.py as
    # a flat top-level module, in which case the relative import
    # above raises ImportError before sys.modules has been seeded).
    from _module_loader import (  # type: ignore[no-redef, import-untyped]
        load_sibling_gateway_module as _load_sibling_gateway_module,
    )


def _lookup_commit_observer_fn(name: str) -> Any:
    """Return a callable from ``commit_observer`` without relative imports."""
    mod = _load_sibling_gateway_module("commit_observer")
    if mod is None:
        return None
    return getattr(mod, name, None)


def _detached_head_hint(
    operation: str,
    exec_path: str,
    repo_path: str,
    container_id: str | None,
) -> str:
    """Return a recovery hint string when a `commit` lands on detached HEAD.

    Used by the git-execute handler to surface the exact ``update-ref``
    invocation an agent needs to set its work branch to the new commit
    (issue #2162).  The empty string means "no hint" — caller appends as-is.

    The trigger is intentionally narrow:

    * Only ``operation == "commit"`` and only when the session has an
      ``assigned_branch`` — we do not want to noise non-pipeline sessions.
    * ``git symbolic-ref --quiet HEAD`` must return exactly 1 with empty
      stdout AND empty stderr.  Returncode 128 (corrupt repo, .git missing,
      "fatal: ...") and any non-empty stderr are treated as ambiguous and
      yield no hint — telling the agent to run ``update-ref`` against a
      broken repository would be misleading.
    """
    if operation != "commit":
        return ""
    session = getattr(g, "session", None)
    assigned = getattr(session, "assigned_branch", None) if session else None
    if not isinstance(assigned, str) or not assigned:
        return ""
    try:
        head_check = subprocess.run(
            git_cmd("symbolic-ref", "--quiet", "HEAD"),
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    # Tight check: returncode 1 with no stdout and no stderr is unambiguously
    # detached HEAD.  Anything else (corrupt repo, missing .git, EAGAIN) gets
    # no hint.
    if head_check.returncode != 1:
        return ""
    if head_check.stdout.strip():
        return ""
    if head_check.stderr.strip():
        # Symbolic-ref returncode==1 with empty stdout but non-empty stderr is
        # ambiguous (e.g. future git versions writing config-deprecation
        # warnings).  Log at debug so a missing hint is debuggable rather than
        # silent, and bail out — telling the agent to run update-ref against
        # an unclear HEAD state would be misleading.
        logger.debug(
            "detached_head_hint_suppressed_stderr",
            repo_path=repo_path,
            container_id=container_id,
            assigned_branch=assigned,
            stderr=head_check.stderr.strip()[:200],
        )
        return ""
    logger.info(
        "detached_head_commit_hint",
        repo_path=repo_path,
        container_id=container_id,
        assigned_branch=assigned,
    )
    return (
        f"\n[gateway] HEAD is detached. Your commit is not on "
        f"branch '{assigned}'. To set the branch to this commit, run:\n"
        f"  git update-ref refs/heads/{assigned} HEAD\n"
    )


def _is_checkpoint_repo_for_request(owner: str, repo: str) -> bool:
    """Check if a repository is a checkpoint repo, using all available signals.

    Extends ``is_checkpoint_repo()`` (config-based) with session-level
    context.  The session's ``checkpoint_repo`` field is set during session
    creation and on git push, so it captures repos that may not appear in
    ``repositories.yaml`` (e.g. when the config file is absent in sandboxes
    but the orchestrator passed ``EGG_CHECKPOINT_REPO``).

    Args:
        owner: Repository owner (e.g. "my-org")
        repo: Repository name (e.g. "checkpoints")

    Returns:
        True if the repo is a known checkpoint destination.
    """
    if is_checkpoint_repo(owner, repo):
        return True
    try:
        session = getattr(g, "session", None)
        if session and session.checkpoint_repo:
            return bool(f"{owner}/{repo}".lower() == session.checkpoint_repo.lower())
    except RuntimeError:
        # Outside Flask request context — fall through
        pass
    return False


app = Flask(__name__)

# Register contract API blueprint
try:
    from .contract_api import contract_bp

    app.register_blueprint(contract_bp)
except ImportError:
    from contract_api import contract_bp  # type: ignore[import-untyped, no-redef]

    app.register_blueprint(contract_bp)

# Register phase API blueprint
try:
    from .phase_api import phase_bp

    app.register_blueprint(phase_bp)
except ImportError:
    from phase_api import phase_bp  # type: ignore[import-untyped, no-redef]

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
DEFAULT_THREADS = int(os.environ.get("GATEWAY_THREADS", "32"))
HEALTH_CHECK_PORT = int(os.environ.get("GATEWAY_HEALTH_PORT", "9851"))

# Host home directory for path translation (explicit override).
# The gateway container uses /home/egg internally, but needs to return
# host paths to the orchestrator because those paths become the
# ``hostPath.path`` source of agent-pod mounts — if the gateway returns
# its in-pod path and the host layout doesn't match, kubelet
# ``DirectoryOrCreate``s an empty root-owned dir and the agent lands in
# an unwritable worktree (#1986).
#
# Normally we discover the host path directly from /proc/self/mountinfo
# (see ``translate_to_host_path``) so no env-var configuration is
# required.  ``HOST_HOME`` is the escape hatch for environments where
# mountinfo doesn't reflect the real host layout (e.g. multi-partition
# setups, or an operator who wants to override the discovered value).
# Set ``EGG_DISABLE_MOUNTINFO=1`` to skip mountinfo entirely and force
# the ``HOST_HOME`` path — needed because real Linux containers always
# expose a rootfs ``/ → /`` entry that matches every path under longest-
# prefix lookup, so without the disable flag the env-var fallback is
# unreachable.
HOST_HOME = os.environ.get("HOST_HOME", "")
CONTAINER_HOME = "/home/egg"


def _mountinfo_disabled() -> bool:
    return os.environ.get("EGG_DISABLE_MOUNTINFO", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _load_mount_mapping() -> list[tuple[str, str]]:
    """Read /proc/self/mountinfo and return a list of (mount_point, host_root) tuples.

    For every mount visible to this process, ``mount_point`` is the path
    in this process's mount namespace and ``host_root`` is the path the
    kernel recorded as the mount root — for kubelet-managed ``hostPath``
    volumes that's the actual host path.  The list includes *all* mount
    types (not just bind mounts); longest-prefix matching in
    ``translate_to_host_path`` ensures the most specific entry wins.

    Note: ``host_root`` (``fields[3]``, the mountinfo *root* field) is
    the path relative to the filesystem's root.  On single-partition
    systems this equals the absolute host path; on multi-partition setups
    it may be relative to the partition root.  The ``HOST_HOME`` env var
    is the escape hatch for those configurations.

    Note: mountinfo uses octal escapes for special characters in paths
    (``\\040`` for space, ``\\011`` for tab, ``\\134`` for backslash).
    We don't decode them — unlikely to matter for ``/home/...`` paths
    but worth knowing if paths ever contain whitespace.
    """
    entries: list[tuple[str, str]] = []
    if _mountinfo_disabled():
        return entries
    try:
        with open("/proc/self/mountinfo") as fh:
            for line in fh:
                # Format: mount_id parent_id major:minor root mount_point ...
                fields = line.split()
                if len(fields) < 5:
                    continue
                entries.append((fields[4], fields[3]))
    except OSError:
        return []
    entries.sort(key=lambda p: len(p[0]), reverse=True)
    return entries


_MOUNT_MAPPING: list[tuple[str, str]] = _load_mount_mapping()


def translate_to_host_path(container_path: str) -> str:
    """
    Translate a container path to the corresponding host path.

    Tries in order:
    1. /proc/self/mountinfo — find the longest mount_point that is a
       prefix of ``container_path`` and substitute with its host root.
       This works for any hostPath volume without configuration. Real
       Linux containers always include a rootfs ``/ → /`` entry, so
       this strategy is reachable unless explicitly disabled.
    2. ``HOST_HOME`` env var — explicit override. To reach this branch
       on Linux, set ``EGG_DISABLE_MOUNTINFO=1`` to skip the mountinfo
       lookup (otherwise the ``/`` entry always matches first).

    Args:
        container_path: Path inside the gateway container

    Returns:
        The corresponding host path, or the original path if no
        translation is possible.
    """
    for mount_point, host_root in _MOUNT_MAPPING:
        if container_path == mount_point or container_path.startswith(mount_point + "/"):
            return host_root + container_path[len(mount_point) :]

    if HOST_HOME and container_path.startswith(CONTAINER_HOME):
        return container_path.replace(CONTAINER_HOME, HOST_HOME, 1)

    return container_path


# Import session auth decorator from auth module to avoid circular imports
try:
    from .auth import require_session_auth
except ImportError:
    from auth import require_session_auth  # type: ignore[no-redef, import-untyped]


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


def require_launcher_auth[F: Callable[..., Any]](f: F) -> F:
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


def require_session_or_launcher_auth[F: Callable[..., Any]](f: F) -> F:
    """Endpoint accepts either a session token or the launcher secret.

    When the Authorization bearer matches the launcher secret, the request
    is treated as orchestrator-originated: ``g.session`` is left ``None``
    and ``g.auth_actor`` is set to ``"launcher"``.  Otherwise the request
    falls through to ``require_session_auth`` (sandbox/agent path), which
    sets ``g.session`` and ``g.session_mode``/``g.session_phase``; this
    wrapper then sets ``g.auth_actor = "session"``.

    The trust split is grounded in the launcher secret already used by
    ``/api/v1/sessions/create`` and other privileged endpoints — only the
    orchestrator holds it (mounted at ``/secrets/launcher-secret``), so a
    request that authenticates with it is by definition not coming from
    a sandboxed agent.

    Used by ``/api/v1/git/push`` so the orchestrator can run its own
    failsafe pushes (contract init, state-sync, completion) without the
    register-session/push/delete ceremony, and without tripping the
    pipeline-push block (#2028) intended for agents.
    """

    # Build the session-auth fallback once at decoration time so we don't
    # re-create the wrapper closure on every request.
    @require_session_auth
    def _session_path(*args: Any, **kwargs: Any) -> Any:
        g.auth_actor = "session"
        return f(*args, **kwargs)

    @functools.wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                launcher_secret = get_launcher_secret()
            except LauncherSecretNotConfiguredError:
                launcher_secret = ""
            if launcher_secret and secrets.compare_digest(auth_header[7:], launcher_secret):
                g.session = None
                g.session_mode = None
                g.session_phase = None
                g.auth_actor = "launcher"
                return f(*args, **kwargs)

        # Fall through to session-token validation.  The session decorator
        # sets g.session/g.session_mode/g.session_phase on success and
        # returns 401 on failure.
        return _session_path(*args, **kwargs)

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


def make_worktree_not_found_error(container_id: str) -> tuple[Response, int]:
    """Return a 500 error when a container's worktree cannot be found.

    This prevents the silent fallback to the main repo that caused #1497:
    agents could not see their own file changes because git ran against
    the main repo instead of the agent's worktree.
    """
    return make_error(
        f"Worktree not found for container '{container_id}'. "
        "The per-agent worktree may not have been created. "
        "Git operations require a valid worktree.",
        status_code=500,
    )


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


def _check_squid_health() -> dict[str, Any]:
    """Check if Squid proxy is running and listening on port 3129.

    Returns a dict with squid health info:
        running: bool - True if the squid process is alive
        listening: bool - True if port 3129 is accepting connections
    """
    result: dict[str, Any] = {"running": False, "listening": False}

    # Check if squid process is running (not zombie)
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "squid"],
            capture_output=True,
            timeout=5,
        )
        result["running"] = proc.returncode == 0
    except subprocess.TimeoutExpired, FileNotFoundError:
        pass

    # Check if squid is actually accepting connections on port 3129.
    # We use a direct TCP connect instead of 'squid -k check' because the
    # latter re-parses squid.conf and fails when run as non-root (can't read
    # the SSL private key), even though Squid itself is running fine.
    try:
        with socket.create_connection(("127.0.0.1", 3129), timeout=2):
            result["listening"] = True
    except OSError:
        pass

    return result


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

    # Check Squid proxy health
    squid_status = _check_squid_health()

    # Gateway always runs with locked Squid.
    # Per-container mode is enforced at container start via network selection.
    # - Private containers: isolated network + proxy (locked to api.anthropic.com)
    # - Public containers: external network + direct internet (no proxy)
    #
    # Status is "degraded" if Squid is down - private containers will be unable
    # to reach the internet. Previously invisible because health check only
    # verified the Python gateway (port 9848), not Squid (port 3129).
    # See: https://github.com/jwbron/egg/issues/1387
    is_healthy = token_valid and launcher_secret_configured and squid_status["listening"]

    # Record this observation so the snapshot can expose transitions (see #1855).
    _health_tracker.record(is_healthy)
    tracker_snapshot = _health_tracker.snapshot()

    response_data: dict[str, Any] = {
        "status": "healthy" if is_healthy else "degraded",
        "github_token_valid": token_valid,
        "auth_configured": launcher_secret_configured,
        "squid_proxy": squid_status,
        "active_sessions": active_sessions,
        "service": "gateway",
        "client_ip": request.remote_addr,
        "process_start_time": tracker_snapshot["process_start_time"],
        "healthy_since": tracker_snapshot["healthy_since"],
        "last_unhealthy_at": tracker_snapshot["last_unhealthy_at"],
        "recent_transitions": tracker_snapshot["recent_transitions"],
    }

    # Include orchestrator status if configured
    if orchestrator_status.get("configured"):
        response_data["orchestrator"] = orchestrator_status

    return jsonify(response_data)


def _reload_all_config() -> None:
    """Reload all cached configuration from disk/environment.

    Called by the SIGHUP handler and the /api/v1/config/reload endpoint.

    Thread safety: all cached values are immutable types (frozenset, tuple,
    None) and global variable assignment is atomic under CPython's GIL, so
    concurrent readers see either the old or new value, never a torn state.
    Avoid replacing any cache with a mutable type (e.g. dict) without adding
    synchronisation.
    """
    try:
        from config.repo_config import reload_config as reload_repo_config
    except ImportError:
        try:
            from repo_config import reload_config as reload_repo_config  # type: ignore[no-redef]
        except ImportError:
            reload_repo_config = None  # type: ignore[assignment]

    if reload_repo_config is not None:
        try:
            reload_repo_config()
        finally:
            reload_policy_caches()
        logger.info("Configuration reloaded")
    else:
        reload_policy_caches()
        logger.warning("Policy caches reloaded (repo_config unavailable)")

    # Jira credentials + project allowlist — both sit on disk next to the
    # other gateway config, so a single ``POST /api/v1/config/reload`` should
    # refresh them alongside the GitHub policy caches.  Failing the Jira
    # reload must not tank the endpoint (operators may be running without
    # Jira configured), so we log and continue.
    try:
        reload_jira_credentials()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Jira credentials reload failed")
    try:
        reload_jira_policy()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Jira project allowlist reload failed")
    # ``_reload_all_config`` is reachable from two call sites: (a) the
    # ``POST /api/v1/config/reload`` endpoint, which runs inside a Flask
    # request; and (b) the SIGHUP handler, which does NOT.  ``audit_log``
    # dereferences ``request.remote_addr`` so calling it outside a request
    # raises ``RuntimeError: Working outside of request context``.  Gate
    # the audit on ``has_request_context`` so HTTP reloads still audit and
    # SIGHUP falls back to a bare logger line.
    if has_request_context():
        audit_log(
            "jira_config_reloaded",
            "config_reload",
            success=True,
            details={"components": ["jira_credentials", "jira_policy"]},
        )
    else:
        logger.info(
            "Jira configuration reloaded",
            components=["jira_credentials", "jira_policy"],
            trigger="sighup",
        )

    # Confluence credentials + space allowlist — same disk-cache pattern as
    # Jira.  The Confluence allowlist lives under the ``confluence:`` section
    # of context-filters.yaml; credentials share the secrets.env file.
    try:
        reload_confluence_credentials()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Confluence credentials reload failed")
    try:
        reload_confluence_policy()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Confluence space allowlist reload failed")
    if has_request_context():
        audit_log(
            "confluence_config_reloaded",
            "config_reload",
            success=True,
            details={"components": ["confluence_credentials", "confluence_policy"]},
        )
    else:
        logger.info(
            "Confluence configuration reloaded",
            components=["confluence_credentials", "confluence_policy"],
            trigger="sighup",
        )


@app.route("/api/v1/config/reload", methods=["POST"])
@require_launcher_auth
def config_reload() -> Response:
    """Reload configuration from disk.

    Clears all in-memory config caches so the next access re-reads from
    repositories.yaml and environment variables. Requires launcher auth.
    """
    _reload_all_config()
    return jsonify({"status": "ok", "message": "Configuration reloaded"})


# Slice integration-branch shape for the synthetic-session exemption (#2368).
# Matches ``egg/<base>/(slice|phase)-<digits>`` where ``<base>`` is a single
# segment naming the parent pipeline branch (issue-driven, JIRA-driven, or
# qualifier-suffixed) — multi-segment bases are never produced by the
# orchestrator, so the second character class excludes ``/``.  Only
# orchestrator-issued sessions can ever set ``synthetic=True`` (the launcher
# secret gates ``/api/v1/sessions/create``), so this exemption is not reachable
# from a sandboxed agent's session token.
_SLICE_INTEGRATION_BRANCH_RE = re.compile(r"^egg/[A-Za-z0-9][A-Za-z0-9_-]*/(?:slice|phase)-\d+$")


@app.route("/api/v1/git/push", methods=["POST"])
@require_session_or_launcher_auth
def git_push() -> tuple[Response, int] | Response:
    """
    Handle git push requests.

    Request body:
        {
            "repo_path": "/path/to/repo",
            "remote": "origin",
            "refspec": "branch-name",
            "force": false,
            "force_with_lease": false,  # safer alternative to force
            "commit_sha": "<40-hex>",   # alternative to refspec; consensus pushes only
        }

    ``force_with_lease`` (#2137 stacked-PR reconciler) is preferred over
    ``force`` for non-fast-forward pushes. Both flags are mutually
    exclusive — ``force_with_lease`` takes precedence if both are set.

    Policy: branch_ownership
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo_path = data.get("repo_path")
    remote = data.get("remote", "origin")
    refspec = data.get("refspec", "")
    force = data.get("force", False)
    force_with_lease = data.get("force_with_lease", False)
    container_id = data.get("container_id")
    commit_sha = data.get("commit_sha", "")

    if not repo_path:
        return make_error("Missing repo_path")

    # Detached-HEAD-tolerant consensus push (#2200): when the agent's HEAD
    # is detached (post-rebase or otherwise), the helper cannot read
    # ``git branch --show-current`` and instead supplies ``commit_sha``.
    # The gateway derives the refspec server-side from the session's
    # assigned branch.  This is strictly tighter than an agent-supplied
    # refspec because the existing ``push_target_enforcement`` block
    # below already requires ``branch == session.assigned_branch``.
    if commit_sha and not refspec:
        if not data.get("consensus_push"):
            audit_log(
                "push_blocked",
                "git_push",
                success=False,
                details={
                    "repo_path": repo_path,
                    "reason": "commit_sha push requires consensus_push=true",
                },
            )
            return make_error(
                "commit_sha push requires consensus_push=true",
                status_code=400,
            )
        # Require a full SHA (40 = SHA-1, 64 = SHA-256). Abbreviated SHAs
        # (7-39 chars) can resolve ambiguously on the gateway side; the
        # helper always emits the full output of ``git rev-parse HEAD`` so
        # there is no legitimate caller of the shorter range. The explicit
        # ``isinstance`` guard turns a non-string payload into a clean 400
        # rather than a 500 from ``re.fullmatch``.
        if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit_sha):
            audit_log(
                "push_blocked",
                "git_push",
                success=False,
                details={
                    "repo_path": repo_path,
                    "reason": f"Invalid commit_sha {commit_sha!r}",
                },
            )
            return make_error(
                f"Invalid commit_sha {commit_sha!r}: must be 40-64 hex chars",
                status_code=400,
            )
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        if not isinstance(assigned, str) or not assigned:
            audit_log(
                "push_blocked",
                "git_push",
                success=False,
                details={
                    "repo_path": repo_path,
                    "reason": "commit_sha push requires a pipeline session with assigned_branch",
                },
            )
            return make_error(
                "commit_sha push requires a pipeline session with an assigned branch",
                status_code=400,
            )
        refspec = f"{commit_sha}:refs/heads/{assigned}"
        # Distinct audit event so post-incident review can distinguish a
        # gateway-constructed refspec (commit_sha path) from an
        # agent-supplied refspec; both flow through the same downstream
        # ``push_*`` audit events and would otherwise be indistinguishable.
        audit_log(
            "push_via_commit_sha",
            "git_push",
            success=True,
            details={
                "repo_path": repo_path,
                "commit_sha": commit_sha,
                "assigned_branch": assigned,
                "constructed_refspec": refspec,
            },
        )

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
    if exec_path is None:
        return make_worktree_not_found_error(container_id)

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

    # Orchestrator-authenticated push (launcher secret).  The orchestrator
    # has a different trust boundary than sandboxed agents — its pushes are
    # programmatic (contract init, state-sync, completion) and bypass the
    # session-derived enforcement (pipeline-push block, push-target check,
    # role/phase file restrictions) that exists to sandbox agent commits.
    # session_mode comes from the request body since there is no session.
    is_orchestrator_push = getattr(g, "auth_actor", None) == "launcher"
    if is_orchestrator_push:
        mode_in = data.get("mode")
        if mode_in is not None and mode_in not in ("public", "private"):
            audit_log(
                "push_blocked",
                "git_push",
                success=False,
                details={
                    "repo_path": repo_path,
                    "reason": f"Invalid mode {mode_in!r} on launcher-auth push",
                },
            )
            return make_error(
                f"Invalid mode {mode_in!r}: must be 'public' or 'private'",
                status_code=400,
            )
        session_mode = mode_in or session_mode
        audit_log(
            "push_orchestrator_authenticated",
            "git_push",
            success=True,
            details={
                "repo_path": repo_path,
                "remote": remote,
                "refspec": refspec,
                "reason": "Push authenticated with launcher secret — orchestrator-trusted",
            },
        )

    # Infrastructure branch bypass: pushes to infrastructure branches always succeed
    # regardless of session mode or phase (checkpoints and pipeline state can be
    # written at any time).
    from egg_config.constants import CHECKPOINT_BRANCH, PIPELINE_STATE_BRANCH

    INFRASTRUCTURE_BRANCHES = {CHECKPOINT_BRANCH, PIPELINE_STATE_BRANCH}
    is_infrastructure_push = branch in INFRASTRUCTURE_BRANCHES

    # Slice integration-branch creation (#2368): the orchestrator pre-creates
    # ``egg/<base>/(slice|phase)-N`` on origin from the parent branch via a
    # synthetic, launcher-authenticated session before any agent runs.  That
    # push is orchestrator infrastructure — not an agent BRC propose — so it
    # must bypass the pipeline-session push block introduced in #2028.  The
    # ``synthetic=True`` flag can only be set by the launcher (the
    # ``/api/v1/sessions/create`` endpoint is gated by ``require_launcher_auth``),
    # so a sandboxed agent's session token cannot reach this branch.
    is_slice_integration_push = False
    if not is_infrastructure_push and _SLICE_INTEGRATION_BRANCH_RE.match(branch):
        # ``Session.synthetic`` is a ``bool`` (default ``False``); only an
        # orchestrator-issued session can carry ``synthetic=True`` because
        # ``/api/v1/sessions/create`` is gated on the launcher secret.  Use
        # an identity check rather than a truthiness test so a future
        # surface that ever stores something other than ``True`` (and any
        # MagicMock fake whose default attr is truthy) cannot accidentally
        # opt into the exemption.
        if hasattr(g, "session") and getattr(g.session, "synthetic", False) is True:
            is_slice_integration_push = True
            is_infrastructure_push = True
            audit_log(
                "push_slice_integration_exempt",
                "git_push",
                success=True,
                details={
                    "repo_path": repo_path,
                    "remote": remote,
                    "refspec": refspec,
                    "branch": branch,
                    "reason": (
                        "Synthetic-session slice integration branch push — "
                        "orchestrator infrastructure (#2368)"
                    ),
                },
            )

    repo_info = parse_owner_repo(repo)
    if repo_info:
        # Infrastructure operations — always accessible regardless of
        # session mode. This covers dedicated checkpoint repos and
        # infrastructure branch pushes (checkpoints, pipeline state).
        is_ckpt_repo = _is_checkpoint_repo_for_request(repo_info.owner, repo_info.repo)
        if is_infrastructure_push or is_ckpt_repo:
            if is_ckpt_repo:
                exempt_type = "checkpoint_repo"
            elif is_slice_integration_push:
                exempt_type = "slice_integration_branch"
            else:
                exempt_type = "infrastructure_branch"
            # A successful slice-integration push intentionally emits BOTH
            # ``push_slice_integration_exempt`` (above, the orchestrator-
            # specific event) AND ``push_infrastructure_exempt`` with
            # ``exempt_type="slice_integration_branch"`` (here, the generic
            # exemption event).  Operators grepping ``push_infrastructure_exempt``
            # for "infra pushes" should filter out the slice variant via
            # ``exempt_type``; the dual emission is intentional so the
            # orchestrator-specific path is also visible to operators
            # filtering on the slice-integration event name (#2370 review).
            audit_log(
                "push_infrastructure_exempt",
                "git_push",
                success=True,
                details={
                    "repo": repo,
                    "branch": branch,
                    "reason": "Infrastructure operation exempt from private mode policy",
                    "exempt_type": exempt_type,
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

    # SECURITY: Pipeline push enforcement.
    # All SDLC producer phases (refine/plan/implement) are BRC phases, so every
    # pipeline-session push must route through mcp__brc__propose (which sets the
    # consensus_push marker).  A direct git push from a pipeline session — whether
    # bare, mis-targeted, or correctly-targeted — is rejected with a single
    # unambiguous error pointing at the right tool, instead of the three-layer
    # error cascade that previously sent agents refspec-hunting (#2028).
    # Infrastructure pushes (checkpoint branches, etc.) are exempt.
    if not is_infrastructure_push:
        # Killswitch: PIPELINE_PUSH_ENFORCEMENT=false (legacy alias:
        # CONCURRENT_PUSH_ENFORCEMENT=false) disables the block.
        enforcement_env = os.environ.get(
            "PIPELINE_PUSH_ENFORCEMENT",
            os.environ.get("CONCURRENT_PUSH_ENFORCEMENT", "true"),
        )
        pipeline_push_enforcement = enforcement_env.lower() not in ("false", "0", "no")
        if pipeline_push_enforcement:
            session_pipeline_id = None
            if hasattr(g, "session") and g.session:
                session_pipeline_id = getattr(g.session, "pipeline_id", None)
            if isinstance(session_pipeline_id, str) and session_pipeline_id:
                if not data.get("consensus_push"):
                    audit_log(
                        "push_denied_pipeline_session",
                        "git_push",
                        success=False,
                        details={
                            "repo": repo,
                            "branch": branch,
                            "pipeline_id": session_pipeline_id,
                            "reason": "Direct push blocked for pipeline session",
                        },
                    )
                    return make_error(
                        "Direct git push is blocked for pipeline sessions. "
                        "Publish your artifact via the mcp__brc__propose tool "
                        "(which pushes to origin and sends CONSENSUS_PROPOSE "
                        "in one step). Fallback CLI: "
                        "`egg-orch consensus propose --push`.",
                        status_code=403,
                        details={
                            "pipeline_id": session_pipeline_id,
                            "requirement": "consensus_push",
                            "recommended_tool": "mcp__brc__propose",
                        },
                    )

        # Push-target enforcement: a consensus_push request must still target the
        # session's assigned branch.  Defense-in-depth against a malformed propose
        # call (consensus_push=true but wrong refspec).  Non-pipeline sessions
        # (e.g. user-mode pushes) are not subject to this check.
        # Killswitch: PUSH_TARGET_ENFORCEMENT=false.
        push_target_enforcement = os.environ.get("PUSH_TARGET_ENFORCEMENT", "true").lower() not in (
            "false",
            "0",
            "no",
        )
        if push_target_enforcement and hasattr(g, "session") and g.session:
            session_pipeline_id = getattr(g.session, "pipeline_id", None)
            session_assigned_branch = getattr(g.session, "assigned_branch", None)
            if isinstance(session_pipeline_id, str) and isinstance(session_assigned_branch, str):
                if branch != session_assigned_branch:
                    audit_log(
                        "push_denied_wrong_branch",
                        "git_push",
                        success=False,
                        details={
                            "repo": repo,
                            "branch": branch,
                            "assigned_branch": session_assigned_branch,
                            "pipeline_id": session_pipeline_id,
                        },
                    )
                    return make_error(
                        f"Pipeline sessions must push to their assigned branch "
                        f"'{session_assigned_branch}'. Got '{branch}'. "
                        f"mcp__brc__propose handles branch targeting for you.",
                        status_code=403,
                        details={
                            "assigned_branch": session_assigned_branch,
                            "attempted_branch": branch,
                            "pipeline_id": session_pipeline_id,
                        },
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

    # SECURITY: Resolve the changed-file set + fail closed if we can't.
    # The agent-role and phase-based restriction checks below both consume
    # ``changed_files``; computing it once here keeps the security gates
    # consistent and lets the fail-closed branch run even if neither
    # session has a role (the phase check still runs in that case).
    #
    # Infrastructure pushes (checkpoint branches, pipeline-state, and synthetic-
    # session slice integration-branch creation pushes; see is_infrastructure_push
    # above) are exempt for two distinct reasons:
    #   1. ``egg/checkpoints/v2`` and ``egg/pipeline-state`` are orphan/disjoint-
    #      history branches written by orchestrator infrastructure, not agent
    #      BRC pushes, so role-based file restrictions don't conceptually apply.
    #   2. Synthetic-session slice integration-branch creation pushes (#2368)
    #      diff against `main` because the target ref doesn't exist yet, which
    #      would otherwise pull in every file modified on the parent branch's
    #      history (drafts, contracts, brc-history, ...) and falsely block a
    #      logical no-op branch-creation push (#2372).
    # The downstream anchor/phase/agent-restriction checks already gate on
    # `not is_infrastructure_push`; this gate makes the role check symmetric.
    session_role = None
    changed_files = None  # populated below; reused by attribution + phase checks
    if hasattr(g, "session") and g.session:
        session_role = getattr(g.session, "agent_role", None)

    if session_role and not is_infrastructure_push:
        # Get the list of files being pushed for downstream attribution-aware
        # role enforcement (the canonical agent-role check below) and the
        # phase-restriction check further down.
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

        # Note: the legacy whole-push-diff role check that used to live here
        # (``check_file_restrictions(session_role, changed_files)``) was
        # removed in #2489.  It treated every file in the diff range as the
        # pushing role's responsibility, even files modified only by pulled
        # commits authored by other roles, which trapped role-restricted
        # producers whose branches inherited unrelated upstream commits
        # (the role had no sanctioned recovery path).  The attribution-
        # aware block below partitions own-authored vs pulled files via the
        # commit-authorship registry and is now the canonical agent-role
        # restriction enforcer; it preserves fail-closed semantics when
        # attribution is unavailable.

    # Agent-role file restrictions (#2039 restricted-path rejection).
    # The gateway partitions the push range into own-authored vs
    # pulled-from-other-role files via the commit-authorship registry,
    # checks the pushing role's write permissions against only the
    # own-authored set, and either pushes unchanged (all allowed)
    # or rejects with 403 restricted_path_modified (any blocked).
    #
    # EGG_AGENT_RESTRICTIONS_ENFORCE=false short-circuits the filter
    # (warn-only, same as the old 403 path).
    auto_filter_response: dict[str, Any] | None = None
    attributed_push: Any = None
    if session_role and changed_files and not is_infrastructure_push:
        enforce = os.environ.get("EGG_AGENT_RESTRICTIONS_ENFORCE", "true").lower() not in (
            "false",
            "0",
            "no",
        )
        _ar_mod = sys.modules.get("agent_restrictions") or sys.modules.get(
            "gateway.agent_restrictions"
        )
        _partition_fn: Any = getattr(_ar_mod, "partition_files_by_role", None) if _ar_mod else None
        if _partition_fn is None:
            try:
                from agent_restrictions import (
                    partition_files_by_role as _imported_partition,
                )

                _partition_fn = _imported_partition
            except ImportError:  # pragma: no cover
                from .agent_restrictions import (
                    partition_files_by_role as _imported_partition,
                )

                _partition_fn = _imported_partition

        _gc_mod = sys.modules.get("git_client") or sys.modules.get("gateway.git_client")
        _get_attributed_fn: Any = (
            getattr(_gc_mod, "get_attributed_changed_files_in_push", None) if _gc_mod else None
        )
        if _get_attributed_fn is None:
            try:
                from git_client import (
                    get_attributed_changed_files_in_push as _imported_attr,
                )

                _get_attributed_fn = _imported_attr
            except ImportError:  # pragma: no cover
                from .git_client import (
                    get_attributed_changed_files_in_push as _imported_attr,
                )

                _get_attributed_fn = _imported_attr

        # Resolve attribution for every commit in the push range.
        try:
            attributed_push = _get_attributed_fn(
                exec_path, remote, branch, session_role=session_role
            )
        except Exception as exc:
            logger.warning("attribution_lookup_exception", error=str(exc), exc_info=True)
            # Fail-closed: an unexpected exception is treated as
            # attribution-unavailable so the rewrite path never
            # pushes unvetted files.
            _apr_cls = getattr(_gc_mod, "AttributedPushRange", None) if _gc_mod else None
            if _apr_cls is not None:
                attributed_push = _apr_cls(error=f"Attribution lookup failed: {exc}")
            else:
                from types import SimpleNamespace

                attributed_push = SimpleNamespace(
                    error=f"Attribution lookup failed: {exc}",
                    commits=[],
                    files=[],
                    attribution={},
                )

        # When the per-commit attribution can't be computed (e.g. the
        # caller mocked only the legacy file-detection path, or git
        # rev-list returned zero commits but there are staged-but-not-
        # pushed changes we can't walk with commit-tree), we FAIL
        # CLOSED.  Treat every file in ``changed_files`` as own-authored
        # and unregistered; if any file is blocked the push is rejected
        # by the restricted-path arm below (#2039).
        attribution_fallback = bool(attributed_push.error or not attributed_push.commits)
        if attribution_fallback:
            own_files: list[str] = list(dict.fromkeys(changed_files))
            pulled_files: list[str] = []
            unregistered_files: list[str] = list(own_files)
            attributed_commits_list: list[str] = []
        else:
            # Split files by author role (pushing role's own vs pulled).
            own_files = []
            pulled_files = []
            unregistered_files = []
            for attr in attributed_push.files:
                if attr.authored_by is None:
                    # Fail-closed: unregistered commits are treated as
                    # own-authored.
                    own_files.append(attr.path)
                    unregistered_files.append(attr.path)
                elif attr.authored_by == session_role:
                    own_files.append(attr.path)
                else:
                    pulled_files.append(attr.path)
            own_files = list(dict.fromkeys(own_files))
            pulled_files = list(dict.fromkeys(pulled_files))
            attributed_commits_list = list(attributed_push.commits)

        # Build the pulled_commits list for the response + audit log.
        pulled_commits_summary: list[dict[str, Any]] = []
        for sha in attributed_commits_list:
            role_for_sha = attributed_push.attribution.get(sha) if attributed_push else None
            if role_for_sha and role_for_sha != session_role:
                pulled_commits_summary.append({"sha": sha, "author_role": role_for_sha})

        allowed_own, blocked_own = _partition_fn(session_role, own_files)

        if unregistered_files and enforce:
            audit_log(
                "push_authorship_unregistered_fallback",
                "git_push",
                success=True,
                details={
                    "repo": repo,
                    "branch": branch,
                    "role": session_role,
                    "unregistered_files": unregistered_files,
                    "blocked_paths": blocked_own,
                    "pulled_commits": pulled_commits_summary,
                },
            )

        if blocked_own and enforce:
            # #2039: reject any push whose diff modifies a path the
            # pushing role cannot write.  The previous behavior — silent
            # tree rewrite (mixed) or silent ``nothing_to_push=true``
            # (all-blocked) — produced destructive deletions on the
            # shared branch and gave the agent no actionable signal.
            # Reject loudly with a structured 403 that points at the
            # supported recovery pattern (#1998 conditional ACK with
            # ``--pre-merge-condition``).
            sorted_blocked = sorted(set(blocked_own))
            audit_log(
                "push_denied_restricted_path_modified",
                "git_push",
                success=False,
                details={
                    "repo": repo,
                    "branch": branch,
                    "role": session_role,
                    "blocked_paths": sorted_blocked,
                    "pulled_commits": pulled_commits_summary,
                    "attribution_fallback": attribution_fallback,
                },
            )
            recommended_action = (
                "Drop the edits to the listed paths and re-propose with "
                "--pre-merge-condition flagging a manual change for the "
                "human reviewer (see issue #1998 for the conditional-ACK "
                "pattern)."
            )
            details: dict[str, Any] = {
                "error": "restricted_path_modified",
                "role": session_role,
                "blocked_paths": sorted_blocked,
                "recommended_action": recommended_action,
                "doc_ref": "#1998",
                "pulled_commits": pulled_commits_summary,
                "attribution_fallback": attribution_fallback,
            }
            # #2355 hint catalogue: surface category-specific guidance
            # (e.g. "Use egg-contract CLI commands…" for contract paths,
            # "Documentation changes belong to the documenter role." for
            # docs/) alongside the generic conditional-ACK pointer.  The
            # legacy whole-push-diff check used to do this; restoring it
            # here keeps the response shape consistent with the anchor-
            # write 403 below.
            hint = _derive_push_denied_hint(sorted_blocked)
            if hint is not None:
                details["hint"] = hint
            return make_error(
                (
                    f"Push denied: role '{session_role}' cannot modify restricted "
                    f"paths: {', '.join(sorted_blocked)}. "
                    f"{recommended_action}"
                ),
                status_code=403,
                details=details,
            )
        elif blocked_own and not enforce:
            # Warn-only mode: log but let the plain push proceed.
            # Explicitly flag ``enforce=false`` so operators scanning
            # audit logs during a kill-switch window can distinguish
            # this from the enforced paths.
            logger.warning(
                "Agent-role file restriction would block push (warn-only)",
                event_type="agent_role_restriction_warning",
                repo=repo,
                branch=branch,
                role=session_role,
                blocked_files=blocked_own,
                enforce=False,
            )
            # Observability parity (#1882 TASK-3-3): even the warn-
            # only passthrough must surface pulled_commits and the
            # filtered=false flag in the success response so
            # downstream tooling sees a consistent schema.
            auto_filter_response = {
                "filtered": False,
                "excluded_files": [],
                "pushed_files": own_files + pulled_files,
                "pulled_commits": pulled_commits_summary,
            }
        else:
            # All own-files are allowed.  No rewrite needed.  We still
            # stash the pulled_commits summary so the success path can
            # surface it in the response for observability.
            auto_filter_response = {
                "filtered": False,
                "excluded_files": [],
                "pushed_files": own_files + pulled_files,
                "pulled_commits": pulled_commits_summary,
            }

    # SECURITY: Check anchor file write scoping.
    # Agents can only write to their own anchor file (.egg-state/agent-anchors/<id>.json).
    # The agent_anchor_id is set via the AGENT_ANCHOR_ID env var in the container.
    if changed_files and not is_infrastructure_push:
        session_anchor_id = None
        if hasattr(g, "session") and g.session:
            session_anchor_id = getattr(g.session, "agent_anchor_id", None)
        for changed_file in changed_files:
            anchor_result = check_anchor_write_permission(changed_file, session_anchor_id)
            if not anchor_result.allowed:
                audit_log(
                    "push_denied_anchor_write",
                    "git_push",
                    success=False,
                    details={
                        "repo": repo,
                        "branch": branch,
                        "agent_anchor_id": session_anchor_id,
                        "blocked_files": anchor_result.blocked_files,
                        "blocked_reason": anchor_result.blocked_reason,
                    },
                )
                anchor_details: dict[str, Any] = {
                    "agent_anchor_id": session_anchor_id,
                    "blocked_files": anchor_result.blocked_files,
                    "blocked_reason": anchor_result.blocked_reason,
                }
                # Anchor-write violations bypass the role-level partition (the
                # coder blocklist exempts .egg-state/agent-anchors/), so they
                # need their own derive_hint call to deliver the
                # orchestrator-API guidance from BLOCKED_HINTS. See #2355.
                anchor_hint = _derive_push_denied_hint(anchor_result.blocked_files)
                if anchor_hint is not None:
                    anchor_details["hint"] = anchor_hint
                return make_error(
                    f"Push denied: {anchor_result.message}",
                    status_code=403,
                    details=anchor_details,
                )

    # SECURITY: Check phase-based file restrictions for local mode sessions.
    # This replaces the blanket local-mode push block with granular phase-based
    # restrictions. Each phase has specific allowed/blocked file patterns:
    # - refine/plan: Can only push .egg-state/ files (contracts, drafts, checkpoints)
    # - implement: Can push code but not .egg-state/ (except checkpoints)
    # - pr: Can push everything
    #
    # Checkpoint branch pushes always bypass this check (see is_infrastructure_push above).
    if session_phase and not is_infrastructure_push:
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
            # Pipeline sessions get a pipeline-specific hint pointing to
            # egg-orch; non-pipeline sessions see the original generic hint.
            session_pipeline_id = None
            if hasattr(g, "session") and g.session:
                session_pipeline_id = getattr(g.session, "pipeline_id", None)

            if has_non_state_files and isinstance(session_pipeline_id, str):
                hint = (
                    "Push contains files from prior pipeline phases that this phase "
                    "cannot modify. This indicates the worktree was not properly synced. "
                    "Signal an error with `egg-orch signal error --error 'Push denied: "
                    "phase file restrictions'` and include this message. "
                    f"Blocked files: {phase_result.blocked_files}"
                )
            elif has_non_state_files:
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
    if force_with_lease:
        # ``--force-with-lease`` rejects the push if the remote has moved
        # since we last fetched it — preferred over ``--force`` for
        # non-fast-forward pushes (e.g. the stacked-PR reconciler's
        # rebase-then-push heal path, #2137).
        push_args.append("--force-with-lease")
    elif force:
        push_args.append("--force")
    # NOTE: The push uses the original refspec (not a SHA-based refspec)
    # because it never calls ``update-ref`` pre-push, so the directory-
    # style ref collision (sibling worktree refs like
    # ``refs/heads/<branch>/work``) does not apply here.  See #1994.
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

            success_payload: dict[str, Any] = {
                "repo": repo,
                "branch": branch,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "auth_mode": auth_mode,
            }
            # Surface pulled_commits / filtered=False on plain pushes so
            # agents get consistent response shape across paths (#1882).
            if auto_filter_response is not None:
                success_payload.setdefault("filtered", auto_filter_response.get("filtered", False))
                success_payload.setdefault("nothing_to_push", False)
                success_payload.setdefault(
                    "excluded_files", auto_filter_response.get("excluded_files", [])
                )
                success_payload.setdefault(
                    "pushed_files", auto_filter_response.get("pushed_files", [])
                )
                success_payload.setdefault(
                    "pulled_commits", auto_filter_response.get("pulled_commits", [])
                )
            return make_success(
                "Push successful",
                success_payload,
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

    # SECURITY: Scope `git update-ref` to the agent's own assigned branch.
    # update-ref is the supported recovery primitive when an agent ends up on
    # detached HEAD with a useful commit (see issue #2162). To keep the blast
    # radius tight, the gateway rejects any update-ref that is not of the form
    # `update-ref <ref> <newvalue> [<oldvalue>]` and force-prepends
    # `--no-deref` below so symref-following semantics never apply.
    if operation == "update-ref":
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        positional = [a for a in validated_args if not a.startswith("-")]
        denial_reason: str | None = None
        if not isinstance(assigned, str) or not assigned:
            denial_reason = (
                "git update-ref is only allowed in pipeline sessions with an assigned branch."
            )
        elif len(positional) < 2 or len(positional) > 3:
            denial_reason = (
                "git update-ref must be of the form `git update-ref <ref> <newvalue> [<oldvalue>]`."
            )
        else:
            expected_ref = f"refs/heads/{assigned}"
            if positional[0] != expected_ref:
                denial_reason = (
                    f"git update-ref target '{positional[0]}' is not allowed. "
                    f"Only '{expected_ref}' (your assigned branch) may be updated. "
                    f"If you are trying to manually retarget your branch to drop "
                    f"pulled upstream commits and recover from a "
                    f"'restricted_path_modified' push 403, that is no longer "
                    f"necessary (#2489) — pulled commits authored by other roles "
                    f"are exempt from your role allowlist; retry the push as-is."
                )
        if denial_reason is not None:
            audit_log(
                "git_execute_blocked",
                operation,
                success=False,
                details={
                    "repo_path": repo_path,
                    "git_args": validated_args,
                    "container_id": container_id,
                    "assigned_branch": assigned,
                    "reason": denial_reason,
                },
            )
            return make_error(denial_reason, status_code=403)

    # SECURITY: Scope `git symbolic-ref HEAD <ref>` to the agent's own
    # assigned or local per-role branch.  symbolic-ref is the canonical
    # reattach primitive when a worktree ends up on detached HEAD (e.g.
    # post-rebase, see issue #2200).  Restricted to the two-positional
    # form `symbolic-ref HEAD <ref>` — read forms (one-arg) and the
    # delete form (`-d`) are rejected because they do not participate
    # in the recovery flow.
    if operation == "symbolic-ref":
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        positional = [a for a in validated_args if not a.startswith("-")]
        denial_reason = None
        if not isinstance(assigned, str) or not assigned:
            denial_reason = (
                "git symbolic-ref is only allowed in pipeline sessions with an assigned branch."
            )
        elif len(positional) != 2:
            denial_reason = "git symbolic-ref must be of the form `git symbolic-ref HEAD <ref>`."
        elif positional[0] != "HEAD":
            denial_reason = (
                f"git symbolic-ref source '{positional[0]}' is not allowed. "
                f"Only HEAD may be retargeted."
            )
        else:
            allowed_refs = {f"refs/heads/{assigned}"}
            # Defense in depth: scope the per-role local work branch from
            # ``session.container_id`` (canonical, set by the orchestrator at
            # session registration), not ``data.get("container_id")`` which
            # is agent-supplied.  Mirrors the ``update-ref`` guard above which
            # also ignores the request-body container_id.
            session_container_id = getattr(session, "container_id", None)
            if isinstance(session_container_id, str) and session_container_id:
                # Per-role local work branch (`egg/{container_id}/work`)
                # — see worktree_manager._create_or_reuse_worktree.
                allowed_refs.add(f"refs/heads/egg/{session_container_id}/work")
            if positional[1] not in allowed_refs:
                denial_reason = (
                    f"git symbolic-ref target '{positional[1]}' is not allowed. "
                    f"Allowed targets: {sorted(allowed_refs)}."
                )
        if denial_reason is not None:
            audit_log(
                "git_execute_blocked",
                operation,
                success=False,
                details={
                    "repo_path": repo_path,
                    "git_args": validated_args,
                    "container_id": container_id,
                    "assigned_branch": assigned,
                    "reason": denial_reason,
                },
            )
            return make_error(denial_reason, status_code=403)

    # SECURITY: Block agent-initiated ``git rebase`` against the base
    # branch from pipeline sessions (#2224, follow-up to #2222).  The
    # pipeline branch is rebased onto the base branch only via the
    # orchestrator's controlled rebase in
    # ``orchestrator/routes/pipelines.py::_rebase_pipeline_branch_onto_base``
    # — which itself uses the *bare* form ``git rebase origin/<base>``
    # but is safe because steps 1–5 of the helper enforce ancestry
    # preconditions and reset HEAD to the pipeline-branch tip *before*
    # the rebase replays.  Crucially, that helper runs as a subprocess
    # on the orchestrator-side worktree and does *not* route through
    # this endpoint, so this guard does not interfere with it.  An
    # agent reaching for ``git rebase origin/main`` (intentionally or
    # via a "resolve conflicts" intuition) reproduces the contamination
    # shape from #2222 even with the orchestrator-side fixes in place.
    #
    # The ``--onto X UP <branch>`` form is allowed when ``X`` (the
    # *new* base) is *not* a protected ref — that shape is used by the
    # stacked-PR healer in
    # ``orchestrator/gateway_client.py::rebase_onto``, which always
    # passes a slice/issue branch as ``new_base`` (never ``origin/main``;
    # see ``stacked_pr_reconciler._resolve_extant_new_base``).  Calls
    # with ``--onto origin/main …`` are *blocked*: when ``X == UP ==
    # origin/main`` the operation reduces to bare ``git rebase
    # origin/main`` and reproduces the contamination shape (the value
    # of ``UP`` is irrelevant — the new HEAD is whatever ``X``
    # resolves to, with the upstream-to-HEAD commits replayed on top).
    if operation == "rebase":
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        if isinstance(assigned, str) and assigned:
            # ``protected_refs`` lists every form an agent (or an
            # innocent rename) could use to name the base branch.  We
            # normalise inputs by stripping ``refs/remotes/`` and
            # ``refs/heads/`` prefixes before comparing so canonical
            # full ref names hit the same guard.  Pipelines whose base
            # is not ``main`` are not currently in production
            # (orchestrator's ``base_branch`` defaults to ``main``); if
            # non-main bases ship, derive this set from the session's
            # recorded base branch instead of hardcoding it.
            protected_refs = {
                "origin/main",
                "main",
                "origin/HEAD",
                "FETCH_HEAD",
            }

            def _normalise_ref(value: str) -> str:
                # Strip ``refs/remotes/`` (canonical full remote-tracking
                # ref) and ``refs/heads/`` (canonical local-branch ref)
                # so e.g. ``refs/remotes/origin/main`` matches
                # ``origin/main`` in ``protected_refs``.  Other shapes
                # (SHAs, ``origin/main~1``, ``origin/main^``) are caught
                # by exact-match below or fall through — they are
                # acknowledged in the docstring as residual gaps.
                if value.startswith("refs/remotes/"):
                    return value[len("refs/remotes/") :]
                if value.startswith("refs/heads/"):
                    return value[len("refs/heads/") :]
                return value

            offender: str | None = None

            # Branch 1: ``--onto <new_base>`` is present.  Reject when
            # the *new base* (the value of ``--onto``) is a protected
            # ref, regardless of what the upstream positional is.  This
            # closes the ``--onto origin/main origin/main`` bypass:
            # ``git rebase --onto X UP`` rebases HEAD onto X using UP as
            # the upstream, so when X is the base branch the operation
            # produces the same contamination shape as bare ``git
            # rebase origin/main``.
            #
            # Collect *every* ``--onto`` occurrence rather than the
            # first — git's ``OPT_STRING`` semantics make duplicate
            # ``--onto`` flags overwrite, so the *last* value wins, and
            # an adversarial ``--onto safe --onto origin/main`` would
            # otherwise slip past a first-match check.  Reject when any
            # of the supplied values is a protected ref.  Empty values
            # (``--onto=`` with nothing after) are treated as "not
            # provided" so the bare-form upstream check below still
            # runs against the positional args.
            onto_values: list[str] = []
            j = 0
            while j < len(validated_args):
                arg = validated_args[j]
                if arg.startswith("--onto="):
                    value = arg.split("=", 1)[1]
                    if value:
                        onto_values.append(value)
                elif arg == "--onto" and j + 1 < len(validated_args):
                    value = validated_args[j + 1]
                    if value:
                        onto_values.append(value)
                    j += 1
                j += 1

            if onto_values:
                offender = next(
                    (v for v in onto_values if _normalise_ref(v) in protected_refs),
                    None,
                )
            else:
                # Branch 2: bare ``git rebase <upstream> [<branch>]``
                # form — first positional is the upstream.  Reject when
                # the upstream is a protected ref.
                positional = [a for a in validated_args if not a.startswith("-")]
                offender = next(
                    (p for p in positional if _normalise_ref(p) in protected_refs),
                    None,
                )

            if offender is not None:
                denial_reason = (
                    f"git rebase against '{offender}' is not allowed in "
                    f"pipeline sessions. The pipeline branch is rebased "
                    f"onto the base branch only via the orchestrator's "
                    f"controlled rebase (`_rebase_pipeline_branch_onto_base`), "
                    f"which runs as a subprocess that does not route through "
                    f"this endpoint; an agent-initiated `git rebase "
                    f"origin/main` (or `--onto origin/main …`) reproduces "
                    f"the contamination shape from #2222. If you need to "
                    f"bring in new commits from the base, ask the operator "
                    f"to resume the pipeline so the orchestrator-side "
                    f"rebase runs. If you were trying to drop pulled upstream "
                    f"commits to recover from a 'restricted_path_modified' "
                    f"push 403, that is no longer necessary (#2489) — pulled "
                    f"commits authored by other roles are exempt from your "
                    f"role allowlist; retry the push as-is."
                )
                audit_log(
                    "git_execute_blocked",
                    operation,
                    success=False,
                    details={
                        "repo_path": repo_path,
                        "git_args": validated_args,
                        "container_id": container_id,
                        "assigned_branch": assigned,
                        "reason": denial_reason,
                    },
                )
                return make_error(denial_reason, status_code=403)

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
                f"Use 'git checkout [<commit-ish>] -- <file>' to restore files instead "
                f"(e.g. 'git checkout HEAD -- <file>' or 'git checkout <sha> -- <file>'). "
                f"If you are recovering from a 'restricted_path_modified' push 403, "
                f"note that pulled commits authored by other roles are exempt from "
                f"your role allowlist (#2489) — only your own commits' paths trigger "
                f"the rejection, so retry the push first; if it still rejects, drop "
                f"the disallowed paths from your own commits and re-propose with "
                f"--pre-merge-condition (#1998 conditional ACK).",
                status_code=403,
            )

    # Map container path to worktree path if container_id is provided
    exec_path = map_container_path_to_worktree(repo_path, container_id, operation)
    if exec_path is None:
        return make_worktree_not_found_error(container_id)
    is_worktree = exec_path != repo_path

    # SECURITY: Block off-lineage `git reset` in pipeline sessions.
    # `git reset <ref>` (any mode) moves HEAD; if <ref> is not an ancestor of
    # HEAD on the assigned branch, the agent's commits are silently dropped
    # from the working tree — the same effect as a branch switch. The
    # checkout/switch lock at :1924 does not catch this (see issue #2089).
    if operation == "reset":
        session = getattr(g, "session", None)
        assigned = getattr(session, "assigned_branch", None) if session else None
        if isinstance(assigned, str) and assigned:
            target_ref = extract_reset_target_ref(validated_args)
            if target_ref is not None:
                ancestor_stderr: str | None = None
                try:
                    ancestor_check = subprocess.run(
                        git_cmd("merge-base", "--is-ancestor", target_ref, "HEAD"),
                        cwd=exec_path,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    is_ancestor = ancestor_check.returncode == 0
                    if not is_ancestor and ancestor_check.stderr:
                        ancestor_stderr = ancestor_check.stderr.strip() or None
                except (OSError, subprocess.TimeoutExpired) as exc:
                    # Fail closed — if we cannot verify safety, treat as off-lineage.
                    is_ancestor = False
                    ancestor_stderr = str(exc)
                if not is_ancestor:
                    audit_details = {
                        "repo_path": repo_path,
                        "git_args": validated_args,
                        "container_id": container_id,
                        "assigned_branch": assigned,
                        "target_ref": target_ref,
                        "reason": "Off-lineage reset blocked in pipeline session",
                    }
                    if ancestor_stderr:
                        audit_details["merge_base_stderr"] = ancestor_stderr
                    audit_log(
                        "git_execute_blocked",
                        operation,
                        success=False,
                        details=audit_details,
                    )
                    return make_error(
                        f"Off-lineage 'git reset' is not allowed in pipeline sessions. "
                        f"Target ref '{target_ref}' is not an ancestor of HEAD on your "
                        f"assigned branch '{assigned}'. To incorporate new commits from the "
                        f"remote, use 'git rebase origin/{assigned}' instead. "
                        f"If you are trying to drop pulled upstream commits to recover "
                        f"from a 'restricted_path_modified' push 403, that is no longer "
                        f"necessary (#2489) — pulled commits authored by other roles are "
                        f"exempt from your role allowlist; retry the push as-is.",
                        status_code=403,
                    )

    # SECURITY: Enforce branch isolation in pipeline worktree sessions.
    # Pipeline agents in worktrees must stay on their assigned branch.
    # Interactive sessions are unrestricted even if they use worktrees.
    # We detect pipeline sessions by the presence of pipeline_id on the
    # session, rather than checking session_mode.
    # See issue #773.
    session = getattr(g, "session", None)
    is_pipeline = session is not None and getattr(session, "pipeline_id", None) is not None
    if is_pipeline and is_worktree and is_branch_switching_operation(operation, validated_args):
        assert session is not None  # guaranteed by is_pipeline check above
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
            "Use 'git restore' for file operations instead of 'git checkout'. "
            "If you are recovering from a 'restricted_path_modified' push 403, "
            "note that pulled commits authored by other roles are exempt from "
            "your role allowlist (#2489) — retry the push as-is; if it still "
            "rejects, drop the disallowed paths from your own commits and "
            "re-propose with --pre-merge-condition (#1998 conditional ACK).",
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

    # SECURITY: Force-prepend `--no-deref` for `update-ref` (#2162). Without it,
    # update-ref follows symref targets — the underlying ref is updated, not
    # `refs/heads/<assigned_branch>`. In practice agent branches are never
    # symrefs, but the gateway is a defense-in-depth boundary and the recovery
    # flow never wants symref-following semantics.
    if operation == "update-ref":
        validated_args = ["--no-deref", *validated_args]

    # Build command
    cmd = git_cmd(operation, *validated_args)

    # Set GIT_EDITOR=true so operations that need an editor (e.g., rebase
    # --continue after conflict resolution) succeed without a terminal.
    # `true` accepts the default commit message, which is the expected
    # behavior for an agent that always provides messages via -m.
    env = os.environ.copy()
    env["GIT_EDITOR"] = "true"

    # Commit-authorship observer (#1882): snapshot HEAD before the git
    # subcommand so we can compute which commits (if any) it created
    # and register them with the orchestrator's authorship registry.
    # Only agent sessions participate; internal gateway ops skip.
    _observer_role: str | None = None
    _observer_pipeline_id: str | None = None
    _observer_repo: str | None = None
    _observer_branch: str | None = None
    _observer_before_head: str | None = None
    _observer_armed: bool = False
    _session_for_observer = getattr(g, "session", None)
    if _session_for_observer is not None:
        _observer_role = getattr(_session_for_observer, "agent_role", None)
        _observer_pipeline_id = getattr(_session_for_observer, "pipeline_id", None)
        _observer_repo = getattr(_session_for_observer, "repo", None) or getattr(
            _session_for_observer, "checkpoint_repo", None
        )
        _observer_branch = getattr(_session_for_observer, "assigned_branch", None) or getattr(
            _session_for_observer, "branch", None
        )
    # Intentionally exhaustive list of commit-creating operations.
    # ``stash`` and ``pull`` can also create commit objects, but agents
    # do not use them — all pushes go through the gateway's push handler
    # which resolves attribution independently.  Extend this list if
    # agent workflows ever include stash or pull.
    if _observer_role and operation in (
        "commit",
        "merge",
        "cherry-pick",
        "revert",
        "rebase",
        "am",
    ):
        _observer_armed = True
        _capture_head = _lookup_commit_observer_fn("capture_head")
        if _capture_head is not None:
            try:
                _observer_before_head = _capture_head(exec_path)
            except Exception:  # pragma: no cover - defensive
                # before_head stays None; observe handles the
                # unborn-branch case via its [after_head] fallback.
                _observer_before_head = None

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
            # Fire the observer only on the narrow list of ref-mutating
            # operations that armed the observer above.  For all other
            # operations (status, checkout, restore, ...) we skip the
            # post-op rev-parse entirely so callers' subprocess
            # mocking isn't perturbed.  Note: _observer_before_head
            # may be None on unborn branches — observe() handles that
            # via its [after_head] fallback.
            if _observer_role and _observer_armed:
                try:
                    _observe_after = _lookup_commit_observer_fn("observe_after_git_execute")
                    if _observe_after is not None:
                        _observe_after(
                            exec_path,
                            before_head=_observer_before_head,
                            branch=_observer_branch,
                            session_role=_observer_role,
                            pipeline_id=_observer_pipeline_id,
                            repo=_observer_repo,
                        )
                except Exception:
                    # Observer is best-effort — never block the git
                    # response on a registry failure.
                    logger.debug(
                        "commit_observer_swallowed",
                        exc_info=True,
                    )
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

            # Detached-HEAD recovery hint (#2162). After a successful commit
            # in a pipeline session, surface a clear hint if HEAD is detached
            # so the agent doesn't spend minutes guessing at policy bypasses
            # to update its work branch ref.
            hint = _detached_head_hint(operation, exec_path, repo_path, container_id)
            stderr_out = (result.stderr or "") + hint if hint else result.stderr

            return make_success(
                f"git {operation} successful",
                {
                    "stdout": result.stdout,
                    "stderr": stderr_out,
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

            # Surface the detached-HEAD recovery hint on failure too. Common
            # cases (rebase --onto mid-conflict, missing --allow-empty, index
            # locks) produce a *failed* commit while detached, and the hint is
            # exactly what cuts that confusion short.
            failure_hint = _detached_head_hint(operation, exec_path, repo_path, container_id)
            failure_stderr = (result.stderr or "") + failure_hint if failure_hint else result.stderr
            return make_error(
                f"git {operation} failed",
                status_code=500,
                details={
                    "stdout": result.stdout,
                    "stderr": failure_stderr,
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
    if exec_path is None:
        return make_worktree_not_found_error(container_id)

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
        if _is_checkpoint_repo_for_request(repo_info.owner, repo_info.repo):
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
        _cleanup_stale_pack_files(exec_path)
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
    except ValueError, TypeError:
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
    github_token = _resolve_checkpoint_token(repo_path)

    try:
        index = handler.fetch_and_read_index(
            repo_path, checkpoint_repo=checkpoint_repo, github_token=github_token
        )
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
    github_token = _resolve_checkpoint_token(repo_path)

    # fetch_and_read_index does ls-remote + fetch + read index in one pass.
    # We then call ensure_ref to get a ref for read_checkpoint calls below.
    # After the fetch in fetch_and_read_index, ensure_ref's fetch is a no-op
    # (branch already up-to-date), so only the ls-remote is repeated.
    try:
        index = handler.fetch_and_read_index(
            repo_path, checkpoint_repo=checkpoint_repo, github_token=github_token
        )
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
        ref = handler.ensure_ref(
            repo_path, checkpoint_repo=checkpoint_repo, github_token=github_token
        )
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
    github_token = _resolve_checkpoint_token(repo_path)

    try:
        ref = handler.ensure_ref(
            repo_path, checkpoint_repo=checkpoint_repo, github_token=github_token
        )
    except Exception as e:
        logger.error("Checkpoint ref fetch failed", error=str(e))
        return make_error("Failed to fetch checkpoint data", status_code=500)

    if not ref:
        return make_error("Checkpoint branch not found", status_code=404)

    checkpoint_id: str | None = identifier
    if not identifier.startswith("ckpt-"):
        # Look up by commit SHA
        index = handler.fetch_and_read_index(
            repo_path, checkpoint_repo=checkpoint_repo, github_token=github_token
        )
        if index:
            checkpoint_id = index.get_by_commit(identifier)
        if not checkpoint_id:
            return make_error(f"Checkpoint not found: {identifier}", status_code=404)

    assert checkpoint_id is not None
    checkpoint = handler.read_checkpoint(repo_path, checkpoint_id, ref)
    if not checkpoint:
        return make_error(f"Checkpoint not found: {identifier}", status_code=404)

    return make_success("OK", {"checkpoint": checkpoint.model_dump(mode="json")})


def _resolve_checkpoint_repo(repo_path: str) -> str | None:
    """Resolve checkpoint_repo from query param or auto-detection.

    Resolution order:
    1. Explicit ``checkpoint_repo`` query parameter (owner/repo format).
    2. Auto-detection from ``repo_path`` (git remote → config lookup).
    3. ``source_repo`` query parameter looked up in config. This is the
       fallback for sandbox containers where ``repositories.yaml`` is not
       available and the sandbox repo path may not exist on the gateway.
    """
    explicit = request.args.get("checkpoint_repo")
    if explicit:
        # Basic validation: must look like "owner/repo"
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$", explicit):
            return explicit
        logger.warning(
            "Invalid checkpoint_repo format, falling back to auto-detection",
            checkpoint_repo=explicit,
        )

    # Try path-based auto-detection (works when repo_path is a local git repo)
    result = _get_checkpoint_repo_for_path(repo_path)
    if result:
        return result

    # Fallback: use source_repo query param for config lookup.
    # The sandbox CLI sends this when it can determine the source repo
    # from git remote but cannot resolve checkpoint_repo locally
    # (repositories.yaml is only mounted on the gateway).
    source_repo = request.args.get("source_repo")
    if source_repo and re.match(
        r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$", source_repo
    ):
        try:
            from config.repo_config import get_checkpoint_repo

            cp_repo = get_checkpoint_repo(source_repo)
            if cp_repo:
                logger.debug(
                    "Resolved checkpoint_repo from source_repo param",
                    source_repo=source_repo,
                    checkpoint_repo=cp_repo,
                )
                return cp_repo
        except Exception as e:
            logger.debug(
                "Config lookup for source_repo failed",
                source_repo=source_repo,
                error=str(e),
            )

    return None


def _resolve_checkpoint_token(repo_path: str) -> str | None:
    """Resolve a GitHub token for checkpoint fetch operations.

    Tries ``_resolve_github_token`` (which reads the git remote from
    ``repo_path``).  When that fails — typically because ``repo_path``
    is the scratch repo with no remotes — falls back to resolving a
    token via the ``source_repo`` query parameter.
    """
    from checkpoint_handler import _resolve_github_token

    token: str | None = _resolve_github_token(repo_path)
    if token:
        return token

    source_repo = request.args.get("source_repo")
    if source_repo and re.match(
        r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$", source_repo
    ):
        token_str, _auth_mode, _error = get_token_for_repo(source_repo)
        if token_str:
            return token_str

    return None


_CHECKPOINT_SCRATCH_DIR = "/home/egg/.egg-worktrees/.checkpoint-scratch"

_checkpoint_scratch_lock = threading.Lock()


def _ensure_checkpoint_scratch_repo() -> str | None:
    """Create or return a bare git repo for checkpoint fetch operations.

    When the sandbox's repo path doesn't exist on the gateway filesystem,
    we still need a valid git directory as cwd for ``git fetch`` and
    ``git ls-remote`` commands.  This creates a minimal bare repo that
    serves as that working directory.

    Returns:
        Path to the scratch repo, or None on failure.
    """
    if os.path.isdir(os.path.join(_CHECKPOINT_SCRATCH_DIR, "objects")):
        return _CHECKPOINT_SCRATCH_DIR
    with _checkpoint_scratch_lock:
        # Re-check after acquiring lock to avoid duplicate init
        if os.path.isdir(os.path.join(_CHECKPOINT_SCRATCH_DIR, "objects")):
            return _CHECKPOINT_SCRATCH_DIR
        try:
            os.makedirs(_CHECKPOINT_SCRATCH_DIR, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare", _CHECKPOINT_SCRATCH_DIR],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            logger.debug("Created checkpoint scratch repo", path=_CHECKPOINT_SCRATCH_DIR)
            return _CHECKPOINT_SCRATCH_DIR
        except Exception as e:
            logger.warning("Failed to create checkpoint scratch repo", error=str(e))
            return None


def _resolve_repo_path_for_checkpoints() -> str | None:
    """Resolve repository path for checkpoint read operations.

    Tries query param, then session's last_repo_path, then EGG_REPO_PATH,
    then a checkpoint scratch repo.  The scratch repo fallback handles
    sandbox → gateway requests where the sandbox's filesystem is not
    mounted on the gateway container.
    """
    # Explicit query param — if provided AND exists locally, use it.
    repo_path = request.args.get("repo_path")
    if repo_path:
        path_valid, _err = validate_repo_path(repo_path)
        if path_valid and os.path.isdir(repo_path):
            return repo_path
        # Path is valid format but doesn't exist on this container.
        # This is expected when the CLI runs in a sandbox whose filesystem
        # is not mounted on the gateway.  Fall through to other sources
        # instead of returning None.
        if not path_valid:
            return None

    # Session's last known repo path (set during push operations)
    session = getattr(g, "session", None)
    if session and getattr(session, "last_repo_path", None):
        if os.path.isdir(session.last_repo_path):
            return str(session.last_repo_path)

    # Environment variable
    env_path = os.environ.get("EGG_REPO_PATH")
    if env_path and os.path.isdir(env_path):
        return env_path

    # Last resort: create a bare scratch repo for checkpoint fetching.
    # This allows the gateway to serve checkpoint queries even without
    # a local copy of the source repo.
    return _ensure_checkpoint_scratch_repo()


def _apply_pr_labels(
    github: GitHubClient,
    repo: str,
    stdout: str,
    auth_mode: str,
    agent_role: str | None,
    pipeline_id: str | None,
) -> None:
    """Apply labels to a newly created PR. Failures are logged but non-fatal."""
    if not pipeline_id:
        return

    # Extract PR number from URL like https://github.com/owner/repo/pull/42
    match = re.search(r"/pull/(\d+)", stdout or "")
    if not match:
        return

    pr_number = match.group(1)
    labels = ["egg"]
    if agent_role:
        labels.append(f"agent:{agent_role}")

    try:
        # Ensure labels exist (idempotent)
        for label in labels:
            github.execute(
                ["label", "create", label, "--force", "--repo", repo],
                timeout=15,
                mode=auth_mode,
            )
        # Apply labels to the PR
        label_args = ["issue", "edit", pr_number, "--repo", repo]
        for label in labels:
            label_args.extend(["--add-label", label])
        github.execute(label_args, timeout=15, mode=auth_mode)
    except Exception:
        logger.warning(
            "Failed to apply labels to PR",
            pr_number=pr_number,
            repo=repo,
            labels=labels,
            exc_info=True,
        )


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
            "head": "feature-branch",
            "draft": false  (optional, forced to true in user mode)
        }

    Policy:
        - Bot mode: allowed (egg can create PRs)
        - User mode: allowed (PRs are forced to draft mode)
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    title = data.get("title")
    body = data.get("body", "")
    base = data.get("base")  # None = gh uses repo's default branch
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

    # Policy check: PR creation may be blocked in reviewer mode
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

    # In user mode, force PRs to be created as drafts
    draft = data.get("draft", False)
    if policy_result.details and policy_result.details.get("force_draft"):
        draft = True

    # Inject machine-parseable pipeline metadata as an HTML comment.
    # Note: _build_pr_body (in the orchestrator) also adds a human-readable
    # "## Pipeline Context" section. The two formats are intentionally
    # complementary — visible for humans, hidden comment for tooling.
    session = getattr(g, "session", None)
    session_pipeline_id = getattr(session, "pipeline_id", None) if session else None
    if session_pipeline_id:
        session_agent_role = getattr(session, "agent_role", None) or ""
        session_issue_number = getattr(session, "issue_number", None) or ""

        # Sanitize values to prevent breaking the HTML comment structure
        def _safe(v: str) -> str:
            return str(v).replace("--", "").replace(">", "")

        metadata_comment = (
            f"<!-- egg-pipeline-context"
            f" pipeline_id={_safe(session_pipeline_id)}"
            f" agent_role={_safe(session_agent_role)}"
            f" issue={_safe(str(session_issue_number))}"
            f" -->"
        )
        body = f"{body}\n\n{metadata_comment}" if body else metadata_comment

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
            "--head",
            head,
        ]

        if base:
            args.extend(["--base", base])

        if draft:
            args.append("--draft")

        result = github.execute(args, timeout=60, mode=auth_mode)

        if result.success:
            # Apply labels to the newly created PR
            _apply_pr_labels(
                github=github,
                repo=repo,
                stdout=result.stdout,
                auth_mode=auth_mode,
                agent_role=getattr(session, "agent_role", None) if session else None,
                pipeline_id=session_pipeline_id,
            )

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
                    "draft": draft,
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
    policy_result = policy.check_pr_comment_allowed(repo, pr_number, auth_mode=auth_mode)

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
    Edit a PR title, body, or base branch.

    Request body:
        {
            "repo": "owner/repo",
            "pr_number": 123,
            "title": "New title",  # optional
            "body": "New body",     # optional
            "base": "main"          # optional — retarget the PR base
        }

    At least one of ``title``, ``body``, or ``base`` must be set.

    The ``base`` field is the merge target branch ref (e.g.
    ``main`` or ``egg/issue-N/slice-3``). It is the canonical
    surface for the stacked-PR reconciler (#2137) to retarget a
    child PR after the parent merges and the parent's branch is
    deleted on origin. The ref is forwarded as-is to the GitHub
    PATCH ``/repos/{owner}/{repo}/pulls/{pr_number}`` API.

    Policy: pr_ownership
    """
    data = request.get_json()
    if not data:
        return make_error("Missing request body")

    repo = data.get("repo")
    pr_number = data.get("pr_number")
    title = data.get("title")
    body = data.get("body")
    base = data.get("base")

    if not repo:
        return make_error("Missing repo")
    if not pr_number:
        return make_error("Missing pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        return make_error("Invalid pr_number: must be a positive integer")
    if not title and not body and not base:
        return make_error("Must provide title, body, or base to edit")
    if base is not None and (not isinstance(base, str) or not base.strip()):
        return make_error("Invalid base: must be a non-empty branch ref")

    # Validate repo format early (before any API calls)
    repo_info = parse_owner_repo(repo)
    if not repo_info:
        return make_error("Invalid repo format: expected 'owner/repo'")

    # Determine auth mode for this repo
    auth_mode = get_auth_mode(repo)

    # Get session mode from request context (set by @require_session_auth decorator)
    session_mode = getattr(g, "session_mode", None)

    # Check Private Repo Mode policy (if enabled)
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
    args = ["api", f"repos/{repo_info.owner}/{repo_info.repo}/pulls/{pr_number}", "-X", "PATCH"]
    if title:
        args.extend(["-f", f"title={title}"])
    if body:
        args.extend(["-f", f"body={body}"])
    if base:
        args.extend(["-f", f"base={base}"])

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

    # --- Phase and role-based operation filtering ---
    # Block operations like "issue comment" / "issue edit" when phase or role restricts them.
    # Build a command string from the first 3 non-flag args for matching.
    non_flag_args = [a for a in args if not a.startswith("-")]
    gh_command_str = " ".join(non_flag_args[:3])

    session_phase = getattr(g, "session_phase", None)
    if session_phase:
        try:
            phase_result = filter_operation(
                phase=session_phase,
                operation_type=OperationType.GH,
                command=gh_command_str,
            )
            if not phase_result.allowed:
                audit_log(
                    "gh_execute_blocked_phase",
                    "gh_execute",
                    success=False,
                    details={
                        "command": gh_command_str,
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
        except ValueError:
            # Invalid phase value - allow for backward compat
            logger.warning("Invalid session phase in gh_execute", phase=session_phase)

    # Role-based operation filtering — block agents from posting issue comments regardless of phase.
    session_role = None
    if hasattr(g, "session") and g.session:
        _role = getattr(g.session, "agent_role", None)
        if isinstance(_role, str) and _role:
            session_role = _role
        elif _role is not None and not isinstance(_role, str):
            # Non-string agent_role — corrupted session, deny
            return make_error(
                "Invalid agent role type",
                status_code=403,
                details={"role": str(_role), "command": gh_command_str},
            )
    if session_role:
        role_allowed, role_reason = check_agent_gh_operation(session_role, gh_command_str)
        if not role_allowed:
            audit_log(
                "gh_execute_blocked_agent_role",
                "gh_execute",
                success=False,
                details={
                    "command": gh_command_str,
                    "role": session_role,
                    "reason": role_reason,
                },
            )
            return make_error(
                role_reason,
                status_code=403,
                details={"role": session_role, "command": gh_command_str},
            )

    # Issue #1962 TASK-2-2: extra guardrails for `gh issue create`
    # from the overseer role. The role-level check above does NOT
    # block `gh issue create` from the overseer (the operation is
    # not on _OVERSEER_BLOCKED_GH_OPS) so the existing handler lets
    # it through. We now layer additional defenses on top:
    # repo enforcement against EGG_PIPELINE_REPO, label injection,
    # title/body size limits, and a defense-in-depth secret-pattern
    # scan on the body. Failure is a structured 403.
    if (
        session_role
        and session_role.lower() == "overseer"
        and len(args) >= 2
        and args[0] == "issue"
        and args[1] == "create"
    ):
        from .agent_restrictions import check_overseer_gh_issue_create

        # Parse the relevant flags from the gh argv. We accept both
        # --title-file/--body-file (the new CLI verb's preferred path)
        # and --title/--body (the historical form) so old callers do
        # not break. Each known flag MUST be followed by a value that
        # does not start with '-' (otherwise a malformed argv like
        # `--repo --label foo` would consume `--label` as the repo
        # value and walk past every subsequent flag — reviewer_code
        # blocker against the original loop's order-dependence).
        repo_arg: str | None = None
        title_text: str = ""
        body_text: str = ""
        labels: list[str] = []
        _OVERSEER_VALUE_FLAGS = {
            "--repo",
            "--label",
            "--title",
            "--title-file",
            "--body",
            "--body-file",
        }

        def _value_for(flag: str, idx: int) -> tuple[str | None, tuple[Response, int] | None]:
            """Return (value, error_response) for a known --flag at args[idx]."""
            if idx + 1 >= len(args):
                return None, make_error(
                    f"Flag {flag!r} requires a value (end of argv)",
                    status_code=400,
                    details={"command": gh_command_str},
                )
            val = args[idx + 1]
            if val.startswith("-"):
                return None, make_error(
                    f"Flag {flag!r} requires a value (got another flag {val!r})",
                    status_code=400,
                    details={"command": gh_command_str},
                )
            return val, None

        i = 2
        while i < len(args):
            tok = args[i]
            if tok in _OVERSEER_VALUE_FLAGS:
                val, err = _value_for(tok, i)
                if err is not None:
                    return err
                if tok == "--repo":
                    repo_arg = val
                elif tok == "--label":
                    labels.append(val or "")
                elif tok == "--title":
                    title_text = val or ""
                elif tok == "--title-file":
                    try:
                        with open(val or "", encoding="utf-8", errors="strict") as _f:
                            title_text = _f.read().strip()
                    except UnicodeDecodeError as _exc:
                        return make_error(
                            f"--title-file {val!r} contains invalid UTF-8: {_exc}",
                            status_code=400,
                            details={"command": gh_command_str},
                        )
                    except OSError as _exc:
                        return make_error(
                            f"Cannot read --title-file {val!r}: {_exc}",
                            status_code=400,
                            details={"command": gh_command_str},
                        )
                elif tok == "--body":
                    body_text = val or ""
                elif tok == "--body-file":
                    try:
                        # errors="strict" so invalid UTF-8 in the body
                        # is rejected loudly (reviewer_code blocker:
                        # silent corruption could swap a leaked-secret
                        # byte sequence past the regex check).
                        with open(val or "", encoding="utf-8", errors="strict") as _f:
                            body_text = _f.read()
                    except UnicodeDecodeError as _exc:
                        return make_error(
                            f"--body-file {val!r} contains invalid UTF-8: {_exc}",
                            status_code=400,
                            details={"command": gh_command_str},
                        )
                    except OSError as _exc:
                        return make_error(
                            f"Cannot read --body-file {val!r}: {_exc}",
                            status_code=400,
                            details={"command": gh_command_str},
                        )
                i += 2
                continue
            else:
                i += 1

        pipeline_repo = os.environ.get("EGG_PIPELINE_REPO")
        ov_check = check_overseer_gh_issue_create(
            role=session_role,
            repo=repo_arg or "",
            pipeline_repo=pipeline_repo,
            labels=labels,
            title=title_text,
            body=body_text,
        )
        if not ov_check.allowed:
            audit_log(
                "gh_overseer_issue_create_blocked",
                "gh_execute",
                success=False,
                details={
                    "command": gh_command_str,
                    "role": session_role,
                    "reason": ov_check.reason,
                    "secret_kinds": list(ov_check.secret_kinds),
                },
            )
            return make_error(
                ov_check.reason,
                status_code=403,
                details={
                    "role": session_role,
                    "command": gh_command_str,
                    "secret_kinds": list(ov_check.secret_kinds),
                },
            )
        # Auto-inject any required labels the caller forgot. The
        # injected labels are tagged in the audit log so operators can
        # spot bypass attempts.
        if ov_check.injected_labels:
            for lbl in ov_check.injected_labels:
                args = (*args, "--label", lbl)
            audit_log(
                "gh_overseer_issue_create_labels_injected",
                "gh_execute",
                success=True,
                details={
                    "command": gh_command_str,
                    "role": session_role,
                    "injected_labels": list(ov_check.injected_labels),
                },
            )

    # For 'gh api' commands, validate the path against allowlist
    api_path: str | None = None
    method: str = "GET"
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

        # Detect issue comment/edit via gh api (bypass prevention).
        # These API calls are equivalent to "gh issue comment/edit {id}" —
        # apply the same phase + role checks.
        synthesized_cmd = None

        # POST to repos/{owner}/{repo}/issues/{id}/comments → issue comment
        _api_issue_comment_match = re.match(r"^repos/[^/]+/[^/]+/issues/(\d+)/comments$", api_path)
        if _api_issue_comment_match and method.upper() == "POST":
            synthesized_cmd = f"issue comment {_api_issue_comment_match.group(1)}"

        # PATCH to repos/{owner}/{repo}/issues/{id} → issue edit
        _api_issue_edit_match = re.match(r"^repos/[^/]+/[^/]+/issues/(\d+)$", api_path)
        if _api_issue_edit_match and method.upper() == "PATCH":
            synthesized_cmd = f"issue edit {_api_issue_edit_match.group(1)}"

        if synthesized_cmd:
            # Phase check
            if session_phase:
                try:
                    api_phase_result = filter_operation(
                        phase=session_phase,
                        operation_type=OperationType.GH,
                        command=synthesized_cmd,
                    )
                    if not api_phase_result.allowed:
                        audit_log(
                            "gh_api_issue_op_blocked_phase",
                            "gh_execute",
                            success=False,
                            details={
                                "api_path": api_path,
                                "synthesized_command": synthesized_cmd,
                                "phase": session_phase,
                            },
                        )
                        return make_error(
                            api_phase_result.message,
                            status_code=403,
                            details={
                                "phase": session_phase,
                                "blocked_reason": api_phase_result.blocked_reason,
                            },
                        )
                except ValueError:
                    pass
            # Role check
            if session_role:
                api_role_allowed, api_role_reason = check_agent_gh_operation(
                    session_role, synthesized_cmd
                )
                if not api_role_allowed:
                    audit_log(
                        "gh_api_issue_op_blocked_role",
                        "gh_execute",
                        success=False,
                        details={
                            "api_path": api_path,
                            "role": session_role,
                            "reason": api_role_reason,
                        },
                    )
                    return make_error(
                        api_role_reason,
                        status_code=403,
                        details={"role": session_role, "api_path": api_path},
                    )

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
            # Checkpoint repos are infrastructure — always accessible
            if _is_checkpoint_repo_for_request(repo_info.owner, repo_info.repo):
                audit_log(
                    "gh_execute_checkpoint_repo_exempt",
                    "gh_execute",
                    success=True,
                    details={
                        "repo": repo,
                        "reason": "Checkpoint repo exempt from private mode policy",
                    },
                )
            else:
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

    # For mutating operations on specific resources via gh api, verify ownership
    if api_path is not None:
        policy = get_policy_engine()

        # PATCH on comment endpoints — verify bot/configured user owns the comment
        comment_info = extract_comment_edit_info(api_path, method)
        if comment_info:
            c_owner, c_repo_name, c_comment_id, c_comment_type = comment_info
            ownership_result = policy.check_comment_ownership(
                f"{c_owner}/{c_repo_name}",
                c_comment_id,
                c_comment_type,
                auth_mode=auth_mode,
            )
            if not ownership_result.allowed:
                audit_log(
                    "comment_edit_denied",
                    "gh_execute",
                    success=False,
                    details={
                        "api_path": api_path,
                        "comment_id": c_comment_id,
                        "comment_type": c_comment_type,
                        "reason": ownership_result.reason,
                    },
                )
                return make_error(
                    ownership_result.reason,
                    status_code=403,
                    details=ownership_result.to_dict(),
                )

        # POST/PATCH on issue labels — verify bot/configured user owns the issue/PR
        label_info = extract_issue_label_info(api_path, method)
        if label_info:
            l_owner, l_repo_name, l_issue_number = label_info
            ownership_result = policy.check_issue_ownership(
                f"{l_owner}/{l_repo_name}",
                l_issue_number,
                auth_mode=auth_mode,
            )
            if not ownership_result.allowed:
                audit_log(
                    "label_edit_denied",
                    "gh_execute",
                    success=False,
                    details={
                        "api_path": api_path,
                        "issue_number": l_issue_number,
                        "reason": ownership_result.reason,
                    },
                )
                return make_error(
                    ownership_result.reason,
                    status_code=403,
                    details=ownership_result.to_dict(),
                )

        # POST on PR requested reviewers — verify bot/configured user owns the PR
        reviewer_info = extract_pr_reviewer_info(api_path, method)
        if reviewer_info:
            r_owner, r_repo_name, r_pr_number = reviewer_info
            ownership_result = policy.check_pr_ownership(
                f"{r_owner}/{r_repo_name}",
                r_pr_number,
                auth_mode=auth_mode,
            )
            if not ownership_result.allowed:
                audit_log(
                    "reviewer_edit_denied",
                    "gh_execute",
                    success=False,
                    details={
                        "api_path": api_path,
                        "pr_number": r_pr_number,
                        "reason": ownership_result.reason,
                    },
                )
                return make_error(
                    ownership_result.reason,
                    status_code=403,
                    details=ownership_result.to_dict(),
                )

        # POST on PR reviews — verify PR exists and review is allowed
        review_info = extract_pr_review_info(api_path, method)
        if review_info:
            rv_owner, rv_repo_name, rv_pr_number = review_info
            review_result = policy.check_pr_review_allowed(
                f"{rv_owner}/{rv_repo_name}",
                rv_pr_number,
                auth_mode=auth_mode,
            )
            if not review_result.allowed:
                audit_log(
                    "review_create_denied",
                    "gh_execute",
                    success=False,
                    details={
                        "api_path": api_path,
                        "pr_number": rv_pr_number,
                        "reason": review_result.reason,
                    },
                )
                return make_error(
                    review_result.reason,
                    status_code=403,
                    details=review_result.to_dict(),
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
# Jira REST Endpoints
# =============================================================================
#
# Read-only wrappers around Atlassian Cloud's REST API v3.  Routes live on
# the ``/api/v1/jira/*`` prefix and mirror the shape of ``/api/v1/gh/*``:
# session auth, private-mode gate, project allowlist, structured audit log.
#
# Credentials come from ``gateway/jira_credentials.py`` (loaded from the same
# ``secrets.env`` file as the GitHub and Anthropic credentials) and are
# never exported to the sandbox.  See:
# - gateway/jira_client.py       — client + path allowlist
# - gateway/jira_policy.py       — project allowlist loader
# - gateway/jira_search.py       — JQL project-scope extractor
# - gateway/mode_gate.py         — @require_private_mode decorator

# Regex for the Jira ticket-key shape agents are allowed to pass in
# ``/api/v1/jira/ticket/*`` request bodies.  ``jira_client`` does its own
# allowlist check on the full REST path, but we validate the shape here so
# the error message is actionable before we ever look at the client.
_JIRA_TICKET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
_JIRA_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _session_jira_context() -> dict[str, Any]:
    """Return session-scoped fields to include in Jira audit records.

    Pipeline ID, agent role, and the new ``jira_ticket`` are observational
    — they aren't used as policy gates (the project allowlist is the only
    hard boundary — refine decision #9) but they make the audit trail
    self-describing.
    """
    ctx: dict[str, Any] = {
        "session_mode": getattr(g, "session_mode", None),
    }
    session = getattr(g, "session", None)
    if session is not None:
        ctx["pipeline_id"] = getattr(session, "pipeline_id", None)
        ctx["agent_role"] = getattr(session, "agent_role", None)
        ctx["jira_ticket"] = getattr(session, "jira_ticket", None)
    return ctx


def _jira_error_from_upstream(exc: JiraUpstreamError) -> tuple[Response, int]:
    """Translate a ``JiraUpstreamError`` to an HTTP response.

    Atlassian status codes in the 4xx range are passed through so the agent
    sees the real reason; 5xx upstream errors collapse to a 502 with the
    raw body in the audit trail.
    """
    if 400 <= exc.status_code < 500:
        status = exc.status_code
    else:
        status = 502
    return make_error(
        f"Jira upstream error {exc.status_code}",
        status_code=status,
        details={
            "upstream_status": exc.status_code,
            "upstream_body": exc.body,
            "path": exc.path,
        },
    )


def _jira_not_configured_error(exc: JiraCredentialsUnavailable) -> tuple[Response, int]:
    """Translate missing credentials to an HTTP 503 response."""
    return make_error(
        "Jira credentials not configured on the gateway",
        status_code=503,
        details={"reason": str(exc)},
    )


def _project_not_allowlisted_response(
    *,
    event: str,
    ticket: str | None,
    project: str | None,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Emit a structured audit record and return the canonical 403."""
    details: dict[str, Any] = {"project": project, "reason": reason}
    if ticket is not None:
        details["ticket"] = ticket
    if extra:
        details.update(extra)
    details.update(_session_jira_context())
    audit_log(event, event, success=False, details=details)
    return make_error(
        "Jira project not allowlisted",
        status_code=403,
        details={"project": project, "reason": reason},
    )


@app.route("/api/v1/jira/ticket/get", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_get() -> tuple[Response, int] | Response:
    """Fetch a single Jira issue.

    Request body::

        {"ticket": "FOO-123", "fields": ["summary", "status"]}

    ``fields`` is optional; when omitted, Atlassian returns the default field
    set.  ``expand`` defaults to ``renderedBody,renderedFields`` in the
    client so agents receive both ADF and rendered HTML.
    """
    data = request.get_json(silent=True) or {}
    ticket = data.get("ticket")
    fields = data.get("fields")

    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        audit_log(
            "jira_ticket_get_rejected",
            "jira_ticket_get",
            success=False,
            details={"reason": "invalid ticket shape", "ticket": ticket, **_session_jira_context()},
        )
        return make_error(
            "Invalid ticket key (expected e.g. 'FOO-123')",
            status_code=400,
            details={"ticket": ticket},
        )

    project = extract_project_key(ticket)
    if not is_project_allowed(project):
        return _project_not_allowlisted_response(
            event="jira_ticket_get_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    try:
        cleaned_fields = validate_jira_fields(fields)
    except ValueError as exc:
        audit_log(
            "jira_ticket_get_rejected",
            "jira_ticket_get",
            success=False,
            details={"reason": str(exc), "ticket": ticket, **_session_jira_context()},
        )
        return make_error(f"Invalid fields: {exc}", status_code=400)

    try:
        body = get_jira_client().get_ticket(ticket, cleaned_fields or None)
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        audit_log(
            "jira_ticket_get_upstream_error",
            "jira_ticket_get",
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    audit_log(
        "jira_ticket_get",
        "jira_ticket_get",
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "not_found": body.get("status") == "not_found",
            **_session_jira_context(),
        },
    )
    return make_success("Jira ticket fetched", body)


@app.route("/api/v1/jira/search", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_search() -> tuple[Response, int] | Response:
    """Run a JQL query against Atlassian Cloud.

    Request body::

        {"jql": "project = ENG AND status = Open",
         "fields": [...],
         "nextPageToken": "...",
         "maxResults": 50}

    The JQL must be statically provable as scoped to allowlisted projects.
    See ``gateway/jira_search.py`` for the exact acceptance rules.
    """
    data = request.get_json(silent=True) or {}
    jql = data.get("jql")
    fields = data.get("fields")
    next_page_token = data.get("nextPageToken")
    max_results = data.get("maxResults")

    if not isinstance(jql, str) or not jql.strip():
        audit_log(
            "jira_search_rejected",
            "jira_search",
            success=False,
            details={"reason": "jql required", **_session_jira_context()},
        )
        return make_error("jql is required", status_code=400)

    # Import allowlist lazily because ``allowed_projects`` resolves the
    # policy singleton on first access.  Getting the frozenset once per
    # request keeps the mtime check out of the hot path for tests that
    # monkeypatch ``is_project_allowed`` directly.
    try:
        from .jira_policy import allowed_projects
    except ImportError:
        from jira_policy import allowed_projects  # type: ignore[no-redef]
    allowed = allowed_projects()

    scope = extract_search_projects(jql, allowed)
    if scope.projects is None:
        audit_log(
            "jira_search_rejected",
            "jira_search",
            success=False,
            details={
                "reason": scope.reason,
                "jql_length": len(jql),
                **_session_jira_context(),
            },
        )
        return make_error(
            f"JQL rejected: {scope.reason}",
            status_code=403,
            details={"reason": scope.reason},
        )

    try:
        cleaned_fields = validate_jira_fields(fields)
    except ValueError as exc:
        audit_log(
            "jira_search_rejected",
            "jira_search",
            success=False,
            details={"reason": str(exc), **_session_jira_context()},
        )
        return make_error(f"Invalid fields: {exc}", status_code=400)

    # Normalise max_results: accept an int or a string-that-parses.  Missing
    # / invalid falls back to the client-side default (50, capped at 100).
    effective_max: int | None = None
    if max_results is not None:
        try:
            effective_max = max(1, min(int(max_results), 100))
        except TypeError, ValueError:
            audit_log(
                "jira_search_rejected",
                "jira_search",
                success=False,
                details={
                    "reason": "maxResults must be an integer",
                    **_session_jira_context(),
                },
            )
            return make_error("maxResults must be an integer", status_code=400)

    try:
        body = get_jira_client().search(
            jql=jql,
            fields=cleaned_fields or None,
            next_page_token=next_page_token if isinstance(next_page_token, str) else None,
            max_results=effective_max,
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        audit_log(
            "jira_search_upstream_error",
            "jira_search",
            success=False,
            details={
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    audit_log(
        "jira_search",
        "jira_search",
        success=True,
        details={
            "projects_extracted": sorted(scope.projects),
            "jql_length": len(jql),
            "max_results": effective_max,
            "next_page_token_present": bool(next_page_token),
            **_session_jira_context(),
        },
    )
    return make_success("Jira search executed", body)


@app.route("/api/v1/jira/ticket/comments", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_comments() -> tuple[Response, int] | Response:
    """Fetch comments for a Jira issue."""
    data = request.get_json(silent=True) or {}
    ticket = data.get("ticket")

    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        audit_log(
            "jira_ticket_comments_rejected",
            "jira_ticket_comments",
            success=False,
            details={"reason": "invalid ticket shape", "ticket": ticket, **_session_jira_context()},
        )
        return make_error(
            "Invalid ticket key (expected e.g. 'FOO-123')",
            status_code=400,
            details={"ticket": ticket},
        )

    project = extract_project_key(ticket)
    if not is_project_allowed(project):
        return _project_not_allowlisted_response(
            event="jira_ticket_comments_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    try:
        body = get_jira_client().get_comments(ticket)
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        audit_log(
            "jira_ticket_comments_upstream_error",
            "jira_ticket_comments",
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    audit_log(
        "jira_ticket_comments",
        "jira_ticket_comments",
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "not_found": body.get("status") == "not_found",
            **_session_jira_context(),
        },
    )
    return make_success("Jira ticket comments fetched", body)


@app.route("/api/v1/jira/execute", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_execute() -> tuple[Response, int] | Response:
    """Generic read-only passthrough for whitelisted Jira REST paths.

    Request body::

        {"method": "GET",
         "path": "issue/FOO-123",
         "query": {"fields": "summary"},
         "body": null}

    Only methods + paths accepted by ``validate_jira_api_path`` are allowed.
    Write verbs (DELETE/PUT/PATCH) and path fragments listed in
    ``JIRA_WRITE_VERBS_DENIED`` are refused unconditionally.
    """
    data = request.get_json(silent=True) or {}
    method = data.get("method") or "GET"
    path = data.get("path")
    query = data.get("query")
    req_body = data.get("body")

    if not isinstance(path, str) or not path:
        audit_log(
            "jira_execute_rejected",
            "jira_execute",
            success=False,
            details={"reason": "path required", **_session_jira_context()},
        )
        return make_error("path is required", status_code=400)

    if not isinstance(method, str):
        audit_log(
            "jira_execute_rejected",
            "jira_execute",
            success=False,
            details={"reason": "method must be a string", **_session_jira_context()},
        )
        return make_error("method must be a string", status_code=400)

    method_upper = method.upper()
    ok, reason = validate_jira_api_path(path, method_upper)
    if not ok:
        audit_log(
            "jira_execute_denied",
            "jira_execute",
            success=False,
            details={
                "method": method_upper,
                "path": path,
                "reason": reason,
                **_session_jira_context(),
            },
        )
        return make_error(
            f"Jira API call rejected: {reason}",
            status_code=403,
            details={"method": method_upper, "path": path, "reason": reason},
        )

    # Path is structurally OK — extract project key (if any) and allowlist it.
    # The accepted shapes are ``issue/<KEY>[/comment]`` and
    # ``project/<KEY>``.  Both carry a project key inline that is checked
    # against the allowlist.  Bare ``project`` is excluded (would leak all
    # projects visible to the API token).
    stripped = path.strip("/").split("?", 1)[0]
    ticket: str | None = None
    project: str | None = None
    head = stripped.split("/")
    if head and head[0] == "issue" and len(head) >= 2:
        ticket = head[1]
        project = extract_project_key(ticket)
    elif head and head[0] == "project" and len(head) >= 2:
        project = head[1]

    if project is not None and not is_project_allowed(project):
        return _project_not_allowlisted_response(
            event="jira_execute_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
            extra={"method": method_upper, "path": path},
        )

    # Normalise query & body — they must be dicts or None.
    if query is not None and not isinstance(query, dict):
        return make_error("query must be an object", status_code=400)
    if req_body is not None and not isinstance(req_body, dict):
        return make_error("body must be an object", status_code=400)

    try:
        body = get_jira_client().execute_raw(
            method=method_upper,
            path=stripped,
            query=query,
            body=req_body,
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        audit_log(
            "jira_execute_upstream_error",
            "jira_execute",
            success=False,
            details={
                "method": method_upper,
                "path": stripped,
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    audit_log(
        "jira_execute",
        "jira_execute",
        success=True,
        details={
            "method": method_upper,
            "path": stripped,
            "project": project,
            "ticket": ticket,
            **_session_jira_context(),
        },
    )
    return make_success("Jira API call executed", body)


# -----------------------------------------------------------------------------
# Jira write verbs (issue #1924)
# -----------------------------------------------------------------------------
#
# Each route validates a tight per-verb body schema before calling the
# matching ``JiraClient`` write method.  Body content (``description``,
# ``comment.body``) is **never** logged to the audit trail — only structural
# metadata (which fields were present, content lengths, label *values*).

# Atlassian's documented summary cap.
_JIRA_SUMMARY_MAX_CHARS: int = 255

# Description / comment body cap (refine feedback Q2): 32 KiB.  Atlassian
# itself accepts larger but the gateway shouldn't proxy multi-MB bodies.
_JIRA_BODY_MAX_CHARS: int = 32 * 1024

# Labels: max 30 entries, each up to 50 chars.
_JIRA_LABELS_MAX_COUNT: int = 30
_JIRA_LABEL_MAX_CHARS: int = 50

# Allowlisted top-level keys for write bodies.  Anything outside this set is
# rejected as either custom-field smuggling, HTTP-method tunnelling, or a
# typo.  Keep this surface tight on purpose.
_JIRA_CREATE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "project",
        "issuetype",
        "summary",
        "description",
        "labels",
        "parent",
        "epicLink",
        "idempotencyKey",
    }
)
_JIRA_EDIT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "ticket",
        "summary",
        "description",
        "labels",
        "addLabels",
        "removeLabels",
        "notifyUsers",
    }
)
_JIRA_COMMENT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "ticket",
        "body",
        "idempotencyKey",
    }
)
_JIRA_LINK_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "type",
        "inwardIssue",
        "outwardIssue",
        "comment",
        "idempotencyKey",
    }
)

# Atlassian-known issuetype names accepted in create requests (refine
# decision-8: both name and numeric ID are accepted).  Names outside this
# tight set are rejected to keep operator surface predictable.
_JIRA_ALLOWED_ISSUETYPE_NAMES: frozenset[str] = frozenset(
    {"Task", "Story", "Bug", "Epic", "Sub-task", "Subtask"}
)


def _jira_write_audit_meta(body: dict[str, Any]) -> dict[str, Any]:
    """Return structural metadata for a write-verb audit record.

    Logs **field names changed**, **content lengths**, **label values**, and
    **link-type names** (refine feedback Q5) — never raw body content.
    """
    meta: dict[str, Any] = {}
    fields_present: list[str] = []
    for key in (
        "summary",
        "description",
        "labels",
        "addLabels",
        "removeLabels",
        "parent",
        "epicLink",
        "issuetype",
        "project",
        "ticket",
        "body",
        "comment",
        "type",
        "inwardIssue",
        "outwardIssue",
    ):
        if key in body:
            fields_present.append(key)
    if fields_present:
        meta["fields_present"] = fields_present

    summary = body.get("summary")
    if isinstance(summary, str):
        meta["summary_length"] = len(summary)

    description = body.get("description")
    if isinstance(description, str):
        meta["description_length"] = len(description)
    elif isinstance(description, dict):
        meta["description_length"] = -1  # ADF passthrough; length unknown
        meta["description_kind"] = "adf"

    comment_body = body.get("body")
    if isinstance(comment_body, str):
        meta["body_length"] = len(comment_body)
    elif isinstance(comment_body, dict):
        meta["body_length"] = -1
        meta["body_kind"] = "adf"

    labels = body.get("labels")
    if isinstance(labels, list):
        meta["labels"] = [v for v in labels if isinstance(v, str)]
    add_labels = body.get("addLabels")
    if isinstance(add_labels, list):
        meta["add_labels"] = [v for v in add_labels if isinstance(v, str)]
    remove_labels = body.get("removeLabels")
    if isinstance(remove_labels, list):
        meta["remove_labels"] = [v for v in remove_labels if isinstance(v, str)]

    link_type = body.get("type")
    if isinstance(link_type, str):
        meta["link_type"] = link_type

    issuetype = body.get("issuetype")
    if isinstance(issuetype, dict):
        if isinstance(issuetype.get("name"), str):
            meta["issuetype_name"] = issuetype["name"]
        if isinstance(issuetype.get("id"), str):
            meta["issuetype_id"] = issuetype["id"]
    elif isinstance(issuetype, str):
        meta["issuetype_name"] = issuetype

    return meta


def _validate_jira_write_keys(
    body: dict[str, Any], allowed: frozenset[str], operation: str
) -> tuple[Response, int] | None:
    """Reject unknown / suspect top-level body keys.

    Returns a 400 response when an unknown key is found (custom-field
    smuggling, ``method``-tunnel attempts, or typos), otherwise ``None``.
    """
    extras = sorted(set(body) - allowed)
    if not extras:
        return None
    audit_log(
        f"{operation}_rejected",
        operation,
        success=False,
        details={
            "reason": "unknown_body_keys",
            "unknown_keys": extras,
            **_session_jira_context(),
        },
    )
    return make_error(
        f"Unknown body keys: {extras}",
        status_code=400,
        details={"unknown_keys": extras},
    )


def _validate_jira_text_field(
    value: Any,
    *,
    field: str,
    max_chars: int,
    allow_adf: bool = False,
) -> tuple[str | dict[str, Any] | None, tuple[Response, int] | None]:
    """Validate a string-or-ADF text field.

    Returns ``(cleaned_value, None)`` on success or
    ``(None, error_response)`` on failure.  ``None`` is treated as "not
    supplied"; callers handle the optional vs required distinction.
    """
    if value is None:
        return None, None

    if allow_adf and isinstance(value, dict):
        # ADF dict — ensure it's structurally valid; size cap applied to
        # serialised length so a malicious nested ADF tree can't hide.
        try:
            from .jira_adf import is_adf_dict
        except ImportError:
            from jira_adf import is_adf_dict  # type: ignore[no-redef, import-untyped]
        if not is_adf_dict(value):
            return None, make_error(
                f"{field} must be a string or a valid ADF document",
                status_code=400,
            )
        # Size check via serialised length as a proxy.
        serialised = json.dumps(value)
        if len(serialised) > max_chars:
            return None, make_error(
                f"{field} exceeds maximum length ({max_chars} chars)",
                status_code=400,
            )
        return value, None

    if not isinstance(value, str):
        return None, make_error(f"{field} must be a string", status_code=400)
    if len(value) > max_chars:
        return None, make_error(
            f"{field} exceeds maximum length ({max_chars} chars)",
            status_code=400,
        )
    return value, None


def _validate_jira_labels(
    value: Any, *, field: str
) -> tuple[list[str] | None, tuple[Response, int] | None]:
    """Validate a labels list (count cap + per-entry length cap)."""
    if value is None:
        return None, None
    if not isinstance(value, list):
        return None, make_error(f"{field} must be a list", status_code=400)
    if len(value) > _JIRA_LABELS_MAX_COUNT:
        return None, make_error(
            f"{field} exceeds maximum of {_JIRA_LABELS_MAX_COUNT} entries",
            status_code=400,
        )
    cleaned: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            return None, make_error(f"{field} entries must be strings", status_code=400)
        if not entry:
            return None, make_error(f"{field} entries must be non-empty", status_code=400)
        if len(entry) > _JIRA_LABEL_MAX_CHARS:
            return None, make_error(
                f"{field} entry exceeds maximum length ({_JIRA_LABEL_MAX_CHARS} chars)",
                status_code=400,
            )
        if " " in entry:
            return None, make_error(
                f"{field} entries must not contain whitespace",
                status_code=400,
            )
        cleaned.append(entry)
    return cleaned, None


@app.route("/api/v1/jira/ticket/create", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_create() -> tuple[Response, int] | Response:
    """Create a Jira issue via ``POST /rest/api/3/issue``.

    Request body::

        {"project": "ENG",
         "issuetype": "Task" | {"name": "Task"} | {"id": "10001"},
         "summary": "...",
         "description": "..." | <ADF dict> | null,
         "labels": ["foo", "bar"],
         "parent": "ENG-1" | null,
         "epicLink": "ENG-2" | null,
         "idempotencyKey": "..." | null}

    ``parent`` and ``epicLink`` are mutually exclusive.  Cross-project
    parents are rejected (refine decision-17).  ``epicLink`` dispatches via
    ``JiraPolicy.epic_link_field`` (``parent`` or ``customfield_10014``).
    """
    operation = "jira_ticket_create"
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return make_error("body must be a JSON object", status_code=400)

    err = _validate_jira_write_keys(data, _JIRA_CREATE_ALLOWED_KEYS, operation)
    if err is not None:
        return err

    project = data.get("project")
    issuetype = data.get("issuetype")
    summary = data.get("summary")
    description = data.get("description")
    labels = data.get("labels")
    parent = data.get("parent")
    epic_link = data.get("epicLink")
    idempotency_key = data.get("idempotencyKey")

    if not isinstance(project, str) or not _JIRA_PROJECT_KEY_RE.fullmatch(project):
        audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "invalid project shape", **_session_jira_context()},
        )
        return make_error("Invalid project key", status_code=400)

    if not is_project_allowed(project):
        return _project_not_allowlisted_response(
            event=f"{operation}_denied",
            ticket=None,
            project=project,
            reason="project not allowlisted",
        )

    # issuetype: name or numeric id (refine decision-8).
    if isinstance(issuetype, str):
        if issuetype not in _JIRA_ALLOWED_ISSUETYPE_NAMES:
            audit_log(
                f"{operation}_rejected",
                operation,
                success=False,
                details={
                    "reason": "unknown issuetype",
                    "issuetype": issuetype,
                    **_session_jira_context(),
                },
            )
            return make_error(
                f"Unknown issuetype name: {issuetype!r}",
                status_code=400,
            )
        issuetype_arg: dict[str, Any] | str = issuetype
    elif isinstance(issuetype, dict):
        if "name" in issuetype:
            name = issuetype["name"]
            if not isinstance(name, str) or name not in _JIRA_ALLOWED_ISSUETYPE_NAMES:
                audit_log(
                    f"{operation}_rejected",
                    operation,
                    success=False,
                    details={"reason": "unknown issuetype name", **_session_jira_context()},
                )
                return make_error(f"Unknown issuetype name: {name!r}", status_code=400)
            issuetype_arg = {"name": name}
        elif "id" in issuetype:
            type_id = issuetype["id"]
            if not isinstance(type_id, str) or not type_id.isdigit():
                return make_error("issuetype.id must be a numeric string", status_code=400)
            issuetype_arg = {"id": type_id}
        else:
            return make_error("issuetype must include name or id", status_code=400)
    else:
        return make_error(
            "issuetype must be a string, or a dict with 'name' or 'id'",
            status_code=400,
        )

    if not isinstance(summary, str) or not summary.strip():
        return make_error("summary is required", status_code=400)
    if len(summary) > _JIRA_SUMMARY_MAX_CHARS:
        return make_error(
            f"summary exceeds maximum length ({_JIRA_SUMMARY_MAX_CHARS} chars)",
            status_code=400,
        )

    cleaned_description, err = _validate_jira_text_field(
        description, field="description", max_chars=_JIRA_BODY_MAX_CHARS, allow_adf=True
    )
    if err is not None:
        return err

    cleaned_labels, err = _validate_jira_labels(labels, field="labels")
    if err is not None:
        return err

    if parent is not None and epic_link is not None:
        audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "parent_and_epic_link", **_session_jira_context()},
        )
        return make_error(
            "parent and epicLink are mutually exclusive",
            status_code=400,
        )

    if parent is not None:
        if not isinstance(parent, str) or not _JIRA_TICKET_KEY_RE.fullmatch(parent):
            return make_error("Invalid parent ticket key", status_code=400)
        # Cross-project parent rejection (refine decision-17).
        parent_project = extract_project_key(parent)
        if parent_project != project:
            audit_log(
                f"{operation}_rejected",
                operation,
                success=False,
                details={
                    "reason": "cross_project_parent",
                    "project": project,
                    "parent_project": parent_project,
                    **_session_jira_context(),
                },
            )
            return make_error(
                "parent.key project must match the new ticket's project",
                status_code=400,
                details={"project": project, "parent_project": parent_project},
            )

    if epic_link is not None:
        if not isinstance(epic_link, str) or not _JIRA_TICKET_KEY_RE.fullmatch(epic_link):
            return make_error("Invalid epicLink ticket key", status_code=400)
        # epicLink writes to the same Atlassian field as `parent` when the
        # site uses next-gen / company-managed projects (default
        # `epic_link_field == "parent"`).  That makes `epicLink` a literal
        # alias for `parent` at the wire level, so it MUST inherit the same
        # allowlist + cross-project policy as `parent` (decision-9, decision-17).
        # Otherwise an agent in an allowlisted project could parent a new
        # ticket under an epic in a non-allowlisted project just by routing
        # through the `epicLink` shorthand instead of `parent`.
        epic_project = extract_project_key(epic_link)
        if not is_project_allowed(epic_project):
            return _project_not_allowlisted_response(
                event=f"{operation}_denied",
                ticket=epic_link,
                project=epic_project,
                reason="epicLink project not allowlisted",
            )
        if epic_project != project:
            audit_log(
                f"{operation}_rejected",
                operation,
                success=False,
                details={
                    "reason": "cross_project_epic_link",
                    "project": project,
                    "epic_project": epic_project,
                    **_session_jira_context(),
                },
            )
            return make_error(
                "epicLink project must match the new ticket's project",
                status_code=400,
                details={"project": project, "epic_project": epic_project},
            )

    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return make_error("idempotencyKey must be a string", status_code=400)

    try:
        status_code, body_json, cache_hit = get_jira_client().create_issue(
            project_key=project,
            issuetype=issuetype_arg,
            summary=summary,
            description=cleaned_description,
            labels=cleaned_labels,
            parent=parent,
            epic_link=epic_link,
            epic_link_field=jira_epic_link_field(),
            idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        audit_log(
            f"{operation}_upstream_error",
            operation,
            success=False,
            details={
                "project": project,
                "upstream_status": exc.status_code,
                **_jira_write_audit_meta(data),
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    new_key = body_json.get("key") if isinstance(body_json, dict) else None
    new_id = body_json.get("id") if isinstance(body_json, dict) else None
    self_url = body_json.get("self") if isinstance(body_json, dict) else None
    browse_url: str | None = None
    if isinstance(self_url, str) and "/rest/api/" in self_url and isinstance(new_key, str):
        # Trim the trailing /rest/api/3/issue/<id> to recover the site root,
        # then append /browse/<KEY>.  This mirrors what Atlassian shows in
        # its UI links.
        site = self_url.split("/rest/api/", 1)[0]
        browse_url = f"{site}/browse/{new_key}"

    # Match the doc's audit grammar: rejection events use ``_rejected`` /
    # ``_denied`` / ``_upstream_error`` suffixes, so successful writes use
    # ``_ok`` (reviewer_code_holistic cycle 1 finding #3, #1924).
    audit_log(
        f"{operation}_ok",
        operation,
        success=True,
        details={
            "project": project,
            "ticket": new_key,
            "upstream_status": status_code,
            "idempotency_key_present": bool(idempotency_key),
            "idempotency_hit": cache_hit,
            **_jira_write_audit_meta(data),
            **_session_jira_context(),
        },
    )

    envelope: dict[str, Any] = {
        "status": "created",
        "key": new_key,
        "id": new_id,
        "browse_url": browse_url,
    }
    return make_success("Jira ticket created", envelope)


@app.route("/api/v1/jira/ticket/edit", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_edit() -> tuple[Response, int] | Response:
    """Edit a Jira issue via ``PUT /rest/api/3/issue/{key}``.

    Request body::

        {"ticket": "ENG-1",
         "summary": "..." | null,
         "description": "..." | <ADF dict> | null,
         "labels": [...] | null,                # replace mode
         "addLabels": [...] | null,             # incremental mode
         "removeLabels": [...] | null,
         "notifyUsers": false | true}           # default: false

    Replace-mode (``labels``) and incremental-mode
    (``addLabels``/``removeLabels``) are mutually exclusive.
    """
    operation = "jira_ticket_edit"
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return make_error("body must be a JSON object", status_code=400)

    err = _validate_jira_write_keys(data, _JIRA_EDIT_ALLOWED_KEYS, operation)
    if err is not None:
        return err

    ticket = data.get("ticket")
    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "invalid ticket shape", **_session_jira_context()},
        )
        return make_error("Invalid ticket key", status_code=400)

    project = extract_project_key(ticket)
    if not is_project_allowed(project):
        return _project_not_allowlisted_response(
            event=f"{operation}_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    summary = data.get("summary")
    description = data.get("description")
    labels = data.get("labels")
    add_labels = data.get("addLabels")
    remove_labels = data.get("removeLabels")
    notify_users = data.get("notifyUsers", False)

    if summary is not None:
        if not isinstance(summary, str):
            return make_error("summary must be a string", status_code=400)
        if len(summary) > _JIRA_SUMMARY_MAX_CHARS:
            return make_error(
                f"summary exceeds maximum length ({_JIRA_SUMMARY_MAX_CHARS} chars)",
                status_code=400,
            )

    cleaned_description, err = _validate_jira_text_field(
        description, field="description", max_chars=_JIRA_BODY_MAX_CHARS, allow_adf=True
    )
    if err is not None:
        return err

    has_replace = labels is not None
    has_incremental = (add_labels is not None) or (remove_labels is not None)
    if has_replace and has_incremental:
        audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "mixed_label_modes", **_session_jira_context()},
        )
        return make_error(
            "labels and addLabels/removeLabels are mutually exclusive",
            status_code=400,
        )

    cleaned_labels, err = _validate_jira_labels(labels, field="labels")
    if err is not None:
        return err
    cleaned_add, err = _validate_jira_labels(add_labels, field="addLabels")
    if err is not None:
        return err
    cleaned_remove, err = _validate_jira_labels(remove_labels, field="removeLabels")
    if err is not None:
        return err

    if not isinstance(notify_users, bool):
        return make_error("notifyUsers must be a boolean", status_code=400)

    # Require at least one mutating field to avoid no-op edits hitting upstream.
    if (
        summary is None
        and cleaned_description is None
        and cleaned_labels is None
        and cleaned_add is None
        and cleaned_remove is None
    ):
        return make_error(
            "edit requires at least one of summary/description/labels/addLabels/removeLabels",
            status_code=400,
        )

    try:
        get_jira_client().edit_issue(
            key=ticket,
            summary=summary,
            description=cleaned_description,
            labels=cleaned_labels,
            add_labels=cleaned_add,
            remove_labels=cleaned_remove,
            notify_users=notify_users,
        )
    except ValueError as exc:
        # Defence in depth — the route already rejected mixed modes.
        return make_error(str(exc), status_code=400)
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        audit_log(
            f"{operation}_upstream_error",
            operation,
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                **_jira_write_audit_meta(data),
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    audit_log(
        f"{operation}_ok",
        operation,
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "notify_users": notify_users,
            # editIssue does not consult the idempotency cache (Atlassian
            # PUT is naturally idempotent), but the field is included here
            # for grammar parity with the create / comment / link routes.
            "idempotency_key_present": False,
            "idempotency_hit": False,
            **_jira_write_audit_meta(data),
            **_session_jira_context(),
        },
    )
    return make_success("Jira ticket updated", {"status": "updated", "key": ticket})


@app.route("/api/v1/jira/ticket/comment/add", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_comment_add() -> tuple[Response, int] | Response:
    """Add a comment to a Jira issue.

    Request body::

        {"ticket": "ENG-1",
         "body": "..." | <ADF dict>,
         "idempotencyKey": "..." | null}

    Visibility (role/group restriction) is rejected — v1 does not expose
    that knob (refine decision-6).  Body content is **never** logged.
    """
    operation = "jira_ticket_comment_add"
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return make_error("body must be a JSON object", status_code=400)

    if "visibility" in data:
        return make_error(
            "comment visibility is not supported in v1",
            status_code=400,
        )

    err = _validate_jira_write_keys(data, _JIRA_COMMENT_ALLOWED_KEYS, operation)
    if err is not None:
        return err

    ticket = data.get("ticket")
    body = data.get("body")
    idempotency_key = data.get("idempotencyKey")

    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "invalid ticket shape", **_session_jira_context()},
        )
        return make_error("Invalid ticket key", status_code=400)

    project = extract_project_key(ticket)
    if not is_project_allowed(project):
        return _project_not_allowlisted_response(
            event=f"{operation}_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    cleaned_body, err = _validate_jira_text_field(
        body, field="body", max_chars=_JIRA_BODY_MAX_CHARS, allow_adf=True
    )
    if err is not None:
        return err
    if cleaned_body is None:
        return make_error("body is required", status_code=400)

    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return make_error("idempotencyKey must be a string", status_code=400)

    try:
        _status, comment_json, cache_hit = get_jira_client().add_comment(
            key=ticket,
            body=cleaned_body,
            idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        audit_log(
            f"{operation}_upstream_error",
            operation,
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                # Note: _jira_write_audit_meta intentionally avoids body content;
                # we still record body_length / body_kind here.
                **_jira_write_audit_meta(data),
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    audit_log(
        f"{operation}_ok",
        operation,
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "idempotency_key_present": bool(idempotency_key),
            "idempotency_hit": cache_hit,
            **_jira_write_audit_meta(data),
            **_session_jira_context(),
        },
    )
    return make_success("Jira comment added", comment_json)


@app.route("/api/v1/jira/issue-link/create", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_issue_link_create() -> tuple[Response, int] | Response:
    """Create an issue link between two tickets.

    Request body::

        {"type": "Blocks",
         "inwardIssue": "ENG-1",
         "outwardIssue": "ENG-2",
         "comment": "..." | <ADF dict> | null,
         "idempotencyKey": "..." | null}

    Both tickets' projects must be in the allowlist (refine decision-9).
    Atlassian does **not** dedupe identical triples, so the gateway uses
    its idempotency cache (decision-28) when ``idempotencyKey`` is set.
    """
    operation = "jira_issue_link_create"
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return make_error("body must be a JSON object", status_code=400)

    err = _validate_jira_write_keys(data, _JIRA_LINK_ALLOWED_KEYS, operation)
    if err is not None:
        return err

    link_type = data.get("type")
    inward = data.get("inwardIssue")
    outward = data.get("outwardIssue")
    comment = data.get("comment")
    idempotency_key = data.get("idempotencyKey")

    if not isinstance(link_type, str) or not link_type:
        return make_error("type is required", status_code=400)
    if not jira_link_type_allowed(link_type):
        audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={
                "reason": "link_type_not_allowlisted",
                "link_type": link_type,
                **_session_jira_context(),
            },
        )
        return make_error(
            f"Link type {link_type!r} not in allowlist",
            status_code=400,
            details={"link_type": link_type},
        )

    if not isinstance(inward, str) or not _JIRA_TICKET_KEY_RE.fullmatch(inward):
        return make_error("inwardIssue must be a Jira ticket key", status_code=400)
    if not isinstance(outward, str) or not _JIRA_TICKET_KEY_RE.fullmatch(outward):
        return make_error("outwardIssue must be a Jira ticket key", status_code=400)

    inward_project = extract_project_key(inward)
    outward_project = extract_project_key(outward)
    for proj, ticket in ((inward_project, inward), (outward_project, outward)):
        if not is_project_allowed(proj):
            return _project_not_allowlisted_response(
                event=f"{operation}_denied",
                ticket=ticket,
                project=proj,
                reason="project not allowlisted",
            )

    cleaned_comment, err = _validate_jira_text_field(
        comment, field="comment", max_chars=_JIRA_BODY_MAX_CHARS, allow_adf=True
    )
    if err is not None:
        return err

    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return make_error("idempotencyKey must be a string", status_code=400)

    try:
        _status, _link_json, cache_hit = get_jira_client().create_issue_link(
            link_type=link_type,
            inward_key=inward,
            outward_key=outward,
            comment=cleaned_comment,
            idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        audit_log(
            f"{operation}_upstream_error",
            operation,
            success=False,
            details={
                "inwardIssue": inward,
                "outwardIssue": outward,
                "type": link_type,
                "upstream_status": exc.status_code,
                **_jira_write_audit_meta(data),
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    audit_log(
        f"{operation}_ok",
        operation,
        success=True,
        details={
            "inwardIssue": inward,
            "outwardIssue": outward,
            "type": link_type,
            "inward_project": inward_project,
            "outward_project": outward_project,
            "idempotency_key_present": bool(idempotency_key),
            "idempotency_hit": cache_hit,
            **_jira_write_audit_meta(data),
            **_session_jira_context(),
        },
    )
    return make_success(
        "Jira issue link created",
        {
            "status": "created",
            "inwardIssue": inward,
            "outwardIssue": outward,
            "type": link_type,
        },
    )


# =============================================================================
# Confluence REST Endpoints
# =============================================================================
#
# Read-only wrappers around Atlassian Cloud Confluence's REST API.  Routes
# live on the ``/api/v1/confluence/*`` prefix and mirror the shape of
# ``/api/v1/jira/*``: session auth, private-mode gate, space allowlist,
# structured audit log.
#
# Credentials come from ``gateway/confluence_credentials.py`` (loaded from
# the same ``secrets.env`` file as Jira and GitHub) and are never exported
# to the sandbox.  See:
# - gateway/confluence_client.py       — client + path allowlist + redaction
# - gateway/confluence_policy.py       — space allowlist loader
# - gateway/confluence_search.py       — CQL space-scope extractor
# - gateway/mode_gate.py               — @require_private_mode decorator

# Numeric Confluence page id / space id shape.
_CONFLUENCE_PAGE_ID_RE = re.compile(r"^\d+$")
_CONFLUENCE_SPACE_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _session_confluence_context() -> dict[str, Any]:
    """Return session-scoped fields to include in Confluence audit records.

    Per refine decision 13 there is no per-session ``session.confluence_*``
    field — pageId / spaceKey are recovered from the request body or
    response per call.
    """
    ctx: dict[str, Any] = {
        "session_mode": getattr(g, "session_mode", None),
    }
    session = getattr(g, "session", None)
    if session is not None:
        ctx["pipeline_id"] = getattr(session, "pipeline_id", None)
        ctx["agent_role"] = getattr(session, "agent_role", None)
    return ctx


def _confluence_error_from_upstream(exc: ConfluenceUpstreamError) -> tuple[Response, int]:
    """Translate a ``ConfluenceUpstreamError`` to an HTTP response.

    Atlassian error envelopes occasionally include user-identifying strings
    (e.g. account ids embedded in messages) and space-enumeration leaks
    (e.g. ``"valid keys are: ENG, DOCS, SECRET"``).  The success-path
    redactor only runs on 2xx bodies, so we apply it here too before the
    upstream body crosses the gateway/sandbox boundary.
    """
    if 400 <= exc.status_code < 500:
        status = exc.status_code
    else:
        status = 502
    return make_error(
        f"Confluence upstream error {exc.status_code}",
        status_code=status,
        details={
            "upstream_status": exc.status_code,
            "upstream_body": _redact_upstream_error_body(exc.body),
            "path": exc.path,
        },
    )


def _redact_upstream_error_body(body: Any) -> Any:
    """Run ``redact_response`` over an Atlassian error envelope.

    Atlassian returns errors as JSON dicts (and very occasionally as plain
    text); the redactor mutates dicts/lists in place.  Non-container shapes
    pass through unchanged.
    """
    if isinstance(body, (dict, list)):
        return redact_response(body)
    return body


def _confluence_not_configured_error(
    exc: ConfluenceCredentialsUnavailable,
) -> tuple[Response, int]:
    """Translate missing credentials to an HTTP 503 response."""
    return make_error(
        "Confluence credentials not configured on the gateway",
        status_code=503,
        details={"reason": str(exc)},
    )


def _confluence_response_too_large(
    exc: ConfluenceResponseTooLarge,
    *,
    page_id: str | None = None,
    space_key: str | None = None,
) -> tuple[Response, int]:
    """Translate an oversized response to HTTP 413."""
    details: dict[str, Any] = {"size_bytes": exc.size_bytes, "path": exc.path}
    if page_id is not None:
        details["pageId"] = page_id
    if space_key is not None:
        details["spaceKey"] = space_key
    return make_error(
        "Confluence response too large",
        status_code=413,
        details=details,
    )


def _confluence_forbidden_response(
    exc: ConfluenceUpstreamForbidden,
    *,
    event: str,
    page_id: str | None = None,
    space_key: str | None = None,
) -> tuple[Response, int]:
    """Translate an upstream 403 into HTTP 403 with the dedicated audit event."""
    details: dict[str, Any] = {
        "upstream_status": 403,
        "reason": "bot_account_lacks_read_access",
        "path": exc.path,
        **_session_confluence_context(),
    }
    if page_id is not None:
        details["pageId"] = page_id
    if space_key is not None:
        details["spaceKey"] = space_key
    audit_log(event, event, success=False, details=details)
    body: dict[str, Any] = {
        "status": "forbidden",
        "reason": "bot_account_lacks_read_access",
    }
    if page_id is not None:
        body["pageId"] = page_id
    if space_key is not None:
        body["spaceKey"] = space_key
    return make_error(
        "Confluence upstream forbidden",
        status_code=403,
        details=body,
    )


def _confluence_space_denied_response(
    *,
    event: str,
    page_id: str | None,
    space_key: str | None,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Emit a structured audit record and return the canonical 403."""
    details: dict[str, Any] = {"spaceKey": space_key, "reason": reason}
    if page_id is not None:
        details["pageId"] = page_id
    if extra:
        details.update(extra)
    details.update(_session_confluence_context())
    audit_log(event, event, success=False, details=details)
    return make_error(
        "Confluence space not allowlisted",
        status_code=403,
        details={"spaceKey": space_key, "reason": reason},
    )


def _resolve_space_key_for_payload(payload: Any) -> str | None:
    """Extract a ``spaceKey`` from an upstream payload, using the client's
    space cache if only ``spaceId`` is present.

    Returns the space key on success; ``None`` if the payload doesn't carry
    one (e.g. v1 fallback with no spaceId — caller falls back to a manual
    list_spaces lookup).
    """
    if not isinstance(payload, dict):
        return None
    direct = payload.get("spaceKey") or payload.get("space_key")
    if isinstance(direct, str) and direct:
        return direct
    # v2 returns ``spaceId`` on page reads; the client caches the mapping
    # opportunistically once ``list_spaces`` runs.
    space_id = payload.get("spaceId")
    if space_id is None:
        space = payload.get("space")
        if isinstance(space, dict):
            sk = space.get("key")
            if isinstance(sk, str) and sk:
                return sk
            space_id = space.get("id")
    if space_id is None:
        return None
    client = get_confluence_client()
    return client.space_cache.key_for_id(str(space_id))


def _resolve_space_key_via_list(allowed: frozenset[str], space_id: str | None) -> str | None:
    """Look up a space key for a space id by warming the space cache.

    Used by the post-fetch allowlist check when the page response carries
    ``spaceId`` but the cache hasn't been populated yet.  Returns ``None``
    if the space isn't visible to the bot (which is itself a deny signal).

    ``allowed`` is unused at this layer; the cache is populated with every
    space the bot can see and the post-fetch allowlist check applies the
    operator allowlist on the resolved key.
    """
    del allowed  # cache holds every visible space; allowlist enforced upstream
    if not space_id:
        return None
    client = get_confluence_client()
    cached = client.space_cache.key_for_id(str(space_id))
    if cached is not None:
        return cached
    # Walk paginated /wiki/api/v2/spaces so a target space on page 2+ still
    # resolves.  populate_space_cache caps iterations defensively.
    try:
        client.populate_space_cache()
    except (
        ConfluenceCredentialsUnavailable,
        ConfluenceUpstreamError,
        ConfluenceUpstreamForbidden,
    ):
        # Forbidden on /wiki/api/v2/spaces (bot lacks space:read globally)
        # is not its own ConfluenceUpstreamError subclass — catch it here
        # so the outer post-fetch check fail-closes through
        # confluence_space_denied rather than leaking a Flask 500.
        return None
    return client.space_cache.key_for_id(str(space_id))


def _confluence_clamp_limit(value: Any) -> int | None:
    """Coerce + clamp a caller-supplied limit (1..HARD_MAX_LIMIT)."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except TypeError, ValueError:
        raise ValueError("limit must be an integer") from None
    if parsed <= 0:
        raise ValueError("limit must be positive")
    return min(parsed, CONFLUENCE_HARD_MAX_LIMIT)


def _validate_confluence_page_id(page_id: Any) -> tuple[bool, str]:
    if not isinstance(page_id, str) or not _CONFLUENCE_PAGE_ID_RE.fullmatch(page_id):
        return False, "invalid pageId shape"
    return True, ""


def _validate_confluence_space_key(space_key: Any) -> tuple[bool, str]:
    if not isinstance(space_key, str) or not _CONFLUENCE_SPACE_KEY_RE.fullmatch(space_key):
        return False, "invalid spaceKey shape"
    return True, ""


def _check_post_fetch_space_allowlist(
    payload: Any,
    *,
    allowed: frozenset[str],
    page_id: str | None,
) -> tuple[bool, str | None]:
    """Verify the response's spaceKey is in the allowlist.

    Returns ``(ok, space_key)``.  When ``ok`` is False the route returns
    HTTP 403 without forwarding the response body; ``space_key`` is the
    resolved key for audit purposes (may be ``None`` if unresolvable).
    """
    if not isinstance(payload, dict):
        return False, None
    if payload.get("status") == "not_found":
        # 404 envelope passes through — no space leakage.
        return True, None
    space_key = _resolve_space_key_for_payload(payload)
    if space_key is None:
        space_id = payload.get("spaceId")
        if isinstance(space_id, (str, int)):
            space_key = _resolve_space_key_via_list(allowed, str(space_id))
    if space_key is None:
        # Couldn't resolve — fail closed.  This protects against the upstream
        # response shape changing.
        return False, None
    return space_key in allowed, space_key


@app.route("/api/v1/confluence/page/get", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_page_get() -> tuple[Response, int] | Response:
    """Fetch a single Confluence page (v2).

    Request body::

        {"pageId": "12345",
         "bodyFormat": ["storage"],
         "expand": null}
    """
    data = request.get_json(silent=True) or {}
    page_id = data.get("pageId")
    body_format = data.get("bodyFormat")
    expand = data.get("expand")

    ok, reason = _validate_confluence_page_id(page_id)
    if not ok:
        audit_log(
            "confluence_page_get_rejected",
            "confluence_page_get",
            success=False,
            details={"reason": reason, "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(
            "Invalid pageId (expected numeric string)",
            status_code=400,
            details={"pageId": page_id},
        )
    assert isinstance(page_id, str)  # narrowed by _validate_confluence_page_id

    allowed = confluence_allowed_spaces()
    try:
        body = get_confluence_client().get_page(page_id, body_format=body_format, expand=expand)
    except ValueError as exc:
        audit_log(
            "confluence_page_get_rejected",
            "confluence_page_get",
            success=False,
            details={"reason": str(exc), "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(f"Invalid request: {exc}", status_code=400)
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        audit_log(
            "confluence_response_too_large",
            "confluence_page_get",
            success=False,
            details={
                "pageId": page_id,
                "size_bytes": exc.size_bytes,
                **_session_confluence_context(),
            },
        )
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        audit_log(
            "confluence_page_get_upstream_error",
            "confluence_page_get",
            success=False,
            details={
                "pageId": page_id,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    ok_space, space_key = _check_post_fetch_space_allowlist(body, allowed=allowed, page_id=page_id)
    if not ok_space:
        return _confluence_space_denied_response(
            event="confluence_space_denied",
            page_id=page_id,
            space_key=space_key,
            reason="space not allowlisted",
        )

    audit_log(
        "confluence_page_get",
        "confluence_page_get",
        success=True,
        details={
            "pageId": page_id,
            "spaceKey": space_key,
            "not_found": body.get("status") == "not_found",
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence page fetched", body)


@app.route("/api/v1/confluence/page/descendants", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_page_descendants() -> tuple[Response, int] | Response:
    """List the descendants of a Confluence page."""
    data = request.get_json(silent=True) or {}
    page_id = data.get("pageId")
    depth = data.get("depth")
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    ok, reason = _validate_confluence_page_id(page_id)
    if not ok:
        audit_log(
            "confluence_page_descendants_rejected",
            "confluence_page_descendants",
            success=False,
            details={"reason": reason, "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(
            "Invalid pageId (expected numeric string)",
            status_code=400,
            details={"pageId": page_id},
        )
    assert isinstance(page_id, str)  # narrowed by _validate_confluence_page_id

    # Apply sensible defaults for runaway-tree protection (risk R8).
    if depth is None:
        depth = 1
    if limit_raw is None:
        limit_raw = CONFLUENCE_DEFAULT_LIMIT
    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        audit_log(
            "confluence_page_descendants_rejected",
            "confluence_page_descendants",
            success=False,
            details={"reason": str(exc), "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(f"Invalid limit: {exc}", status_code=400)

    allowed = confluence_allowed_spaces()
    try:
        body = get_confluence_client().get_page_descendants(
            page_id,
            depth=depth,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) else None,
        )
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        audit_log(
            "confluence_page_descendants_upstream_error",
            "confluence_page_descendants",
            success=False,
            details={
                "pageId": page_id,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    # Resolve the parent page's space for the allowlist check.  The
    # descendants response doesn't carry it directly, so we fetch the parent
    # page once (cheap — the v2 page endpoint is small).
    parent_space_key: str | None = None
    if body.get("status") != "not_found":
        try:
            parent = get_confluence_client().get_page(page_id, body_format=("storage",))
        except (
            ConfluenceCredentialsUnavailable,
            ConfluenceUpstreamError,
            ConfluenceUpstreamForbidden,
        ):
            parent = None
        if parent is not None and parent.get("status") != "not_found":
            ok_space, parent_space_key = _check_post_fetch_space_allowlist(
                parent, allowed=allowed, page_id=page_id
            )
            if not ok_space:
                return _confluence_space_denied_response(
                    event="confluence_space_denied",
                    page_id=page_id,
                    space_key=parent_space_key,
                    reason="space not allowlisted",
                )
        else:
            return _confluence_space_denied_response(
                event="confluence_space_denied",
                page_id=page_id,
                space_key=None,
                reason="parent page space could not be resolved",
            )

    audit_log(
        "confluence_page_descendants",
        "confluence_page_descendants",
        success=True,
        details={
            "pageId": page_id,
            "spaceKey": parent_space_key,
            "depth": depth,
            "limit": limit,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence descendants fetched", body)


@app.route("/api/v1/confluence/page/footer-comments", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_page_footer_comments() -> tuple[Response, int] | Response:
    """Fetch footer comments on a Confluence page."""
    data = request.get_json(silent=True) or {}
    page_id = data.get("pageId")
    body_format = data.get("bodyFormat")
    include_replies = bool(data.get("includeReplies"))
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    ok, reason = _validate_confluence_page_id(page_id)
    if not ok:
        audit_log(
            "confluence_page_footer_comments_rejected",
            "confluence_page_footer_comments",
            success=False,
            details={"reason": reason, "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(
            "Invalid pageId (expected numeric string)",
            status_code=400,
            details={"pageId": page_id},
        )
    assert isinstance(page_id, str)  # narrowed by _validate_confluence_page_id

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    allowed = confluence_allowed_spaces()
    try:
        body = get_confluence_client().get_page_footer_comments(
            page_id,
            body_format=body_format,
            include_replies=include_replies,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) else None,
        )
    except ValueError as exc:
        return make_error(f"Invalid request: {exc}", status_code=400)
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        audit_log(
            "confluence_page_footer_comments_upstream_error",
            "confluence_page_footer_comments",
            success=False,
            details={
                "pageId": page_id,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    parent_space_key: str | None = None
    if body.get("status") != "not_found":
        try:
            parent = get_confluence_client().get_page(page_id, body_format=("storage",))
        except (
            ConfluenceCredentialsUnavailable,
            ConfluenceUpstreamError,
            ConfluenceUpstreamForbidden,
        ):
            parent = None
        if parent is not None and parent.get("status") != "not_found":
            ok_space, parent_space_key = _check_post_fetch_space_allowlist(
                parent, allowed=allowed, page_id=page_id
            )
            if not ok_space:
                return _confluence_space_denied_response(
                    event="confluence_space_denied",
                    page_id=page_id,
                    space_key=parent_space_key,
                    reason="space not allowlisted",
                )
        else:
            # Fail-closed when the parent page's space cannot be resolved
            # (parent fetch raised, or returned the not_found envelope while
            # the comment fetch returned data — Atlassian's per-page
            # restriction inheritance can produce exactly this shape).
            # We MUST NOT ship the comment body to the sandbox without an
            # allowlist verdict.
            return _confluence_space_denied_response(
                event="confluence_space_denied",
                page_id=page_id,
                space_key=None,
                reason="parent page space could not be resolved",
            )

    audit_log(
        "confluence_page_footer_comments",
        "confluence_page_footer_comments",
        success=True,
        details={
            "pageId": page_id,
            "spaceKey": parent_space_key,
            "includeReplies": include_replies,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence footer comments fetched", body)


@app.route("/api/v1/confluence/page/inline-comments", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_page_inline_comments() -> tuple[Response, int] | Response:
    """Fetch inline comments on a Confluence page (with v1 fallback)."""
    data = request.get_json(silent=True) or {}
    page_id = data.get("pageId")
    body_format = data.get("bodyFormat")
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    ok, reason = _validate_confluence_page_id(page_id)
    if not ok:
        audit_log(
            "confluence_page_inline_comments_rejected",
            "confluence_page_inline_comments",
            success=False,
            details={"reason": reason, "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(
            "Invalid pageId (expected numeric string)",
            status_code=400,
            details={"pageId": page_id},
        )
    assert isinstance(page_id, str)  # narrowed by _validate_confluence_page_id

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    allowed = confluence_allowed_spaces()
    try:
        body = get_confluence_client().get_page_inline_comments(
            page_id,
            body_format=body_format,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) else None,
        )
    except ValueError as exc:
        return make_error(f"Invalid request: {exc}", status_code=400)
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        audit_log(
            "confluence_page_inline_comments_upstream_error",
            "confluence_page_inline_comments",
            success=False,
            details={
                "pageId": page_id,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    used_fallback = bool(body.get("used_fallback"))
    parent_space_key: str | None = None
    if body.get("status") != "not_found":
        try:
            parent = get_confluence_client().get_page(page_id, body_format=("storage",))
        except (
            ConfluenceCredentialsUnavailable,
            ConfluenceUpstreamError,
            ConfluenceUpstreamForbidden,
        ):
            parent = None
        if parent is not None and parent.get("status") != "not_found":
            ok_space, parent_space_key = _check_post_fetch_space_allowlist(
                parent, allowed=allowed, page_id=page_id
            )
            if not ok_space:
                return _confluence_space_denied_response(
                    event="confluence_space_denied",
                    page_id=page_id,
                    space_key=parent_space_key,
                    reason="space not allowlisted",
                )
        else:
            # Fail-closed when the parent page's space cannot be resolved.
            # See confluence_page_footer_comments — same risk applies here:
            # the v1 fallback can return inline comments even when v2 page
            # reads 403, so we MUST NOT ship the body without an allowlist
            # verdict.
            return _confluence_space_denied_response(
                event="confluence_space_denied",
                page_id=page_id,
                space_key=None,
                reason="parent page space could not be resolved",
            )

    audit_log(
        "confluence_page_inline_comments",
        "confluence_page_inline_comments",
        success=True,
        details={
            "pageId": page_id,
            "spaceKey": parent_space_key,
            "used_fallback": used_fallback,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence inline comments fetched", body)


@app.route("/api/v1/confluence/space/pages", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_space_pages() -> tuple[Response, int] | Response:
    """List pages in a Confluence space."""
    data = request.get_json(silent=True) or {}
    space_key = data.get("spaceKey")
    limit_raw = data.get("limit")
    cursor = data.get("cursor")
    body_format = data.get("bodyFormat")

    ok, reason = _validate_confluence_space_key(space_key)
    if not ok:
        audit_log(
            "confluence_space_pages_rejected",
            "confluence_space_pages",
            success=False,
            details={"reason": reason, "spaceKey": space_key, **_session_confluence_context()},
        )
        return make_error(
            "Invalid spaceKey",
            status_code=400,
            details={"spaceKey": space_key},
        )
    assert isinstance(space_key, str)  # narrowed by _validate_confluence_space_key

    if not is_confluence_space_allowed(space_key):
        return _confluence_space_denied_response(
            event="confluence_space_pages_denied",
            page_id=None,
            space_key=space_key,
            reason="space not allowlisted",
        )

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    client = get_confluence_client()

    # Resolve spaceKey → spaceId, using the cache when populated.  Walk
    # paginated /wiki/api/v2/spaces so tenants with more spaces than fit on
    # one v2 page still resolve a target on page 2+.
    space_id = client.space_cache.id_for_key(space_key)
    if space_id is None:
        try:
            client.populate_space_cache()
        except ConfluenceCredentialsUnavailable as exc:
            return _confluence_not_configured_error(exc)
        except ConfluenceUpstreamForbidden as exc:
            return _confluence_forbidden_response(
                exc, event="confluence_upstream_403", space_key=space_key
            )
        except ConfluenceUpstreamError as exc:
            return _confluence_error_from_upstream(exc)
        space_id = client.space_cache.id_for_key(space_key)

    if space_id is None:
        return make_error(
            "Confluence space not found or not visible to bot account",
            status_code=404,
            details={"status": "not_found", "spaceKey": space_key},
        )

    try:
        body = client.get_space_pages(
            space_id,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) else None,
            body_format=body_format,
        )
    except ValueError as exc:
        return make_error(f"Invalid request: {exc}", status_code=400)
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(
            exc, event="confluence_upstream_403", space_key=space_key
        )
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, space_key=space_key)
    except ConfluenceUpstreamError as exc:
        audit_log(
            "confluence_space_pages_upstream_error",
            "confluence_space_pages",
            success=False,
            details={
                "spaceKey": space_key,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    audit_log(
        "confluence_space_pages",
        "confluence_space_pages",
        success=True,
        details={
            "spaceKey": space_key,
            "limit": limit,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence space pages fetched", body)


@app.route("/api/v1/confluence/space/list", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_space_list() -> tuple[Response, int] | Response:
    """List Confluence spaces (filtered to the operator's allowlist)."""
    data = request.get_json(silent=True) or {}
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    allowed = confluence_allowed_spaces()

    try:
        body = get_confluence_client().list_spaces(
            allowed_spaces=allowed,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) else None,
        )
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403")
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc)
    except ConfluenceUpstreamError as exc:
        audit_log(
            "confluence_space_list_upstream_error",
            "confluence_space_list",
            success=False,
            details={
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    spaces_returned = 0
    if isinstance(body, dict):
        results = body.get("results")
        if isinstance(results, list):
            spaces_returned = len(results)

    audit_log(
        "confluence_space_list",
        "confluence_space_list",
        success=True,
        details={
            "spaces_returned": spaces_returned,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence spaces fetched", body)


@app.route("/api/v1/confluence/search", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_search() -> tuple[Response, int] | Response:
    """Run a CQL search against Atlassian Cloud Confluence.

    Request body::

        {"cql": "space = ENG AND text ~ \"rfc\"",
         "limit": 50,
         "cursor": null}

    The CQL must be statically provable as scoped to allowlisted spaces.
    """
    data = request.get_json(silent=True) or {}
    cql = data.get("cql")
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    if not isinstance(cql, str) or not cql.strip():
        audit_log(
            "confluence_search_rejected",
            "confluence_search",
            success=False,
            details={"reason": "cql required", **_session_confluence_context()},
        )
        return make_error("cql is required", status_code=400)

    allowed = confluence_allowed_spaces()
    scope = extract_search_spaces(cql, allowed)
    if scope.spaces is None:
        audit_log(
            "confluence_search_rejected",
            "confluence_search",
            success=False,
            details={
                "reason": scope.reason,
                "cql_length": len(cql),
                **_session_confluence_context(),
            },
        )
        return make_error(
            f"CQL rejected: {scope.reason}",
            status_code=403,
            details={"reason": scope.reason},
        )

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    try:
        body = get_confluence_client().search_cql(
            cql=cql,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) else None,
        )
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403")
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc)
    except ConfluenceUpstreamError as exc:
        audit_log(
            "confluence_search_upstream_error",
            "confluence_search",
            success=False,
            details={
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    audit_log(
        "confluence_search",
        "confluence_search",
        success=True,
        details={
            "spaces_extracted": sorted(scope.spaces),
            "cql_length": len(cql),
            "limit": limit,
            "cursor_present": bool(cursor),
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence search executed", body)


@app.route("/api/v1/confluence/execute", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_execute() -> tuple[Response, int] | Response:
    """Generic read-only passthrough for whitelisted Confluence REST paths.

    Request body::

        {"method": "GET",
         "path": "api/v2/pages/12345",
         "query": {"body-format": "storage"},
         "body": null}
    """
    data = request.get_json(silent=True) or {}
    method = data.get("method") or "GET"
    path = data.get("path")
    query = data.get("query")
    req_body = data.get("body")

    if not isinstance(path, str) or not path:
        audit_log(
            "confluence_execute_rejected",
            "confluence_execute",
            success=False,
            details={"reason": "path required", **_session_confluence_context()},
        )
        return make_error("path is required", status_code=400)

    if not isinstance(method, str):
        audit_log(
            "confluence_execute_rejected",
            "confluence_execute",
            success=False,
            details={"reason": "method must be a string", **_session_confluence_context()},
        )
        return make_error("method must be a string", status_code=400)

    method_upper = method.upper()
    ok, reason = validate_confluence_api_path(path, method_upper)
    if not ok:
        audit_log(
            "confluence_execute_denied",
            "confluence_execute",
            success=False,
            details={
                "method": method_upper,
                "path": path,
                "reason": reason,
                **_session_confluence_context(),
            },
        )
        return make_error(
            f"Confluence API call rejected: {reason}",
            status_code=403,
            details={"method": method_upper, "path": path, "reason": reason},
        )

    stripped = path.strip("/").split("?", 1)[0]
    head = stripped.split("/")
    page_id: str | None = None
    space_id_in_path: str | None = None
    if len(head) >= 4 and head[0] == "api" and head[1] == "v2" and head[2] == "pages":
        # api/v2/pages/<id>
        if head[3].isdigit():
            page_id = head[3]
    elif len(head) >= 5 and head[0] == "api" and head[1] == "v2" and head[2] == "spaces":
        # api/v2/spaces/<id>/pages
        if head[3].isdigit():
            space_id_in_path = head[3]

    # Anti-bypass invariant (issue #1931 cycle-3 NACK from reviewer_code +
    # reviewer_security): the four path families an attacker could use to
    # bypass narrow-route safeguards — ``rest/api/search`` (CQL extractor
    # bypass), ``api/v2/spaces`` (allowlist-filter bypass),
    # ``api/v2/footer-comments`` / ``api/v2/inline-comments`` (flat
    # endpoints with page-id-in-query and no upstream spaceKey filter) —
    # are dropped from CONFLUENCE_API_ALLOWED_PATHS in confluence_client.py,
    # so reaching this point implies a page- or space-scoped path family.
    # All of those carry an id inline that the post-fetch allowlist check
    # below resolves to a spaceKey.

    if query is not None and not isinstance(query, dict):
        return make_error("query must be an object", status_code=400)
    if req_body is not None and not isinstance(req_body, dict):
        return make_error("body must be an object", status_code=400)

    allowed = confluence_allowed_spaces()
    client = get_confluence_client()

    try:
        body = client.execute_raw(
            method=method_upper,
            path=stripped,
            query=query,
            body=req_body,
        )
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        audit_log(
            "confluence_execute_upstream_error",
            "confluence_execute",
            success=False,
            details={
                "method": method_upper,
                "path": stripped,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    # Post-fetch allowlist check for path families that carry an id inline.
    audited_space_key: str | None = None
    if page_id is not None and isinstance(body, dict) and body.get("status") != "not_found":
        ok_space, audited_space_key = _check_post_fetch_space_allowlist(
            body, allowed=allowed, page_id=page_id
        )
        if not ok_space:
            return _confluence_space_denied_response(
                event="confluence_execute_denied",
                page_id=page_id,
                space_key=audited_space_key,
                reason="space not allowlisted",
                extra={"method": method_upper, "path": stripped},
            )
    elif space_id_in_path is not None:
        resolved = client.space_cache.key_for_id(space_id_in_path)
        if resolved is None:
            # Walk paginated /wiki/api/v2/spaces so a target on page 2+
            # still resolves.  Catch ConfluenceUpstreamForbidden alongside
            # the other upstream errors — it's a sibling of
            # ConfluenceUpstreamError (both inherit from RuntimeError, not
            # one from the other) and would otherwise escape as a Flask
            # 500 when the bot lacks space:read globally.  Mirrors the
            # handler at _resolve_space_key_via_list.
            try:
                client.populate_space_cache()
            except (
                ConfluenceCredentialsUnavailable,
                ConfluenceUpstreamError,
                ConfluenceUpstreamForbidden,
            ):
                resolved = None
            else:
                resolved = client.space_cache.key_for_id(space_id_in_path)
        if resolved is None or resolved not in allowed:
            return _confluence_space_denied_response(
                event="confluence_execute_denied",
                page_id=None,
                space_key=resolved,
                reason="space not allowlisted",
                extra={"method": method_upper, "path": stripped},
            )
        audited_space_key = resolved

    audit_log(
        "confluence_execute",
        "confluence_execute",
        success=True,
        details={
            "method": method_upper,
            "path": stripped,
            "pageId": page_id,
            "spaceKey": audited_space_key,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence API call executed", body)


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
) -> str | None:
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
        The worktree path if mapping succeeds, the original repo_path if no
        container_id was provided (interactive session), or None if a
        container_id was provided but the worktree could not be found.
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
        else:
            logger.warning(
                f"Worktree path does not exist for {operation} — "
                f"container_id may not match any created worktree",
                container_path=repo_path,
                expected_worktree=str(worktree_path),
                container_id=container_id,
            )
            return None
    except ValueError as e:
        logger.warning(
            f"Failed to map container path to worktree for {operation}",
            error=str(e),
            container_id=container_id,
            repo_name=repo_name,
        )
        return None


def _cleanup_stale_pack_files(exec_path: str) -> None:
    """Best-effort opportunistic cleanup of previously-orphaned temporary pack files.

    Called after a git operation times out, but does NOT target the specific
    operation's artifacts (those are too recent to match the age filter).
    Instead, it scans for ``tmp_pack_*``/``tmp_obj_*``/``tmp_idx_*`` files
    older than 5 minutes — orphans left by *earlier* interrupted operations.
    The age filter avoids racing with concurrent fetch operations on the same
    repository.
    """
    try:
        # Determine repo_name from exec_path.
        # Worktree paths:  /home/egg/.egg-worktrees/{container_id}/{repo_name}[/subdir]
        # Main repo paths: /home/egg/repos/{repo_name}[/subdir]
        repo_name = None
        worktree_prefix = str(WORKTREE_BASE_DIR) + "/"
        repos_prefix = str(REPOS_BASE_DIR) + "/"

        if exec_path.startswith(worktree_prefix):
            # e.g. /home/egg/.egg-worktrees/container-123/my-repo/src → my-repo
            relative = exec_path[len(worktree_prefix) :]
            parts = relative.split("/")
            if len(parts) >= 2:
                repo_name = parts[1]
        elif exec_path.startswith(repos_prefix):
            # e.g. /home/egg/repos/my-repo/src → my-repo
            relative = exec_path[len(repos_prefix) :]
            parts = relative.split("/")
            if parts and parts[0]:
                repo_name = parts[0]

        if not repo_name:
            return

        manager = get_worktree_manager()
        manager.cleanup_orphaned_pack_files(
            repo_name=repo_name,
            max_age_seconds=300,
        )
    except Exception as e:
        logger.debug(
            "Stale pack file cleanup failed (best-effort)",
            exec_path=exec_path,
            error=str(e),
        )


def _cleanup_empty_container_dir(container_id: str) -> None:
    """Best-effort removal of an orphan container worktree directory.

    Called from the total-failure branch of worktree_create.  Only acts
    when the directory is actually empty — if any per-repo subdir is
    present (partial failure with leftover state), we leave it for an
    operator-driven prune so we don't accidentally drop in-progress
    work.  See #2186.

    Defense-in-depth: validate `container_id` and verify the resolved
    path stays under `WORKTREE_BASE_DIR` before any filesystem mutation.
    `worktree_create` already validates at the route boundary, but a
    raw `..`-bearing identifier reaching `Path / container_id` would
    otherwise let `rmdir(2)` follow the literal path and unlink an
    empty directory adjacent to the base dir.
    """
    try:
        validate_identifier(container_id, "container_id")
    except ValueError as e:
        logger.warning(
            "Skipping orphan container dir cleanup: invalid container_id",
            container_id=container_id,
            error=str(e),
        )
        return

    target = WORKTREE_BASE_DIR / container_id
    try:
        # Resolve and verify containment as a second line of defense
        # against any future caller that bypasses validate_identifier.
        base_resolved = WORKTREE_BASE_DIR.resolve()
        target_resolved = target.resolve(strict=False)
        if target_resolved != base_resolved and base_resolved not in target_resolved.parents:
            logger.warning(
                "Skipping orphan container dir cleanup: outside base dir",
                container_id=container_id,
                resolved=str(target_resolved),
                base=str(base_resolved),
            )
            return
        if not target.exists():
            return
        if any(target.iterdir()):
            logger.warning(
                "Skipping orphan container dir cleanup: not empty",
                container_id=container_id,
                path=str(target),
            )
            return
        target.rmdir()
        logger.info(
            "Removed empty orphan container worktree dir",
            container_id=container_id,
            path=str(target),
        )
    except OSError as e:
        logger.warning(
            "Failed to clean orphan container dir",
            container_id=container_id,
            path=str(target),
            error=str(e),
        )


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
    base_branch = data.get("base_branch")  # None = resolve per-repo
    assigned_branch = data.get("assigned_branch")  # None = skip upstream config
    # UID/GID for worktree ownership (default: 1000 for egg user)
    uid = data.get("uid")
    gid = data.get("gid")

    if not container_id:
        return make_error("Missing container_id")
    if not repos:
        return make_error("Missing repos list")

    # Validate container_id at the route boundary so every downstream
    # filesystem touch (including the post-failure cleanup helper) is
    # safe.  Without this, a `..`-bearing container_id would reach
    # `_cleanup_empty_container_dir` after `manager.create_worktree`
    # raised ValueError into the per-repo `errors[]` list, letting
    # `rmdir(2)` follow the literal path out of WORKTREE_BASE_DIR.
    # See #2186 review feedback.
    try:
        validate_identifier(container_id, "container_id")
    except ValueError as e:
        return make_error(str(e))

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

        # Pre-bind so the except clauses below can reference
        # effective_branch even if resolve_default_branch raises (e.g.,
        # OSError if the git binary is missing).  See #2186 review
        # feedback — previously hoisted out of the try, which let
        # resolve_default_branch failures bypass the per-repo errors[]
        # safety net entirely.
        effective_branch = base_branch
        try:
            # Resolve the default branch per-repo when no explicit base is given.
            # This ensures repos with non-standard default branches (e.g., master)
            # are handled correctly.  See #860.
            effective_branch = base_branch or manager.resolve_default_branch(repo_name)
            info = manager.create_worktree(
                repo_name=repo_name,
                container_id=container_id,
                base_branch=effective_branch,
                uid=uid,
                gid=gid,
                assigned_branch=assigned_branch,
            )
            # Translate container path to host path for egg launcher mount sources
            worktrees[repo_name] = translate_to_host_path(str(info.worktree_path))
        except (ValueError, RuntimeError) as e:
            # Capture full traceback so operators can diagnose without
            # re-instrumenting the gateway.  See #2186.
            logger.exception(
                "worktree_create per-repo failure",
                repo_name=repo_name,
                container_id=container_id,
                base_branch=effective_branch,
                assigned_branch=assigned_branch,
                error_type=type(e).__name__,
            )
            errors.append(f"{repo_name}: {e}")
        except Exception as e:
            logger.exception(
                "worktree_create unexpected per-repo failure",
                repo_name=repo_name,
                container_id=container_id,
                base_branch=effective_branch,
                assigned_branch=assigned_branch,
                error_type=type(e).__name__,
            )
            errors.append(f"{repo_name}: unexpected error - {e}")

    if errors and not worktrees:
        # Total failure: surface aggregate errors in logs + audit, then
        # best-effort clean up the empty container directory so retries
        # aren't blocked by stale state.  See #2186.
        logger.error(
            "worktree_create failed for all repos",
            container_id=container_id,
            errors=errors,
        )
        audit_log(
            "worktrees_create_failed",
            "worktree_create",
            success=False,
            details={
                "container_id": container_id,
                "errors": errors,
            },
        )
        _cleanup_empty_container_dir(container_id)
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


# Shared mutex: single lock shared by the prune route so that a
# dry_run=true and dry_run=false call can never interleave. Local to
# this module; tests exercise `request` against the Flask test client
# so the process-wide lock is safe.
_worktree_prune_lock = threading.Lock()


# Slice-scoped per-agent worktree dir suffix:
# ``{pipeline_id}-slice-{N}-{role}``.  The role part follows AgentRole
# values (``[a-z_]+``).  Used by ``_derive_worktree_anchor_ids`` to
# recognise slice-scoped worktrees of a live pipeline that the session
# metadata alone can't express (sessions carry pipeline_id + agent_role
# but not slice_id).
_SLICE_WORKTREE_SUFFIX_RE = re.compile(r"^-slice-[0-9]+-[a-z_]+$")


def _derive_worktree_anchor_ids(sessions: list[dict[str, Any]]) -> set[str]:
    """Return the set of worktree directory names implied by active sessions.

    Session ``container_id`` is the k8s Job name / Docker container name
    (e.g. ``egg-agent-issue-1758-again-coder``), but per-agent worktrees
    on disk are named after the orchestrator-assigned
    ``agent_worktree_id`` of the form ``{pipeline_id}-{agent_role}`` and
    the pipeline-level worktree is just ``{pipeline_id}``.  Without this
    derivation, ``cleanup_orphaned_worktrees`` treats every per-agent
    worktree as an orphan, which wiped live pipelines' worktrees during
    gateway startup cleanup (#1874).

    Slice-scoped per-agent worktrees (``{pipeline_id}-slice-{N}-{role}``,
    introduced in #2403) carry no slice context on the session, so for
    each live pipeline we additionally scan the worktree base for
    matching directories and add them to the anchor set.  Without this
    scan, slice-scoped agent worktrees with unpushed local commits are
    wiped during gateway startup cleanup — silently destroying the work
    that ``salvage_agent_commits`` was designed to recover (#2463).
    """
    anchors: set[str] = set()
    pipeline_ids: set[str] = set()
    for session_info in sessions:
        pipeline_id = session_info.get("pipeline_id")
        agent_role = session_info.get("agent_role")
        if pipeline_id:
            anchors.add(pipeline_id)
            pipeline_ids.add(pipeline_id)
            if agent_role:
                anchors.add(f"{pipeline_id}-{agent_role}")

    if pipeline_ids:
        try:
            entries = list(WORKTREE_BASE_DIR.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            name = entry.name
            for pid in pipeline_ids:
                if not name.startswith(pid):
                    continue
                suffix = name[len(pid) :]
                if _SLICE_WORKTREE_SUFFIX_RE.match(suffix):
                    anchors.add(name)
                    break
    return anchors


def _container_ids_from_sessions(sessions: list[dict[str, Any]]) -> set[str]:
    """Return container IDs and worktree anchor IDs from session dicts.

    Each session contributes its own ``container_id`` plus the derived
    per-agent (``{pipeline_id}-{agent_role}``), pipeline-level
    (``{pipeline_id}``), and slice-scoped
    (``{pipeline_id}-slice-{N}-{role}``) worktree anchor IDs so that
    cleanup never wipes a live pipeline's worktrees (#1874, #2463).
    """
    ids: set[str] = set()
    for session_info in sessions:
        cid = session_info.get("container_id")
        if cid:
            ids.add(cid)
    ids |= _derive_worktree_anchor_ids(sessions)
    return ids


def _collect_active_container_ids() -> set[str]:
    """Return the best-effort set of container IDs that back live sessions.

    Mirrors the startup-cleanup logic at module level so the prune route
    never issues a sweep with an empty active set (which would otherwise
    treat every worktree as an orphan — see #1759 review).

    Consults:

    1. Persisted sessions via :func:`get_session_manager` (primary source
       of truth — survives gateway restarts).  Each session contributes
       its own ``container_id`` plus the derived per-agent and
       pipeline-level worktree anchor IDs so that cleanup never wipes a
       live pipeline's worktrees (#1874).
    2. ``docker ps`` when Docker is reachable (safety net for sessions
       that outlive the session-manager snapshot).

    Failures in either step degrade silently so the prune still runs —
    the worst case is that a genuinely orphaned dir is preserved, which
    the next scheduled prune will clean up.
    """
    active_container_ids: set[str] = set()
    try:
        session_manager = get_session_manager()
        sessions = session_manager.list_sessions()
        active_container_ids |= _container_ids_from_sessions(sessions)
    except Exception as exc:
        logger.warning(
            "prune: session-manager active-container lookup failed",
            error=str(exc),
        )
    try:
        active_container_ids |= get_active_docker_containers()
    except Exception as exc:
        # Non-fatal: on k3s there is no dockerd reachable from the
        # orchestrator's sidecar, and that is fine.
        logger.debug(
            "prune: docker active-container probe unavailable",
            error=str(exc),
        )
    return active_container_ids


@app.route("/api/v1/worktrees/prune", methods=["POST"])
@require_launcher_auth
def worktrees_prune() -> tuple[Response, int] | Response:
    """
    Run ``git worktree prune`` across every repo and sweep orphan dirs
    under the worktree base.

    Request body::

        {"dry_run": bool = true}

    When ``dry_run`` is true, returns the set of orphan directories
    that would be removed but does not mutate the filesystem. When
    false, removes them using the existing ``cleanup_orphaned_worktrees``
    helper.  The active-container set is derived from the session
    manager (plus an opportunistic ``docker ps`` fallback) so a live
    pipeline's worktree is never mistaken for an orphan.

    Proxied from the orchestrator's
    ``/api/v1/deployment/prune-worktrees`` endpoint (#1759).
    """
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run", True))

    manager = get_worktree_manager()

    # Serialize all prune activity — git operations on the same repo
    # must not interleave even if two callers hit this endpoint
    # concurrently.
    if not _worktree_prune_lock.acquire(timeout=60):
        return make_error("Another worktree prune is in progress", status_code=409)
    try:
        active_container_ids = _collect_active_container_ids()
        git_prune_report = manager.git_worktree_prune_all()
        orphan_dirs = manager.list_orphan_worktree_dirs(active_containers=active_container_ids)

        removed_count = 0
        removed_paths: list[str] = []
        if not dry_run and orphan_dirs:
            removed_count = manager.cleanup_orphaned_worktrees(
                active_containers=active_container_ids,
            )
            # Any orphan we enumerated that no longer exists on disk
            # was removed by the helper.
            for path in orphan_dirs:
                try:
                    if not Path(path).exists():
                        removed_paths.append(path)
                except OSError:
                    pass

        audit_log(
            "worktrees_pruned",
            "worktrees_prune",
            success=True,
            details={
                "dry_run": dry_run,
                "git_worktree_prune": git_prune_report,
                "orphan_dirs_count": len(orphan_dirs),
                "active_containers_count": len(active_container_ids),
                "removed_count": removed_count,
            },
        )

        return make_success(
            "Worktree prune complete",
            {
                "dry_run": dry_run,
                "git_worktree_prune": git_prune_report,
                "orphan_dirs": orphan_dirs,
                "active_containers_count": len(active_container_ids),
                "removed_count": removed_count,
                "removed_paths": removed_paths,
            },
        )
    finally:
        _worktree_prune_lock.release()


# =============================================================================
# Session Management Endpoints (Per-Container Repository Mode)
# =============================================================================


def _branch_exists_on_remote(manager: WorktreeManager, repo_name: str, branch: str) -> bool:
    """Check if a branch exists on the remote (origin) for a repository.

    Args:
        manager: WorktreeManager instance (provides repos_base path)
        repo_name: Name of the repository
        branch: Branch name without origin/ prefix (e.g., "egg/issue-42/work")

    Returns:
        True if origin/{branch} exists, False otherwise.
    """
    main_repo = manager.repos_base / repo_name
    if not main_repo.exists():
        return False
    # Uses local tracking refs (origin/*) rather than querying the remote.
    # This is reliable here because the gateway handles push/fetch operations
    # which keep tracking refs up to date.  If stale refs ever become an
    # issue, switch to `git ls-remote --exit-code origin {branch}`.
    result = subprocess.run(
        git_cmd("rev-parse", "--verify", f"origin/{branch}"),
        cwd=main_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


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
            "local_only_repos": ["repo-name"],  // optional; no GitHub remote
            "uid": 1000,
            "gid": 1000,
            // Optional: when the caller already created per-agent worktrees
            // via /api/v1/worktrees/create, pass that container_id here so
            // the session reuses the existing worktrees instead of racing
            // to re-create them on the same bare repo (#1857).
            "worktree_container_id": "pipeline-role"
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
    local_only_repos = data.get("local_only_repos", [])
    uid = data.get("uid")
    gid = data.get("gid")
    # Optional worktree container_id — when provided, look up existing
    # worktrees created by a prior /api/v1/worktrees/create call instead of
    # re-creating them here.  Prevents the double create that races on
    # .git/config.lock for concurrent per-agent spawns (#1857).
    worktree_container_id = data.get("worktree_container_id")
    phase = data.get("phase")  # Optional SDLC pipeline phase
    pipeline_id = data.get("pipeline_id")  # Optional pipeline run ID
    issue_number = data.get("issue_number")  # Optional GitHub issue number
    pr_number = data.get("pr_number")  # Optional GitHub PR number
    agent_role = data.get("agent_role")  # Optional agent role
    agent_anchor_id = data.get("agent_anchor_id")  # Optional agent anchor ID
    claude_code_version = data.get("claude_code_version")  # Optional Claude Code version
    branch = data.get("branch")  # Optional git branch for non-pushing sessions
    jira_ticket = data.get("jira_ticket")  # Optional Atlassian ticket key — advisory only
    synthetic = data.get("synthetic", False)  # Orchestrator-internal temp session

    # Validate required fields
    if not container_id:
        return make_error("Missing container_id")
    # container_ip is optional — k8s pod IPs are ephemeral and may not be
    # known at session creation time.  When omitted, token-only auth is used.
    # Kept for backward compatibility with Docker-based deployments.
    if mode not in ("private", "public"):
        return make_error("Invalid mode: must be 'private' or 'public'")
    # repos can be omitted for orchestrator-internal sessions that have a pipeline_id
    if not repos and not local_only_repos and not pipeline_id:
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
        if not agent_role:
            return make_error("Invalid agent_role: must be non-empty if provided")
        if len(agent_role) > 64:
            return make_error("Invalid agent_role: must be 64 characters or fewer")

    # Validate agent_anchor_id if provided
    if agent_anchor_id is not None:
        if not isinstance(agent_anchor_id, str):
            return make_error("Invalid agent_anchor_id: must be a string")
        if len(agent_anchor_id) > 128:
            return make_error("Invalid agent_anchor_id: must be 128 characters or fewer")
        if not re.match(r"^[a-zA-Z0-9_-]+$", agent_anchor_id):
            return make_error(
                "Invalid agent_anchor_id: must contain only alphanumeric characters, hyphens, and underscores"
            )

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

    # Validate synthetic if provided
    if not isinstance(synthetic, bool):
        return make_error("Invalid synthetic: must be a boolean")

    # Validate worktree_container_id if provided
    if worktree_container_id is not None:
        if not isinstance(worktree_container_id, str):
            return make_error("Invalid worktree_container_id: must be a string")
        if not worktree_container_id:
            return make_error("Invalid worktree_container_id: must be non-empty if provided")
        if len(worktree_container_id) > 256:
            return make_error("Invalid worktree_container_id: must be 256 characters or fewer")
        if ".." in worktree_container_id or not re.match(
            r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", worktree_container_id
        ):
            return make_error("Invalid worktree_container_id: contains unsafe characters")

    # Validate local_only_repos if provided
    if local_only_repos:
        if not isinstance(local_only_repos, list):
            return make_error("Invalid local_only_repos: must be a list")
        if len(local_only_repos) > 50:
            return make_error("Invalid local_only_repos: too many entries")
        for repo_name in local_only_repos:
            if not isinstance(repo_name, str):
                return make_error("Invalid local_only_repos: all items must be strings")
            if not repo_name or len(repo_name) > 256:
                return make_error("Invalid local_only_repos: repo name must be 1-256 chars")
            if ".." in repo_name or "/" in repo_name:
                return make_error(f"Invalid local_only_repos: unsafe repo name '{repo_name}'")

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
    # private mode: include repos with known visibility (private, internal, public).
    #   Write access is controlled separately by push policy (only private/internal
    #   repos are writable). The network is locked down — mounting a public repo
    #   in private mode doesn't grant broader internet access.
    #   Repos with unknown visibility (None) are excluded (fail-closed).
    # public mode: keep only public repos (don't mount private repos on open network)
    filtered_repos = []
    for repo, visibility in repo_visibilities.items():
        if mode == "private":
            # Private mode: include repos with known visibility — network is
            # locked down anyway so mounting a public repo is safe.
            # Push policy enforces write restrictions to private/internal repos.
            if visibility is None:
                # Unknown visibility — repo may not exist or API unreachable.
                # Fail closed: don't attempt to mount a repo we can't verify.
                logger.warning(
                    "Unknown visibility for repo, excluding in private mode",
                    repo=repo,
                    container_id=container_id,
                )
                continue
            elif visibility not in ("private", "internal"):
                logger.info(
                    "Including public repo in private mode (network locked down)",
                    repo=repo,
                    visibility=visibility,
                    container_id=container_id,
                )
            filtered_repos.append(repo)
        else:
            # Public mode: only mount public repos
            if visibility is None:
                # Unknown visibility — can't confirm public, exclude
                logger.warning(
                    "Unknown visibility for repo, excluding in public mode",
                    repo=repo,
                    container_id=container_id,
                )
            elif visibility == "public":
                filtered_repos.append(repo)
            else:
                logger.debug(
                    "Excluding non-public repo in public mode",
                    repo=repo,
                    visibility=visibility,
                    container_id=container_id,
                )

    # Step 2b: Include local-only repos in private mode.
    # These repos have no GitHub remote so GitHub visibility cannot be checked.
    # They are always treated as private: included in private mode, excluded in public mode.
    if local_only_repos and mode == "private":
        for repo_name in local_only_repos:
            filtered_repos.append(repo_name)
            logger.info(
                "Including local-only repo in private mode",
                repo=repo_name,
                container_id=container_id,
            )
    elif local_only_repos and mode != "private":
        logger.debug(
            "Excluding local-only repos in public/local mode",
            repos=local_only_repos,
            mode=mode,
            container_id=container_id,
        )

    # Step 3: Create worktrees for filtered repos
    worktrees = {}
    worktree_errors = []
    first_worktree_path: str | None = None  # Gateway-side path for checkpoint context
    first_repo: str | None = None  # First filtered repo in "owner/repo" format

    # Only initialise the worktree manager when there are repos to process.
    # Local-mode sessions (no repos) skip worktree creation entirely, so
    # avoid hitting the filesystem for the worktree base directory.
    if filtered_repos:
        manager = get_worktree_manager()

    for repo in filtered_repos:
        # Extract repo name from owner/repo format
        if "/" in repo:
            repo_name = repo.split("/")[-1]
        else:
            repo_name = repo

        try:
            if worktree_container_id:
                # Reuse worktrees created by a prior /api/v1/worktrees/create
                # call.  Avoids a second concurrent ``git worktree add`` on the
                # same bare repo, which races on ``.git/config.lock`` (#1857).
                info = manager.lookup_worktree(
                    repo_name=repo_name,
                    container_id=worktree_container_id,
                )
            else:
                # For pipeline sessions, prefer the pipeline's existing worktree
                # branch (which contains artifacts from prior agents) over a fresh
                # branch from origin/main.  This ensures HITL exec sessions can
                # see drafts, contracts, and reviews committed by pipeline agents.
                # See #1016.
                #
                # For fresh pipelines (no prior worktree branch), fall back to the
                # remote default branch (e.g., origin/main) instead of HEAD.  HEAD
                # may point to a feature branch in the main repo, which would
                # pollute the worktree with commits outside the current phase's
                # allowed scope and cause push rejections.  See #860.
                if pipeline_id:
                    pipeline_work_branch = f"egg/{pipeline_id}/work"
                    if _branch_exists_on_remote(manager, repo_name, pipeline_work_branch):
                        worktree_base_branch = f"origin/{pipeline_work_branch}"
                    else:
                        worktree_base_branch = manager.resolve_default_branch(repo_name)
                else:
                    worktree_base_branch = "HEAD"

                info = manager.create_worktree(
                    repo_name=repo_name,
                    container_id=container_id,
                    base_branch=worktree_base_branch,
                    uid=uid,
                    gid=gid,
                    assigned_branch=branch,
                )
            # Capture the first worktree's gateway-side path for checkpoint context
            if first_worktree_path is None:
                first_worktree_path = str(info.worktree_path)
                first_repo = repo
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
        agent_anchor_id=agent_anchor_id,
        claude_code_version=claude_code_version,
        branch=branch,
        jira_ticket=jira_ticket if isinstance(jira_ticket, str) and jira_ticket else None,
        synthetic=synthetic,
    )

    # Pre-populate checkpoint context so non-pushing sessions (reviewers,
    # architects, etc.) have a repo_path and checkpoint_repo for session-end
    # checkpoint storage. These fields are also set on git push, but pipeline
    # agents that never push would otherwise have None values.
    if first_worktree_path is not None:
        _session.last_repo_path = first_worktree_path
    if first_repo is not None:
        # Note: for local-only repos, first_repo is a bare name (e.g. "my-repo")
        # rather than "owner/repo" format. get_checkpoint_repo() won't match it
        # in repo_settings, so checkpoint_repo will be None. This is acceptable:
        # local-only repos have no GitHub remote and aren't expected to produce
        # checkpoints.
        _session.checkpoint_repo = get_checkpoint_repo(first_repo)

    # Use the shared pipeline branch for push enforcement.
    # session_manager already sets assigned_branch from the `branch`
    # request parameter (session_manager.py:560-564). If that wasn't
    # provided, fall back to the canonical pipeline branch name.
    if pipeline_id and not _session.assigned_branch:
        _session.assigned_branch = f"egg/{pipeline_id}/work"

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
    deleted, _checkpoint_event = session_manager.delete_session(session_token)

    if not deleted:
        return make_error("Session not found", status_code=404)

    # _capture_and_cleanup_session (called inside delete_session) already
    # waits for checkpoint storage to complete before returning, so the
    # worktree is safe to remove at this point — no second wait needed.

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
    deleted, _checkpoint_event = session_manager.delete_session_by_container(container_id)

    if not deleted:
        return make_error("Session not found for container", status_code=404)

    # _capture_and_cleanup_session (called inside delete_session_by_container)
    # already waits for checkpoint storage to complete before returning, so
    # the worktree is safe to remove at this point — no second wait needed.

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


@app.route("/api/v1/sessions/by-container/<container_id>/heartbeat", methods=["POST"])
@require_launcher_auth
def session_heartbeat_by_container(container_id: str) -> tuple[Response, int] | Response:
    """
    Refresh a session's idle timer by container ID (orchestrator-only path).

    Used by the orchestrator to keep agent sessions alive while their
    container is heartbeating on the BRC bus but not making gateway
    requests — without this, the idle pruner evicts the session after
    EGG_SESSION_IDLE_TIMEOUT_MINUTES even though the agent is still
    working (see #2068).

    Auth: Bearer {launcher_secret}

    Returns 404 if no session exists for the container.  No per-session
    rate limit because the launcher secret already gates access — only
    the orchestrator can call this.
    """
    session_manager = get_session_manager()
    refreshed = session_manager.heartbeat_session_by_container(container_id)

    if not refreshed:
        return make_error("Session not found for container", status_code=404)

    return make_success("Heartbeat recorded")


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


# Valid SDLC pipeline phases — derived from phase_filter.PipelinePhase to avoid drift.
VALID_PIPELINE_PHASES = frozenset(p.value for p in PipelinePhase)


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
            base_url="https://api.anthropic.com",  # noqa: EGG200 - gateway proxy client, not direct LLM call
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
    except json.JSONDecodeError, TypeError:
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
    except json.JSONDecodeError, TypeError:
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


SSEResult = tuple[
    list[dict[str, Any]] | None,
    dict[str, Any] | None,
    str | None,
    str | None,
]


class _SSEAccumulator:
    """Parse Anthropic SSE streaming responses incrementally.

    Accepts bytes chunks via ``feed()``. Holds only parsed state plus a small
    partial-line buffer — never the raw response bytes. Call ``result()`` once
    at end of stream to get ``(content, usage, model, stop_reason)``.

    Why: the previous implementation did ``b"".join(chunks).decode().split("\\n")``
    at end-of-stream, peaking at ~3× the full response size per concurrent
    request. At ~15 concurrent streams that's hundreds of MB of transient
    allocation that the pod's 1Gi cgroup couldn't absorb (see #1885).
    """

    def __init__(self) -> None:
        # errors="replace" matches the prior decode() behavior.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line_buf = ""
        self._content_by_index: dict[int, dict[str, Any]] = {}
        # Error events produce extra content blocks ordered before indexed
        # blocks (preserves the pre-refactor ordering).
        self._error_blocks: list[dict[str, Any]] = []
        self._usage: dict[str, Any] | None = None
        self._model: str | None = None
        self._stop_reason: str | None = None
        self._finalized = False

    def feed(self, chunk: bytes) -> None:
        """Decode ``chunk`` and process any complete lines it completes."""
        if self._finalized or not chunk:
            return
        text = self._decoder.decode(chunk)
        if not text:
            return
        if "\n" not in text:
            self._line_buf += text
            return
        combined = self._line_buf + text
        parts = combined.split("\n")
        # parts[-1] is either "" (chunk ended on a newline) or the new partial line.
        self._line_buf = parts[-1]
        for line in parts[:-1]:
            self._process_line(line)

    def result(self) -> SSEResult:
        """Flush any trailing partial line and return the parsed result."""
        if not self._finalized:
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._line_buf += tail
            if self._line_buf:
                self._process_line(self._line_buf)
                self._line_buf = ""
            self._finalized = True

        content_blocks: list[dict[str, Any]] = list(self._error_blocks)
        for index in sorted(self._content_by_index.keys()):
            block = self._content_by_index[index]
            if block.get("type") == "tool_use" and "partial_input" in block:
                partial_input = block.pop("partial_input")
                try:
                    block["input"] = json.loads(partial_input)
                except json.JSONDecodeError:
                    logger.debug(
                        "Failed to parse tool_use input JSON",
                        tool_id=block.get("id"),
                    )
                    block["input"] = {}
                    block["input_parse_error"] = True
                    block["raw_partial_input"] = (
                        partial_input[:RAW_INPUT_TRUNCATE_SIZE]
                        if len(partial_input) > RAW_INPUT_TRUNCATE_SIZE
                        else partial_input
                    )
            content_blocks.append(block)

        return content_blocks or None, self._usage, self._model, self._stop_reason

    def _process_line(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("data: "):
            return
        data_str = line[6:]
        if data_str == "[DONE]":
            return
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            return
        self._process_event(event)

    def _process_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")

        if event_type == "message_start":
            message = event.get("message", {})
            self._model = message.get("model")
            message_usage = message.get("usage", {})
            if message_usage:
                if self._usage is None:
                    self._usage = {}
                # input_tokens / cache_* come from message_start, not message_delta.
                if "input_tokens" in message_usage:
                    self._usage["input_tokens"] = message_usage["input_tokens"]
                if "cache_read_input_tokens" in message_usage:
                    self._usage["cache_read_input_tokens"] = message_usage[
                        "cache_read_input_tokens"
                    ]
                if "cache_creation_input_tokens" in message_usage:
                    self._usage["cache_creation_input_tokens"] = message_usage[
                        "cache_creation_input_tokens"
                    ]

        elif event_type == "error":
            error_info = event.get("error", {})
            self._error_blocks.append({"type": "error", "error": error_info})
            self._stop_reason = "error"

        elif event_type == "content_block_start":
            index = event.get("index", 0)
            content_block = event.get("content_block", {})
            self._content_by_index[index] = content_block.copy()

        elif event_type == "content_block_delta":
            index = event.get("index", 0)
            delta = event.get("delta", {})
            delta_type = delta.get("type")
            if index not in self._content_by_index:
                self._content_by_index[index] = {
                    "type": delta_type.replace("_delta", "") if delta_type else "unknown"
                }
            block = self._content_by_index[index]
            if delta_type == "text_delta":
                text = delta.get("text", "")
                block["text"] = block.get("text", "") + text
            elif delta_type == "input_json_delta":
                partial_json = delta.get("partial_json", "")
                block["partial_input"] = block.get("partial_input", "") + partial_json

        elif event_type == "message_delta":
            delta = event.get("delta", {})
            self._stop_reason = delta.get("stop_reason")
            event_usage = event.get("usage")
            if event_usage:
                if self._usage is None:
                    self._usage = {}
                # message_delta contains output_tokens.
                self._usage.update(event_usage)


def _capture_streaming_response(
    container_id: str,
    request_json: dict[str, Any],
    result: SSEResult,
    start_time: float,
) -> None:
    """
    Capture a streaming API response to the transcript buffer.

    Args:
        container_id: Container ID for buffer lookup
        request_json: Parsed request body
        result: Parsed SSE result tuple from ``_SSEAccumulator.result()``
        start_time: Request start time for duration calculation
    """
    duration_ms = (time.time() - start_time) * 1000
    response_content, response_usage, response_model, stop_reason = result

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


def _parse_sse_response(chunks: list[bytes]) -> SSEResult:
    """
    Parse SSE response chunks and return ``(content, usage, model, stop_reason)``.

    Thin wrapper over ``_SSEAccumulator`` retained for test coverage. Production
    streaming capture feeds the accumulator directly so it never holds the
    chunks list in memory — see ``proxy_anthropic_messages``.
    """
    acc = _SSEAccumulator()
    for chunk in chunks:
        acc.feed(chunk)
    return acc.result()


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
    except json.JSONDecodeError, TypeError:
        request_json = {}

    client = get_anthropic_client()

    try:
        if is_streaming:
            # Stream SSE response using httpx's send() with stream=True
            # This gives us direct control over the response lifecycle.
            #
            # Resilience strategy (see #1907):
            #   (A) Pre-stream retry — if the upstream TCP connection resets
            #       before any byte has been yielded downstream, open a fresh
            #       upstream connection and retry the request once. The
            #       downstream SDK never sees the error. Covers
            #       connection-pool staleness and very-early resets.
            #   (B) Mid-stream synthetic error — if the reset happens after
            #       bytes have already flowed, emit a well-formed SSE
            #       ``event: error`` frame and close the downstream stream
            #       cleanly. Lets the SDK fail gracefully instead of dying
            #       on a truncated socket.
            #
            # Full stream resumption is not attempted — Anthropic's API has
            # no resume tokens, and the partial generation on the wire is
            # orphaned on any mid-stream reset regardless.
            def _send_and_prime() -> tuple[Any, Any, bytes | None]:
                """Send upstream request and pre-fetch the first chunk.

                Returns ``(upstream_response, iterator, first_chunk)`` where
                ``first_chunk`` is ``None`` if upstream returned an empty
                body. Raises ``httpx.ReadError`` or
                ``httpx.RemoteProtocolError`` if the connection resets during
                ``send()`` or the first ``iter_bytes()`` call — callers use
                that signal to retry transparently.
                """
                http_req = client.build_request(
                    "POST",
                    "/v1/messages",
                    headers=headers,
                    content=request_body,
                )
                upstream_resp = client.send(http_req, stream=True)
                try:
                    iterator = upstream_resp.iter_bytes()
                    try:
                        first = next(iterator)
                    except StopIteration:
                        first = None
                    return upstream_resp, iterator, first
                except BaseException:
                    # Close the failed upstream so the caller's retry can
                    # open a fresh connection without leaking the old one.
                    # Broad catch ensures cleanup on *any* exception from
                    # iter_bytes() / next(), not just the two transport
                    # errors we expect.
                    try:
                        upstream_resp.close()
                    except Exception:
                        pass
                    raise

            upstream: Any = None
            primed_iterator: Any = None
            first_chunk: bytes | None = None
            for attempt in range(2):
                try:
                    upstream, primed_iterator, first_chunk = _send_and_prime()
                    break
                except (httpx.ReadError, httpx.RemoteProtocolError) as reset_err:
                    if attempt == 0:
                        logger.warning(
                            "Upstream Anthropic connection reset before any byte "
                            "was forwarded; retrying once",
                            container_id=container_id,
                            error=str(reset_err),
                        )
                        continue
                    # Retry exhausted — fall through to the outer
                    # exception handler which returns a 502 to the caller.
                    raise

            response_headers = _filter_response_headers(upstream.headers)
            # Forward actual Content-Type from upstream (usually text/event-stream)
            content_type = upstream.headers.get("content-type", "text/event-stream")

            # Parse the SSE stream incrementally into a small accumulator so
            # per-request peak memory is O(parsed content), not O(response × 3)
            # as the prior b"".join(chunks).decode().split("\n") pattern was.
            # Under concurrent pipeline load that triple allocation was the
            # gateway's memory high-water mark — see #1885.
            #
            # MAX_CAPTURE_SIZE is still enforced as a defensive cap so a
            # pathologically large upstream response can't drive the
            # accumulator unbounded, but the raw bytes are never buffered.
            MAX_CAPTURE_SIZE = 10 * 1024 * 1024  # 10MB
            accumulator: _SSEAccumulator | None = _SSEAccumulator() if container_id else None
            bytes_seen = 0
            capture_truncated = False

            def _consume_chunk(chunk: bytes) -> None:
                """Feed a chunk into the capture accumulator if under budget."""
                nonlocal bytes_seen, capture_truncated
                if accumulator is not None and not capture_truncated:
                    if bytes_seen + len(chunk) <= MAX_CAPTURE_SIZE:
                        accumulator.feed(chunk)
                        bytes_seen += len(chunk)
                    else:
                        capture_truncated = True
                        logger.debug(
                            "Streaming capture truncated due to size limit",
                            container_id=container_id,
                            size_limit=MAX_CAPTURE_SIZE,
                        )

            def generate() -> Any:
                try:
                    try:
                        if first_chunk is not None:
                            _consume_chunk(first_chunk)
                            yield first_chunk
                        for chunk in primed_iterator:
                            _consume_chunk(chunk)
                            yield chunk
                    except (httpx.ReadError, httpx.RemoteProtocolError) as mid_err:
                        # Mid-stream reset: emit a synthetic SSE `error`
                        # frame so the downstream SDK treats this as a
                        # clean API error instead of a truncated socket.
                        # The frame shape matches Anthropic's documented
                        # error event and is parsed by ``_SSEAccumulator``
                        # so operators still see the failed turn in the
                        # captured transcript.
                        logger.warning(
                            "Upstream Anthropic stream reset mid-response; "
                            "emitting synthetic SSE error frame",
                            container_id=container_id,
                            bytes_seen=bytes_seen,
                            error=str(mid_err),
                        )
                        error_payload = {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": "upstream connection reset",
                            },
                        }
                        error_frame = (
                            b"event: error\ndata: "
                            + json.dumps(error_payload).encode("utf-8")
                            + b"\n\n"
                        )
                        _consume_chunk(error_frame)
                        yield error_frame
                finally:
                    upstream.close()
                    if accumulator is not None and container_id:
                        try:
                            _capture_streaming_response(
                                container_id=container_id,
                                request_json=request_json,
                                result=accumulator.result(),
                                start_time=start_time,
                            )
                        except Exception as e:
                            logger.debug(
                                "Failed to capture streaming response to transcript",
                                container_id=container_id,
                                error=str(e),
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


def _run_health_server(host: str, port: int) -> None:
    """Run a dedicated lightweight HTTP server for health checks.

    This server runs on a separate port from the main Waitress thread pool,
    ensuring health checks are never blocked by long-running API requests
    (e.g., synchronous git operations holding Waitress threads).
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/api/v1/health":
                self.send_response(404)
                self.end_headers()
                return

            # Lightweight health check for Docker liveness probes.
            # Note: is_token_valid() can block during token refresh (up to 30s
            # synchronous HTTP call to GitHub). ThreadingHTTPServer ensures a
            # slow refresh doesn't block concurrent health check requests.
            # The full health endpoint on the main port still does
            # orchestrator/squid process checks for detailed diagnostics.
            try:
                github = get_github_client()
                token_valid = github.is_token_valid()
            except Exception:
                token_valid = False

            try:
                get_launcher_secret()
                launcher_ok = True
            except Exception:
                launcher_ok = False

            # Quick squid port check
            squid_listening = False
            try:
                with socket.create_connection(("127.0.0.1", 3129), timeout=2):
                    squid_listening = True
            except OSError:
                pass

            is_healthy = token_valid and launcher_ok and squid_listening
            body = json.dumps(
                {
                    "status": "healthy" if is_healthy else "degraded",
                    "github_token_valid": token_valid,
                    "auth_configured": launcher_ok,
                    "squid_proxy": {"listening": squid_listening},
                    "service": "gateway",
                }
            ).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress default stderr logging for health checks
            pass

    server = ThreadingHTTPServer((host, port), HealthHandler)
    server.serve_forever()


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
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"Waitress thread pool size (default: {DEFAULT_THREADS})",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=HEALTH_CHECK_PORT,
        help=f"Dedicated health check port (default: {HEALTH_CHECK_PORT})",
    )

    args = parser.parse_args()

    # Initialize token refresher for in-memory token management.
    # Retry with backoff on transient failures (e.g. DNS not available at startup).
    # Without a GitHub token the gateway can't serve its purpose, so exit if
    # initialization never succeeds.
    token_init_timeout = int(os.environ.get("EGG_TOKEN_INIT_TIMEOUT", "120"))
    try:
        from token_refresher import (
            initialize_token_refresher,
            is_token_refresher_permanently_failed,
        )

        refresher = None
        start_time = time.time()
        attempt = 0
        while True:
            attempt += 1
            refresher = initialize_token_refresher()
            if refresher:
                logger.info("Token refresher initialized (in-memory token refresh enabled)")
                break

            # Permanent failures (missing credentials/key file) won't resolve
            # on retry — exit immediately instead of waiting for the timeout.
            if is_token_refresher_permanently_failed():
                logger.warning("Token refresher not configured - GitHub operations will fail")
                break

            elapsed = time.time() - start_time
            if elapsed >= token_init_timeout:
                logger.error(
                    "Token refresher failed to initialize after timeout — exiting",
                    timeout_seconds=token_init_timeout,
                    attempts=attempt,
                )
                sys.exit(1)

            backoff = min(5 * (2 ** (attempt - 1)), 30)
            remaining = token_init_timeout - elapsed
            wait = min(backoff, remaining)
            logger.warning(
                "Token refresher not ready, retrying",
                attempt=attempt,
                retry_in_seconds=wait,
                elapsed_seconds=round(elapsed, 1),
                timeout_seconds=token_init_timeout,
            )
            time.sleep(wait)
    except ImportError:
        logger.error("Token refresher module not available - GitHub operations will fail")
        sys.exit(1)
    except Exception as e:
        logger.error(
            "Token refresher initialization failed unexpectedly",
            error=str(e),
            error_type=type(e).__name__,
        )
        sys.exit(1)

    # Initialize reviewer token refresher (optional — for posting reviews with
    # approve/request-changes using a separate GitHub App identity).
    # Reviewer is optional so we don't retry or block startup.
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
    #
    # Each session contributes its own ``container_id`` plus the per-agent
    # and pipeline-level worktree anchor IDs ({pipeline_id}-{role} and
    # {pipeline_id}).  Without those derived anchors, cleanup would treat
    # every live pipeline's per-agent worktree as orphaned because the
    # on-disk dir name never matches the session container_id (#1874).
    active_container_ids: set[str] = set()
    try:
        session_manager = get_session_manager()
        pruned = session_manager.prune_expired_sessions()
        if pruned > 0:
            logger.info(f"Startup session cleanup pruned {pruned} expired session(s)")
        # Extract active container IDs from surviving sessions, plus the
        # per-agent/pipeline worktree anchors the orchestrator assigns.
        sessions = session_manager.list_sessions()
        active_container_ids |= _container_ids_from_sessions(sessions)
        if active_container_ids:
            logger.info(
                "Active containers from sessions",
                count=len(active_container_ids),
            )
    except Exception as e:
        logger.warning("Startup session cleanup failed", error=str(e))

    # Check for active sessions with missing transcript buffers.
    # Buffers are now persisted, but may still be missing if the session hasn't
    # made any API calls yet or the buffer was cleaned up prematurely.
    try:
        from egg_contracts.transcript_extractor import get_proxy_buffer_path

        for session_info in session_manager.list_sessions():
            cid = session_info.get("container_id")
            if cid:
                bp = get_proxy_buffer_path(cid)
                if not bp.exists():
                    logger.warning(
                        "Active session has no transcript buffer — may not have been created yet or was cleaned up prematurely",
                        container_id=cid,
                        buffer_path=str(bp),
                    )
    except Exception as e:
        logger.warning("Startup transcript buffer check failed", error=str(e))

    # Also check Docker directly as safety net — sessions may be
    # pruned but containers still running.
    try:
        docker_containers = get_active_docker_containers()
        active_container_ids |= docker_containers
    except Exception as e:
        logger.warning("Could not query Docker containers", error=str(e))

    # Clean up orphaned worktrees in a background thread so it doesn't block
    # the Waitress thread pool at startup. Worktree cleanup involves synchronous
    # git operations that can hold threads for seconds each, and with many
    # orphaned sessions this was exhausting the thread pool before the gateway
    # could serve any requests. See: https://github.com/jwbron/egg/issues/1400
    def _background_worktree_cleanup() -> None:
        try:
            orphans_removed = startup_cleanup(
                active_containers=active_container_ids,
                session_manager=get_session_manager(),
            )
            if orphans_removed > 0:
                logger.info(f"Startup cleanup removed {orphans_removed} orphaned worktree(s)")
        except Exception as e:
            logger.warning("Startup worktree cleanup failed", error=str(e))

    cleanup_thread = threading.Thread(
        target=_background_worktree_cleanup,
        name="startup-worktree-cleanup",
        daemon=True,
    )
    cleanup_thread.start()

    # Start background session pruner so stale entries don't accumulate across
    # restarts.  Without this, sessions for dead containers survive until their
    # 24h TTL lapses and are reloaded on every gateway restart (#1884).
    try:
        prune_interval = max(1, int(os.environ.get("EGG_SESSION_CLEANUP_INTERVAL_MINUTES", "15")))
        idle_timeout = max(5, int(os.environ.get("EGG_SESSION_IDLE_TIMEOUT_MINUTES", "60")))
        get_session_manager().start_background_pruner(
            interval_minutes=prune_interval,
            idle_timeout_minutes=idle_timeout,
        )
    except Exception as e:
        logger.warning("Failed to start session background pruner", error=str(e))

    # Ensure launcher secret is configured - fail startup if not
    try:
        get_launcher_secret()
    except LauncherSecretNotConfiguredError as e:
        logger.error("Startup failed: launcher secret not configured", error=str(e))
        sys.exit(1)

    # Under k8s the compose-era default hostname "egg-orchestrator" does
    # not resolve, so falling back to it produces cryptic "Orchestrator
    # unreachable" errors on the agent side mid-pipeline. Fail startup
    # instead so the misconfiguration is visible at deploy time (#1803).
    if os.environ.get("KUBERNETES_SERVICE_HOST") and not os.environ.get("EGG_ORCHESTRATOR_URL"):
        logger.error(
            "Startup failed: EGG_ORCHESTRATOR_URL must be set when running in Kubernetes. "
            "Set it on the gateway Deployment, e.g. "
            "http://orchestrator.egg-system.svc.cluster.local:9849"
        )
        sys.exit(1)

    # Register SIGHUP handler for config reload.
    # Usage: docker kill -s HUP egg-gateway
    def _handle_sighup(signum: int, frame: Any) -> None:
        _reload_all_config()

    signal.signal(signal.SIGHUP, _handle_sighup)

    # Register SIGTERM handler for graceful shutdown.
    # When Docker sends SIGTERM, delay for 5s to let in-flight session cleanup
    # requests complete before exiting. This prevents the race condition where
    # the gateway becomes unreachable before the launcher's cleanup hook runs.
    # NOTE: This is a delay, not a true drain — waitress continues accepting new
    # requests during the sleep. If a new long-running request starts during this
    # window, it will be killed when sys.exit(0) fires. For our use case (Docker
    # stop), this is acceptable since the only in-flight requests at shutdown are
    # short-lived session cleanup calls.
    def _handle_shutdown(signum: int, frame: Any) -> None:
        logger.info("Received SIGTERM, delaying 5s for in-flight requests before shutdown...")
        time.sleep(5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_shutdown)

    logger.info(
        "Starting Gateway Sidecar",
        host=args.host,
        port=args.port,
        debug=args.debug,
        threads=args.threads,
        health_port=args.health_port,
    )
    logger.info("Session authentication required for all container operations")

    # Optional tracemalloc sampler (opt-in via GATEWAY_MEM_TRACE=1). Emits
    # periodic RSS + top-allocation-site log records to stdout so the trail
    # survives pod OOM via `kubectl logs --previous`. See #1885.
    try:
        from .mem_trace import start_if_enabled as _mem_trace_start
    except ImportError:
        from mem_trace import (  # type: ignore[no-redef, import-untyped]
            start_if_enabled as _mem_trace_start,
        )
    _mem_trace_start()

    # Start dedicated health check server on a separate port so Docker/orchestrator
    # health checks are never blocked by long-running git operations on the main
    # Waitress thread pool. See: https://github.com/jwbron/egg/issues/1400
    health_thread = threading.Thread(
        target=_run_health_server,
        args=(args.host, args.health_port),
        name="health-check-server",
        daemon=True,
    )
    health_thread.start()
    logger.info("Dedicated health check server started", port=args.health_port)

    # Run with production server in production, debug server in debug mode
    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
    else:
        # Use waitress for production with configurable thread pool.
        # Increased from 8 (previous default) to 32 to handle concurrent load
        # from multiple SDLC pipelines. See: https://github.com/jwbron/egg/issues/1400
        serve(app, host=args.host, port=args.port, threads=args.threads)


if __name__ == "__main__":
    main()
