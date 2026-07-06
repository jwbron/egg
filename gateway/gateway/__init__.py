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

import functools
import os
import secrets
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from flask import Flask, Response, g, jsonify, request
from waitress import serve

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"

if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))

from egg_health import HealthTracker
from egg_logging import get_logger

_health_tracker = HealthTracker()

try:
    from ..agent_restrictions import (
        check_agent_gh_operation,
        get_agent_pattern,  # noqa: F401 — re-exported for test patching
    )
    from ..anthropic_credentials import (
        get_credentials_manager,
        get_litellm_credentials_manager,
    )
    from ..confluence_client import (
        DEFAULT_LIMIT as CONFLUENCE_DEFAULT_LIMIT,
    )
    from ..confluence_client import (
        HARD_MAX_LIMIT as CONFLUENCE_HARD_MAX_LIMIT,
    )
    from ..confluence_client import (
        ConfluenceCredentialsUnavailable,
        ConfluenceResponseTooLarge,
        ConfluenceUpstreamError,
        ConfluenceUpstreamForbidden,
        get_confluence_client,
        redact_response,
        validate_confluence_api_path,
    )
    from ..confluence_credentials import reload_confluence_credentials
    from ..confluence_policy import (
        allowed_spaces as confluence_allowed_spaces,
    )
    from ..confluence_policy import (
        is_space_allowed as is_confluence_space_allowed,
    )
    from ..confluence_policy import (
        reload_confluence_policy,
    )
    from ..confluence_search import extract_search_spaces
    from ..git_client import (
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
    from ..github_client import (
        ALLOWED_GH_COMMANDS,
        BLOCKED_GH_COMMANDS,
        GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE,
        GitHubClient,
        extract_comment_edit_info,
        extract_issue_label_info,
        extract_pr_review_info,
        extract_pr_reviewer_info,
        extract_repo_from_gh_command,
        find_gh_command_index,
        get_github_client,
        is_gh_command_allowed,
        parse_gh_api_args,
        resolve_gh_api_template_variables,
        validate_gh_api_path,
    )
    from ..jira_client import (
        JiraCredentialsUnavailable,
        JiraUpstreamError,
        get_jira_client,
        validate_jira_api_path,
    )
    from ..jira_client import (
        validate_fields as validate_jira_fields,
    )
    from ..jira_credentials import reload_jira_credentials
    from ..jira_policy import (
        epic_link_field as jira_epic_link_field,
    )
    from ..jira_policy import (
        extract_project_key,
        is_project_allowed,
        reload_jira_policy,
    )
    from ..jira_policy import (
        link_type_allowed as jira_link_type_allowed,
    )
    from ..jira_search import extract_search_projects
    from ..mode_gate import require_private_mode
    from ..orchestrator_pipelines import (
        fetch_active_pipeline_ids,
        wait_for_active_pipeline_ids,
    )
    from ..phase_filter import (
        OperationType,
        PipelinePhase,
        check_agent_restrictions,  # noqa: F401 — re-exported for test patching
        check_anchor_write_permission,
        check_phase_file_restrictions,
        filter_operation,
    )
    from ..policy import (
        extract_branch_from_refspec,
        extract_repo_from_remote,
        get_policy_engine,
        reload_policy_caches,
    )
    from ..private_repo_policy import (
        check_private_repo_access,
    )
    from ..rate_limiter import (
        check_heartbeat_rate_limit,
        record_failed_lookup,
    )
    from ..repo_parser import OWNER_REPO_PATTERN, parse_owner_repo
    from ..repo_visibility import get_repo_visibility
    from ..routing_policy import (
        RouteHop,
        get_routing_policy_manager,
    )
    from ..session_manager import (
        get_session_manager,
        validate_session_for_request,
    )
    from ..upstream_registry import (
        UnknownUpstreamError,
        get_upstream_registry,
    )
    from ..worktree_manager import (
        REPOS_BASE_DIR,
        WORKTREE_BASE_DIR,
        WorktreeManager,
        get_active_docker_containers,
        startup_cleanup,
        validate_branch_ref,
        validate_identifier,
    )
except ImportError:
    from agent_restrictions import (  # type: ignore[no-redef, import-untyped]
        check_agent_gh_operation,
        get_agent_pattern,  # noqa: F401 — re-exported for test patching
    )
    from anthropic_credentials import (  # type: ignore[no-redef]
        get_credentials_manager,
        get_litellm_credentials_manager,
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
        ALLOWED_GH_COMMANDS,
        BLOCKED_GH_COMMANDS,
        GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE,
        GitHubClient,
        extract_comment_edit_info,
        extract_issue_label_info,
        extract_pr_review_info,
        extract_pr_reviewer_info,
        extract_repo_from_gh_command,
        find_gh_command_index,
        get_github_client,
        is_gh_command_allowed,
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
    from orchestrator_pipelines import (  # type: ignore[no-redef, import-untyped]
        fetch_active_pipeline_ids,
        wait_for_active_pipeline_ids,
    )
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
    from repo_parser import (  # type: ignore[no-redef, import-untyped]
        OWNER_REPO_PATTERN,
        parse_owner_repo,
    )
    from repo_visibility import get_repo_visibility  # type: ignore[no-redef]
    from routing_policy import (  # type: ignore[no-redef, import-untyped]
        RouteHop,
        get_routing_policy_manager,
    )
    from session_manager import (  # type: ignore[no-redef, import-untyped]
        get_session_manager,
        validate_session_for_request,
    )
    from upstream_registry import (  # type: ignore[no-redef, import-untyped]
        UnknownUpstreamError,
        get_upstream_registry,
    )
    from worktree_manager import (  # type: ignore[no-redef, import-untyped]
        REPOS_BASE_DIR,
        WORKTREE_BASE_DIR,
        WorktreeManager,
        get_active_docker_containers,
        startup_cleanup,
        validate_branch_ref,
        validate_identifier,
    )

_config_path = Path(__file__).parent.parent.parent / "config"

if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))

from repo_config import get_auth_mode

logger = get_logger("gateway")

# --- decomposition submodules (#3312 slice-3) ------------------------------
# Each cluster's @app.route handlers are thin wrappers above; their bodies and
# the helper functions/constants live in the _<cluster> submodules. The barrel
# re-exports every non-route symbol so gateway.gateway.<name> (external imports
# and unittest.mock.patch targets) keeps resolving after the split.
from . import (
    _confluence,  # noqa: E402
    _gh_execute,  # noqa: E402
    _gh_ops,  # noqa: E402
    _git_execute,  # noqa: E402
    _git_ops,  # noqa: E402
    _health,  # noqa: E402
    _jira,  # noqa: E402
    _jira_writes,  # noqa: E402
    _proxy,  # noqa: E402
    _sessions,  # noqa: E402
    _worktree,  # noqa: E402
)
from ._confluence import (  # noqa: E402,F401
    _CONFLUENCE_PAGE_ID_RE,
    _CONFLUENCE_SPACE_KEY_RE,
    _check_post_fetch_space_allowlist,
    _confluence_clamp_limit,
    _confluence_error_from_upstream,
    _confluence_forbidden_response,
    _confluence_not_configured_error,
    _confluence_response_too_large,
    _confluence_space_denied_response,
    _redact_upstream_error_body,
    _resolve_space_key_for_payload,
    _resolve_space_key_via_list,
    _session_confluence_context,
    _validate_confluence_page_id,
    _validate_confluence_space_key,
)
from ._gh_ops import (  # noqa: E402,F401
    _apply_pr_labels,
)
from ._git_ops import (  # noqa: E402,F401
    _SLICE_INTEGRATION_BRANCH_RE,
    LS_REMOTE_VALUE_FLAGS,
    _detached_head_hint,
)
from ._health import (  # noqa: E402,F401
    _reload_all_config,
)
from ._helpers import (  # noqa: E402,F401
    _check_orchestrator_connectivity,
    _check_squid_health,
    _lookup_commit_observer_fn,
    audit_log,
    make_error,
    make_response,
    make_success,
    make_worktree_not_found_error,
)
from ._jira import (  # noqa: E402,F401
    _JIRA_PROJECT_KEY_RE,
    _JIRA_TICKET_KEY_RE,
    _TRANSITION_ALLOWLIST,
    _is_in_cluster_source,
    _jira_error_from_upstream,
    _jira_not_configured_error,
    _project_not_allowlisted_response,
    _session_jira_context,
    _verify_orchestrator_transition_auth,
)
from ._jira_writes import (  # noqa: E402,F401
    _JIRA_ALLOWED_ISSUETYPE_NAMES,
    _JIRA_BODY_MAX_CHARS,
    _JIRA_COMMENT_ALLOWED_KEYS,
    _JIRA_CREATE_ALLOWED_KEYS,
    _JIRA_EDIT_ALLOWED_KEYS,
    _JIRA_LABEL_MAX_CHARS,
    _JIRA_LABELS_MAX_COUNT,
    _JIRA_LINK_ALLOWED_KEYS,
    _JIRA_SUMMARY_MAX_CHARS,
    _jira_write_audit_meta,
    _validate_jira_labels,
    _validate_jira_text_field,
    _validate_jira_write_keys,
)
from ._proxy import (  # noqa: E402,F401
    _UPSTREAM_TRANSPORT_ERRORS,
    BLOCKED_TOOLS_PRIVATE_MODE,
    _attempt_hop_streaming,
    _classify_route_status,
    _close_quietly,
    _extract_wire_model,
    _filter_blocked_tools,
    _filter_response_headers,
    _get_forwarded_headers,
    _HopPrepError,
    _inject_anthropic_credentials,
    _inject_upstream_credentials,
    _is_streaming_request,
    _prepare_hop,
    _PreparedHop,
    _resolve_proxy_session,
    _resolve_route_chain,
    _rewrite_upstream_model,
    _sanitize_attribution_value,
    _send_and_prime,
    _with_attribution_headers,
)
from ._server import (  # noqa: E402,F401
    _run_health_server,
    main,
)
from ._sessions import (  # noqa: E402,F401
    VALID_PIPELINE_PHASES,
    _branch_exists_on_remote,
    _cleanup_container_worktrees,
)
from ._worktree import (  # noqa: E402,F401
    _SLICE_WORKTREE_SUFFIX_RE,
    _cleanup_empty_container_dir,
    _cleanup_stale_pack_files,
    _collect_active_container_ids,
    _container_ids_from_sessions,
    _derive_worktree_anchor_ids,
    _worktree_prune_lock,
    map_container_path_to_worktree,
)

try:
    # Production / package mode.
    from .._module_loader import load_sibling_gateway_module as _load_sibling_gateway_module
except ImportError:
    # Standalone-script mode (the test conftest loads gateway.py as
    # a flat top-level module, in which case the relative import
    # above raises ImportError before sys.modules has been seeded).
    from _module_loader import (  # type: ignore[no-redef, import-untyped]
        load_sibling_gateway_module as _load_sibling_gateway_module,
    )

app = Flask(__name__)

try:
    from ..contract_api import contract_bp

    app.register_blueprint(contract_bp)
except ImportError:
    from contract_api import contract_bp  # type: ignore[import-untyped, no-redef]

    app.register_blueprint(contract_bp)

try:
    from ..phase_api import phase_bp

    app.register_blueprint(phase_bp)
except ImportError:
    from phase_api import phase_bp  # type: ignore[import-untyped, no-redef]

    app.register_blueprint(phase_bp)

try:
    from ..artifact_api import artifact_bp

    app.register_blueprint(artifact_bp)
except ImportError:
    from artifact_api import artifact_bp  # type: ignore[import-untyped, no-redef]

    app.register_blueprint(artifact_bp)


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


DEFAULT_HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")  # Listen on all interfaces by default

DEFAULT_PORT = 9848

DEFAULT_THREADS = int(os.environ.get("GATEWAY_THREADS", "32"))

HEALTH_CHECK_PORT = int(os.environ.get("GATEWAY_HEALTH_PORT", "9851"))

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


try:
    from ..auth import require_session_auth
except ImportError:
    from auth import require_session_auth  # type: ignore[no-redef, import-untyped]

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


@app.route("/api/v1/proxy/ca-cert", methods=["GET"])
def get_proxy_ca_cert() -> tuple[Response, int] | Response:
    return _health.get_proxy_ca_cert()


@app.route("/api/v1/health", methods=["GET"])
def health_check() -> Response:
    return _health.health_check()


@app.route("/api/v1/config/reload", methods=["POST"])
@require_launcher_auth
def config_reload() -> Response:
    return _health.config_reload()


@app.route("/api/v1/git/push", methods=["POST"])
@require_session_or_launcher_auth
def git_push() -> tuple[Response, int] | Response:
    return _git_ops.git_push()


@app.route("/api/v1/git/execute", methods=["POST"])
@require_session_auth
def git_execute() -> tuple[Response, int] | Response:
    return _git_execute.git_execute()


@app.route("/api/v1/git/fetch", methods=["POST"])
@require_session_auth
def git_fetch() -> tuple[Response, int] | Response:
    return _git_ops.git_fetch()


@app.route("/api/v1/gh/pr/create", methods=["POST"])
@require_session_auth
def gh_pr_create() -> tuple[Response, int] | Response:
    return _gh_ops.gh_pr_create()


@app.route("/api/v1/gh/pr/comment", methods=["POST"])
@require_session_auth
def gh_pr_comment() -> tuple[Response, int] | Response:
    return _gh_ops.gh_pr_comment()


@app.route("/api/v1/gh/pr/edit", methods=["POST"])
@require_session_auth
def gh_pr_edit() -> tuple[Response, int] | Response:
    return _gh_ops.gh_pr_edit()


@app.route("/api/v1/gh/pr/close", methods=["POST"])
@require_session_auth
def gh_pr_close() -> tuple[Response, int] | Response:
    return _gh_ops.gh_pr_close()


@app.route("/api/v1/gh/execute", methods=["POST"])
@require_session_auth
def gh_execute() -> tuple[Response, int] | Response:
    return _gh_execute.gh_execute()


@app.route("/api/v1/gh/find_open_pr", methods=["POST"])
@require_launcher_auth
def gh_find_open_pr() -> tuple[Response, int] | Response:
    return _gh_ops.gh_find_open_pr()


@app.route("/api/v1/gh/list_open_prs", methods=["POST"])
@require_launcher_auth
def gh_list_open_prs() -> tuple[Response, int] | Response:
    return _gh_ops.gh_list_open_prs()


@app.route("/api/v1/gh/pr/merge_state", methods=["POST"])
@require_launcher_auth
def gh_pr_merge_state() -> tuple[Response, int] | Response:
    return _gh_ops.gh_pr_merge_state()


@app.route("/api/v1/gh/pr/ready", methods=["POST"])
@require_launcher_auth
def gh_pr_ready() -> tuple[Response, int] | Response:
    return _gh_ops.gh_pr_ready()


@app.route("/api/v1/jira/ticket/get", methods=["POST"])
@require_session_or_launcher_auth
@require_private_mode
def jira_ticket_get() -> tuple[Response, int] | Response:
    return _jira.jira_ticket_get()


@app.route("/api/v1/jira/search", methods=["POST"])
@require_session_or_launcher_auth
@require_private_mode
def jira_search() -> tuple[Response, int] | Response:
    return _jira.jira_search()


@app.route("/api/v1/jira/ticket/comments", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_comments() -> tuple[Response, int] | Response:
    return _jira.jira_ticket_comments()


@app.route("/api/v1/jira/ticket/remotelinks", methods=["POST"])
@require_session_or_launcher_auth
@require_private_mode
def jira_ticket_remotelinks() -> tuple[Response, int] | Response:
    return _jira.jira_ticket_remotelinks()


@app.route("/api/v1/jira/ticket/transition", methods=["POST"])
def jira_ticket_transition() -> tuple[Response, int] | Response:
    return _jira.jira_ticket_transition()


try:
    from ..mode_gate import PRIVATE_MODE_MARKER_ATTR as _PRIVATE_MODE_MARKER_ATTR  # noqa: E402
except ImportError:
    from mode_gate import PRIVATE_MODE_MARKER_ATTR as _PRIVATE_MODE_MARKER_ATTR  # type: ignore[no-redef]  # noqa: E402, I001

setattr(jira_ticket_transition, _PRIVATE_MODE_MARKER_ATTR, True)


@app.route("/api/v1/jira/execute", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_execute() -> tuple[Response, int] | Response:
    return _jira.jira_execute()


@app.route("/api/v1/jira/ticket/create", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_create() -> tuple[Response, int] | Response:
    return _jira_writes.jira_ticket_create()


@app.route("/api/v1/jira/ticket/edit", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_edit() -> tuple[Response, int] | Response:
    return _jira_writes.jira_ticket_edit()


@app.route("/api/v1/jira/ticket/comment/add", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_ticket_comment_add() -> tuple[Response, int] | Response:
    return _jira_writes.jira_ticket_comment_add()


@app.route("/api/v1/jira/issue-link/create", methods=["POST"])
@require_session_auth
@require_private_mode
def jira_issue_link_create() -> tuple[Response, int] | Response:
    return _jira_writes.jira_issue_link_create()


@app.route("/api/v1/confluence/page/get", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_page_get() -> tuple[Response, int] | Response:
    return _confluence.confluence_page_get()


@app.route("/api/v1/confluence/page/descendants", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_page_descendants() -> tuple[Response, int] | Response:
    return _confluence.confluence_page_descendants()


@app.route("/api/v1/confluence/page/footer-comments", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_page_footer_comments() -> tuple[Response, int] | Response:
    return _confluence.confluence_page_footer_comments()


@app.route("/api/v1/confluence/page/inline-comments", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_page_inline_comments() -> tuple[Response, int] | Response:
    return _confluence.confluence_page_inline_comments()


@app.route("/api/v1/confluence/space/pages", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_space_pages() -> tuple[Response, int] | Response:
    return _confluence.confluence_space_pages()


@app.route("/api/v1/confluence/space/list", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_space_list() -> tuple[Response, int] | Response:
    return _confluence.confluence_space_list()


@app.route("/api/v1/confluence/search", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_search() -> tuple[Response, int] | Response:
    return _confluence.confluence_search()


@app.route("/api/v1/confluence/execute", methods=["POST"])
@require_session_auth
@require_private_mode
def confluence_execute() -> tuple[Response, int] | Response:
    return _confluence.confluence_execute()


_worktree_manager: WorktreeManager | None = None


def get_worktree_manager() -> WorktreeManager:
    """Get or create the global WorktreeManager instance."""
    global _worktree_manager
    if _worktree_manager is None:
        _worktree_manager = WorktreeManager()
    return _worktree_manager


@app.route("/api/v1/worktree/create", methods=["POST"])
@require_launcher_auth
def worktree_create() -> tuple[Response, int] | Response:
    return _worktree.worktree_create()


@app.route("/api/v1/worktree/delete", methods=["POST"])
@require_launcher_auth
def worktree_delete() -> tuple[Response, int] | Response:
    return _worktree.worktree_delete()


@app.route("/api/v1/worktree/list", methods=["GET"])
@require_launcher_auth
def worktree_list() -> tuple[Response, int] | Response:
    return _worktree.worktree_list()


@app.route("/api/v1/worktrees/prune", methods=["POST"])
@require_launcher_auth
def worktrees_prune() -> tuple[Response, int] | Response:
    return _worktree.worktrees_prune()


@app.route("/api/v1/sessions/create", methods=["POST"])
@require_launcher_auth
def session_create() -> tuple[Response, int] | Response:
    return _sessions.session_create()


@app.route("/api/v1/sessions/<session_token>", methods=["DELETE"])
@require_launcher_auth
def session_delete(session_token: str) -> tuple[Response, int] | Response:
    return _sessions.session_delete(session_token)


@app.route("/api/v1/sessions/by-container/<container_id>", methods=["DELETE"])
@require_launcher_auth
def session_delete_by_container(container_id: str) -> tuple[Response, int] | Response:
    return _sessions.session_delete_by_container(container_id)


@app.route("/api/v1/sessions/by-container/<container_id>/heartbeat", methods=["POST"])
@require_launcher_auth
def session_heartbeat_by_container(container_id: str) -> tuple[Response, int] | Response:
    return _sessions.session_heartbeat_by_container(container_id)


@app.route("/api/v1/sessions/<session_token>", methods=["GET"])
@require_launcher_auth
def session_get(session_token: str) -> tuple[Response, int] | Response:
    return _sessions.session_get(session_token)


@app.route("/api/v1/sessions/<session_token>/heartbeat", methods=["POST"])
@require_launcher_auth
def session_heartbeat(session_token: str) -> tuple[Response, int] | Response:
    return _sessions.session_heartbeat(session_token)


@app.route("/api/v1/sessions/<session_token>", methods=["PATCH"])
@require_launcher_auth
def session_update(session_token: str) -> tuple[Response, int] | Response:
    return _sessions.session_update(session_token)


@app.route("/api/v1/sessions/<session_token>/phase", methods=["PATCH"])
@require_launcher_auth
def session_update_phase(session_token: str) -> tuple[Response, int] | Response:
    return _sessions.session_update_phase(session_token)


@app.route("/api/v1/repos/visibility", methods=["GET"])
@require_launcher_auth
def repos_visibility() -> tuple[Response, int] | Response:
    return _sessions.repos_visibility()


@app.route("/api/v1/sessions", methods=["GET"])
@require_launcher_auth
def sessions_list() -> tuple[Response, int] | Response:
    return _sessions.sessions_list()


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


ANTHROPIC_BLOCKED_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "authorization",
    "x-api-key",
    "connection",
}


@app.route("/v1/messages", methods=["POST"])
def proxy_anthropic_messages() -> tuple[Response, int] | Response:
    return _proxy.proxy_anthropic_messages()


@app.route("/v1/messages/count_tokens", methods=["POST"])
def proxy_count_tokens() -> tuple[Response, int] | Response:
    return _proxy.proxy_count_tokens()


if __name__ == "__main__":
    main()

__all__ = [
    "ALLOWED_GH_COMMANDS",
    "BLOCKED_GH_COMMANDS",
    "BLOCKED_TOOLS_PRIVATE_MODE",
    "CONFLUENCE_DEFAULT_LIMIT",
    "CONFLUENCE_HARD_MAX_LIMIT",
    "ConfluenceCredentialsUnavailable",
    "ConfluenceResponseTooLarge",
    "ConfluenceUpstreamError",
    "ConfluenceUpstreamForbidden",
    "GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE",
    "GIT_ALLOWED_COMMANDS",
    "GitHubClient",
    "JiraCredentialsUnavailable",
    "JiraUpstreamError",
    "LS_REMOTE_VALUE_FLAGS",
    "OWNER_REPO_PATTERN",
    "OperationType",
    "PipelinePhase",
    "REPOS_BASE_DIR",
    "RouteHop",
    "UnknownUpstreamError",
    "VALID_PIPELINE_PHASES",
    "WORKTREE_BASE_DIR",
    "WorktreeManager",
    "_CONFLUENCE_PAGE_ID_RE",
    "_CONFLUENCE_SPACE_KEY_RE",
    "_HopPrepError",
    "_JIRA_ALLOWED_ISSUETYPE_NAMES",
    "_JIRA_BODY_MAX_CHARS",
    "_JIRA_COMMENT_ALLOWED_KEYS",
    "_JIRA_CREATE_ALLOWED_KEYS",
    "_JIRA_EDIT_ALLOWED_KEYS",
    "_JIRA_LABELS_MAX_COUNT",
    "_JIRA_LABEL_MAX_CHARS",
    "_JIRA_LINK_ALLOWED_KEYS",
    "_JIRA_PROJECT_KEY_RE",
    "_JIRA_SUMMARY_MAX_CHARS",
    "_JIRA_TICKET_KEY_RE",
    "_PRIVATE_MODE_MARKER_ATTR",
    "_PreparedHop",
    "_SLICE_INTEGRATION_BRANCH_RE",
    "_SLICE_WORKTREE_SUFFIX_RE",
    "_TRANSITION_ALLOWLIST",
    "_UPSTREAM_TRANSPORT_ERRORS",
    "_apply_pr_labels",
    "_attempt_hop_streaming",
    "_branch_exists_on_remote",
    "_check_orchestrator_connectivity",
    "_check_post_fetch_space_allowlist",
    "_check_squid_health",
    "_classify_route_status",
    "_cleanup_container_worktrees",
    "_cleanup_empty_container_dir",
    "_cleanup_stale_pack_files",
    "_close_quietly",
    "_collect_active_container_ids",
    "_confluence_clamp_limit",
    "_confluence_error_from_upstream",
    "_confluence_forbidden_response",
    "_confluence_not_configured_error",
    "_confluence_response_too_large",
    "_confluence_space_denied_response",
    "_container_ids_from_sessions",
    "_derive_worktree_anchor_ids",
    "_detached_head_hint",
    "_extract_wire_model",
    "_filter_blocked_tools",
    "_filter_response_headers",
    "_get_forwarded_headers",
    "_inject_anthropic_credentials",
    "_inject_upstream_credentials",
    "_is_in_cluster_source",
    "_is_streaming_request",
    "_jira_error_from_upstream",
    "_jira_not_configured_error",
    "_jira_write_audit_meta",
    "_load_sibling_gateway_module",
    "_lookup_commit_observer_fn",
    "_prepare_hop",
    "_project_not_allowlisted_response",
    "_redact_upstream_error_body",
    "_reload_all_config",
    "_resolve_proxy_session",
    "_resolve_route_chain",
    "_resolve_space_key_for_payload",
    "_resolve_space_key_via_list",
    "_rewrite_upstream_model",
    "_run_health_server",
    "_sanitize_attribution_value",
    "_send_and_prime",
    "_session_confluence_context",
    "_session_jira_context",
    "_validate_confluence_page_id",
    "_validate_confluence_space_key",
    "_validate_jira_labels",
    "_validate_jira_text_field",
    "_validate_jira_write_keys",
    "_verify_orchestrator_transition_auth",
    "_with_attribution_headers",
    "_worktree_prune_lock",
    "app",
    "artifact_bp",
    "audit_log",
    "check_agent_gh_operation",
    "check_agent_restrictions",
    "check_anchor_write_permission",
    "check_heartbeat_rate_limit",
    "check_phase_file_restrictions",
    "check_private_repo_access",
    "cleanup_credential_helper",
    "confluence_allowed_spaces",
    "contract_bp",
    "create_credential_helper",
    "extract_branch_from_refspec",
    "extract_comment_edit_info",
    "extract_issue_label_info",
    "extract_pr_review_info",
    "extract_pr_reviewer_info",
    "extract_project_key",
    "extract_repo_from_gh_command",
    "extract_repo_from_remote",
    "extract_reset_target_ref",
    "extract_search_projects",
    "extract_search_spaces",
    "fetch_active_pipeline_ids",
    "filter_operation",
    "find_gh_command_index",
    "get_active_docker_containers",
    "get_agent_pattern",
    "get_auth_mode",
    "get_authenticated_remote_target",
    "get_changed_files_in_push",
    "get_confluence_client",
    "get_credentials_manager",
    "get_github_client",
    "get_jira_client",
    "get_litellm_credentials_manager",
    "get_policy_engine",
    "get_repo_visibility",
    "get_routing_policy_manager",
    "get_session_manager",
    "get_token_for_repo",
    "get_upstream_registry",
    "git_cmd",
    "is_branch_switch",
    "is_branch_switching_operation",
    "is_confluence_space_allowed",
    "is_gh_command_allowed",
    "is_project_allowed",
    "is_repos_parent_directory",
    "jira_epic_link_field",
    "jira_link_type_allowed",
    "main",
    "make_error",
    "make_response",
    "make_success",
    "make_worktree_not_found_error",
    "map_container_path_to_worktree",
    "os",
    "parse_gh_api_args",
    "parse_owner_repo",
    "phase_bp",
    "record_failed_lookup",
    "redact_response",
    "reload_confluence_credentials",
    "reload_confluence_policy",
    "reload_jira_credentials",
    "reload_jira_policy",
    "reload_policy_caches",
    "require_private_mode",
    "require_session_auth",
    "resolve_gh_api_template_variables",
    "resolve_remote_url",
    "serve",
    "socket",
    "startup_cleanup",
    "subprocess",
    "time",
    "validate_branch_ref",
    "validate_confluence_api_path",
    "validate_gh_api_path",
    "validate_git_args",
    "validate_identifier",
    "validate_jira_api_path",
    "validate_jira_fields",
    "validate_repo_path",
    "validate_session_for_request",
    "wait_for_active_pipeline_ids",
]
