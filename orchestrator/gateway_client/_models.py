"""Gateway client dataclasses (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SessionInfo:
    """Information about a gateway session."""

    session_token: str
    container_id: str
    container_ip: str | None  # Optional; k8s pod IPs are ephemeral
    mode: str  # "private" or "public"
    created_at: datetime
    expires_at: datetime


@dataclass
class WorktreeResult:
    """Result of a worktree create/delete operation."""

    success: bool
    # create: full ``owner/repo`` slug -> host_path (#3393 slice-3, operator
    # ruling #6 — re-keyed from bare name so same-short-name repos under
    # different owners don't collide). delete: repo_name -> status.
    worktrees: dict[str, str]
    errors: list[str]


@dataclass
class GatewayHealth:
    """Gateway health status."""

    healthy: bool
    status: str
    version: str | None = None
    uptime_seconds: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class PushResult:
    """Outcome of a ``push_worktree_branch`` call.

    On failure (``ok`` is ``False``), ``category`` names a coarse failure
    class so callers can build actionable operator messages; ``detail``
    carries the raw git stderr or inner error text.

    Supports ``bool()`` so ``if push_result:`` callers that only care
    about success keep working — only callers that need to surface the
    reason need to inspect ``category`` / ``detail``.
    """

    ok: bool
    category: str | None = None
    detail: str | None = None

    def __bool__(self) -> bool:
        return self.ok

    def describe(self) -> str:
        """Return a human-readable ``category: detail`` string for logs/errors."""
        if self.ok:
            return "ok"
        cat = self.category or "unknown"
        if self.detail:
            return f"{cat}: {self.detail}"
        return cat
