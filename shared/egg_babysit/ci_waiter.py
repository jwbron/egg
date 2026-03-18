"""CI check waiter with polling loop.

Polls GitHub CI check statuses at a configurable interval until all
checks complete (pass or fail) or a timeout is reached. Detects stale
checks that show no progress.
"""

import logging
import time

from .pr_state import fetch_ci_checks
from .types import CICheckResult, CICheckStatus

logger = logging.getLogger(__name__)

# Number of consecutive polls with no status change before marking checks stale.
_STALE_THRESHOLD = 20


def wait_for_ci(
    pr_number: int,
    repo: str,
    *,
    poll_interval: int = 30,
    timeout: int = 1800,
) -> tuple[CICheckStatus, list[CICheckResult]]:
    """Wait for all CI checks to complete.

    Polls CI check statuses at ``poll_interval`` seconds until all checks
    have a terminal status (passing or failing), a timeout is reached, or
    checks are detected as stale.

    Args:
        pr_number: Pull request number.
        repo: Repository in owner/repo format.
        poll_interval: Seconds between polls.
        timeout: Maximum seconds to wait.

    Returns:
        Tuple of (aggregated status, list of check results).
    """
    start = time.monotonic()
    polls_without_change = 0
    last_status_snapshot: dict[str, str] = {}

    logger.info("Waiting for CI checks on PR #%d (timeout=%ds)", pr_number, timeout)

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= timeout:
            logger.warning("CI wait timed out after %.0fs for PR #%d", elapsed, pr_number)
            # Fetch final state and return whatever we have.
            checks = _safe_fetch(pr_number, repo)
            return CICheckStatus.PENDING, checks

        checks = _safe_fetch(pr_number, repo)
        if not checks:
            logger.info("No CI checks found yet for PR #%d, waiting...", pr_number)
            time.sleep(poll_interval)
            continue

        # Build current status snapshot for stale detection.
        current_snapshot = {c.name: c.status.value for c in checks}

        # Check if all checks are in a terminal state.
        all_terminal = all(
            c.status in (CICheckStatus.PASSING, CICheckStatus.FAILING) for c in checks
        )

        if all_terminal:
            aggregate = _aggregate_status(checks)
            passing = sum(1 for c in checks if c.status == CICheckStatus.PASSING)
            failing = sum(1 for c in checks if c.status == CICheckStatus.FAILING)
            logger.info(
                "CI checks complete for PR #%d: %d passing, %d failing",
                pr_number,
                passing,
                failing,
            )
            return aggregate, checks

        # Stale detection: if no status changes for many consecutive polls.
        if current_snapshot == last_status_snapshot:
            polls_without_change += 1
        else:
            polls_without_change = 0
            last_status_snapshot = current_snapshot

        if polls_without_change >= _STALE_THRESHOLD:
            logger.warning(
                "CI checks appear stale for PR #%d (%d polls with no change)",
                pr_number,
                polls_without_change,
            )
            return CICheckStatus.STALE, checks

        # Log progress.
        pending = sum(1 for c in checks if c.status == CICheckStatus.PENDING)
        terminal = len(checks) - pending
        logger.info(
            "CI progress for PR #%d: %d/%d complete (%.0fs elapsed)",
            pr_number,
            terminal,
            len(checks),
            elapsed,
        )

        time.sleep(poll_interval)


def _safe_fetch(pr_number: int, repo: str) -> list[CICheckResult]:
    """Fetch CI checks, returning empty list on error."""
    try:
        return fetch_ci_checks(pr_number, repo)
    except Exception as exc:
        logger.warning("Failed to fetch CI checks for PR #%d: %s", pr_number, exc)
        return []


def _aggregate_status(checks: list[CICheckResult]) -> CICheckStatus:
    """Aggregate check statuses into a single status.

    Args:
        checks: List of CI check results.

    Returns:
        Aggregated CICheckStatus.
    """
    if not checks:
        return CICheckStatus.PENDING
    if any(c.status == CICheckStatus.FAILING for c in checks):
        return CICheckStatus.FAILING
    if all(c.status == CICheckStatus.PASSING for c in checks):
        return CICheckStatus.PASSING
    return CICheckStatus.PENDING


__all__ = [
    "wait_for_ci",
]
