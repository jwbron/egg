"""
Policy Engine - Ownership and access control checks for egg sandbox.

Enforces policies for git/gh operations:
- Branch ownership: Can push to egg-prefixed branches OR branches with authorized PRs
- PR creation: Allowed in bot mode, blocked in user mode
- PR comments: Can comment on any PR
- PR edit/close: Can only modify owned PRs
- Merge blocked: No merge operations allowed (human must merge)

Configuration:
- EGG_TRUSTED_USERS: Comma-separated list of GitHub usernames whose branches
  the sandbox is allowed to push to (e.g., "jwbron,octocat")
"""

import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from shared.egg_logging import get_logger

if TYPE_CHECKING:
    from .github_client import GitHubClient

logger = get_logger("gateway.policy")

# Cache size limits
MAX_PR_CACHE_SIZE = 500
MAX_BRANCH_PR_CACHE_SIZE = 200


def _get_bot_identities(bot_name: str = "egg") -> frozenset[str]:
    """Get bot identity variants."""
    return frozenset(
        {
            bot_name,
            f"{bot_name}[bot]",
            f"app/{bot_name}",
            f"apps/{bot_name}",
        }
    )


def _get_branch_prefixes(prefix: str = "egg/") -> tuple[str, ...]:
    """Get branch prefixes that indicate ownership."""
    # Support both slash and dash variants
    base = prefix.rstrip("/-")
    return (f"{base}-", f"{base}/")


def _load_trusted_users() -> frozenset[str]:
    """Load trusted users from environment variable."""
    env_value = os.environ.get("EGG_TRUSTED_USERS", "")
    if not env_value.strip():
        return frozenset()
    users = [u.strip().lower() for u in env_value.split(",") if u.strip()]
    return frozenset(users)


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

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.max_size:
            self.popitem(last=False)


class PolicyEngine:
    """
    Policy enforcement engine for git/gh operations.

    Caches PR info to reduce GitHub API calls.
    Uses bounded caches to prevent unbounded memory growth.
    """

    def __init__(
        self,
        github_client: "GitHubClient | None" = None,
        bot_name: str = "egg",
        branch_prefix: str = "egg/",
        protected_branches: list[str] | None = None,
    ):
        self.github = github_client
        self.bot_name = bot_name
        self.bot_identities = _get_bot_identities(bot_name)
        self.branch_prefixes = _get_branch_prefixes(branch_prefix)
        self.protected_branches = tuple(protected_branches or ["main", "master"])
        self.trusted_users = _load_trusted_users()

        # Caches
        self._pr_cache: BoundedCache = BoundedCache(MAX_PR_CACHE_SIZE)
        self._branch_pr_cache: BoundedCache = BoundedCache(MAX_BRANCH_PR_CACHE_SIZE)

    def _is_bot_author(self, author: str | dict[str, Any]) -> bool:
        """Check if author is a bot identity."""
        if isinstance(author, dict):
            login = author.get("login", "")
        else:
            login = author
        return login.lower() in self.bot_identities

    def _is_owned_branch(self, branch: str) -> bool:
        """Check if branch name indicates ownership."""
        return branch.startswith(self.branch_prefixes)

    def _is_trusted_author(self, author: str | dict[str, Any]) -> bool:
        """Check if author is a trusted user."""
        if not self.trusted_users:
            return False
        if isinstance(author, dict):
            login = author.get("login", "")
        else:
            login = author
        return login.lower() in self.trusted_users

    def _get_pr_info(self, repo: str, pr_number: int) -> CachedPRInfo | None:
        """Get PR info, using cache if available and fresh."""
        if not self.github:
            return None

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
        if not self.github:
            return []

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

    def check_pr_ownership(self, repo: str, pr_number: int) -> PolicyResult:
        """Check if the current identity owns a PR."""
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

        # Check if PR is owned by bot
        if self._is_bot_author(pr_info.author):
            logger.debug(
                "PR ownership verified",
                repo=repo,
                pr_number=pr_number,
                author=pr_info.author,
            )
            return PolicyResult(
                allowed=True,
                reason=f"PR is owned by {self.bot_name}",
                details={"author": pr_info.author},
            )

        # Check if PR is owned by trusted user
        if self._is_trusted_author(pr_info.author):
            logger.debug(
                "PR ownership verified (trusted user)",
                repo=repo,
                pr_number=pr_number,
                author=pr_info.author,
            )
            return PolicyResult(
                allowed=True,
                reason=f"PR is owned by trusted user ({pr_info.author})",
                details={"author": pr_info.author},
            )

        logger.info(
            "PR ownership denied",
            repo=repo,
            pr_number=pr_number,
            author=pr_info.author,
        )
        return PolicyResult(
            allowed=False,
            reason=f"PR #{pr_number} is not owned by {self.bot_name} (author: {pr_info.author})",
            details={"author": pr_info.author, "expected": list(self.bot_identities)},
        )

    def check_pr_comment_allowed(self, repo: str, pr_number: int) -> PolicyResult:
        """Check if commenting on a PR is allowed. Always allowed."""
        pr_info = self._get_pr_info(repo, pr_number)

        if not pr_info:
            logger.warning(
                "PR not found for comment",
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
        )
        return PolicyResult(
            allowed=True,
            reason="Comments are allowed on any PR",
            details={"pr_number": pr_number, "author": pr_info.author},
        )

    def check_branch_ownership(self, repo: str, branch: str) -> PolicyResult:
        """
        Check if pushing to a branch is allowed.

        Allowed if:
        1. Branch name starts with configured prefix (e.g., egg/, egg-)
        2. Branch has an open PR authored by bot or trusted user
        """
        # Block protected branches
        if branch in self.protected_branches:
            logger.warning(
                "Push to protected branch blocked",
                repo=repo,
                branch=branch,
            )
            return PolicyResult(
                allowed=False,
                reason=f"Branch '{branch}' is protected. Direct pushes not allowed.",
                details={
                    "branch": branch,
                    "protected_branches": list(self.protected_branches),
                    "hint": "Create a feature branch and open a PR instead.",
                },
            )

        # Check branch prefix
        if self._is_owned_branch(branch):
            logger.debug(
                "Branch ownership verified by prefix",
                repo=repo,
                branch=branch,
            )
            return PolicyResult(
                allowed=True,
                reason=f"Branch '{branch}' is owned (prefixed)",
                details={"branch": branch, "reason": "prefix"},
            )

        # Check for open PR
        pr_numbers = self._get_prs_for_branch(repo, branch)

        for pr_number in pr_numbers:
            pr_info = self._get_pr_info(repo, pr_number)
            if not pr_info:
                continue

            if self._is_bot_author(pr_info.author):
                logger.debug(
                    "Branch ownership verified by PR",
                    repo=repo,
                    branch=branch,
                    pr_number=pr_number,
                )
                return PolicyResult(
                    allowed=True,
                    reason=f"Branch '{branch}' has open PR #{pr_number} owned by {self.bot_name}",
                    details={
                        "branch": branch,
                        "pr_number": pr_number,
                        "author": pr_info.author,
                    },
                )

            if self._is_trusted_author(pr_info.author):
                logger.debug(
                    "Branch push allowed (trusted user PR)",
                    repo=repo,
                    branch=branch,
                    pr_number=pr_number,
                )
                return PolicyResult(
                    allowed=True,
                    reason=f"Branch '{branch}' has open PR #{pr_number} by trusted user",
                    details={
                        "branch": branch,
                        "pr_number": pr_number,
                        "author": pr_info.author,
                    },
                )

        # Not allowed
        logger.info(
            "Branch push denied",
            repo=repo,
            branch=branch,
            open_prs=pr_numbers,
        )
        prefixes = ", ".join(self.branch_prefixes)
        return PolicyResult(
            allowed=False,
            reason=f"Branch '{branch}' is not owned. Use prefix ({prefixes}) or create a PR first.",
            details={
                "branch": branch,
                "open_prs": pr_numbers,
                "allowed_prefixes": list(self.branch_prefixes),
            },
        )

    def check_merge_allowed(self, repo: str, pr_number: int) -> PolicyResult:
        """Check if merge is allowed. Always returns False - human must merge."""
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
    """Extract owner/repo from a git remote URL."""
    patterns = [
        r"github\.com[/:]([^/]+)/([^/\.]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, remote_url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def extract_branch_from_refspec(refspec: str) -> str | None:
    """Extract branch name from a git refspec."""
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
