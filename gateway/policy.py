"""
Policy Engine - Ownership and access control checks.

Enforces policies for git/gh operations:
- Branch ownership (bot/user): egg can push to bot-prefixed branches (egg-*) OR branches with PRs by egg/configured-user/trusted-user
- PR creation: allowed in bot and user mode (user mode forces draft), blocked in reviewer mode
- PR comments: egg can comment on any PR
- PR edit/close: egg can only modify PRs it created or PRs by configured user
- Merge blocked: No merge operations allowed (human must merge)

Configuration:
- GATEWAY_TRUSTED_USERS: Comma-separated list of GitHub usernames whose branches
  egg is allowed to push to (e.g., "your-username,octocat")
- Configured user: The user mode user from repositories.yaml, treated as an owner in both modes
"""

import os
import re
import sys
from collections import OrderedDict
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

# Import github_client - try relative import first (module mode),
# fall back to absolute import (standalone script mode in container)
try:
    from .github_client import GitHubClient, get_github_client
except ImportError:
    from github_client import (  # type: ignore[no-redef, import-untyped]
        GitHubClient,
        get_github_client,
    )


logger = get_logger("gateway.policy")

# Cache size limits
MAX_PR_CACHE_SIZE = 500
MAX_BRANCH_PR_CACHE_SIZE = 200


# Bot identity configuration
# Loaded from GATEWAY_BOT_NAME environment variable (REQUIRED)
# This should match your GitHub App name for PR ownership checks
# Example: GATEWAY_BOT_NAME="my-bot" (set to your bot's GitHub username)

# Cached values (loaded lazily on first access)
_bot_identities_cache: frozenset[str] | None = None
_bot_branch_prefixes_cache: tuple[str, ...] | None = None
_reviewer_identities_cache: frozenset[str] | None = None


def _get_bot_name() -> str:
    """Get the configured bot name for use in messages.

    Note: This function assumes GATEWAY_BOT_NAME is set, as get_bot_identities()
    will raise ValueError before any policy checks run if it's not configured.
    """
    return os.environ.get("GATEWAY_BOT_NAME", "").strip().lower()


def _get_branch_prefix() -> str:
    """Get the configured branch prefix for use in messages.

    Note: This function assumes GATEWAY_BOT_BRANCH_PREFIX is set, as
    get_bot_branch_prefixes() will raise ValueError before any policy checks
    run if it's not configured.
    """
    return os.environ.get("GATEWAY_BOT_BRANCH_PREFIX", "").strip().lower()


def _reset_bot_config_caches() -> None:
    """Reset bot configuration caches. For testing only.

    This allows tests to verify behavior with different configurations
    without restarting the process.
    """
    global _bot_identities_cache, _bot_branch_prefixes_cache, _reviewer_identities_cache
    _bot_identities_cache = None
    _bot_branch_prefixes_cache = None
    _reviewer_identities_cache = None


def get_bot_identities() -> frozenset[str]:
    """Get bot identities, loading from environment on first access.

    The bot name is used to generate identity variants that GitHub may use:
    - "name" (plain username)
    - "name[bot]" (GitHub App bot suffix)
    - "app/name" (GitHub App author format in API)
    - "apps/name" (alternate app format)

    Raises:
        ValueError: If GATEWAY_BOT_NAME is not configured.
    """
    global _bot_identities_cache
    if _bot_identities_cache is not None:
        return _bot_identities_cache

    bot_name = os.environ.get("GATEWAY_BOT_NAME", "").strip().lower()
    if not bot_name:
        raise ValueError(
            "GATEWAY_BOT_NAME environment variable is required. "
            "Set it to your GitHub App name (e.g., GATEWAY_BOT_NAME=my-bot). "
            "Run 'egg --setup' to configure."
        )
    _bot_identities_cache = frozenset(
        {
            bot_name,
            f"{bot_name}[bot]",
            f"app/{bot_name}",
            f"apps/{bot_name}",
        }
    )
    return _bot_identities_cache


def get_bot_branch_prefixes() -> tuple[str, ...]:
    """Get bot branch prefixes, loading from environment on first access.

    Raises:
        ValueError: If GATEWAY_BOT_BRANCH_PREFIX is not configured.
    """
    global _bot_branch_prefixes_cache
    if _bot_branch_prefixes_cache is not None:
        return _bot_branch_prefixes_cache

    prefix = os.environ.get("GATEWAY_BOT_BRANCH_PREFIX", "").strip().lower()
    if not prefix:
        raise ValueError(
            "GATEWAY_BOT_BRANCH_PREFIX environment variable is required. "
            "Set it to your branch prefix (e.g., GATEWAY_BOT_BRANCH_PREFIX=my-bot). "
            "Run 'egg --setup' to configure."
        )
    _bot_branch_prefixes_cache = (f"{prefix}-", f"{prefix}/")
    return _bot_branch_prefixes_cache


def get_reviewer_identities() -> frozenset[str]:
    """Get reviewer bot identities, loading from environment on first access.

    The reviewer bot is a separate GitHub App used for posting code reviews.
    This allows reviews to use the full GitHub Reviews API (approve/request-changes)
    since the reviewer is not the same account as the PR author.

    The bot name is used to generate identity variants that GitHub may use:
    - "name" (plain username)
    - "name[bot]" (GitHub App bot suffix)
    - "app/name" (GitHub App author format in API)
    - "apps/name" (alternate app format)

    Returns:
        Empty frozenset if GATEWAY_REVIEWER_BOT_NAME is not configured (reviewer disabled).
    """
    global _reviewer_identities_cache
    if _reviewer_identities_cache is not None:
        return _reviewer_identities_cache

    reviewer_name = os.environ.get("GATEWAY_REVIEWER_BOT_NAME", "").strip().lower()
    if not reviewer_name:
        # Reviewer is optional - return empty set if not configured
        _reviewer_identities_cache = frozenset()
        return _reviewer_identities_cache

    _reviewer_identities_cache = frozenset(
        {
            reviewer_name,
            f"{reviewer_name}[bot]",
            f"app/{reviewer_name}",
            f"apps/{reviewer_name}",
        }
    )
    return _reviewer_identities_cache


def is_reviewer_configured() -> bool:
    """Check if a separate reviewer bot is configured.

    Returns:
        True if GATEWAY_REVIEWER_BOT_NAME is set, False otherwise.
    """
    return bool(os.environ.get("GATEWAY_REVIEWER_BOT_NAME", "").strip())


# Trusted GitHub users whose branches egg can push to
# Loaded from GATEWAY_TRUSTED_USERS environment variable (comma-separated)
# Example: GATEWAY_TRUSTED_USERS="your-username,octocat"
def _load_trusted_users() -> frozenset[str]:
    """Load trusted users from environment variable."""
    env_value = os.environ.get("GATEWAY_TRUSTED_USERS", "")
    if not env_value.strip():
        return frozenset()
    users = [u.strip().lower() for u in env_value.split(",") if u.strip()]
    return frozenset(users)


TRUSTED_BRANCH_OWNERS: frozenset[str] = _load_trusted_users()


@dataclass
class PolicyResult:
    """Result of a policy check."""

    allowed: bool
    reason: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {"allowed": self.allowed, "reason": self.reason}
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class CachedPRInfo:
    """Cached PR information with TTL."""

    pr_number: int
    author: str
    state: str
    head_branch: str
    fetched_at: float

    @property
    def is_stale(self) -> bool:
        """Check if cache entry is stale (> 5 minutes old)."""
        return (datetime.now(UTC).timestamp() - self.fetched_at) > 300


class BoundedCache(OrderedDict[Any, Any]):
    """An OrderedDict with a maximum size that evicts oldest entries."""

    def __init__(self, max_size: int):
        super().__init__()
        self.max_size = max_size

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            # Move to end if updating existing key
            self.move_to_end(key)
        super().__setitem__(key, value)
        # Evict oldest entries if over max size
        while len(self) > self.max_size:
            self.popitem(last=False)


class PolicyEngine:
    """
    Policy enforcement engine for git/gh operations.

    Caches PR info to reduce GitHub API calls.
    Uses bounded caches to prevent unbounded memory growth.
    """

    def __init__(self, github_client: GitHubClient | None = None):
        self.github = github_client or get_github_client()
        # Cache: (repo, pr_number) -> CachedPRInfo (bounded)
        self._pr_cache: BoundedCache = BoundedCache(MAX_PR_CACHE_SIZE)
        # Cache: (repo, branch) -> (list of PR numbers, timestamp) (bounded)
        self._branch_pr_cache: BoundedCache = BoundedCache(MAX_BRANCH_PR_CACHE_SIZE)

    def _is_bot_author(self, author: str | dict[str, Any]) -> bool:
        """Check if author is a bot identity."""
        if isinstance(author, dict):
            # GitHub API returns author as {"login": "username"}
            login = str(author.get("login", ""))
        else:
            login = author

        return login.lower() in get_bot_identities()

    def _is_reviewer_author(self, author: str | dict[str, Any]) -> bool:
        """Check if author is a reviewer bot identity.

        The reviewer bot is a separate GitHub App used for posting code reviews.
        """
        reviewer_identities = get_reviewer_identities()
        if not reviewer_identities:
            return False

        if isinstance(author, dict):
            login = str(author.get("login", ""))
        else:
            login = author

        return login.lower() in reviewer_identities

    def _is_bot_branch(self, branch: str) -> bool:
        """Check if branch name indicates bot ownership."""
        return branch.startswith(get_bot_branch_prefixes())

    def _is_trusted_author(self, author: str | dict[str, Any]) -> bool:
        """Check if author is a trusted user (whose branches egg can push to)."""
        if not TRUSTED_BRANCH_OWNERS:
            return False
        if isinstance(author, dict):
            login = str(author.get("login", ""))
        else:
            login = author
        return login.lower() in TRUSTED_BRANCH_OWNERS

    def _get_configured_user(self) -> str | None:
        """Get the configured user mode GitHub username."""
        try:
            # Import here to avoid circular imports and handle missing config
            _config_path = Path(__file__).parent.parent / "config"
            if _config_path.exists() and str(_config_path) not in sys.path:
                sys.path.insert(0, str(_config_path))
            from repo_config import get_user_mode_config

            config = get_user_mode_config()
            return config.get("github_user", "").lower() or None
        except (ImportError, FileNotFoundError):
            return None

    def _is_configured_user_author(
        self, author: str | dict[str, Any], configured_user: str
    ) -> bool:
        """Check if author matches the configured user."""
        if isinstance(author, dict):
            login = str(author.get("login", ""))
        else:
            login = author
        return login.lower() == configured_user.lower()

    def _get_pr_info(self, repo: str, pr_number: int, mode: str = "bot") -> CachedPRInfo | None:
        """Get PR info, using cache if available and fresh."""
        cache_key = (repo, pr_number, mode)

        # Check cache
        cached: CachedPRInfo | None = self._pr_cache.get(cache_key)
        if cached and not cached.is_stale:
            return cached

        # Fetch from GitHub
        pr_data = self.github.get_pr_info(repo, pr_number, mode=mode)
        if not pr_data:
            return None

        # Cache the result
        author = pr_data.get("author", {})
        cached_info = CachedPRInfo(
            pr_number=pr_number,
            author=author.get("login", "") if isinstance(author, dict) else str(author),
            state=pr_data.get("state", ""),
            head_branch=pr_data.get("headRefName", ""),
            fetched_at=datetime.now(UTC).timestamp(),
        )
        self._pr_cache[cache_key] = cached_info
        return cached_info

    def _get_prs_for_branch(self, repo: str, branch: str, mode: str = "bot") -> list[int]:
        """Get open PR numbers for a branch, using cache if available."""
        cache_key = (repo, branch, mode)

        # Check cache (2 minute TTL for branch->PR mapping)
        cached = self._branch_pr_cache.get(cache_key)
        if cached:
            pr_numbers, fetched_at = cached
            if (datetime.now(UTC).timestamp() - fetched_at) < 120:
                return list(pr_numbers)

        # Fetch from GitHub
        prs = self.github.list_prs_for_branch(repo, branch, state="open", mode=mode)
        pr_numbers = [pr.get("number") for pr in prs if pr.get("number")]
        self._branch_pr_cache[cache_key] = (pr_numbers, datetime.now(UTC).timestamp())

        # Also cache individual PR info
        for pr in prs:
            pr_number = pr.get("number")
            if pr_number:
                author = pr.get("author", {})
                self._pr_cache[(repo, pr_number, mode)] = CachedPRInfo(
                    pr_number=pr_number,
                    author=author.get("login", "") if isinstance(author, dict) else str(author),
                    state=pr.get("state", ""),
                    head_branch=pr.get("headRefName", ""),
                    fetched_at=datetime.now(UTC).timestamp(),
                )

        return pr_numbers

    def check_pr_ownership(self, repo: str, pr_number: int, auth_mode: str = "bot") -> PolicyResult:
        """
        Check if the current identity owns a PR.

        In both modes, a PR is owned if the author is:
        - A bot identity, OR
        - The configured user (user mode user)

        Args:
            repo: Repository in "owner/repo" format
            pr_number: PR number
            auth_mode: "bot" (default) or "user"
        """
        pr_info = self._get_pr_info(repo, pr_number, mode=auth_mode)

        if not pr_info:
            logger.warning(
                "PR not found or inaccessible",
                repo=repo,
                pr_number=pr_number,
            )
            return PolicyResult(
                allowed=False,
                reason=f"PR #{pr_number} not found or inaccessible",
                details={"repo": repo, "pr_number": pr_number},
            )

        # Check if PR is owned by egg
        if self._is_bot_author(pr_info.author):
            logger.debug(
                "PR ownership verified (bot author)",
                repo=repo,
                pr_number=pr_number,
                author=pr_info.author,
            )
            return PolicyResult(
                allowed=True,
                reason=f"PR is owned by {_get_bot_name()}",
                details={"author": pr_info.author, "auth_mode": auth_mode},
            )

        # Check if PR is owned by the configured user (user mode user)
        configured_user = self._get_configured_user()
        if configured_user and self._is_configured_user_author(pr_info.author, configured_user):
            logger.debug(
                "PR ownership verified (configured user)",
                repo=repo,
                pr_number=pr_number,
                author=pr_info.author,
                configured_user=configured_user,
            )
            return PolicyResult(
                allowed=True,
                reason=f"PR is owned by configured user ({configured_user})",
                details={
                    "author": pr_info.author,
                    "auth_mode": auth_mode,
                    "configured_user": configured_user,
                },
            )

        logger.info(
            f"PR ownership denied - not owned by {_get_bot_name()} or configured user",
            repo=repo,
            pr_number=pr_number,
            author=pr_info.author,
            auth_mode=auth_mode,
        )
        expected = list(get_bot_identities())
        if configured_user:
            expected.append(configured_user)
        return PolicyResult(
            allowed=False,
            reason=f"PR #{pr_number} is not owned by {_get_bot_name()} or configured user (author: {pr_info.author})",
            details={"author": pr_info.author, "expected": expected, "auth_mode": auth_mode},
        )

    def check_pr_comment_allowed(
        self, repo: str, pr_number: int, auth_mode: str = "bot"
    ) -> PolicyResult:
        """
        Check if egg can comment on a PR.

        Egg can comment on ANY PR - this enables collaboration on PRs owned by others.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: PR number
            auth_mode: "bot" (default) or "user"
        """
        pr_info = self._get_pr_info(repo, pr_number, mode=auth_mode)

        if not pr_info:
            logger.warning(
                "PR not found or inaccessible for comment",
                repo=repo,
                pr_number=pr_number,
            )
            return PolicyResult(
                allowed=False,
                reason=f"PR #{pr_number} not found or inaccessible",
                details={"repo": repo, "pr_number": pr_number},
            )

        logger.debug(
            "PR comment allowed",
            repo=repo,
            pr_number=pr_number,
            author=pr_info.author,
        )
        return PolicyResult(
            allowed=True,
            reason="Comments are allowed on any PR",
            details={"pr_number": pr_number, "author": pr_info.author},
        )

    def check_branch_ownership(
        self, repo: str, branch: str, auth_mode: str = "bot"
    ) -> PolicyResult:
        """
        Check if the current identity can push to a branch.

        In bot or user mode, egg can push to a branch if:
        1. Branch name starts with egg- or egg/ (allows pushing before PR exists), OR
        2. Branch has an open PR authored by egg, OR
        3. Branch has an open PR authored by the configured user, OR
        4. Branch has an open PR authored by a trusted user (from GATEWAY_TRUSTED_USERS)

        In reviewer mode: ALWAYS blocked - reviewer can only post reviews, not push.

        Protected branches (main, master) are always blocked regardless of mode.

        Args:
            repo: Repository in "owner/repo" format
            branch: Branch name
            auth_mode: "bot" (default), "user", or "reviewer"
        """
        # SAFETY: Reviewer mode is NEVER allowed to push
        if auth_mode == "reviewer":
            logger.info(
                "Push blocked in reviewer mode",
                repo=repo,
                branch=branch,
            )
            return PolicyResult(
                allowed=False,
                reason="Push operations are not allowed in reviewer mode. "
                "The reviewer account can only post reviews.",
                details={
                    "repo": repo,
                    "branch": branch,
                    "auth_mode": "reviewer",
                    "hint": "Use the main bot account to push code.",
                },
            )

        # SAFETY: Always block pushes to protected branches
        protected_branches = ("main", "master")
        if branch in protected_branches:
            logger.warning(
                "Push to protected branch blocked",
                repo=repo,
                branch=branch,
                auth_mode=auth_mode,
            )
            return PolicyResult(
                allowed=False,
                reason=f"Branch '{branch}' is protected. Direct pushes to {', '.join(protected_branches)} are not allowed.",
                details={
                    "branch": branch,
                    "protected_branches": list(protected_branches),
                    "auth_mode": auth_mode,
                    "hint": "Create a feature branch and open a PR instead.",
                },
            )

        # Bot/User mode: Check 1: Branch prefix
        if self._is_bot_branch(branch):
            logger.debug(
                "Branch ownership verified by prefix",
                repo=repo,
                branch=branch,
            )
            return PolicyResult(
                allowed=True,
                reason=f"Branch '{branch}' is owned by {_get_bot_name()} (bot-prefixed branch)",
                details={"branch": branch, "reason": "bot_prefix"},
            )

        # Check 2-4: Open PR by egg, configured user, or trusted user
        pr_numbers = self._get_prs_for_branch(repo, branch, mode=auth_mode)
        configured_user = self._get_configured_user()

        for pr_number in pr_numbers:
            pr_info = self._get_pr_info(repo, pr_number, mode=auth_mode)
            if not pr_info:
                continue

            # Check if PR is owned by egg
            if self._is_bot_author(pr_info.author):
                logger.debug(
                    "Branch ownership verified by PR (bot author)",
                    repo=repo,
                    branch=branch,
                    pr_number=pr_number,
                    author=pr_info.author,
                )
                return PolicyResult(
                    allowed=True,
                    reason=f"Branch '{branch}' has open PR #{pr_number} owned by {_get_bot_name()}",
                    details={
                        "branch": branch,
                        "pr_number": pr_number,
                        "author": pr_info.author,
                        "reason": "bot_pr",
                    },
                )

            # Check if PR is owned by the configured user
            if configured_user and self._is_configured_user_author(pr_info.author, configured_user):
                logger.debug(
                    "Branch push allowed - PR owned by configured user",
                    repo=repo,
                    branch=branch,
                    pr_number=pr_number,
                    author=pr_info.author,
                    configured_user=configured_user,
                )
                return PolicyResult(
                    allowed=True,
                    reason=f"Branch '{branch}' has open PR #{pr_number} owned by configured user '{pr_info.author}'",
                    details={
                        "branch": branch,
                        "pr_number": pr_number,
                        "author": pr_info.author,
                        "configured_user": configured_user,
                        "reason": "configured_user_pr",
                    },
                )

            # Check if PR is owned by a trusted user
            if self._is_trusted_author(pr_info.author):
                logger.debug(
                    "Branch push allowed - PR owned by trusted user",
                    repo=repo,
                    branch=branch,
                    pr_number=pr_number,
                    author=pr_info.author,
                )
                return PolicyResult(
                    allowed=True,
                    reason=f"Branch '{branch}' has open PR #{pr_number} owned by trusted user '{pr_info.author}'",
                    details={
                        "branch": branch,
                        "pr_number": pr_number,
                        "author": pr_info.author,
                        "reason": "trusted_user_pr",
                    },
                )

        # Not allowed
        logger.info(
            f"Branch push denied - not owned by {_get_bot_name()}, configured user, or trusted user",
            repo=repo,
            branch=branch,
            open_prs=pr_numbers,
            configured_user=configured_user,
            trusted_users=list(TRUSTED_BRANCH_OWNERS) if TRUSTED_BRANCH_OWNERS else [],
            auth_mode=auth_mode,
        )
        prefix = _get_branch_prefix()
        hint = f"Use '{prefix}-' or '{prefix}/' branch prefix for new work"
        if configured_user:
            hint += f". Configured user: {configured_user}"
        if TRUSTED_BRANCH_OWNERS:
            hint += f". Trusted users: {', '.join(sorted(TRUSTED_BRANCH_OWNERS))}"
        return PolicyResult(
            allowed=False,
            reason=f"Branch '{branch}' is not owned by {_get_bot_name()} or an authorized user. "
            f"Use a bot-prefixed branch ({_get_branch_prefix()}-* or {_get_branch_prefix()}/*).",
            details={
                "branch": branch,
                "open_prs": pr_numbers,
                "configured_user": configured_user,
                "hint": hint,
                "auth_mode": auth_mode,
            },
        )

    def check_pr_create_allowed(self, repo: str, auth_mode: str = "bot") -> PolicyResult:
        """
        Check if PR creation is allowed.

        In bot or user mode: Always allowed - egg can create PRs.
        In user mode: PRs are forced to draft mode.
        In reviewer mode: Blocked - reviewer can only post reviews, not create PRs.

        Args:
            repo: Repository in "owner/repo" format
            auth_mode: "bot" (default), "user", or "reviewer"
        """
        if auth_mode == "reviewer":
            logger.info(
                "PR creation blocked in reviewer mode",
                repo=repo,
            )
            return PolicyResult(
                allowed=False,
                reason="PR creation is not allowed in reviewer mode. "
                "The reviewer account can only post reviews.",
                details={
                    "repo": repo,
                    "auth_mode": "reviewer",
                    "hint": "Use the main bot account to create PRs.",
                },
            )

        # Bot and user mode: allowed
        force_draft = auth_mode == "user"
        logger.debug(
            "PR creation allowed",
            repo=repo,
            auth_mode=auth_mode,
            force_draft=force_draft,
        )
        return PolicyResult(
            allowed=True,
            reason=f"PR creation allowed in {auth_mode} mode",
            details={
                "repo": repo,
                "auth_mode": auth_mode,
                "force_draft": force_draft,
            },
        )

    def check_merge_allowed(self, repo: str, pr_number: int) -> PolicyResult:
        """
        Check if merge is allowed.

        ALWAYS returns False - merging is not supported. Human must merge via GitHub UI.
        """
        logger.info(
            "Merge operation blocked by policy",
            repo=repo,
            pr_number=pr_number,
        )
        return PolicyResult(
            allowed=False,
            reason="Merge operations are not supported. Human must merge via GitHub UI.",
            details={
                "repo": repo,
                "pr_number": pr_number,
                "action": f"Use GitHub web UI or 'gh pr merge' from a non-{_get_bot_name()} environment",
            },
        )

    def check_comment_ownership(
        self,
        repo: str,
        comment_id: int,
        comment_type: str,
        auth_mode: str = "bot",
    ) -> PolicyResult:
        """
        Check if the current identity owns a comment (for edit/PATCH operations).

        A comment is considered owned if the author is:
        - A bot identity, OR
        - The configured user (user mode user)

        Args:
            repo: Repository in "owner/repo" format
            comment_id: The comment ID
            comment_type: One of "issues", "pulls", "commits"
            auth_mode: "bot" (default) or "user"
        """
        author = self.github.get_comment_author(repo, comment_id, comment_type, mode=auth_mode)

        if not author:
            logger.warning(
                "Comment not found or inaccessible",
                repo=repo,
                comment_id=comment_id,
                comment_type=comment_type,
            )
            return PolicyResult(
                allowed=False,
                reason=f"Comment {comment_id} not found or inaccessible",
                details={
                    "repo": repo,
                    "comment_id": comment_id,
                    "comment_type": comment_type,
                },
            )

        # Check if comment is owned by bot
        if self._is_bot_author(author):
            logger.debug(
                "Comment ownership verified (bot author)",
                repo=repo,
                comment_id=comment_id,
                author=author,
            )
            return PolicyResult(
                allowed=True,
                reason=f"Comment is owned by {_get_bot_name()}",
                details={
                    "author": author,
                    "comment_id": comment_id,
                    "auth_mode": auth_mode,
                },
            )

        # Check if comment is owned by the configured user
        configured_user = self._get_configured_user()
        if configured_user and self._is_configured_user_author(author, configured_user):
            logger.debug(
                "Comment ownership verified (configured user)",
                repo=repo,
                comment_id=comment_id,
                author=author,
                configured_user=configured_user,
            )
            return PolicyResult(
                allowed=True,
                reason=f"Comment is owned by configured user ({configured_user})",
                details={
                    "author": author,
                    "comment_id": comment_id,
                    "auth_mode": auth_mode,
                    "configured_user": configured_user,
                },
            )

        # Not owned — deny
        logger.info(
            f"Comment edit denied - not owned by {_get_bot_name()} or configured user",
            repo=repo,
            comment_id=comment_id,
            author=author,
            auth_mode=auth_mode,
        )
        expected = list(get_bot_identities())
        if configured_user:
            expected.append(configured_user)
        return PolicyResult(
            allowed=False,
            reason=f"Comment {comment_id} is not owned by {_get_bot_name()} or configured user "
            f"(author: {author}). Only comments authored by the bot or configured user can be edited.",
            details={
                "author": author,
                "comment_id": comment_id,
                "comment_type": comment_type,
                "expected": expected,
                "auth_mode": auth_mode,
            },
        )

    def check_pr_review_allowed(
        self, repo: str, pr_number: int, auth_mode: str = "bot"
    ) -> PolicyResult:
        """
        Check if posting a PR review is allowed.

        In bot mode: Allowed on any PR (same account can comment, but not approve/request-changes own PRs).
        In user mode: Allowed on any PR.
        In reviewer mode: Allowed on any PR - this is the primary purpose of the reviewer account.

        Args:
            repo: Repository in "owner/repo" format
            pr_number: PR number
            auth_mode: "bot" (default), "user", or "reviewer"
        """
        pr_info = self._get_pr_info(repo, pr_number, mode=auth_mode)

        if not pr_info:
            logger.warning(
                "PR not found or inaccessible for review",
                repo=repo,
                pr_number=pr_number,
            )
            return PolicyResult(
                allowed=False,
                reason=f"PR #{pr_number} not found or inaccessible",
                details={"repo": repo, "pr_number": pr_number},
            )

        logger.debug(
            "PR review allowed",
            repo=repo,
            pr_number=pr_number,
            author=pr_info.author,
            auth_mode=auth_mode,
        )
        return PolicyResult(
            allowed=True,
            reason=f"Reviews are allowed on PR #{pr_number}",
            details={"pr_number": pr_number, "author": pr_info.author, "auth_mode": auth_mode},
        )


# Global policy engine instance
_engine: PolicyEngine | None = None


def get_policy_engine() -> PolicyEngine:
    """Get the global policy engine instance."""
    global _engine
    if _engine is None:
        _engine = PolicyEngine()
    return _engine


def extract_repo_from_remote(remote_url: str) -> str | None:
    """
    Extract owner/repo from a git remote URL.

    Supports:
    - https://github.com/owner/repo.git
    - https://github.com/owner/repo
    - git@github.com:owner/repo.git
    """
    patterns = [
        r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$",
    ]

    for pattern in patterns:
        match = re.search(pattern, remote_url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"

    return None


def extract_branch_from_refspec(refspec: str) -> str | None:
    """
    Extract branch name from a git refspec.

    Supports:
    - branch
    - refs/heads/branch
    - local:remote (returns remote)
    - +refs/heads/local:refs/heads/remote (returns remote)
    """
    # Handle empty refspec
    if not refspec:
        return None

    # Handle local:remote format
    if ":" in refspec:
        remote_ref = refspec.split(":")[-1]
    else:
        remote_ref = refspec

    # Strip refs/heads/ prefix
    if remote_ref.startswith("refs/heads/"):
        return remote_ref[len("refs/heads/") :]

    # Strip leading + (force push indicator)
    if remote_ref.startswith("+"):
        remote_ref = remote_ref[1:]

    return remote_ref
