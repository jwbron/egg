"""
GitHub Client - Wraps gh CLI with token management and command validation.

Provides:
- Token management
- gh CLI command execution
- Command validation (allowlist/blocklist)
- API path validation
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shared.egg_logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger("gateway.github-client")

GH_CLI = "/usr/bin/gh"

# User token from environment variable (for user mode)
USER_TOKEN_VAR = "EGG_GITHUB_USER_TOKEN"


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
GH_API_ALLOWED_PATHS = [
    # PR operations
    re.compile(r"^repos/[^/]+/[^/]+/pulls$"),
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+$"),
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/comments$"),
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews$"),
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews/\d+$"),
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews/\d+/comments$"),
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/requested_reviewers$"),
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/files$"),
    re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/commits$"),
    # Issue operations
    re.compile(r"^repos/[^/]+/[^/]+/issues$"),
    re.compile(r"^repos/[^/]+/[^/]+/issues/\d+$"),
    re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/comments$"),
    re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/labels$"),
    # Repository info
    re.compile(r"^repos/[^/]+/[^/]+$"),
    re.compile(r"^repos/[^/]+/[^/]+/branches$"),
    re.compile(r"^repos/[^/]+/[^/]+/branches/[^/]+$"),
    re.compile(r"^repos/[^/]+/[^/]+/commits$"),
    re.compile(r"^repos/[^/]+/[^/]+/commits/[a-f0-9]+$"),
    re.compile(r"^repos/[^/]+/[^/]+/contents/.*$"),
    re.compile(r"^repos/[^/]+/[^/]+/git/refs.*$"),
    re.compile(r"^repos/[^/]+/[^/]+/compare/.*$"),
    # User info
    re.compile(r"^user$"),
    re.compile(r"^users/[^/]+$"),
]


def validate_gh_api_path(path: str, method: str = "GET") -> tuple[bool, str]:
    """Validate gh api path against allowlist."""
    if method.upper() not in ("GET", "POST", "PATCH"):
        return False, f"HTTP method '{method}' not allowed for gh api"

    path = path.lstrip("/")
    for pattern in GH_API_ALLOWED_PATHS:
        if pattern.match(path):
            return True, ""

    return False, f"API path '{path}' not in allowlist"


# gh api flags that take a value argument
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
    """Parse gh api command arguments to extract API path and HTTP method."""
    method = "GET"
    api_path = None
    i = 0

    while i < len(args):
        arg = args[i]

        if arg in ("-X", "--method"):
            if i + 1 < len(args):
                method = args[i + 1].upper()
                i += 2
                continue
            else:
                i += 1
                continue

        if arg in GH_API_FLAGS_WITH_VALUES:
            i += 2
            continue

        if arg in GH_API_FLAGS_NO_VALUE:
            i += 1
            continue

        if "=" in arg and arg.startswith("-"):
            if arg.startswith(("-X=", "--method=")):
                method = arg.split("=", 1)[1].upper()
            i += 1
            continue

        if arg.startswith("-"):
            i += 1
            continue

        api_path = arg
        break

    return api_path, method


def extract_repo_from_gh_api_path(api_path: str) -> str | None:
    """Extract owner/repo from a gh api path."""
    path = api_path.lstrip("/")

    if not path.startswith("repos/"):
        return None

    parts = path.split("/")
    if len(parts) >= 3:
        owner, repo = parts[1], parts[2]
        if owner and repo and not owner.startswith("-") and not repo.startswith("-"):
            return f"{owner}/{repo}"

    return None


def extract_repo_from_gh_command(args: list[str]) -> str | None:
    """Extract target repository from any gh command."""
    if not args:
        return None

    for i, arg in enumerate(args):
        if arg in ("--repo", "-R") and i + 1 < len(args):
            return args[i + 1]

    if args[0] == "repo" and len(args) >= 3:
        subcommand = args[1]
        repo_arg = args[2]

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
        }

        if (
            subcommand in positional_repo_subcommands
            and "/" in repo_arg
            and not repo_arg.startswith("-")
        ):
            return repo_arg

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
        """Initialize the GitHub client."""
        self.mode = mode
        self._cached_token: GitHubToken | None = None
        self._cached_user_token: str | None = None

    def get_token(self) -> GitHubToken | None:
        """Get the current GitHub token from the token refresher."""
        if self._cached_token and not self._cached_token.is_expired:
            return self._cached_token

        try:
            from .token_refresher import get_token_refresher

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
        """Get the user mode token from environment."""
        if self._cached_user_token:
            return self._cached_user_token

        token = os.environ.get(USER_TOKEN_VAR, "").strip()
        if token:
            self._cached_user_token = token
            return token

        logger.warning("User token not configured", env_var=USER_TOKEN_VAR)
        return None

    def get_token_for_mode(self, mode: str | None = None) -> str | None:
        """Get the appropriate token string for the specified mode."""
        mode = mode or self.mode
        if mode == "user":
            return self.get_user_token()
        else:
            token = self.get_token()
            return token.token if token else None

    def is_user_token_valid(self) -> bool:
        """Check if a user token is configured and non-empty.

        Returns:
            True if user token is available.
        """
        token = self.get_user_token()
        return token is not None and len(token) > 0

    def get_authenticated_user(self, mode: str = "bot") -> str | None:
        """Get the GitHub username for the authenticated token.

        Makes an API call to determine the username associated with
        the current authentication token.

        Args:
            mode: "bot" or "user" - which token to use

        Returns:
            GitHub username string, or None if unable to determine.
        """
        result = self.execute(["api", "user", "--jq", ".login"], mode=mode)
        if result.success and result.stdout.strip():
            return result.stdout.strip()

        logger.warning(
            "Failed to get authenticated user",
            mode=mode,
            stderr=result.stderr[:200] if result.stderr else None,
        )
        return None

    def validate_user_mode_config(self) -> tuple[bool, str]:
        """Validate that user mode is properly configured.

        Checks that:
        1. User token is available
        2. User token can authenticate to GitHub
        3. (Optionally) Token username matches configured github_user

        Returns:
            Tuple of (is_valid, error_message).
        """
        if not self.is_user_token_valid():
            return False, f"User token not configured. Set {USER_TOKEN_VAR} environment variable."

        # Verify token works by getting authenticated user
        username = self.get_authenticated_user(mode="user")
        if not username:
            return False, "User token is invalid or expired. Unable to authenticate to GitHub."

        # Optionally check if username matches configured user
        try:
            from .repo_config import get_user_mode_config

            config = get_user_mode_config()
            expected_user = config.get("github_user", "")
            if expected_user and username.lower() != expected_user.lower():
                return False, (
                    f"Token username '{username}' does not match configured "
                    f"github_user '{expected_user}'"
                )
        except ImportError:
            pass  # repo_config not available, skip validation

        return True, ""

    def execute(
        self,
        args: list[str],
        timeout: int = 60,
        cwd: str | Path | None = None,
        mode: str | None = None,
    ) -> GitHubResult:
        """Execute a gh CLI command with authentication."""
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

        env = {
            "GH_TOKEN": token_str,
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
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
                stderr_lower = (result.stderr or "").lower()
                if "rate limit" in stderr_lower or "api rate limit exceeded" in stderr_lower:
                    logger.error(
                        "GitHub rate limit exceeded",
                        command_args=args,
                        returncode=result.returncode,
                    )
                else:
                    logger.warning(
                        "gh command failed",
                        command_args=args,
                        returncode=result.returncode,
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
        """Get information about a PR."""
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
            data: dict[str, Any] = json.loads(result.stdout)
            return data
        except json.JSONDecodeError:
            logger.error("Failed to parse PR info", stdout=result.stdout[:500])
            return None

    def list_prs_for_branch(
        self, repo: str, branch: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        """List PRs for a specific head branch."""
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
            data: list[dict[str, Any]] = json.loads(result.stdout)
            return data
        except json.JSONDecodeError:
            return []

    def branch_exists(self, repo: str, branch: str, mode: str = "bot") -> bool | None:
        """Check if a branch exists in the remote repository."""
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

        stderr = result.stderr or ""
        if "404" in stderr or "Not Found" in stderr:
            return False

        logger.warning(
            "Could not determine branch existence",
            repo=repo,
            branch=branch,
            mode=mode,
        )
        return None


# Global client instances (one per mode)
_clients: dict[str, GitHubClient] = {}


def get_github_client(mode: str = "bot") -> GitHubClient:
    """Get a GitHub client instance for the specified mode."""
    if mode not in _clients:
        _clients[mode] = GitHubClient(mode=mode)
    return _clients[mode]
