"""
Private Mode Policy Enforcement.

Controls repository and network access based on PRIVATE_MODE:
- When true: Private/internal repos only, network locked down (Anthropic API only)
- When false: Public repos only, full internet access

This single flag controls the entire security posture - there's no way to
accidentally combine open network with private repo access.

Security Properties:
- FAIL CLOSED: If visibility cannot be determined, treat as public (deny access)
- Per-operation checking: Every operation validates the target repository
- Audit logging: All policy decisions are logged
- Thread-safe: Global instances use double-checked locking

Known Limitations (TOCTOU):
    There is an inherent Time-of-Check-Time-of-Use (TOCTOU) window between when
    visibility is checked and when the actual Git/GitHub operation executes.

    Implications:
    - A repository's visibility could change between check and operation
    - For read operations: 60-second cache means visibility changes may not be
      detected immediately (acceptable for performance)
    - For write operations: No caching (TTL=0) minimizes but doesn't eliminate
      the window

    Mitigations:
    - Write operations always verify visibility (no caching)
    - The gateway verifies again at operation time where possible
    - Audit logging captures all decisions for forensic analysis
    - Repository visibility changes are rare in practice

    This is a documented architectural limitation. For higher security
    requirements, consider additional controls at the Git server level.
"""

import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

# Import using try/except for both module and standalone script mode
try:
    from .error_messages import get_error_message
    from .repo_parser import (
        RepoInfo,
        extract_repo_from_request,
        parse_owner_repo,
    )
    from .repo_visibility import get_repo_visibility
except ImportError:
    from error_messages import get_error_message  # type: ignore[no-redef]
    from repo_parser import (  # type: ignore[no-redef, import-not-found]
        RepoInfo,
        extract_repo_from_request,
        parse_owner_repo,
    )
    from repo_visibility import get_repo_visibility  # type: ignore[no-redef]


logger = get_logger("gateway.private-repo-policy")


# Environment variable to control private mode
# When true: private repos only, network locked down
# When false: public repos only, full internet access
PRIVATE_MODE_VAR = "PRIVATE_MODE"


@dataclass
class PrivateRepoPolicyResult:
    """Result of a private repo policy check."""

    allowed: bool
    reason: str
    visibility: str | None = None
    details: dict[str, Any] | None = None
    session_mode: str | None = None  # "private", "public", or None (global mode)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {
            "allowed": self.allowed,
            "reason": self.reason,
            "policy": "private_mode",
        }
        if self.visibility:
            result["visibility"] = self.visibility
        if self.session_mode:
            result["session_mode"] = self.session_mode
        if self.details:
            result["details"] = self.details
        return result


class PrivateRepoPolicy:
    """
    Policy engine for repository visibility enforcement.

    Mode is determined per-session (not globally):
    - "private": Only private/internal repos accessible
    - "public": Only public repos accessible

    The PRIVATE_MODE environment variable controls Squid proxy config
    (network lockdown), but repo visibility is per-session.
    """

    def __init__(self) -> None:
        """Initialize the policy engine."""
        # No state needed - mode is per-session

    def _log_policy_event(
        self,
        operation: str,
        repo: RepoInfo | str | None,
        visibility: str | None,
        allowed: bool,
        reason: str,
    ) -> None:
        """Log a policy decision."""
        repo_str = str(repo) if repo else "unknown"

        log_data: dict[str, Any] = {
            "event_type": "private_repo_policy",
            "operation": operation,
            "repository": repo_str,
            "visibility": visibility,
            "decision": "allowed" if allowed else "denied",
            "reason": reason,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if allowed:
            logger.info("Private repo policy check passed", **log_data)
        else:
            logger.warning("Private repo policy check failed", **log_data)

    def check_repository_access(
        self,
        operation: str,
        owner: str | None = None,
        repo: str | None = None,
        repo_path: str | None = None,
        url: str | None = None,
        for_write: bool = False,
        session_mode: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """
        Check if access to a repository is allowed under private mode policy.

        Mode is determined by per-container session:
        - "private": Only private/internal repos accessible
        - "public": Only public repos accessible

        Sessions are mandatory - requests without session_mode are denied.

        Args:
            operation: Name of the operation (push, fetch, clone, etc.)
            owner: Repository owner (if known)
            repo: Repository name (if known)
            repo_path: Local repository path
            url: GitHub URL
            for_write: If True, use stricter caching (always verify)
            session_mode: Per-container mode from session ("private" or "public").
                          Required - requests without session_mode are denied.

        Returns:
            PrivateRepoPolicyResult with allowed status and reason
        """
        # Session mode is required - no legacy fallback to global env var
        if session_mode is None:
            reason = (
                f"Operation '{operation}' denied: No session mode specified. "
                "All requests must include a valid session with mode (private/public)."
            )
            self._log_policy_event(operation, None, None, False, reason)
            return PrivateRepoPolicyResult(
                allowed=False,
                reason=reason,
                details={
                    "error": "Missing session mode",
                    "hint": "Container must have a valid EGG_SESSION_TOKEN",
                },
                session_mode=None,
            )

        use_private_mode = session_mode == "private"

        # Try to determine the repository
        repo_info: RepoInfo | None = None

        # If owner and repo are provided directly
        if owner and repo:
            repo_info = RepoInfo(owner=owner, repo=repo)
        # Try full repo string (owner/repo format)
        elif repo and "/" in repo:
            repo_info = parse_owner_repo(repo)
        # Try to extract from other sources
        else:
            repo_str = f"{owner}/{repo}" if owner and repo else repo
            repo_info = extract_repo_from_request(
                repo=repo_str,
                repo_path=repo_path,
                url=url,
            )

        if not repo_info:
            # Cannot determine repository - FAIL CLOSED
            reason = get_error_message(
                "visibility_unknown",
                operation=operation,
                hint="Could not determine target repository",
            )
            self._log_policy_event(operation, None, None, False, reason)
            return PrivateRepoPolicyResult(
                allowed=False,
                reason=reason,
                details={
                    "error": "Could not determine target repository",
                    "repo": repo,
                    "repo_path": repo_path,
                    "url": url,
                },
                session_mode=session_mode,
            )

        # Check repository visibility
        visibility = get_repo_visibility(
            repo_info.owner,
            repo_info.repo,
            for_write=for_write,
        )

        if visibility is None:
            # Cannot determine visibility - FAIL CLOSED
            reason = get_error_message(
                "visibility_unknown",
                repo=str(repo_info),
                operation=operation,
            )
            self._log_policy_event(operation, repo_info, None, False, reason)
            return PrivateRepoPolicyResult(
                allowed=False,
                reason=reason,
                visibility=None,
                details={
                    "error": "Could not determine repository visibility",
                    "repository": str(repo_info),
                    "hint": "GitHub API may be unavailable or token may lack permissions",
                },
                session_mode=session_mode,
            )

        # Check based on mode
        if use_private_mode:
            # Private Repo Mode: only allow private/internal repositories
            if visibility == "public":
                # Public repository - DENY
                mode_src = "session" if session_mode else "global"
                reason = get_error_message(
                    f"{operation}_public",
                    repo=str(repo_info),
                )
                if session_mode:
                    reason = f"Private Repo Mode (session): {reason}"
                self._log_policy_event(operation, repo_info, visibility, False, reason)
                return PrivateRepoPolicyResult(
                    allowed=False,
                    reason=reason,
                    visibility=visibility,
                    details={
                        "repository": str(repo_info),
                        "visibility": visibility,
                        "private_mode": True,
                        "hint": "Private Repo Mode only allows interaction with private repositories",
                        "mode_source": mode_src,
                    },
                    session_mode=session_mode,
                )

            # Private or internal - ALLOW
            mode_src = "session" if session_mode else "global"
            self._log_policy_event(
                operation,
                repo_info,
                visibility,
                True,
                f"Repository is {visibility} (private repo mode, source={mode_src})",
            )
            return PrivateRepoPolicyResult(
                allowed=True,
                reason=f"Repository '{repo_info}' is {visibility}",
                visibility=visibility,
                details={
                    "repository": str(repo_info),
                    "visibility": visibility,
                    "private_mode": True,
                    "mode_source": mode_src,
                },
                session_mode=session_mode,
            )
        # Public Repo Only Mode: only allow public repositories
        elif visibility == "public":
            # Public repository - ALLOW
            mode_src = "session" if session_mode else "global"
            self._log_policy_event(
                operation,
                repo_info,
                visibility,
                True,
                f"Repository is {visibility} (public repo only mode, source={mode_src})",
            )
            return PrivateRepoPolicyResult(
                allowed=True,
                reason=f"Repository '{repo_info}' is {visibility}",
                visibility=visibility,
                details={
                    "repository": str(repo_info),
                    "visibility": visibility,
                    "private_mode": False,
                    "mode_source": mode_src,
                },
                session_mode=session_mode,
            )
        else:
            # Private/internal repository - DENY
            mode_src = "session" if session_mode else "global"
            reason = (
                f"Public Repo Only Mode ({mode_src}): Operation '{operation}' on repository "
                f"'{repo_info}' denied. Only public repositories are accessible "
                f"(repository is {visibility})."
            )
            self._log_policy_event(operation, repo_info, visibility, False, reason)
            return PrivateRepoPolicyResult(
                allowed=False,
                reason=reason,
                visibility=visibility,
                details={
                    "repository": str(repo_info),
                    "visibility": visibility,
                    "private_mode": False,
                    "hint": "Only public repositories are accessible (PRIVATE_MODE=false)",
                    "mode_source": mode_src,
                },
                session_mode=session_mode,
            )

    def check_push(
        self,
        owner: str | None = None,
        repo: str | None = None,
        repo_path: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if push is allowed."""
        return self.check_repository_access(
            operation="push",
            owner=owner,
            repo=repo,
            repo_path=repo_path,
            for_write=True,
        )

    def check_fetch(
        self,
        owner: str | None = None,
        repo: str | None = None,
        repo_path: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if fetch is allowed."""
        return self.check_repository_access(
            operation="fetch",
            owner=owner,
            repo=repo,
            repo_path=repo_path,
            for_write=False,
        )

    def check_clone(
        self,
        url: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if clone is allowed."""
        return self.check_repository_access(
            operation="clone",
            owner=owner,
            repo=repo,
            url=url,
            for_write=False,
        )

    def check_pr_create(
        self,
        owner: str | None = None,
        repo: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if PR creation is allowed."""
        return self.check_repository_access(
            operation="pr_create",
            owner=owner,
            repo=repo,
            for_write=True,
        )

    def check_pr_comment(
        self,
        owner: str | None = None,
        repo: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if PR comment is allowed."""
        return self.check_repository_access(
            operation="pr_comment",
            owner=owner,
            repo=repo,
            for_write=True,
        )

    def check_issue(
        self,
        owner: str | None = None,
        repo: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if issue operations are allowed."""
        return self.check_repository_access(
            operation="issue",
            owner=owner,
            repo=repo,
            for_write=True,
        )

    def check_gh_execute(
        self,
        owner: str | None = None,
        repo: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if generic gh execute is allowed."""
        return self.check_repository_access(
            operation="gh_execute",
            owner=owner,
            repo=repo,
            for_write=False,
        )


# Global policy instance with thread-safe initialization
_policy: PrivateRepoPolicy | None = None
_policy_lock = threading.Lock()


def get_private_repo_policy() -> PrivateRepoPolicy:
    """Get the global private repo policy instance (thread-safe)."""
    global _policy
    if _policy is None:
        with _policy_lock:
            # Double-checked locking pattern
            if _policy is None:
                _policy = PrivateRepoPolicy()
    return _policy


def check_private_repo_access(
    operation: str,
    owner: str | None = None,
    repo: str | None = None,
    repo_path: str | None = None,
    url: str | None = None,
    for_write: bool = False,
    session_mode: str | None = None,
) -> PrivateRepoPolicyResult:
    """
    Check private repo access (convenience function).

    Args:
        operation: Name of the operation
        owner: Repository owner
        repo: Repository name or owner/repo
        repo_path: Local repository path
        url: GitHub URL
        for_write: If True, use stricter caching
        session_mode: Per-container mode from session ("private" or "public").
                      Required - requests without session_mode are denied.

    Returns:
        PrivateRepoPolicyResult
    """
    return get_private_repo_policy().check_repository_access(
        operation=operation,
        owner=owner,
        repo=repo,
        repo_path=repo_path,
        url=url,
        for_write=for_write,
        session_mode=session_mode,
    )
