"""
Fork-specific policy rules for Private Repo Mode.

Enforces restrictions on forking operations:
- Fork from public -> anywhere: BLOCKED
- Fork from private -> public: BLOCKED
- Fork from private -> private: ALLOWED
- Fork from internal -> internal/private: ALLOWED

This ensures that private code cannot be exposed via forking operations.
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
    from .repo_visibility import get_repo_visibility
except ImportError:
    from error_messages import get_error_message  # type: ignore[no-redef]
    from repo_visibility import get_repo_visibility  # type: ignore[no-redef]


logger = get_logger("gateway.fork-policy")


@dataclass
class ForkPolicyResult:
    """Result of a fork policy check."""

    allowed: bool
    reason: str
    source_visibility: str | None = None
    target_visibility: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {
            "allowed": self.allowed,
            "reason": self.reason,
            "policy": "fork_policy",
        }
        if self.source_visibility:
            result["source_visibility"] = self.source_visibility
        if self.target_visibility:
            result["target_visibility"] = self.target_visibility
        if self.details:
            result["details"] = self.details
        return result


class ForkPolicy:
    """
    Policy engine for fork operations in Private Repo Mode.

    Enforces that:
    1. Cannot fork from public repositories
    2. Cannot fork to public visibility
    3. Private/internal repos can only be forked to private/internal
    """

    def __init__(self, enabled: bool):
        """
        Initialize the fork policy.

        Args:
            enabled: Whether fork policy restrictions are enabled (True for private mode)
        """
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        """Check if fork policy is enabled (follows Private Repo Mode)."""
        return self._enabled

    def _log_policy_event(
        self,
        source_repo: str | None,
        target_org: str | None,
        source_visibility: str | None,
        target_visibility: str | None,
        allowed: bool,
        reason: str,
    ) -> None:
        """Log a fork policy decision."""
        log_data: dict[str, Any] = {
            "event_type": "fork_policy",
            "source_repository": source_repo,
            "target_organization": target_org,
            "source_visibility": source_visibility,
            "target_visibility": target_visibility,
            "decision": "allowed" if allowed else "denied",
            "reason": reason,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if allowed:
            logger.info("Fork policy check passed", **log_data)
        else:
            logger.warning("Fork policy check failed", **log_data)

    def check_fork_source(
        self,
        source_owner: str,
        source_repo: str,
    ) -> ForkPolicyResult:
        """
        Check if forking from a repository is allowed.

        In Private Repo Mode, forking from public repositories is blocked.

        Args:
            source_owner: Owner of the source repository
            source_repo: Name of the source repository

        Returns:
            ForkPolicyResult
        """
        if not self._enabled:
            return ForkPolicyResult(
                allowed=True,
                reason="Private Repo Mode is disabled",
                details={"private_mode": False},
            )

        source_full = f"{source_owner}/{source_repo}"

        # Check source visibility
        visibility = get_repo_visibility(source_owner, source_repo)

        if visibility is None:
            reason = get_error_message(
                "visibility_unknown",
                repo=source_full,
                operation="fork",
            )
            self._log_policy_event(source_full, None, None, None, False, reason)
            return ForkPolicyResult(
                allowed=False,
                reason=reason,
                details={
                    "error": "Could not determine source repository visibility",
                    "source_repository": source_full,
                },
            )

        if visibility == "public":
            reason = get_error_message(
                "fork_from_public",
                repo=source_full,
            )
            self._log_policy_event(source_full, None, visibility, None, False, reason)
            return ForkPolicyResult(
                allowed=False,
                reason=reason,
                source_visibility=visibility,
                details={
                    "source_repository": source_full,
                    "source_visibility": visibility,
                    "hint": "In Private Repo Mode, you can only fork from private repositories",
                },
            )

        self._log_policy_event(
            source_full,
            None,
            visibility,
            None,
            True,
            f"Source repository is {visibility}",
        )
        return ForkPolicyResult(
            allowed=True,
            reason=f"Source repository '{source_full}' is {visibility}",
            source_visibility=visibility,
            details={"source_repository": source_full, "source_visibility": visibility},
        )

    def check_fork_target(
        self,
        target_org: str,
        make_private: bool = True,
    ) -> ForkPolicyResult:
        """
        Check if forking to a target organization with visibility is allowed.

        In Private Repo Mode, forks must be private or internal.

        Args:
            target_org: Target organization for the fork
            make_private: Whether the fork will be private (default: True)

        Returns:
            ForkPolicyResult
        """
        if not self._enabled:
            return ForkPolicyResult(
                allowed=True,
                reason="Private Repo Mode is disabled",
                details={"private_mode": False},
            )

        # In Private Repo Mode, forks must be private
        if not make_private:
            reason = get_error_message(
                "fork_to_public",
            )
            self._log_policy_event(None, target_org, None, "public", False, reason)
            return ForkPolicyResult(
                allowed=False,
                reason=reason,
                target_visibility="public",
                details={
                    "target_organization": target_org,
                    "make_private": make_private,
                    "hint": "In Private Repo Mode, all forks must be private",
                },
            )

        self._log_policy_event(
            None,
            target_org,
            None,
            "private",
            True,
            "Fork will be private",
        )
        return ForkPolicyResult(
            allowed=True,
            reason=f"Fork to '{target_org}' will be private",
            target_visibility="private",
            details={"target_organization": target_org, "make_private": True},
        )

    def check_fork(
        self,
        source_owner: str,
        source_repo: str,
        target_org: str | None = None,
        make_private: bool = True,
    ) -> ForkPolicyResult:
        """
        Check if a complete fork operation is allowed.

        Validates both source and target.

        Args:
            source_owner: Owner of the source repository
            source_repo: Name of the source repository
            target_org: Target organization (None for personal account)
            make_private: Whether to make the fork private

        Returns:
            ForkPolicyResult
        """
        if not self._enabled:
            return ForkPolicyResult(
                allowed=True,
                reason="Private Repo Mode is disabled",
                details={"private_mode": False},
            )

        # Check source first
        source_result = self.check_fork_source(source_owner, source_repo)
        if not source_result.allowed:
            return source_result

        # Check target
        target_result = self.check_fork_target(
            target_org or "personal",
            make_private=make_private,
        )
        if not target_result.allowed:
            return target_result

        # Both passed
        source_full = f"{source_owner}/{source_repo}"
        return ForkPolicyResult(
            allowed=True,
            reason=f"Fork from '{source_full}' to '{target_org or 'personal'}' is allowed",
            source_visibility=source_result.source_visibility,
            target_visibility=target_result.target_visibility,
            details={
                "source_repository": source_full,
                "source_visibility": source_result.source_visibility,
                "target_organization": target_org or "personal",
                "target_visibility": target_result.target_visibility,
            },
        )


# Note: No global singleton - ForkPolicy should be instantiated per-request
# with explicit enabled parameter based on session mode.
