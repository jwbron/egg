"""
GitHub Client - Wraps gh CLI with token management and command validation.

Provides:
- Token management (bot and user modes)
- gh CLI command execution
- Command validation (allowlist/blocklist)
- API path validation

Token management is handled by the in-memory token refresher (token_refresher.py).
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_logging
# In container, egg_logging is at /app/egg_logging
# On host, it's at ../../shared/egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

# Import repo_config for user mode support
# Path setup needed because config is in a sibling directory
_config_path = Path(__file__).parent.parent / "config"
if _config_path.exists() and str(_config_path) not in sys.path:
    sys.path.insert(0, str(_config_path))
from repo_config import get_repos_for_sync, get_user_mode_config, is_user_mode_repo

logger = get_logger("gateway.github-client")

GH_CLI = "/usr/bin/gh"

# User token from environment variable (for user mode)
USER_TOKEN_VAR = "GITHUB_USER_TOKEN"


# =============================================================================
# gh Command Validation
# =============================================================================

# Read-only gh commands that don't require ownership checks
READONLY_GH_COMMANDS = frozenset(
    {
        "pr view",
        "pr list",
        "pr checks",
        "pr diff",
        "pr status",
        "issue view",
        "issue list",
        "issue status",
        "repo view",
        "repo list",
        "release view",
        "release list",
        "api",  # Read-only API calls (GET)
        "auth status",
        "config get",
    }
)

# Blocked gh commands (dangerous operations)
BLOCKED_GH_COMMANDS = frozenset(
    {
        "pr merge",  # Human must merge
        "repo delete",
        "repo archive",
        "release delete",
        "auth logout",
        "auth login",
        "config set",
    }
)

# Allowlist of gh api paths that are permitted
# These patterns match GitHub API endpoints that are safe for read/write operations
GH_API_ALLOWED_PATHS = [
    # PR operations
    re.compile(r"^repos/[^/]+/[^/]+/pulls$"),  # List PRs
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+$"),  # View PR
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/comments$"),  # PR review comments (on diff)
    re.compile(r"^repos/[^/]+/[^/]+/pulls/comments/\d+$"),  # Specific PR review comment
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews$"),  # PR reviews
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews/\d+$"),  # Specific review
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews/\d+/comments$"),  # Review comments
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/requested_reviewers$"),  # Requested reviewers
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/files$"),  # PR files
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/commits$"),  # PR commits
    # Issue operations
    re.compile(r"^repos/[^/]+/[^/]+/issues$"),  # List issues
    re.compile(r"^repos/[^/]+/[^/]+/issues/\d+$"),  # View issue
    re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/comments$"),  # Issue comments
    re.compile(r"^repos/[^/]+/[^/]+/issues/comments/\d+$"),  # Specific issue/PR comment
    re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/labels$"),  # Issue labels
    re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/events$"),  # Issue events
    re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/timeline$"),  # Issue timeline
    # Repository info
    re.compile(r"^repos/[^/]+/[^/]+$"),  # Repo info
    re.compile(r"^repos/[^/]+/[^/]+/branches$"),  # List branches
    re.compile(r"^repos/[^/]+/[^/]+/branches/[^/]+$"),  # Branch info
    re.compile(r"^repos/[^/]+/[^/]+/commits$"),  # List commits
    re.compile(r"^repos/[^/]+/[^/]+/commits/[a-f0-9]+$"),  # Specific commit
    re.compile(r"^repos/[^/]+/[^/]+/commits/[a-f0-9]+/comments$"),  # Commit comments
    re.compile(r"^repos/[^/]+/[^/]+/comments/\d+$"),  # Specific commit comment
    re.compile(r"^repos/[^/]+/[^/]+/contents/.*$"),  # File contents
    re.compile(r"^repos/[^/]+/[^/]+/git/refs.*$"),  # Git refs
    re.compile(r"^repos/[^/]+/[^/]+/compare/.*$"),  # Compare commits
    # Releases
    re.compile(r"^repos/[^/]+/[^/]+/releases$"),  # List releases
    re.compile(r"^repos/[^/]+/[^/]+/releases/\d+$"),  # Specific release
    re.compile(r"^repos/[^/]+/[^/]+/releases/latest$"),  # Latest release
    re.compile(r"^repos/[^/]+/[^/]+/releases/tags/[^/]+$"),  # Release by tag
    # User info
    re.compile(r"^user$"),  # Current user
    re.compile(r"^users/[^/]+$"),  # User info
]


def validate_gh_api_path(path: str, method: str = "GET") -> tuple[bool, str]:
    """
    Validate gh api path against allowlist.

    Args:
        path: The API path (e.g., "repos/owner/repo/pulls/123")
        method: The HTTP method (GET, POST, etc.)

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Only GET, POST, PATCH allowed - no DELETE for safety
    if method.upper() not in ("GET", "POST", "PATCH"):
        return False, f"HTTP method '{method}' not allowed for gh api"

    # Strip leading slash if present
    path = path.lstrip("/")

    # Check against allowed patterns
    for pattern in GH_API_ALLOWED_PATHS:
        if pattern.match(path):
            return True, ""

    return False, f"API path '{path}' not in allowlist"


# gh api flags that take a value argument
# These must be skipped when looking for the API path
GH_API_FLAGS_WITH_VALUES = frozenset(
    {
        "-X",
        "--method",
        "-H",
        "--header",
        "-f",
        "--field",
        "-F",
        "--raw-field",
        "-q",
        "--jq",
        "-t",
        "--template",
        "-R",
        "--repo",
        "--input",
        "--cache",
        "--hostname",
    }
)

# gh api flags that don't take a value (boolean flags)
GH_API_FLAGS_NO_VALUE = frozenset(
    {
        "-p",
        "--paginate",
        "--slurp",
        "-i",
        "--include",
        "--silent",
        "--verbose",
    }
)


def parse_gh_api_args(args: list[str]) -> tuple[str | None, str]:
    """
    Parse gh api command arguments to extract the API path and HTTP method.

    The gh api command accepts various flags before the API path:
        gh api -X PATCH repos/owner/repo/pulls/123 -f base=main
        gh api --method POST -H "Accept: application/json" /repos/owner/repo/issues

    This function properly skips flags and their values to find the actual API path.

    Args:
        args: The argument list after 'api' (e.g., ["-X", "PATCH", "repos/..."])

    Returns:
        Tuple of (api_path, http_method).
        api_path is None if no path could be found.
        http_method defaults to "GET" if not specified.
    """
    method = "GET"
    api_path = None
    i = 0

    while i < len(args):
        arg = args[i]

        # Check for HTTP method flag
        if arg in ("-X", "--method"):
            if i + 1 < len(args):
                method = args[i + 1].upper()
                i += 2
                continue
            else:
                # Flag without value - skip it
                i += 1
                continue

        # Check for other flags that take values
        if arg in GH_API_FLAGS_WITH_VALUES:
            # Skip flag and its value
            i += 2
            continue

        # Check for flags that don't take values
        if arg in GH_API_FLAGS_NO_VALUE:
            i += 1
            continue

        # Check for combined flag=value format (e.g., --method=PATCH, -f=key=value)
        if "=" in arg and arg.startswith("-"):
            # Handle method specially
            if arg.startswith(("-X=", "--method=")):
                method = arg.split("=", 1)[1].upper()
            i += 1
            continue

        # Check for other flags we don't recognize (starts with -)
        if arg.startswith("-"):
            i += 1
            continue

        # This is a positional argument - should be the API path
        api_path = arg
        break

    return api_path, method


# =============================================================================
# Repository Extraction for Private Mode Enforcement
# =============================================================================

# Commands blocked entirely in private mode (too broad to filter by repository)
GH_COMMANDS_BLOCKED_IN_PRIVATE_MODE = frozenset(
    {
        "search",  # gh search repos/issues/prs/commits - too broad
    }
)


def extract_repo_from_gh_api_path(api_path: str) -> str | None:
    """
    Extract owner/repo from a gh api path.

    Examples:
        "repos/owner/repo/pulls" -> "owner/repo"
        "repos/owner/repo" -> "owner/repo"
        "/repos/owner/repo/issues/123" -> "owner/repo"
        "user" -> None
        "orgs/myorg/repos" -> None

    Args:
        api_path: The API path (with or without leading slash)

    Returns:
        "owner/repo" string or None if not a repo-scoped path
    """
    path = api_path.lstrip("/")

    # Must start with "repos/"
    if not path.startswith("repos/"):
        return None

    # Split and extract owner/repo
    parts = path.split("/")
    if len(parts) >= 3:
        owner, repo = parts[1], parts[2]
        # Validate they look like valid GitHub identifiers
        if owner and repo and not owner.startswith("-") and not repo.startswith("-"):
            return f"{owner}/{repo}"

    return None


def extract_repo_from_gh_command(args: list[str]) -> str | None:
    """
    Extract target repository from any gh command.

    Handles multiple patterns:
    1. --repo/-R flag: gh pr view 123 -R owner/repo
    2. gh repo commands: gh repo view owner/repo
    3. gh api paths: gh api /repos/owner/repo/issues

    Args:
        args: Command arguments (after 'gh')

    Returns:
        "owner/repo" string or None if not determinable
    """
    if not args:
        return None

    # Pattern 1: --repo or -R flag (highest priority)
    for i, arg in enumerate(args):
        if arg in ("--repo", "-R") and i + 1 < len(args):
            return args[i + 1]

    # Pattern 2: gh repo <subcommand> <owner/repo>
    # Subcommands that take repo as first positional arg:
    # view, clone, fork, edit, delete, archive, rename, sync
    if args[0] == "repo" and len(args) >= 3:
        subcommand = args[1]
        repo_arg = args[2]

        # These subcommands take owner/repo as positional argument
        # Note: credits and deploy-key also take repo but are less common
        positional_repo_subcommands = {
            "view",
            "clone",
            "fork",
            "edit",
            "delete",
            "archive",
            "rename",
            "sync",
            "set-default",
            "credits",
            "deploy-key",
        }

        # Validate it looks like owner/repo (not a flag)
        if (
            subcommand in positional_repo_subcommands
            and "/" in repo_arg
            and not repo_arg.startswith("-")
        ):
            return repo_arg

    # Pattern 3: gh api /repos/owner/repo/...
    if args[0] == "api" and len(args) > 1:
        api_path, _ = parse_gh_api_args(args[1:])
        if api_path:
            return extract_repo_from_gh_api_path(api_path)

    return None


@dataclass
class GitHubToken:
    """GitHub App installation token with metadata."""

    token: str
    expires_at_unix: float
    expires_at: str
    generated_at: str

    @property
    def is_expired(self) -> bool:
        """Check if token is expired (with 5 minute buffer)."""
        now = datetime.now(UTC).timestamp()
        return now > (self.expires_at_unix - 5 * 60)

    @property
    def minutes_until_expiry(self) -> float:
        """Minutes until token expires."""
        now = datetime.now(UTC).timestamp()
        return (self.expires_at_unix - now) / 60


@dataclass
class GitHubResult:
    """Result from a gh CLI command."""

    success: bool
    stdout: str
    stderr: str
    returncode: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
        }


class GitHubClient:
    """Client for executing gh CLI commands with token management."""

    def __init__(self, mode: str = "bot"):
        """
        Initialize the GitHub client.

        Args:
            mode: Authentication mode - "bot" (default) or "user"
        """
        self.mode = mode
        self._cached_token: GitHubToken | None = None
        self._cached_user_token: str | None = None

    def get_token(self) -> GitHubToken | None:
        """
        Get the current GitHub token from the in-memory token refresher.

        Returns cached token if still valid.
        """
        # Return cached token if still valid
        if self._cached_token and not self._cached_token.is_expired:
            return self._cached_token

        # Get token from the in-memory refresher
        try:
            from token_refresher import get_token_refresher

            refresher = get_token_refresher()
            if refresher:
                token_info = refresher.get_token_info()
                if token_info:
                    self._cached_token = GitHubToken(
                        token=token_info.token,
                        expires_at_unix=token_info.expires_at.timestamp(),
                        expires_at=token_info.expires_at.isoformat(),
                        generated_at=token_info.generated_at.isoformat(),
                    )
                    logger.debug(
                        "Token loaded from refresher",
                        minutes_until_expiry=f"{self._cached_token.minutes_until_expiry:.1f}",
                    )
                    return self._cached_token
        except ImportError:
            logger.error("token_refresher module not available")

        logger.warning("No valid token available from token refresher")
        return None

    def is_token_valid(self) -> bool:
        """Check if we have a valid (non-expired) token."""
        token = self.get_token()
        return token is not None and not token.is_expired

    def get_user_token(self) -> str | None:
        """
        Get the user mode token from environment.

        The user token is a Personal Access Token (PAT) that attributes
        git/gh operations to a personal GitHub account instead of the bot.

        Returns:
            PAT string or None if not configured
        """
        if self._cached_user_token:
            return self._cached_user_token

        token = os.environ.get(USER_TOKEN_VAR, "").strip()
        if token:
            self._cached_user_token = token
            return token

        logger.warning("User token not configured", env_var=USER_TOKEN_VAR)
        return None

    def is_user_token_valid(self) -> bool:
        """Check if user token is configured."""
        return bool(self.get_user_token())

    def get_authenticated_user(self, mode: str = "bot") -> str | None:
        """
        Get the GitHub username for the authenticated token.

        Args:
            mode: "bot" or "user"

        Returns:
            GitHub username or None if request fails
        """
        result = self.execute(["api", "/user", "--jq", ".login"], mode=mode)
        if result.success and result.stdout:
            return result.stdout.strip()
        return None

    def validate_user_mode_config(self) -> tuple[bool, str]:
        """
        Validate that the user token matches the configured github_user.

        Returns:
            Tuple of (is_valid, message)
        """
        # Get configured user from repo config
        config = get_user_mode_config()
        configured_user = config.get("github_user", "").strip()

        if not configured_user:
            # No user configured - that's fine, user mode just won't be used
            return True, "No user mode configured (user mode disabled)"

        # Check if any repos actually use user mode
        repos_using_user_mode = [repo for repo in get_repos_for_sync() if is_user_mode_repo(repo)]
        if not repos_using_user_mode:
            # User is configured but no repos use auth_mode: user - that's fine
            return True, f"User '{configured_user}' configured but no repos use user mode"

        # Check if token is configured
        token = self.get_user_token()
        if not token:
            return (
                False,
                f"User '{configured_user}' configured but {USER_TOKEN_VAR} not set",
            )

        # Get the actual user from the token
        actual_user = self.get_authenticated_user(mode="user")
        if not actual_user:
            return (
                False,
                f"Could not authenticate with {USER_TOKEN_VAR} - token may be invalid",
            )

        # Compare users (case-insensitive)
        if actual_user.lower() != configured_user.lower():
            logger.error(
                "User token/user mismatch",
                configured_user=configured_user,
                actual_user=actual_user,
            )
            return False, (
                f"Token/user mismatch: {USER_TOKEN_VAR} belongs to '{actual_user}' "
                f"but user_mode.github_user is '{configured_user}'"
            )

        logger.info(
            "User mode config validated",
            github_user=actual_user,
        )
        return True, f"User mode configured for user '{actual_user}'"

    def get_token_for_mode(self, mode: str | None = None) -> str | None:
        """
        Get the appropriate token string for the specified mode.

        Args:
            mode: "bot" or "user" (defaults to self.mode)

        Returns:
            Token string or None if not available
        """
        mode = mode or self.mode
        if mode == "user":
            return self.get_user_token()
        else:
            token = self.get_token()
            return token.token if token else None

    def execute(
        self,
        args: list[str],
        timeout: int = 60,
        cwd: str | Path | None = None,
        mode: str | None = None,
    ) -> GitHubResult:
        """
        Execute a gh CLI command with authentication.

        Args:
            args: Command arguments (without 'gh' prefix)
            timeout: Command timeout in seconds
            cwd: Working directory for the command
            mode: Auth mode override ("bot" or "user"), defaults to self.mode

        Returns:
            GitHubResult with command output
        """
        effective_mode = mode or self.mode
        token_str = self.get_token_for_mode(effective_mode)

        if not token_str:
            if effective_mode == "user":
                return GitHubResult(
                    success=False,
                    stdout="",
                    stderr=f"User token not available. Set {USER_TOKEN_VAR} environment variable.",
                    returncode=1,
                )
            else:
                return GitHubResult(
                    success=False,
                    stdout="",
                    stderr="GitHub token not available. Token refresher may not be initialized.",
                    returncode=1,
                )

        # Build environment with token
        # Include git safe.directory config for worktree paths - gh uses git internally
        # and would otherwise fail with "dubious ownership" on container worktree paths
        env = {
            "GH_TOKEN": token_str,
            "PATH": "/usr/bin:/bin",
            # Pass git config via environment
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
            # Rewrite SSH URLs to HTTPS so git uses token auth instead of SSH keys.
            # This is needed because gh commands like 'pr checkout' internally run
            # git fetch, which would fail if the remote uses SSH URL format.
            "GIT_CONFIG_KEY_1": "url.https://github.com/.insteadOf",
            "GIT_CONFIG_VALUE_1": "git@github.com:",
            "GIT_CONFIG_KEY_2": "url.https://github.com/.insteadOf",
            "GIT_CONFIG_VALUE_2": "ssh://git@github.com/",
        }

        cmd = [GH_CLI, *args]
        logger.debug("Executing gh command", command_args=args, cwd=str(cwd) if cwd else None)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
                check=False,
            )

            success = result.returncode == 0
            if not success:
                # Check for GitHub rate limit errors
                stderr_lower = (result.stderr or "").lower()
                if "rate limit" in stderr_lower or "api rate limit exceeded" in stderr_lower:
                    logger.error(
                        "GitHub rate limit exceeded",
                        command_args=args,
                        returncode=result.returncode,
                        stderr=result.stderr[:500] if result.stderr else None,
                    )
                else:
                    logger.warning(
                        "gh command failed",
                        command_args=args,
                        returncode=result.returncode,
                        stderr=result.stderr[:500] if result.stderr else None,
                    )

            return GitHubResult(
                success=success,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
            )

        except subprocess.TimeoutExpired:
            logger.error("gh command timed out", command_args=args, timeout=timeout)
            return GitHubResult(
                success=False,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                returncode=-1,
            )
        except Exception as e:
            logger.error("gh command failed", command_args=args, error=str(e))
            return GitHubResult(
                success=False,
                stdout="",
                stderr=str(e),
                returncode=-1,
            )

    def get_pr_info(self, repo: str, pr_number: int) -> dict[str, Any] | None:
        """
        Get information about a PR.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: PR number

        Returns:
            PR info dict or None on error
        """
        result = self.execute(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "number,title,author,state,headRefName,baseRefName",
            ]
        )

        if not result.success:
            return None

        try:
            parsed: dict[str, Any] = json.loads(result.stdout)
            return parsed
        except json.JSONDecodeError:
            logger.error("Failed to parse PR info", stdout=result.stdout[:500])
            return None

    def list_prs_for_branch(
        self, repo: str, branch: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        """
        List PRs for a specific head branch.

        Args:
            repo: Repository in "owner/repo" format
            branch: Head branch name
            state: PR state filter (open, closed, all)

        Returns:
            List of PR info dicts
        """
        result = self.execute(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                branch,
                "--state",
                state,
                "--json",
                "number,title,author,state,headRefName",
            ]
        )

        if not result.success:
            return []

        try:
            parsed: list[dict[str, Any]] = json.loads(result.stdout)
            return parsed
        except json.JSONDecodeError:
            return []

    def branch_exists(self, repo: str, branch: str, mode: str = "bot") -> bool | None:
        """
        Check if a branch exists in the remote repository.

        Args:
            repo: Repository in "owner/repo" format
            branch: Branch name
            mode: Auth mode - "bot" or "user" (use user mode for private repos)

        Returns:
            True if the branch exists, False if it doesn't, None if unknown (error)
        """
        # Use gh api to check if branch exists
        # GET /repos/{owner}/{repo}/branches/{branch} returns 200 if exists, 404 if not
        result = self.execute(
            [
                "api",
                f"repos/{repo}/branches/{branch}",
                "--silent",
            ],
            mode=mode,
        )

        if result.success:
            return True

        # Check if it's a 404 (branch doesn't exist) vs other error
        stderr = result.stderr or ""
        if "404" in stderr or "Not Found" in stderr:
            return False

        # Other error (network, rate limit, auth failure, etc.)
        # Return None to indicate we couldn't determine branch existence
        logger.warning(
            "Could not determine branch existence",
            repo=repo,
            branch=branch,
            mode=mode,
            error=stderr[:200] if stderr else "unknown error",
        )
        return None


# Global client instances (one per mode)
_clients: dict[str, GitHubClient] = {}


def get_github_client(mode: str = "bot") -> GitHubClient:
    """
    Get a GitHub client instance for the specified mode.

    Args:
        mode: Authentication mode - "bot" (default) or "user"

    Returns:
        GitHubClient configured for the specified mode
    """
    if mode not in _clients:
        _clients[mode] = GitHubClient(mode=mode)
    return _clients[mode]


def get_user_mode_client() -> GitHubClient:
    """
    Get a GitHub client configured for user mode.

    Convenience function equivalent to get_github_client(mode="user").

    Returns:
        GitHubClient configured for user mode
    """
    return get_github_client(mode="user")
