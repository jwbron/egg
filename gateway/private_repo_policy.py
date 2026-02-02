"""
Private Mode Policy Enforcement.

Controls repository and network access based on mode:
- When "private": Private/internal repos only, network locked down (Anthropic API only)
- When "public": Public repos only, full internet access

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
"""

import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from shared.egg_logging import get_logger

from .error_messages import get_error_message
from .repo_parser import RepoInfo, extract_repo_from_request, parse_owner_repo
from .repo_visibility import get_repo_visibility

logger = get_logger("gateway.private-repo-policy")

# Environment variable to control private mode
PRIVATE_MODE_VAR = "PRIVATE_MODE"


@dataclass
class PrivateRepoPolicyResult:
    """Result of a private repo policy check."""

    allowed: bool
    reason: str
    visibility: str | None = None
    details: dict[str, Any] | None = None
    session_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result: dict[str, Any] = {
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


def is_private_mode_enabled() -> bool:
    """Check if Private Mode is enabled via environment variable.

    When true: private repos only, network locked down (Anthropic API only).
    When false: public repos only, full internet access.
    """
    value = os.environ.get(PRIVATE_MODE_VAR, "false").lower().strip()
    return value in ("true", "1", "yes")


class PrivateRepoPolicy:
    """Policy engine for repository visibility enforcement.

    Mode is determined per-session (not globally):
    - "private": Only private/internal repos accessible
    - "public": Only public repos accessible
    """

    def __init__(self):
        """Initialize the policy engine."""
        pass

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

        log_data = {
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
        """Check if access to a repository is allowed under private mode policy.

        Mode is determined by per-container session:
        - "private": Only private/internal repos accessible
        - "public": Only public repos accessible

        Sessions are mandatory - requests without session_mode are denied.
        """
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

        if owner and repo:
            repo_info = RepoInfo(owner=owner, repo=repo)
        elif repo and "/" in repo:
            repo_info = parse_owner_repo(repo)
        else:
            repo_str = f"{owner}/{repo}" if owner and repo else repo
            repo_info = extract_repo_from_request(
                repo=repo_str,
                repo_path=repo_path,
                url=url,
            )

        if not repo_info:
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
            if visibility == "public":
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
                        "hint": "Private Mode only allows private repositories",
                        "mode_source": mode_src,
                    },
                    session_mode=session_mode,
                )

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
        elif visibility == "public":
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
        session_mode: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if push is allowed."""
        return self.check_repository_access(
            operation="push",
            owner=owner,
            repo=repo,
            repo_path=repo_path,
            for_write=True,
            session_mode=session_mode,
        )

    def check_fetch(
        self,
        owner: str | None = None,
        repo: str | None = None,
        repo_path: str | None = None,
        session_mode: str | None = None,
    ) -> PrivateRepoPolicyResult:
        """Check if fetch is allowed."""
        return self.check_repository_access(
            operation="fetch",
            owner=owner,
            repo=repo,
            repo_path=repo_path,
            for_write=False,
            session_mode=session_mode,
        )


# Global policy instance with thread-safe initialization
_policy: PrivateRepoPolicy | None = None
_policy_lock = threading.Lock()


def get_private_repo_policy() -> PrivateRepoPolicy:
    """Get the global private repo policy instance (thread-safe)."""
    global _policy
    if _policy is None:
        with _policy_lock:
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
    """Check private repo access (convenience function)."""
    return get_private_repo_policy().check_repository_access(
        operation=operation,
        owner=owner,
        repo=repo,
        repo_path=repo_path,
        url=url,
        for_write=for_write,
        session_mode=session_mode,
    )
