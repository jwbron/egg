"""Typed data structures for the babysit-pr loop.

Provides enums, state snapshots, and result types used throughout the
babysit package to track PR state, CI checks, review verdicts, and
loop execution progress.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class BabysitStep(StrEnum):
    """Steps in the babysit-pr loop."""

    CHECK_CONFLICTS = "check_conflicts"
    WAIT_CI = "wait_ci"
    FIX_CHECKS = "fix_checks"
    REVIEW = "review"
    ADDRESS_FEEDBACK = "address_feedback"
    DONE = "done"


class BabysitExitReason(StrEnum):
    """Reasons the babysit loop can exit."""

    MERGED = "merged"
    READY_TO_MERGE = "ready_to_merge"
    TIMEOUT = "timeout"
    MAX_ITERATIONS = "max_iterations"
    ESCALATED = "escalated"
    ERROR = "error"
    CANCELLED = "cancelled"


class CICheckStatus(StrEnum):
    """Status of a CI check run."""

    PENDING = "pending"
    PASSING = "passing"
    FAILING = "failing"
    STALE = "stale"


class ReviewVerdict(StrEnum):
    """GitHub pull request review verdict."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    PENDING = "pending"


@dataclass
class CICheckResult:
    """Result of a single CI check run.

    Attributes:
        name: Job name as reported by GitHub Actions.
        status: Aggregated status enum.
        conclusion: Raw conclusion string (pass/fail/neutral/etc).
        url: URL to the check run logs.
    """

    name: str
    status: CICheckStatus
    conclusion: str
    url: str = ""


@dataclass
class PRState:
    """Snapshot of PR state from GitHub.

    Attributes:
        number: PR number.
        title: PR title.
        state: GitHub PR state (open, closed, merged).
        merged: Whether the PR has been merged.
        mergeable: Whether the PR can be merged without conflicts.
        mergeable_state: GitHub mergeable state (clean, dirty, blocked, behind, unknown).
        head_sha: SHA of the PR head commit.
        base_branch: Target branch name.
        head_branch: Source branch name.
        ci_checks: List of CI check results.
        review_verdict: Aggregated review verdict.
        review_comments: List of review comment bodies.
    """

    number: int
    title: str
    state: str
    merged: bool
    mergeable: bool
    mergeable_state: str
    head_sha: str
    base_branch: str
    head_branch: str
    ci_checks: list[CICheckResult] = field(default_factory=list)
    review_verdict: ReviewVerdict = ReviewVerdict.PENDING
    review_comments: list[str] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        """Whether the PR has merge conflicts."""
        return self.mergeable_state == "dirty"

    @property
    def ci_status(self) -> CICheckStatus:
        """Aggregated CI status across all checks."""
        if not self.ci_checks:
            return CICheckStatus.PENDING
        if any(c.status == CICheckStatus.FAILING for c in self.ci_checks):
            return CICheckStatus.FAILING
        if all(c.status == CICheckStatus.PASSING for c in self.ci_checks):
            return CICheckStatus.PASSING
        return CICheckStatus.PENDING

    @property
    def failed_checks(self) -> list[CICheckResult]:
        """List of CI checks that are failing."""
        return [c for c in self.ci_checks if c.status == CICheckStatus.FAILING]


@dataclass
class LoopState:
    """Persisted state of the babysit loop for crash recovery.

    Attributes:
        iteration: Current iteration number.
        current_step: Current step in the loop.
        last_head_sha: Last known HEAD SHA of the PR branch.
        retry_counts: Per-job retry counters (job_name -> retry count).
        feedback_rounds: Number of feedback addressing rounds completed.
        started_at: ISO 8601 timestamp when the loop started.
        last_activity_at: ISO 8601 timestamp of last activity.
    """

    iteration: int = 0
    current_step: BabysitStep = BabysitStep.CHECK_CONFLICTS
    last_head_sha: str = ""
    retry_counts: dict[str, int] = field(default_factory=dict)
    feedback_rounds: int = 0
    started_at: str = ""
    last_activity_at: str = ""


@dataclass
class BabysitResult:
    """Result of a babysit-pr session.

    Attributes:
        exit_reason: Why the babysit loop exited.
        iterations: Total number of iterations completed.
        duration_seconds: Total wall-clock duration.
        last_step: The step the loop was on when it exited.
        message: Human-readable summary message.
    """

    exit_reason: BabysitExitReason
    iterations: int
    duration_seconds: float
    last_step: BabysitStep
    message: str = ""


__all__ = [
    "BabysitExitReason",
    "BabysitResult",
    "BabysitStep",
    "CICheckResult",
    "CICheckStatus",
    "LoopState",
    "PRState",
    "ReviewVerdict",
]
