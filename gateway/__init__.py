"""Gateway sidecar for egg sandbox environment.

This module provides the REST API server, policy engine, session management,
and other components for the gateway sidecar container.
"""

from .config_validator import (
    ConfigError,
    is_private_mode_enabled,
    validate_config,
    validate_network_lockdown_mode,
)
from .error_messages import (
    format_policy_blocked_response,
    get_error_message,
    get_hints_for_error,
)
from .fork_policy import ForkPolicy, ForkPolicyResult, check_fork_allowed, get_fork_policy
from .git_client import (
    GIT_ALLOWED_COMMANDS,
    cleanup_credential_helper,
    configure_paths,
    create_credential_helper,
    get_authenticated_remote_target,
    get_token_for_repo,
    git_cmd,
    is_repos_parent_directory,
    is_ssh_url,
    ssh_url_to_https,
    validate_git_args,
    validate_repo_path,
)
from .github_client import (
    BLOCKED_GH_COMMANDS,
    READONLY_GH_COMMANDS,
    GitHubClient,
    GitHubResult,
    GitHubToken,
    extract_repo_from_gh_command,
    get_github_client,
    parse_gh_api_args,
    validate_gh_api_path,
)
from .policy import (
    PolicyEngine,
    PolicyResult,
    extract_branch_from_refspec,
    extract_repo_from_remote,
    get_policy_engine,
)
from .private_repo_policy import (
    PrivateRepoPolicy,
    PrivateRepoPolicyResult,
    check_private_repo_access,
    get_private_repo_policy,
)
from .rate_limiter import (
    RateLimitResult,
    SlidingWindowRateLimiter,
    check_heartbeat_rate_limit,
    check_registration_rate_limit,
    get_all_limiter_stats,
    record_failed_lookup,
)
from .repo_config import RepoConfig, get_auth_mode, get_repo_config
from .repo_parser import (
    RepoInfo,
    extract_repo_from_request,
    is_github_url,
    normalize_repo_name,
    parse_github_url,
    parse_owner_repo,
    parse_repo_from_path,
    parse_worktree_path,
)
from .repo_visibility import (
    RepoVisibilityChecker,
    get_repo_visibility,
    get_visibility_checker,
    is_repo_private,
)
from .session_manager import (
    Session,
    SessionManager,
    SessionValidationResult,
    get_session_manager,
    validate_session_for_request,
)
from .token_refresher import (
    TokenInfo,
    TokenRefresher,
    get_bot_token,
    get_token_refresher,
    initialize_token_refresher,
    reset_token_refresher,
)
from .worktree_manager import (
    WorktreeInfo,
    WorktreeManager,
    WorktreeRemovalResult,
    get_active_docker_containers,
    startup_cleanup,
)

__all__ = [
    # config_validator
    "ConfigError",
    "is_private_mode_enabled",
    "validate_config",
    "validate_network_lockdown_mode",
    # error_messages
    "format_policy_blocked_response",
    "get_error_message",
    "get_hints_for_error",
    # fork_policy
    "ForkPolicy",
    "ForkPolicyResult",
    "check_fork_allowed",
    "get_fork_policy",
    # git_client
    "GIT_ALLOWED_COMMANDS",
    "cleanup_credential_helper",
    "configure_paths",
    "create_credential_helper",
    "get_authenticated_remote_target",
    "get_token_for_repo",
    "git_cmd",
    "is_repos_parent_directory",
    "is_ssh_url",
    "ssh_url_to_https",
    "validate_git_args",
    "validate_repo_path",
    # github_client
    "BLOCKED_GH_COMMANDS",
    "GitHubClient",
    "GitHubResult",
    "GitHubToken",
    "READONLY_GH_COMMANDS",
    "extract_repo_from_gh_command",
    "get_github_client",
    "parse_gh_api_args",
    "validate_gh_api_path",
    # policy
    "PolicyEngine",
    "PolicyResult",
    "extract_branch_from_refspec",
    "extract_repo_from_remote",
    "get_policy_engine",
    # private_repo_policy
    "PrivateRepoPolicy",
    "PrivateRepoPolicyResult",
    "check_private_repo_access",
    "get_private_repo_policy",
    # rate_limiter
    "RateLimitResult",
    "SlidingWindowRateLimiter",
    "check_heartbeat_rate_limit",
    "check_registration_rate_limit",
    "get_all_limiter_stats",
    "record_failed_lookup",
    # repo_config
    "RepoConfig",
    "get_auth_mode",
    "get_repo_config",
    # repo_parser
    "RepoInfo",
    "extract_repo_from_request",
    "is_github_url",
    "normalize_repo_name",
    "parse_github_url",
    "parse_owner_repo",
    "parse_repo_from_path",
    "parse_worktree_path",
    # repo_visibility
    "RepoVisibilityChecker",
    "get_repo_visibility",
    "get_visibility_checker",
    "is_repo_private",
    # session_manager
    "Session",
    "SessionManager",
    "SessionValidationResult",
    "get_session_manager",
    "validate_session_for_request",
    # token_refresher
    "TokenInfo",
    "TokenRefresher",
    "get_bot_token",
    "get_token_refresher",
    "initialize_token_refresher",
    "reset_token_refresher",
    # worktree_manager
    "WorktreeInfo",
    "WorktreeManager",
    "WorktreeRemovalResult",
    "get_active_docker_containers",
    "startup_cleanup",
]
