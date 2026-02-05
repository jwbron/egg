"""
Policy Engine - Ownership and access control checks.

Enforces policies for git/gh operations:
- Branch ownership (bot): egg can push to bot-prefixed branches (egg-*) OR branches with PRs by egg/configured-user/trusted-user
- Branch ownership (user mode): user can push to new branches OR branches with PRs by egg/configured-user
- PR creation: allowed in bot mode, blocked in user mode (user creates PRs manually)
- PR comments: egg can comment on any PR
- PR edit/close: egg can only modify PRs it created or PRs by configured user
- Merge blocked: No merge operations allowed (human must merge)

Configuration:
- GATEWAY_TRUSTED_USERS: Comma-separated list of GitHub usernames whose branches
  egg is allowed to push to (e.g., "jwbron,octocat")
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
    from github_client import GitHubClient, get_github_client


logger = get_logger("gateway.policy")

# Cache size limits
MAX_PR_CACHE_SIZE = 500
MAX_BRANCH_PR_CACHE_SIZE = 200


# Bot identity configuration
# Loaded from GATEWAY_BOT_NAME environment variable (REQUIRED)
# This should match your GitHub App name for PR ownership checks
# Example: GATEWAY_BOT_NAME="james-in-a-box"

# Cached values (loaded lazily on first access)
_bot_identities_cache: frozenset[str] | None = None
_bot_branch_prefixes_cache: tuple[str, ...] | None = None


def _get_bot_identities() -> frozenset[str]:
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
            "Set it to your GitHub App name (e.g., GATEWAY_BOT_NAME=james-in-a-box). "
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


def _get_bot_branch_prefixes() -> tuple[str, ...]:
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
            "Set it to your branch prefix (e.g., GATEWAY_BOT_BRANCH_PREFIX=egg). "
            "Run 'egg --setup' to configure."
        )
    _bot_branch_prefixes_cache = (f"{prefix}-", f"{prefix}/")
    return _bot_branch_prefixes_cache


# Module-level accessors (lazy loaded)
def get_bot_identities() -> frozenset[str]:
    """Get configured bot identities."""
    return _get_bot_identities()


def get_bot_branch_prefixes() -> tuple[str, ...]:
    """Get configured bot branch prefixes."""
    return _get_bot_branch_prefixes()


# Trusted GitHub users whose branches egg can push to
# Loaded from GATEWAY_TRUSTED_USERS environment variable (comma-separated)
# Example: GATEWAY_TRUSTED_USERS="jwbron,octocat"
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


class BoundedCache(OrderedDict):
    """An OrderedDict with a maximum size that evicts oldest entries."""

    def __init__(self, max_size: int):
        super().__init__()
        self.max_size = max_size

    def __setitem__(self, key, value):
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
            login = author.get("login", "")
        else:
            login = author

        return login.lower() in get_bot_identities()

    def _is_bot_branch(self, branch: str) -> bool:
        """Check if branch name indicates bot ownership."""
        return branch.startswith(get_bot_branch_prefixes())

    def _is_trusted_author(self, author: str | dict[str, Any]) -> bool:
        """Check if author is a trusted user (whose branches egg can push to)."""
        if not TRUSTED_BRANCH_OWNERS:
            return False
        if isinstance(author, dict):
            login = author.get("login", "")
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
            login = author.get("login", "")
        else:
            login = author
        return login.lower() == configured_user.lower()

    def _get_pr_info(self, repo: str, pr_number: int) -> CachedPRInfo | None:
        """Get PR info, using cache if available and fresh."""
        cache_key = (repo, pr_number)

        # Check cache
        cached = self._pr_cache.get(cache_key)
        if cached and not cached.is_stale:
            return cached

        # Fetch from GitHub
        pr_data = self.github.get_pr_info(repo, pr_number)
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

    def _get_prs_for_branch(self, repo: str, branch: str) -> list[int]:
        """Get open PR numbers for a branch, using cache if available."""
        cache_key = (repo, branch)

        # Check cache (2 minute TTL for branch->PR mapping)
        cached = self._branch_pr_cache.get(cache_key)
        if cached:
            pr_numbers, fetched_at = cached
            if (datetime.now(UTC).timestamp() - fetched_at) < 120:
                return pr_numbers

        # Fetch from GitHub
        prs = self.github.list_prs_for_branch(repo, branch, state="open")
        pr_numbers = [pr.get("number") for pr in prs if pr.get("number")]
        self._branch_pr_cache[cache_key] = (pr_numbers, datetime.now(UTC).timestamp())

        # Also cache individual PR info
        for pr in prs:
            pr_number = pr.get("number")
            if pr_number:
                author = pr.get("author", {})
                self._pr_cache[(repo, pr_number)] = CachedPRInfo(
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
        pr_info = self._get_pr_info(repo, pr_number)

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
                reason="PR is owned by egg",
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
            "PR ownership denied - not owned by egg or configured user",
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
            reason=f"PR #{pr_number} is not owned by egg or configured user (author: {pr_info.author})",
            details={"author": pr_info.author, "expected": expected, "auth_mode": auth_mode},
        )

    def check_pr_comment_allowed(self, repo: str, pr_number: int) -> PolicyResult:
        """
        Check if egg can comment on a PR.

        Egg can comment on ANY PR - this enables collaboration on PRs owned by others.
        """
        pr_info = self._get_pr_info(repo, pr_number)

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

        In bot mode, egg can push to a branch if:
        1. Branch name starts with egg- or egg/ (allows pushing before PR exists), OR
        2. Branch has an open PR authored by egg, OR
        3. Branch has an open PR authored by the configured user, OR
        4. Branch has an open PR authored by a trusted user (from GATEWAY_TRUSTED_USERS)

        In user mode, the user can push to a branch if:
        1. Branch does not exist upstream yet (new branch), OR
        2. Branch has an open PR authored by egg, OR
        3. Branch has an open PR authored by the configured user

        Protected branches (main, master) are always blocked regardless of mode.

        Args:
            repo: Repository in "owner/repo" format
            branch: Branch name
            auth_mode: "bot" (default) or "user"
        """
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

        # In user mode:
        # 1. Allow pushing to NEW branches (don't exist upstream yet)
        # 2. Allow pushing to branches with open PR by egg OR configured user
        if auth_mode == "user":
            configured_user = self._get_configured_user()

            # Check if branch exists upstream (use user token for private repos)
            branch_exists = self.github.branch_exists(repo, branch, mode=auth_mode)

            # If we couldn't determine branch existence (API error), fail closed
            if branch_exists is None:
                logger.warning(
                    "Branch push denied - could not verify branch existence",
                    repo=repo,
                    branch=branch,
                    auth_mode=auth_mode,
                )
                return PolicyResult(
                    allowed=False,
                    reason=f"Could not verify if branch '{branch}' exists (API error). Try again later.",
                    details={
                        "branch": branch,
                        "configured_user": configured_user,
                        "auth_mode": "user",
                        "hint": "Check network connectivity and token permissions.",
                    },
                )

            # Branch doesn't exist - allow push to new branch
            if not branch_exists:
                logger.debug(
                    "Branch push allowed (user mode) - new branch",
                    repo=repo,
                    branch=branch,
                    configured_user=configured_user,
                )
                return PolicyResult(
                    allowed=True,
                    reason=f"User mode: push allowed to new branch '{branch}'",
                    details={
                        "branch": branch,
                        "configured_user": configured_user,
                        "auth_mode": "user",
                        "reason": "new_branch",
                    },
                )

            # Branch exists - check for open PR by egg or configured user
            pr_numbers = self._get_prs_for_branch(repo, branch)
            for pr_number in pr_numbers:
                pr_info = self._get_pr_info(repo, pr_number)
                if not pr_info:
                    continue

                # Check if PR is owned by egg
                if self._is_bot_author(pr_info.author):
                    logger.debug(
                        "Branch push allowed (user mode) - bot PR",
                        repo=repo,
                        branch=branch,
                        pr_number=pr_number,
                        author=pr_info.author,
                    )
                    return PolicyResult(
                        allowed=True,
                        reason=f"Branch '{branch}' has open PR #{pr_number} owned by egg",
                        details={
                            "branch": branch,
                            "pr_number": pr_number,
                            "author": pr_info.author,
                            "auth_mode": "user",
                            "reason": "bot_pr",
                        },
                    )

                # Check if PR is owned by configured user
                if configured_user and self._is_configured_user_author(
                    pr_info.author, configured_user
                ):
                    logger.debug(
                        "Branch push allowed (user mode) - configured user PR",
                        repo=repo,
                        branch=branch,
                        pr_number=pr_number,
                        author=pr_info.author,
                        configured_user=configured_user,
                    )
                    return PolicyResult(
                        allowed=True,
                        reason=f"Branch '{branch}' has open PR #{pr_number} owned by '{configured_user}'",
                        details={
                            "branch": branch,
                            "pr_number": pr_number,
                            "author": pr_info.author,
                            "configured_user": configured_user,
                            "auth_mode": "user",
                            "reason": "configured_user_pr",
                        },
                    )

            # Branch exists but no PR by egg or configured user
            logger.info(
                "Branch push denied (user mode) - no owned PR",
                repo=repo,
                branch=branch,
                configured_user=configured_user,
                open_prs=pr_numbers,
            )
            return PolicyResult(
                allowed=False,
                reason=f"Branch '{branch}' exists but has no open PR owned by egg or '{configured_user}'",
                details={
                    "branch": branch,
                    "open_prs": pr_numbers,
                    "configured_user": configured_user,
                    "auth_mode": "user",
                    "hint": "Create a PR from this branch on GitHub first, or push to a new branch name.",
                },
            )

        # Bot mode: Check 1: Branch prefix
        if self._is_bot_branch(branch):
            logger.debug(
                "Branch ownership verified by prefix",
                repo=repo,
                branch=branch,
            )
            return PolicyResult(
                allowed=True,
                reason=f"Branch '{branch}' is owned by egg (bot-prefixed branch)",
                details={"branch": branch, "reason": "bot_prefix"},
            )

        # Check 2-4: Open PR by egg, configured user, or trusted user
        pr_numbers = self._get_prs_for_branch(repo, branch)
        configured_user = self._get_configured_user()

        for pr_number in pr_numbers:
            pr_info = self._get_pr_info(repo, pr_number)
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
                    reason=f"Branch '{branch}' has open PR #{pr_number} owned by egg",
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
            "Branch push denied - not owned by egg, configured user, or trusted user",
            repo=repo,
            branch=branch,
            open_prs=pr_numbers,
            configured_user=configured_user,
            trusted_users=list(TRUSTED_BRANCH_OWNERS) if TRUSTED_BRANCH_OWNERS else [],
            auth_mode=auth_mode,
        )
        hint = "Use 'egg-' or 'egg/' branch prefix for new work"
        if configured_user:
            hint += f". Configured user: {configured_user}"
        if TRUSTED_BRANCH_OWNERS:
            hint += f". Trusted users: {', '.join(sorted(TRUSTED_BRANCH_OWNERS))}"
        return PolicyResult(
            allowed=False,
            reason=f"Branch '{branch}' is not owned by egg or an authorized user. "
            "Use a bot-prefixed branch (egg-* or egg/*).",
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

        In bot mode: Always allowed - egg can create PRs.
        In user mode: Blocked - user must create PRs manually via GitHub UI.

        This ensures the human maintains control over what PRs are opened in their name.
        egg can push branches in user mode, but the human decides which become PRs.

        Args:
            repo: Repository in "owner/repo" format
            auth_mode: "bot" (default) or "user"
        """
        if auth_mode == "user":
            configured_user = self._get_configured_user()
            logger.info(
                "PR creation blocked in user mode",
                repo=repo,
                configured_user=configured_user,
            )
            return PolicyResult(
                allowed=False,
                reason="PR creation is not allowed in user mode. "
                "Create the PR manually via GitHub UI.",
                details={
                    "repo": repo,
                    "auth_mode": "user",
                    "configured_user": configured_user,
                    "hint": "Push your branch, then create the PR at github.com",
                },
            )

        # Bot mode: always allowed
        logger.debug(
            "PR creation allowed (bot mode)",
            repo=repo,
        )
        return PolicyResult(
            allowed=True,
            reason="PR creation allowed in bot mode",
            details={"repo": repo, "auth_mode": "bot"},
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
                "action": "Use GitHub web UI or 'gh pr merge' from a non-egg environment",
            },
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
        r"github\.com[/:]([^/]+)/([^/\.]+?)(?:\.git)?$",
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
