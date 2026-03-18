"""Main babysit-pr loop.

Orchestrates the full PR babysitting lifecycle: conflict detection,
CI waiting, check fixing, code review, and feedback addressing. The
loop runs until the PR is merged, a timeout is reached, the maximum
iteration count is exceeded, or an unrecoverable error occurs.
"""

import logging
import signal
import subprocess
import time
from datetime import UTC, datetime
from types import FrameType

from .ci_waiter import wait_for_ci
from .config import BabysitConfig
from .escalation import escalate
from .pr_state import detect_head_sha_change, get_full_pr_state
from .steps.check_fix import fix_failed_checks
from .steps.conflict import resolve_conflicts
from .steps.feedback import address_feedback
from .steps.review import run_review
from .types import (
    BabysitExitReason,
    BabysitResult,
    BabysitStep,
    CICheckStatus,
    LoopState,
    PRState,
    ReviewVerdict,
)

logger = logging.getLogger(__name__)


class BabysitLoop:
    """Main babysit-pr loop controller.

    Manages the state machine that drives the PR through conflict
    resolution, CI checks, code review, and feedback addressing.

    Attributes:
        config: Babysit configuration.
        state: Mutable loop state for crash recovery.
    """

    def __init__(self, config: BabysitConfig) -> None:
        self.config = config
        self.state = LoopState(
            started_at=datetime.now(UTC).isoformat(),
            last_activity_at=datetime.now(UTC).isoformat(),
        )
        self._cancelled = False
        self._start_time = time.monotonic()

    def run(self) -> BabysitResult:
        """Execute the babysit loop.

        Returns:
            BabysitResult describing the outcome.
        """
        self._install_signal_handlers()

        logger.info(
            "Starting babysit loop for PR #%d in %s (timeout=%ds, max_iter=%d)",
            self.config.pr_number,
            self.config.repo,
            self.config.timeout_seconds,
            self.config.max_iterations,
        )

        try:
            return self._loop()
        except Exception as exc:
            logger.error("Babysit loop error: %s", exc, exc_info=True)
            return self._result(BabysitExitReason.ERROR, message=str(exc))
        finally:
            self._restore_signal_handlers()

    def _loop(self) -> BabysitResult:
        """Inner loop implementation."""
        while self.state.iteration < self.config.max_iterations:
            # Check for cancellation.
            if self._cancelled:
                logger.info("Babysit loop cancelled")
                return self._result(
                    BabysitExitReason.CANCELLED, message="Received termination signal"
                )

            # Check timeout.
            if self._is_timed_out():
                logger.warning("Babysit loop timed out after %.0fs", self._elapsed())
                return self._result(BabysitExitReason.TIMEOUT, message="Loop timed out")

            self.state.iteration += 1
            self._update_activity()
            logger.info("=== Iteration %d/%d ===", self.state.iteration, self.config.max_iterations)

            # Step 1: Check if PR is already merged or closed.
            self._set_step(BabysitStep.CHECK_CONFLICTS)
            pr_state = self._fetch_pr_state()
            if pr_state is None:
                return self._result(BabysitExitReason.ERROR, message="Failed to fetch PR state")

            if pr_state.merged:
                logger.info("PR #%d is merged", self.config.pr_number)
                return self._result(BabysitExitReason.MERGED, message="PR merged")

            if pr_state.state == "closed":
                logger.info("PR #%d is closed", self.config.pr_number)
                return self._result(BabysitExitReason.CANCELLED, message="PR closed")

            # Detect concurrent pushes.
            if detect_head_sha_change(self.state.last_head_sha, pr_state):
                logger.info("HEAD changed, resetting retry counts")
                self.state.retry_counts.clear()
            self.state.last_head_sha = pr_state.head_sha

            # Step 2: Check and resolve conflicts.
            if pr_state.has_conflicts:
                conflict_result = resolve_conflicts(self.config, pr_state, elapsed=self._elapsed())
                self._emit_progress("conflict_resolution", conflict_result.success)

                if conflict_result.escalate:
                    escalate(self.config, "Merge conflicts", conflict_result.message)
                    return self._result(
                        BabysitExitReason.ESCALATED, message=conflict_result.message
                    )

                if not conflict_result.success:
                    continue  # Retry next iteration.

            # Step 3: Wait for CI checks.
            self._set_step(BabysitStep.WAIT_CI)
            ci_status, ci_checks = wait_for_ci(
                self.config.pr_number,
                self.config.repo,
                poll_interval=self.config.poll_interval_seconds,
                timeout=max(0, min(1800, self.config.timeout_seconds - int(self._elapsed()))),
            )

            if ci_status == CICheckStatus.STALE:
                logger.warning("CI checks are stale, escalating")
                escalate(self.config, "Stale CI checks", "CI checks show no progress")
                return self._result(BabysitExitReason.ESCALATED, message="CI checks stale")

            # Step 4: Fix failing checks.
            if ci_status == CICheckStatus.FAILING:
                self._set_step(BabysitStep.FIX_CHECKS)
                failed = [c for c in ci_checks if c.status == CICheckStatus.FAILING]
                fix_result = fix_failed_checks(
                    self.config,
                    failed,
                    self.state.retry_counts,
                    base_branch=pr_state.base_branch,
                    elapsed=self._elapsed(),
                )
                self._emit_progress("fix_checks", fix_result.success)

                if fix_result.escalate:
                    escalate(self.config, "CI fix failures", fix_result.message)
                    return self._result(BabysitExitReason.ESCALATED, message=fix_result.message)

                if not fix_result.success:
                    continue  # Retry next iteration after fixes.

                # Wait for CI again after fixes.
                self._set_step(BabysitStep.WAIT_CI)
                ci_status, ci_checks = wait_for_ci(
                    self.config.pr_number,
                    self.config.repo,
                    poll_interval=self.config.poll_interval_seconds,
                    timeout=max(0, min(1800, self.config.timeout_seconds - int(self._elapsed()))),
                )

                if ci_status != CICheckStatus.PASSING:
                    continue  # Loop again to retry fixes.

            # Step 5: All checks passing. Run review.
            if ci_status == CICheckStatus.PASSING:
                # Re-fetch PR state to check if already approved.
                pr_state = self._fetch_pr_state()
                if pr_state and pr_state.merged:
                    return self._result(BabysitExitReason.MERGED, message="PR merged")

                if pr_state and pr_state.review_verdict == ReviewVerdict.APPROVED:
                    logger.info(
                        "PR #%d approved and CI passing — ready for merge", self.config.pr_number
                    )
                    self._set_step(BabysitStep.DONE)
                    return self._result(
                        BabysitExitReason.READY_TO_MERGE,
                        message="PR approved with all checks passing — ready for merge",
                    )

                # Concurrent BRC mode: run fixer and reviewer in parallel.
                if self.config.concurrent_mode:
                    self._set_step(BabysitStep.REVIEW)
                    from .concurrent import run_concurrent_review

                    self.state.consensus_round += 1
                    concurrent_result = run_concurrent_review(self.config, elapsed=self._elapsed())
                    self._emit_progress("concurrent_review", concurrent_result.consensus_reached)

                    if concurrent_result.escalated:
                        escalate(
                            self.config,
                            "Concurrent review",
                            concurrent_result.message,
                        )
                        return self._result(
                            BabysitExitReason.ESCALATED,
                            message=concurrent_result.message,
                        )

                    if concurrent_result.verdict == ReviewVerdict.APPROVED:
                        logger.info(
                            "PR #%d approved by concurrent review — ready for merge",
                            self.config.pr_number,
                        )
                        self._set_step(BabysitStep.DONE)
                        return self._result(
                            BabysitExitReason.READY_TO_MERGE,
                            message="PR approved with all checks passing — ready for merge",
                        )

                    # If not approved, continue to next iteration.
                    continue

                # Sequential mode (default): run review then address feedback.
                self._set_step(BabysitStep.REVIEW)
                review_result = run_review(self.config)
                self._emit_progress("review", review_result.success)

                if review_result.verdict == ReviewVerdict.APPROVED:
                    logger.info(
                        "PR #%d approved by reviewer — ready for merge", self.config.pr_number
                    )
                    self._set_step(BabysitStep.DONE)
                    return self._result(
                        BabysitExitReason.READY_TO_MERGE,
                        message="PR approved with all checks passing — ready for merge",
                    )

                # Step 6: Address review feedback.
                if review_result.verdict == ReviewVerdict.CHANGES_REQUESTED:
                    self._set_step(BabysitStep.ADDRESS_FEEDBACK)
                    self.state.feedback_rounds += 1

                    feedback_result = address_feedback(
                        self.config,
                        review_result.comments,
                        self.state.feedback_rounds,
                        elapsed=self._elapsed(),
                    )
                    self._emit_progress("address_feedback", feedback_result.success)

                    if feedback_result.escalate:
                        escalate(
                            self.config,
                            "Feedback addressing limit",
                            feedback_result.message,
                        )
                        return self._result(
                            BabysitExitReason.ESCALATED,
                            message=feedback_result.message,
                        )

                    # Continue to next iteration (re-check CI after feedback fixes).
                    continue

                # Review was just a comment, not changes requested. Continue loop.
                logger.info("Review had comments only, continuing loop")
                continue

            # CI is still pending - continue to next iteration.
            continue

        # Exceeded max iterations.
        logger.warning(
            "Babysit loop exceeded max iterations (%d) for PR #%d",
            self.config.max_iterations,
            self.config.pr_number,
        )
        return self._result(
            BabysitExitReason.MAX_ITERATIONS,
            message=f"Exceeded {self.config.max_iterations} iterations",
        )

    def _fetch_pr_state(self) -> PRState | None:
        """Fetch PR state, returning None on error."""
        try:
            return get_full_pr_state(self.config.pr_number, self.config.repo)
        except Exception as exc:
            logger.error("Failed to fetch PR state: %s", exc)
            return None

    def _set_step(self, step: BabysitStep) -> None:
        """Update the current step and log the transition."""
        if self.state.current_step != step:
            logger.info("Step: %s -> %s", self.state.current_step, step)
            self.state.current_step = step

    def _is_timed_out(self) -> bool:
        """Check if the loop has exceeded its timeout."""
        return self._elapsed() >= self.config.timeout_seconds

    def _elapsed(self) -> float:
        """Seconds elapsed since loop start."""
        return time.monotonic() - self._start_time

    def _update_activity(self) -> None:
        """Update the last activity timestamp."""
        self.state.last_activity_at = datetime.now(UTC).isoformat()

    def _result(self, reason: BabysitExitReason, message: str = "") -> BabysitResult:
        """Build a BabysitResult from current state."""
        return BabysitResult(
            exit_reason=reason,
            iterations=self.state.iteration,
            duration_seconds=self._elapsed(),
            last_step=self.state.current_step,
            message=message,
        )

    def _emit_progress(self, step: str, success: bool) -> None:
        """Emit progress via egg-orch CLI (best-effort).

        Args:
            step: Step name for the progress event.
            success: Whether the step succeeded.
        """
        state = "complete" if success else "blocked"
        detail = f"PR #{self.config.pr_number} iteration {self.state.iteration}"

        try:
            subprocess.run(
                [
                    "egg-orch",
                    "progress",
                    "emit",
                    "--step",
                    step,
                    "--state",
                    state,
                    "--detail",
                    detail,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            pass  # egg-orch not available.
        except Exception as exc:
            logger.debug("Failed to emit progress: %s", exc)

    def _install_signal_handlers(self) -> None:
        """Install signal handlers for graceful shutdown.

        Saves original handlers so they can be restored by
        ``_restore_signal_handlers`` when the loop exits.
        """
        self._prev_sigterm = None
        self._prev_sigint = None

        def _handle_signal(signum: int, frame: FrameType | None) -> None:
            sig_name = signal.Signals(signum).name
            logger.info("Received %s, cancelling babysit loop", sig_name)
            self._cancelled = True

        try:
            self._prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
            self._prev_sigint = signal.signal(signal.SIGINT, _handle_signal)
        except (OSError, ValueError):
            # Cannot set signal handlers (e.g., not main thread).
            logger.debug("Could not install signal handlers")

    def _restore_signal_handlers(self) -> None:
        """Restore original signal handlers saved by ``_install_signal_handlers``."""
        try:
            if self._prev_sigterm is not None:
                signal.signal(signal.SIGTERM, self._prev_sigterm)
            if self._prev_sigint is not None:
                signal.signal(signal.SIGINT, self._prev_sigint)
        except (OSError, ValueError):
            logger.debug("Could not restore signal handlers")


def babysit(config: BabysitConfig) -> BabysitResult:
    """Run the babysit-pr loop.

    Main entry point for the babysit package. Creates a BabysitLoop
    and executes it.

    Args:
        config: Babysit configuration.

    Returns:
        BabysitResult describing the outcome.
    """
    loop = BabysitLoop(config)
    return loop.run()


__all__ = [
    "BabysitLoop",
    "babysit",
]
